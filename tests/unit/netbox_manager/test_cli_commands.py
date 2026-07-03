# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer command wiring (issue #258, Tier 10 of #232).

This tier covers the thin ``@app.command()`` / ``@app.callback()`` shells in
``netbox_manager.main`` plus the entrypoint plumbing (``main``, the SIGINT
handler, ``init_logger``, the ``--version`` eager callback). The goal is to
verify that CLI argument/option parsing maps onto the right worker arguments,
that each command dispatches to the correct worker, and that exit codes are
correct -- *not* to re-test the workers themselves (Tiers 1-9 do that).

The defining property of this tier is that every heavy worker is patched out
via the shared ``mock_cli_workers`` fixture (see ``conftest.py``): ``_run_main``,
``_run_autoconf_for_devices``, ``validate_ip_addresses_have_prefixes`` /
``validate_vrf_consistency``, the connection guard and the API factory. As a
result these tests touch no live NetBox, no ``ansible_runner``, no ``git.Repo``
and no real filesystem -- they exercise only the ``typer`` dispatch surface, and
the destructive ``purge`` deletion path is asserted unreached in dry-run.

``init_logger`` is among the patched workers because the real one calls
``logger.remove()``, which would tear down the shared loguru->``caplog`` bridge
mid-command and blind the log-message assertions; the spy also serves as the
``--debug`` / ``--verbose`` plumbing assertion point.
"""

import signal
from importlib import metadata
from unittest.mock import MagicMock

import pytest
import typer

from netbox_manager import main
from netbox_manager.main import app

VERSION = metadata.version("netbox-manager")

# ``_run_main`` receives all 17 ``run`` options positionally (main.py). This is
# the tuple ``run`` forwards for a bare ``run`` / no-subcommand invocation.
RUN_MAIN_DEFAULTS = (
    True,  # always
    False,  # debug
    False,  # dryrun
    None,  # limit
    1,  # parallel
    None,  # version
    False,  # skipdtl
    False,  # skipmtl
    False,  # skipres
    True,  # wait
    None,  # filter_task
    False,  # include_ignored_files
    None,  # filter_device
    False,  # fail_fast
    False,  # show_playbooks
    False,  # verbose
    False,  # ignore_errors
)


class _FakeResource:
    """A single purge target: a real ``name`` plus a spy ``delete``.

    ``get_resource_name`` (main.py) reads ``name`` off the resource, so it must
    be a real attribute (a bare ``MagicMock`` would not satisfy the ``name``
    lookup). ``delete`` is a spy the tests assert was never called in dry-run.
    """

    def __init__(self, name):
        self.name = name
        self.delete = MagicMock()


class _FakePurgeTree:
    """Stand-in for the ``pynetbox.api(...)`` client used by ``purge_command``.

    ``purge_command`` resolves each endpoint with a two-level ``getattr``
    traversal (e.g. ``api.ipam.ip_addresses``); ``__getattr__`` returns ``self``
    so any dotted path collapses back to this tree. ``all()`` records how many
    times it was reached (``all_calls``) and returns the canned resources, which
    lets the ``--limit`` narrowing tests assert on the number of endpoints
    actually visited.
    """

    def __init__(self, resources):
        self._resources = resources
        self.all_calls = 0

    def __getattr__(self, name):
        return self

    def all(self):
        self.all_calls += 1
        return list(self._resources)


class TestRunCommand:
    """Group 1 -- ``run_command`` option mapping and dispatch (main.py)."""

    def test_options_map_positionally_to_run_main(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(
            app,
            [
                "run",
                "--no-always",
                "--dryrun",
                "--limit",
                "300",
                "--parallel",
                "4",
                "--skipdtl",
                "--skipmtl",
                "--skipres",
                "--no-wait",
                "--filter-task",
                "device",
                "--filter-device",
                "node-a",
                "--filter-device",
                "node-b",
                "--fail-fast",
                "--show-playbooks",
                "--verbose",
                "--ignore-errors",
            ],
        )

        assert result.exit_code == 0
        assert mock_cli_workers.run_main.call_count == 1
        assert mock_cli_workers.run_main.call_args.kwargs == {}
        assert mock_cli_workers.run_main.call_args.args == (
            False,  # always (--no-always)
            False,  # debug
            True,  # dryrun
            "300",  # limit
            4,  # parallel
            None,  # version
            True,  # skipdtl
            True,  # skipmtl
            True,  # skipres
            False,  # wait (--no-wait)
            "device",  # filter_task
            False,  # include_ignored_files
            ["node-a", "node-b"],  # filter_device (repeatable -> list)
            True,  # fail_fast
            True,  # show_playbooks
            True,  # verbose
            True,  # ignore_errors
        )

    def test_bare_run_uses_documented_defaults(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(app, ["run"])

        assert result.exit_code == 0
        assert mock_cli_workers.run_main.call_count == 1
        assert mock_cli_workers.run_main.call_args.args == RUN_MAIN_DEFAULTS

    def test_dispatch_touches_only_the_patched_worker(
        self, cli_runner, mock_cli_workers, mock_ansible_runner
    ):
        result = cli_runner.invoke(app, ["run"])

        assert result.exit_code == 0
        # The connection guard and the API factory both live *inside* the
        # patched ``_run_main``, so a hermetic dispatch never reaches them.
        mock_cli_workers.validate_netbox_connection.assert_not_called()
        mock_cli_workers.create_netbox_api.assert_not_called()
        assert mock_ansible_runner.calls == []

    def test_version_eager_callback_skips_run_main(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(app, ["run", "--version"])

        assert result.exit_code == 0
        assert f"Version {VERSION}" in result.output
        mock_cli_workers.run_main.assert_not_called()


class TestAutoconfCommand:
    """Group 2 -- ``autoconf_command`` dispatch and exit codes (main.py)."""

    def test_flat_mode_dispatches_worker_once(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        detect = MagicMock(return_value={})
        monkeypatch.setattr(main, "detect_site_folders", detect)

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 0
        detect.assert_called_once_with(str(tmp_path))
        assert mock_cli_workers.run_autoconf_for_devices.call_count == 1
        assert mock_cli_workers.run_autoconf_for_devices.call_args.kwargs == {
            "device_filter": None,
            "output_dir": str(tmp_path),
            "prefix": "999-autoconf",
            "dryrun": False,
        }
        mock_cli_workers.validate_netbox_connection.assert_called_once_with()

    def test_output_option_derives_prefix_and_output_dir(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))

        result = cli_runner.invoke(
            app, ["autoconf", "--output", "sub/dir/500-site.yml"]
        )

        assert result.exit_code == 0
        kwargs = mock_cli_workers.run_autoconf_for_devices.call_args.kwargs
        assert kwargs["prefix"] == "500-site"
        assert kwargs["output_dir"] == "sub/dir"

    def test_dryrun_and_debug_plumbing(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))

        result = cli_runner.invoke(app, ["autoconf", "--dryrun", "--debug"])

        assert result.exit_code == 0
        kwargs = mock_cli_workers.run_autoconf_for_devices.call_args.kwargs
        assert kwargs["dryrun"] is True
        mock_cli_workers.init_logger.assert_called_once_with(True)

    def test_request_error_from_worker_exits_1(
        self,
        cli_runner,
        mock_cli_workers,
        monkeypatch,
        tmp_path,
        caplog,
        make_request_error,
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))
        mock_cli_workers.run_autoconf_for_devices.side_effect = make_request_error(
            "boom"
        )

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 1
        assert "NetBox API error" in caplog.text

    def test_generic_error_from_worker_exits_1(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))
        mock_cli_workers.run_autoconf_for_devices.side_effect = RuntimeError("kaput")

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 1
        assert "Error generating autoconf" in caplog.text


class TestValidateCommand:
    """Group 3 -- ``validate_command`` check dispatch and exit codes (main.py)."""

    def test_check_ip_prefixes_runs_only_ip_check(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(app, ["validate", "--check", "ip-prefixes"])

        assert result.exit_code == 0
        assert mock_cli_workers.validate_ip_addresses_have_prefixes.call_count == 1
        assert mock_cli_workers.validate_ip_addresses_have_prefixes.call_args.args == (
            mock_cli_workers.create_netbox_api.return_value,
            False,
        )
        mock_cli_workers.validate_vrf_consistency.assert_not_called()

    def test_check_vrf_consistency_runs_only_vrf_check(
        self, cli_runner, mock_cli_workers
    ):
        result = cli_runner.invoke(app, ["validate", "--check", "vrf-consistency"])

        assert result.exit_code == 0
        assert mock_cli_workers.validate_vrf_consistency.call_count == 1
        assert mock_cli_workers.validate_vrf_consistency.call_args.args == (
            mock_cli_workers.create_netbox_api.return_value,
            False,
        )
        mock_cli_workers.validate_ip_addresses_have_prefixes.assert_not_called()

    def test_no_check_runs_both_and_exits_0_on_pass(
        self, cli_runner, mock_cli_workers, caplog
    ):
        result = cli_runner.invoke(app, ["validate"])

        assert result.exit_code == 0
        assert mock_cli_workers.validate_ip_addresses_have_prefixes.call_count == 1
        assert mock_cli_workers.validate_vrf_consistency.call_count == 1
        # The inner ``typer.Exit(0)`` must escape the ``except typer.Exit:
        # raise`` guard, not get mangled to exit 1 by the generic handler.
        assert "Error during validation" not in caplog.text

    def test_invalid_check_exits_1_before_any_worker(
        self, cli_runner, mock_cli_workers, caplog
    ):
        result = cli_runner.invoke(app, ["validate", "--check", "foo"])

        assert result.exit_code == 1
        assert "Invalid check(s): foo" in caplog.text
        mock_cli_workers.create_netbox_api.assert_not_called()
        mock_cli_workers.validate_ip_addresses_have_prefixes.assert_not_called()
        mock_cli_workers.validate_vrf_consistency.assert_not_called()

    def test_ip_findings_exit_1(self, cli_runner, mock_cli_workers, caplog):
        mock_cli_workers.validate_ip_addresses_have_prefixes.return_value = (
            False,
            [
                {
                    "address": "10.0.0.5/32",
                    "vrf": "Global",
                    "device": "node-0",
                    "interface": "eth0",
                    "assigned_object": "node-0",
                    "reason": "No matching prefix found in same VRF",
                }
            ],
        )

        result = cli_runner.invoke(app, ["validate", "--check", "ip-prefixes"])

        assert result.exit_code == 1
        assert "IP-Prefix Check FAILED" in caplog.text

    def test_vrf_findings_exit_1(self, cli_runner, mock_cli_workers, caplog):
        mock_cli_workers.validate_vrf_consistency.return_value = (
            False,
            [
                {
                    "ip_address": "10.0.0.5/32",
                    "ip_vrf": "red",
                    "device": "node-0",
                    "interface": "eth0",
                    "interface_vrf": "blue",
                    "reason": "VRF mismatch",
                }
            ],
        )

        result = cli_runner.invoke(app, ["validate", "--check", "vrf-consistency"])

        assert result.exit_code == 1
        assert "VRF Consistency Check FAILED" in caplog.text

    def test_worker_request_error_exits_1(
        self, cli_runner, mock_cli_workers, caplog, make_request_error
    ):
        mock_cli_workers.validate_ip_addresses_have_prefixes.side_effect = (
            make_request_error()
        )

        result = cli_runner.invoke(app, ["validate", "--check", "ip-prefixes"])

        assert result.exit_code == 1
        assert "NetBox API error" in caplog.text

    def test_worker_generic_error_exits_1(self, cli_runner, mock_cli_workers, caplog):
        mock_cli_workers.validate_ip_addresses_have_prefixes.side_effect = RuntimeError(
            "kaput"
        )

        result = cli_runner.invoke(app, ["validate", "--check", "ip-prefixes"])

        assert result.exit_code == 1
        assert "Error during validation" in caplog.text


class TestPurgeCommand:
    """Group 4 -- ``purge_command`` dry-run / confirmation gate (main.py).

    Only the dry-run and confirmation gates are covered here; the real
    destructive deletion path is out of scope for this tier. Every test asserts
    the deletion worker recorded zero destructive calls.
    """

    def test_dryrun_skips_confirm_and_never_deletes(
        self, cli_runner, mock_cli_workers, caplog, monkeypatch
    ):
        resources = [_FakeResource("a"), _FakeResource("b")]
        tree = _FakePurgeTree(resources)
        monkeypatch.setattr(main.pynetbox, "api", lambda *a, **k: tree)
        confirm_spy = MagicMock()
        monkeypatch.setattr(main.typer, "confirm", confirm_spy)

        result = cli_runner.invoke(app, ["purge", "--dryrun"])

        assert result.exit_code == 0
        confirm_spy.assert_not_called()
        assert all(r.delete.call_count == 0 for r in resources)
        assert "Dry run complete - no resources were deleted" in caplog.text

    def test_declined_confirmation_cancels_before_api_construction(
        self, cli_runner, mock_cli_workers, caplog, monkeypatch
    ):
        monkeypatch.setattr(main.typer, "confirm", lambda *a, **k: False)
        api_factory = MagicMock()
        monkeypatch.setattr(main.pynetbox, "api", api_factory)

        result = cli_runner.invoke(app, ["purge"])

        assert result.exit_code == 0
        assert "Purge cancelled by user" in caplog.text
        # The client is only built *after* the confirmation gate, so declining
        # leaves the destructive path unreachable.
        api_factory.assert_not_called()

    def test_unknown_limit_exits_1(
        self, cli_runner, mock_cli_workers, caplog, monkeypatch
    ):
        resources = [_FakeResource("a"), _FakeResource("b")]
        tree = _FakePurgeTree(resources)
        monkeypatch.setattr(main.pynetbox, "api", lambda *a, **k: tree)

        result = cli_runner.invoke(
            app, ["purge", "--dryrun", "--limit", "does-not-exist"]
        )

        assert result.exit_code == 1
        assert "No resource type matching 'does-not-exist' found" in caplog.text
        assert tree.all_calls == 0
        assert all(r.delete.call_count == 0 for r in resources)

    def test_matching_limit_narrows_order(
        self, cli_runner, mock_cli_workers, caplog, monkeypatch
    ):
        resources = [_FakeResource("10.0.0.1/32"), _FakeResource("10.0.0.2/32")]
        tree = _FakePurgeTree(resources)
        monkeypatch.setattr(main.pynetbox, "api", lambda *a, **k: tree)

        result = cli_runner.invoke(
            app, ["purge", "--dryrun", "--limit", "ip-addresses"]
        )

        assert result.exit_code == 0
        # ``ip-addresses`` matches exactly one entry (ipam.ip_addresses), so the
        # traversal reaches ``all()`` once.
        assert tree.all_calls == 1
        assert "Would delete 2 IP addresses" in caplog.text
        assert all(r.delete.call_count == 0 for r in resources)


class TestVersionCommand:
    """Group 5 -- ``version_command`` prints and returns normally (main.py)."""

    def test_prints_version_and_exits_0(self, cli_runner):
        result = cli_runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert result.output == f"netbox-manager {VERSION}\n"

    def test_returns_normally_without_typer_exit(self, capsys):
        assert main.version_command() is None
        assert capsys.readouterr().out == f"netbox-manager {VERSION}\n"


class TestMainCallback:
    """Group 5 -- ``main_callback`` no-subcommand default (main.py)."""

    def test_no_subcommand_dispatches_to_run_command(
        self, cli_runner, mock_cli_workers
    ):
        result = cli_runner.invoke(app, [])

        assert result.exit_code == 0
        assert mock_cli_workers.run_main.call_count == 1
        assert mock_cli_workers.run_main.call_args.args == RUN_MAIN_DEFAULTS


class TestMainEntrypoint:
    """Group 5 -- ``main`` registers the SIGINT handler and runs the app."""

    def test_registers_sigint_handler_and_invokes_app(self, monkeypatch):
        signal_spy = MagicMock()
        monkeypatch.setattr(main.signal, "signal", signal_spy)
        app_mock = MagicMock()
        monkeypatch.setattr(main, "app", app_mock)

        main.main()

        signal_spy.assert_called_once_with(signal.SIGINT, main.signal_handler_sigint)
        app_mock.assert_called_once_with()


class TestSignalHandlerSigint:
    """Group 5 -- ``signal_handler_sigint`` prints and raises (main.py)."""

    def test_prints_and_raises_typer_exit(self, capsys):
        with pytest.raises(typer.Exit) as exc_info:
            main.signal_handler_sigint(signal.SIGINT, None)

        assert exc_info.value.exit_code == 0
        assert capsys.readouterr().out == "SIGINT received. Exit.\n"


class TestInitLogger:
    """Group 5 -- ``init_logger`` level selection (main.py).

    The real ``logger`` is replaced with a mock so the load-bearing
    ``logger.remove()`` cannot tear down the shared caplog bridge; this also
    exposes the sink wiring (``remove`` then ``add``) for assertion.
    """

    def test_debug_selects_debug_level(self, monkeypatch):
        logger_mock = MagicMock()
        monkeypatch.setattr(main, "logger", logger_mock)

        main.init_logger(True)

        logger_mock.remove.assert_called_once_with()
        assert logger_mock.add.call_count == 1
        assert logger_mock.add.call_args.args[0] is main.sys.stderr
        assert logger_mock.add.call_args.kwargs["level"] == "DEBUG"

    def test_default_selects_info_level(self, monkeypatch):
        logger_mock = MagicMock()
        monkeypatch.setattr(main, "logger", logger_mock)

        main.init_logger(False)

        logger_mock.remove.assert_called_once_with()
        assert logger_mock.add.call_count == 1
        assert logger_mock.add.call_args.kwargs["level"] == "INFO"


class TestCallbackVersion:
    """Group 5 -- ``callback_version`` eager option branches (main.py)."""

    def test_true_prints_and_exits(self, capsys):
        with pytest.raises(typer.Exit) as exc_info:
            main.callback_version(True)

        assert exc_info.value.exit_code == 0
        assert capsys.readouterr().out == f"Version {VERSION}\n"

    def test_false_is_noop(self, capsys):
        assert main.callback_version(False) is None
        assert capsys.readouterr().out == ""
