# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the NetBox connection helpers (issue #256, Tier 8 of #232).

Two small helpers in ``netbox_manager.main`` bridge to NetBox:

* ``validate_netbox_connection`` registers ``TOKEN`` / ``URL`` validators on
  ``settings.validators`` and calls ``validate_all()``; a raised
  ``dynaconf.ValidationError`` is logged and re-raised as ``typer.Exit()`` (the
  default, exit code **0** -- not 1).
* ``create_netbox_api`` builds ``pynetbox.api(settings.URL,
  token=str(settings.TOKEN))`` and, when ``IGNORE_SSL_ERRORS`` is truthy,
  disables ``api.http_session.verify``.

The validator tests replace ``main.settings`` wholesale with a
``SimpleNamespace`` recorder -- the helper reads nothing else off settings, so
this avoids depending on dynaconf's ``validators`` internals -- and raise the
**real** ``dynaconf.ValidationError`` via ``main.ValidationError`` (the exact
class the helper's ``except`` matches). The api tests patch ``main.pynetbox.api``
with a call recorder and monkeypatch the individual settings keys. Neither
helper calls ``init_logger()``, so the conftest loguru->``caplog`` bridge
survives and the error-log assertion can use ``caplog``.
"""

from types import SimpleNamespace

import pytest
import typer

from netbox_manager import main


class _RecordingValidators:
    """Stand-in for ``settings.validators`` recording register / validate_all.

    ``register(*validators)`` appends the positional tuple to ``registered`` and
    ``validate_all()`` bumps ``validate_all_calls`` and, when ``error`` was set,
    raises it (drive the ``ValidationError`` branch).
    """

    def __init__(self, error=None):
        self.registered = []
        self.validate_all_calls = 0
        self._error = error

    def register(self, *validators):
        self.registered.append(validators)

    def validate_all(self):
        self.validate_all_calls += 1
        if self._error is not None:
            raise self._error


def make_api_recorder(monkeypatch):
    """Patch ``main.pynetbox.api`` with a recorder; return ``(calls, fake_api)``.

    ``calls`` collects the ``(args, kwargs)`` of each ``api(...)`` call;
    ``fake_api`` exposes ``http_session.verify`` seeded with the ``"untouched"``
    sentinel so the ``IGNORE_SSL_ERRORS`` toggle is observable.
    """
    calls = []
    fake_api = SimpleNamespace(http_session=SimpleNamespace(verify="untouched"))

    def _api(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_api

    monkeypatch.setattr(main.pynetbox, "api", _api)
    return calls, fake_api


class TestValidateNetboxConnection:
    """Group 1 -- ``validate_netbox_connection`` (main.py:128-138)."""

    def test_validate_registers_validators_and_passes(self, monkeypatch):
        validators = _RecordingValidators()
        monkeypatch.setattr(main, "settings", SimpleNamespace(validators=validators))

        result = main.validate_netbox_connection()

        assert result is None
        # One register call carrying a TOKEN and a URL validator.
        assert len(validators.registered) == 1
        assert [v.names for v in validators.registered[0]] == [("TOKEN",), ("URL",)]
        assert validators.validate_all_calls == 1

    def test_validate_error_exits_with_default_code(self, monkeypatch, caplog):
        error = main.ValidationError("boom", details=[("TOKEN", "must be str")])
        validators = _RecordingValidators(error=error)
        monkeypatch.setattr(main, "settings", SimpleNamespace(validators=validators))

        with pytest.raises(typer.Exit) as exc_info:
            main.validate_netbox_connection()

        # Source raises a bare typer.Exit() -> default exit code 0, not 1.
        assert exc_info.value.exit_code == 0
        assert "Error validating NetBox connection settings" in caplog.text


class TestCreateNetboxApi:
    """Group 2 -- ``create_netbox_api`` (main.py:212-217)."""

    def test_create_api_positional_url_and_stringified_token(self, monkeypatch):
        calls, fake_api = make_api_recorder(monkeypatch)
        monkeypatch.setattr(
            main.settings, "URL", "http://netbox.example", raising=False
        )
        # A non-string token proves the str(...) coercion on the way in.
        monkeypatch.setattr(main.settings, "TOKEN", 12345, raising=False)
        monkeypatch.setattr(main.settings, "IGNORE_SSL_ERRORS", False, raising=False)

        result = main.create_netbox_api()

        assert result is fake_api
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ("http://netbox.example",)
        assert kwargs == {"token": "12345"}
        # Falsy IGNORE_SSL_ERRORS leaves verify untouched.
        assert fake_api.http_session.verify == "untouched"

    def test_create_api_ignore_ssl_disables_verify(self, monkeypatch):
        _, fake_api = make_api_recorder(monkeypatch)
        monkeypatch.setattr(
            main.settings, "URL", "http://netbox.example", raising=False
        )
        monkeypatch.setattr(main.settings, "TOKEN", "abc", raising=False)
        monkeypatch.setattr(main.settings, "IGNORE_SSL_ERRORS", True, raising=False)

        main.create_netbox_api()

        assert fake_api.http_session.verify is False
