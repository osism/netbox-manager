# SPDX-License-Identifier: Apache-2.0

import os
from types import SimpleNamespace

import pytest

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

# Heavier per-module fixtures -- full `pynetbox.api` clients, mocked
# `ansible_runner.run`, `git.Repo`, `subprocess` -- belong to the later #232
# tiers. Tier 1 (#247) only needs the lightweight attribute-bag factories below.


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
