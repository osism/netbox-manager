# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_generate_loopback_interfaces`` (issue #253, group 1).

The generator queries ``dcim.devices.all()`` and emits one ``device_interface``
task per device for which ``should_have_loopback_interface(device)`` is truthy.
The loopback-gate helper itself is covered in ``test_loopback_gate.py`` (Tier 1,
#247); here only its effect inside the generator is asserted. Node / switch
roles are pinned via ``main.settings`` so eligibility is explicit rather than
driven by the in-module default role lists.
"""

import pytest

from netbox_manager import main


@pytest.fixture
def roles(monkeypatch):
    """Pin NODE_ROLES / SWITCH_ROLES so device eligibility is unambiguous."""
    monkeypatch.setattr(main.settings, "NODE_ROLES", ["control"], raising=False)
    monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)


def _wire(monkeypatch, make_netbox_api, devices):
    """Point ``main.create_netbox_api`` at a fake API serving ``devices``."""
    fake_api = make_netbox_api(devices=devices)
    monkeypatch.setattr(main, "create_netbox_api", lambda: fake_api)
    return fake_api


def _loopback_task(name):
    return {
        "device_interface": {
            "device": name,
            "name": "Loopback0",
            "type": "virtual",
            "enabled": True,
            "tags": ["managed-by-osism"],
        }
    }


def test_emits_one_task_per_eligible_device_in_device_order(
    roles, monkeypatch, make_netbox_api, make_device
):
    # node (eligible), sonic switch (eligible via hwsku), plain switch
    # (ineligible: switch role without hwsku).
    devices = [
        make_device(name="node-0", role_slug="control"),
        make_device(
            name="switch-sonic",
            role_slug="leaf",
            custom_fields={"sonic_parameters": {"hwsku": "AS7326"}},
        ),
        make_device(name="switch-plain", role_slug="leaf"),
    ]
    _wire(monkeypatch, make_netbox_api, devices)

    tasks = main._generate_loopback_interfaces()

    assert tasks == [_loopback_task("node-0"), _loopback_task("switch-sonic")]


def test_task_shape_is_exact(roles, monkeypatch, make_netbox_api, make_device):
    _wire(
        monkeypatch, make_netbox_api, [make_device(name="node-0", role_slug="control")]
    )

    tasks = main._generate_loopback_interfaces()

    assert tasks == [
        {
            "device_interface": {
                "device": "node-0",
                "name": "Loopback0",
                "type": "virtual",
                "enabled": True,
                "tags": ["managed-by-osism"],
            }
        }
    ]


def test_empty_device_list_yields_empty_list(roles, monkeypatch, make_netbox_api):
    _wire(monkeypatch, make_netbox_api, [])

    assert main._generate_loopback_interfaces() == []


def test_only_ineligible_devices_yield_empty_list(
    roles, monkeypatch, make_netbox_api, make_device
):
    devices = [
        make_device(name="switch-plain", role_slug="leaf"),
        make_device(name="misc", role_slug="unmapped"),
    ]
    _wire(monkeypatch, make_netbox_api, devices)

    assert main._generate_loopback_interfaces() == []
