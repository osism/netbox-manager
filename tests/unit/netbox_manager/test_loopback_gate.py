# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the loopback-gate helpers in ``netbox_manager.main`` (issue #247).

Covers group 5 of the Tier 1 scope tracked in #232: whether a device carries a
``sonic_parameters.hwsku`` custom field, and whether a device should get a
Loopback0 interface. The loopback decision reads the settings-driven role lists
from group 1, so a few cases stub ``main.settings`` to exercise overrides.
"""

from types import SimpleNamespace

from netbox_manager import main


class TestHasSonicHwskuParameter:
    """``has_sonic_hwsku_parameter`` is true only for a dict hwsku value."""

    def test_false_when_custom_fields_attribute_missing(self):
        assert main.has_sonic_hwsku_parameter(SimpleNamespace()) is False

    def test_false_when_custom_fields_falsy(self, make_device):
        # make_device defaults custom_fields to an empty (falsy) dict.
        assert main.has_sonic_hwsku_parameter(make_device()) is False

    def test_false_when_sonic_parameters_absent(self, make_device):
        device = make_device(custom_fields={"other": 1})
        assert main.has_sonic_hwsku_parameter(device) is False

    def test_false_when_sonic_parameters_not_a_dict(self, make_device):
        device = make_device(custom_fields={"sonic_parameters": "not-a-dict"})
        assert main.has_sonic_hwsku_parameter(device) is False

    def test_false_when_hwsku_missing(self, make_device):
        device = make_device(custom_fields={"sonic_parameters": {"foo": "bar"}})
        assert main.has_sonic_hwsku_parameter(device) is False

    def test_false_when_hwsku_empty(self, make_device):
        device = make_device(custom_fields={"sonic_parameters": {"hwsku": ""}})
        assert main.has_sonic_hwsku_parameter(device) is False

    def test_true_when_hwsku_present(self, make_device):
        device = make_device(custom_fields={"sonic_parameters": {"hwsku": "AS7326"}})
        assert main.has_sonic_hwsku_parameter(device) is True


class TestShouldHaveLoopbackInterface:
    """``should_have_loopback_interface`` gates by role and SONiC hwsku.

    These cases rely on the in-module default role lists, since the conftest
    settings stub leaves NODE_ROLES / SWITCH_ROLES unset.
    """

    def test_node_role_always_gets_loopback(self, make_device):
        device = make_device(role_slug="control")
        assert main.should_have_loopback_interface(device) is True

    def test_node_role_does_not_require_hwsku(self, make_device):
        # The node-role path short-circuits before the hwsku check, so a node
        # role gets Loopback0 even with a SONiC hwsku present -- the opposite
        # hwsku state from ``test_node_role_always_gets_loopback`` (which has
        # none). Together the two pin node-role eligibility as hwsku-independent.
        device = make_device(
            role_slug="control",
            custom_fields={"sonic_parameters": {"hwsku": "AS7326"}},
        )
        assert main.should_have_loopback_interface(device) is True

    def test_switch_role_with_hwsku_gets_loopback(self, make_device):
        device = make_device(
            role_slug="leaf",
            custom_fields={"sonic_parameters": {"hwsku": "AS7326"}},
        )
        assert main.should_have_loopback_interface(device) is True

    def test_switch_role_without_hwsku_no_loopback(self, make_device):
        device = make_device(role_slug="leaf")
        assert main.should_have_loopback_interface(device) is False

    def test_switch_device_type_with_hwsku_gets_loopback(self, make_device):
        device = make_device(
            role_slug="unmapped",
            device_type_model="Generic Switch",
            custom_fields={"sonic_parameters": {"hwsku": "AS7326"}},
        )
        assert main.should_have_loopback_interface(device) is True

    def test_switch_device_type_without_hwsku_no_loopback(self, make_device):
        device = make_device(role_slug="unmapped", device_type_model="Generic Switch")
        assert main.should_have_loopback_interface(device) is False

    def test_non_node_non_switch_no_loopback(self, make_device):
        device = make_device(role_slug="unmapped", device_type_model="Server")
        assert main.should_have_loopback_interface(device) is False

    def test_no_role_and_no_device_type_no_loopback(self, make_device):
        assert main.should_have_loopback_interface(make_device()) is False

    def test_custom_node_role_from_settings(self, make_device, monkeypatch):
        monkeypatch.setattr(main.settings, "NODE_ROLES", ["myrole"], raising=False)
        device = make_device(role_slug="myrole")
        assert main.should_have_loopback_interface(device) is True

    def test_settings_override_replaces_default_node_roles(
        self, make_device, monkeypatch
    ):
        # Overriding NODE_ROLES replaces the defaults, so "control" no longer
        # counts as a node role and the device gets no loopback.
        monkeypatch.setattr(main.settings, "NODE_ROLES", ["myrole"], raising=False)
        device = make_device(role_slug="control")
        assert main.should_have_loopback_interface(device) is False

    def test_custom_switch_role_from_settings(self, make_device, monkeypatch):
        monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["myswitch"], raising=False)
        device = make_device(
            role_slug="myswitch",
            custom_fields={"sonic_parameters": {"hwsku": "AS7326"}},
        )
        assert main.should_have_loopback_interface(device) is True
