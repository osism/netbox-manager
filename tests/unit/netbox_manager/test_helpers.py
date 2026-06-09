# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure-logic helpers in ``netbox_manager.main`` (issue #247).

Covers the settings/role helpers (group 1) and the generic
data-transformation helpers (group 2) of the Tier 1 scope tracked in #232.
These functions are deterministic transformations of plain Python data or of
tiny attribute-bag objects, so they need no live NetBox, ``ansible_runner`` or
filesystem -- at most a ``types.SimpleNamespace`` or one of the shared
``make_device`` / ``make_interface`` factories from ``conftest.py``.
"""

from types import SimpleNamespace

from netbox_manager import main


class TestGetNodeRoles:
    """``get_node_roles`` reads ``settings.NODE_ROLES`` or falls back."""

    def test_returns_setting_when_present(self, monkeypatch):
        monkeypatch.setattr(main.settings, "NODE_ROLES", ["custom-node"], raising=False)
        assert main.get_node_roles() == ["custom-node"]

    def test_falls_back_to_default_when_absent(self):
        # The conftest stub never sets NODE_ROLES, so this is the natural state.
        assert main.get_node_roles() is main._DEFAULT_NODE_ROLES

    def test_falls_back_to_default_when_none(self, monkeypatch):
        monkeypatch.setattr(main.settings, "NODE_ROLES", None, raising=False)
        assert main.get_node_roles() is main._DEFAULT_NODE_ROLES

    def test_falls_back_to_default_when_empty_list(self, monkeypatch):
        # Empty list is falsy, so the ``or`` falls through to the default.
        monkeypatch.setattr(main.settings, "NODE_ROLES", [], raising=False)
        assert main.get_node_roles() is main._DEFAULT_NODE_ROLES


class TestGetSwitchRoles:
    """``get_switch_roles`` mirrors ``get_node_roles`` for SWITCH_ROLES."""

    def test_returns_setting_when_present(self, monkeypatch):
        monkeypatch.setattr(
            main.settings, "SWITCH_ROLES", ["custom-switch"], raising=False
        )
        assert main.get_switch_roles() == ["custom-switch"]

    def test_falls_back_to_default_when_absent(self):
        assert main.get_switch_roles() is main._DEFAULT_SWITCH_ROLES

    def test_falls_back_to_default_when_none(self, monkeypatch):
        monkeypatch.setattr(main.settings, "SWITCH_ROLES", None, raising=False)
        assert main.get_switch_roles() is main._DEFAULT_SWITCH_ROLES

    def test_falls_back_to_default_when_empty_list(self, monkeypatch):
        monkeypatch.setattr(main.settings, "SWITCH_ROLES", [], raising=False)
        assert main.get_switch_roles() is main._DEFAULT_SWITCH_ROLES


class TestGetLeadingNumber:
    """``get_leading_number`` returns ``basename.split('-')[0]``."""

    def test_extracts_leading_number(self):
        assert main.get_leading_number("300-devices.yml") == "300"

    def test_strips_path_to_basename_first(self):
        assert main.get_leading_number("/abs/path/300-devices.yml") == "300"

    def test_no_dash_returns_whole_basename(self):
        assert main.get_leading_number("noprefix.yml") == "noprefix.yml"

    def test_no_dash_with_path_returns_basename_only(self):
        assert main.get_leading_number("/abs/path/noprefix.yml") == "noprefix.yml"


class TestDeepMerge:
    """``deep_merge`` recursively merges with dict2 winning conflicts."""

    def test_recursive_merge_of_nested_dicts(self):
        merged = main.deep_merge(
            {"a": 1, "nested": {"x": 1, "shared": "old"}},
            {"b": 2, "nested": {"y": 2, "shared": "new"}},
        )
        assert merged == {
            "a": 1,
            "b": 2,
            "nested": {"x": 1, "y": 2, "shared": "new"},
        }

    def test_dict2_wins_on_scalar_conflict(self):
        assert main.deep_merge({"k": 1}, {"k": 2}) == {"k": 2}

    def test_lists_are_replaced_not_merged(self):
        assert main.deep_merge({"k": [1, 2]}, {"k": [3]}) == {"k": [3]}

    def test_dict_replaced_by_scalar_from_dict2(self):
        assert main.deep_merge({"k": {"x": 1}}, {"k": 5}) == {"k": 5}

    def test_scalar_replaced_by_dict_from_dict2(self):
        assert main.deep_merge({"k": 5}, {"k": {"x": 1}}) == {"k": {"x": 1}}

    def test_inputs_are_not_mutated(self):
        dict1 = {"a": {"x": 1}}
        dict2 = {"a": {"y": 2}}
        main.deep_merge(dict1, dict2)
        assert dict1 == {"a": {"x": 1}}
        assert dict2 == {"a": {"y": 2}}

    def test_result_is_deepcopy_independent_of_inputs(self):
        dict1 = {"nested": {"x": 1}}
        dict2 = {"other": 2}
        result = main.deep_merge(dict1, dict2)
        result["nested"]["x"] = 999
        assert dict1["nested"]["x"] == 1


class TestGetResourceName:
    """``get_resource_name`` resolves name -> address -> id -> 'unknown'."""

    def test_prefers_name(self):
        resource = SimpleNamespace(name="my-name", address="1.2.3.4/32", id=7)
        assert main.get_resource_name(resource) == "my-name"

    def test_falls_back_to_address(self):
        resource = SimpleNamespace(address="1.2.3.4/32", id=7)
        assert main.get_resource_name(resource) == "1.2.3.4/32"

    def test_falls_back_to_id(self):
        resource = SimpleNamespace(id=7)
        assert main.get_resource_name(resource) == 7

    def test_falls_back_to_unknown(self):
        assert main.get_resource_name(SimpleNamespace()) == "unknown"


class TestGetDeviceRoleSlug:
    """``get_device_role_slug`` prefers role.slug, then role.name, lowercased."""

    def test_prefers_slug_lowercased(self, make_device):
        device = make_device(role_slug="Control")
        assert main.get_device_role_slug(device) == "control"

    def test_prefers_slug_over_name(self, make_device):
        device = make_device(role_slug="Leaf", role_name="Some Name")
        assert main.get_device_role_slug(device) == "leaf"

    def test_falls_back_to_name_lowercased(self, make_device):
        device = make_device(role_name="Compute")
        assert main.get_device_role_slug(device) == "compute"

    def test_returns_empty_when_role_is_falsy(self, make_device):
        device = make_device()  # role defaults to None
        assert main.get_device_role_slug(device) == ""

    def test_returns_empty_when_role_has_neither_attribute(self):
        device = SimpleNamespace(role=SimpleNamespace())
        assert main.get_device_role_slug(device) == ""


class TestFindDeviceNamesInStructure:
    """``find_device_names_in_structure`` collects every string device name."""

    def test_empty_structure_returns_empty(self):
        assert main.find_device_names_in_structure({}) == []

    def test_flat_structure_without_device_returns_empty(self):
        assert main.find_device_names_in_structure({"name": "x", "vid": 1}) == []

    def test_collects_top_level_device(self):
        assert main.find_device_names_in_structure({"device": "node-0"}) == ["node-0"]

    def test_ignores_non_string_device_values(self):
        assert main.find_device_names_in_structure({"device": 123}) == []

    def test_collects_from_lists_of_dicts(self):
        data = {"items": [{"device": "a"}, {"device": "b"}]}
        assert main.find_device_names_in_structure(data) == ["a", "b"]

    def test_collects_from_deep_nesting(self):
        data = {"a": {"b": {"c": {"device": "deep"}}}}
        assert main.find_device_names_in_structure(data) == ["deep"]

    def test_collects_multiple_nested_devices(self):
        data = {
            "termination_a": {"device": "switch-1"},
            "termination_b": {"device": "node-0"},
        }
        assert main.find_device_names_in_structure(data) == ["switch-1", "node-0"]


class TestIsVirtualInterface:
    """``is_virtual_interface`` is true when type.value/label contains virtual."""

    def test_true_when_value_contains_virtual(self, make_interface):
        assert main.is_virtual_interface(make_interface(type_value="virtual")) is True

    def test_value_match_is_case_insensitive(self, make_interface):
        assert main.is_virtual_interface(make_interface(type_value="VIRTUAL")) is True

    def test_true_when_value_contains_virtual_as_substring(self, make_interface):
        interface = make_interface(type_value="bridge-virtual")
        assert main.is_virtual_interface(interface) is True

    def test_true_when_label_contains_virtual(self, make_interface):
        interface = make_interface(type_label="Virtual Interface")
        assert main.is_virtual_interface(interface) is True

    def test_label_used_when_value_does_not_match(self, make_interface):
        interface = make_interface(type_value="1000base-t", type_label="Virtual")
        assert main.is_virtual_interface(interface) is True

    def test_false_when_type_is_falsy(self, make_interface):
        assert main.is_virtual_interface(make_interface()) is False

    def test_false_when_value_does_not_match(self, make_interface):
        assert (
            main.is_virtual_interface(make_interface(type_value="1000base-t")) is False
        )

    def test_false_when_type_has_neither_attribute(self):
        interface = SimpleNamespace(type=SimpleNamespace())
        assert main.is_virtual_interface(interface) is False


class TestSplitTasksByType:
    """``_split_tasks_by_type`` groups tasks by their first dict key."""

    def test_empty_input_returns_empty_dict(self):
        assert main._split_tasks_by_type([]) == {}

    def test_groups_by_first_key_and_preserves_order(self):
        tasks = [
            {"device_interface": {"name": "Ethernet0"}},
            {"cable": {"type": "cat6a"}},
            {"device_interface": {"name": "Ethernet1"}},
        ]
        result = main._split_tasks_by_type(tasks)
        assert result == {
            "device_interface": [
                {"device_interface": {"name": "Ethernet0"}},
                {"device_interface": {"name": "Ethernet1"}},
            ],
            "cable": [{"cable": {"type": "cat6a"}}],
        }
        # Resource types appear in first-seen order; tasks keep their order.
        assert list(result.keys()) == ["device_interface", "cable"]
