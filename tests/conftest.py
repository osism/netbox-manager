# SPDX-License-Identifier: Apache-2.0

import os
from types import SimpleNamespace

import pytest
from loguru import logger

# Set dynaconf env vars before any test imports `netbox_manager.main`, since
# `Dynaconf(...)` and the validator registration run at import time. Force-set
# (not `setdefault`) so an exported `NETBOX_MANAGER_URL`/`TOKEN`/
# `IGNORE_SSL_ERRORS` from a real deployment cannot leak into tests that
# assert these baseline values, and tests do not depend on a real
# `settings.toml` being present.
os.environ["NETBOX_MANAGER_URL"] = "http://localhost:8000"
os.environ["NETBOX_MANAGER_TOKEN"] = "test-token"
os.environ["NETBOX_MANAGER_IGNORE_SSL_ERRORS"] = "false"

# Keep the role helpers hermetic. A real deployment env may export
# `NETBOX_MANAGER_NODE_ROLES` / `..._SWITCH_ROLES`, which dynaconf would load as
# `settings.NODE_ROLES` / `settings.SWITCH_ROLES` and make `get_node_roles()` /
# `get_switch_roles()` return the deployment list -- breaking the default-role
# and `is _DEFAULT_*` assertions. Drop them before `netbox_manager.main` is
# imported so the in-module defaults are the baseline; tests that need an
# override set it explicitly via `monkeypatch.setattr(main.settings, ...)`.
os.environ.pop("NETBOX_MANAGER_NODE_ROLES", None)
os.environ.pop("NETBOX_MANAGER_SWITCH_ROLES", None)

# Cross-tier fixtures. The lightweight attribute-bag factories `make_device` /
# `make_interface` (Tier 1, #247) come first; the heavier `mock_ansible_runner`
# recorder and the loguru->`caplog` bridge (Tier 4, #252) follow at the end of
# the file. The remaining heavy seams -- full `pynetbox.api` clients, `git.Repo`,
# `subprocess` -- belong to the later #232 tiers.


@pytest.fixture
def make_device():
    """Build device-like attribute bags for the pure-logic helpers.

    Returns a factory producing a :class:`types.SimpleNamespace` that exposes
    only the attributes the helpers in :mod:`netbox_manager.main` read from a
    pynetbox device -- ``name``, ``role`` (with ``slug`` / ``name``),
    ``device_type`` (with ``model``) and ``custom_fields``. No live NetBox or
    full ``pynetbox`` mock is involved.

    Keyword arguments of the factory:
        name: Device name.
        role_slug / role_name: Attributes for ``device.role``; when both are
            omitted ``device.role`` is ``None`` (an unset role).
        device_type_model: ``model`` for ``device.device_type``; when omitted
            ``device.device_type`` is ``None``.
        custom_fields: Mapping for ``device.custom_fields`` (defaults to ``{}``).

    Shared with later #232 test tiers; extend it here rather than re-deriving
    device shapes per test module.
    """

    def _make_device(
        *,
        name="test-device",
        role_slug=None,
        role_name=None,
        device_type_model=None,
        custom_fields=None,
    ):
        if role_slug is None and role_name is None:
            role = None
        else:
            role = SimpleNamespace()
            if role_slug is not None:
                role.slug = role_slug
            if role_name is not None:
                role.name = role_name

        device_type = (
            SimpleNamespace(model=device_type_model)
            if device_type_model is not None
            else None
        )

        return SimpleNamespace(
            name=name,
            role=role,
            device_type=device_type,
            custom_fields={} if custom_fields is None else custom_fields,
        )

    return _make_device


@pytest.fixture
def make_interface():
    """Build interface-like attribute bags for the pure-logic helpers.

    Returns a factory producing a :class:`types.SimpleNamespace` that exposes
    only ``type`` (with ``value`` / ``label``), which is all
    :func:`netbox_manager.main.is_virtual_interface` reads. When neither
    ``type_value`` nor ``type_label`` is given, ``type`` is ``None``.

    Shared with later #232 test tiers.
    """

    def _make_interface(*, type_value=None, type_label=None):
        if type_value is None and type_label is None:
            interface_type = None
        else:
            interface_type = SimpleNamespace()
            if type_value is not None:
                interface_type.value = type_value
            if type_label is not None:
                interface_type.label = type_label
        return SimpleNamespace(type=interface_type)

    return _make_interface


@pytest.fixture
def caplog(caplog):
    """Bridge loguru log output into pytest's ``caplog`` capture.

    ``netbox_manager.main`` logs via loguru, whose messages pytest's stock
    ``caplog`` fixture does not see (loguru does not propagate through the
    standard ``logging`` root). This override -- the recipe from the loguru
    documentation -- registers ``caplog.handler`` as a loguru sink for the
    duration of the test, so ``caplog.text`` / ``caplog.records`` capture loguru
    messages as usual, and removes the sink again on teardown.

    Shared with the later #232 tiers (#256, #258, #259) that assert on the
    warn / error / info log branches; reuse it rather than re-deriving the
    bridge per test module.
    """
    handler_id = logger.add(
        caplog.handler,
        format="{message}",
        level=0,
        filter=lambda record: record["level"].no >= caplog.handler.level,
        enqueue=False,
    )
    yield caplog
    logger.remove(handler_id)


@pytest.fixture
def mock_ansible_runner(monkeypatch):
    """Replace ``main.ansible_runner`` with a call-recording fake.

    ``handle_file`` invokes ``ansible_runner.run(...)`` to execute a rendered
    playbook. This fixture swaps the module reference on ``netbox_manager.main``
    for a recorder that never runs Ansible, so tests can assert both *whether*
    ``run`` was reached and *with which kwargs*.

    The returned object exposes:
        calls: list of the keyword-argument dicts passed to each ``run`` call
            (stays empty when ``run`` is never reached -- dry-run, show-playbooks,
            no-tasks-after-filter, or an early error return).
        status: the ``result.status`` string ``run`` reports back; set it to
            ``"failed"`` before calling ``handle_file`` to drive the fail-fast
            failure gate.

    ``main`` is imported inside the fixture body to preserve the
    env-vars-before-import discipline established at the top of this module.
    Shared with the later #232 tiers (#258) that mock playbook execution.
    """
    from netbox_manager import main

    class _RecordingAnsibleRunner:
        def __init__(self):
            self.calls = []
            self.status = "successful"

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status=self.status)

    fake = _RecordingAnsibleRunner()
    monkeypatch.setattr(main, "ansible_runner", fake)
    return fake
