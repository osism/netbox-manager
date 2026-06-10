# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the autoconf helpers in ``netbox_manager.main`` (issue #247).

Covers group 4 of the Tier 1 scope tracked in #232: deriving the autoconf file
prefix from a site folder name, extracting device names from autoconf tasks,
and the device-filter helpers. All operate on plain dicts/lists and sets.
"""

from netbox_manager import main


class TestGetAutoconfPrefix:
    """``get_autoconf_prefix`` maps a folder number to its hundred-range x99."""

    def test_two_hundred_range(self):
        assert main.get_autoconf_prefix("200-aa") == "299-autoconf"

    def test_thousand_range(self):
        assert main.get_autoconf_prefix("1000-xx") == "1099-autoconf"

    def test_zero_range(self):
        assert main.get_autoconf_prefix("0-x") == "99-autoconf"

    def test_floors_to_start_of_hundred_range(self):
        # 250 -> 200 -> 299; the part inside the range does not matter.
        assert main.get_autoconf_prefix("250-foo") == "299-autoconf"


class TestExtractDeviceNamesFromAutoconfTask:
    """``_extract_device_names_from_autoconf_task`` finds refs and definitions."""

    def test_nested_device_reference(self):
        task = {"device_interface": {"device": "node-0", "name": "Ethernet0"}}
        assert main._extract_device_names_from_autoconf_task(task) == ["node-0"]

    def test_device_definition_task(self):
        task = {"device": {"name": "node-0", "site": "Discworld"}}
        assert main._extract_device_names_from_autoconf_task(task) == ["node-0"]

    def test_top_level_string_device_reference(self):
        assert main._extract_device_names_from_autoconf_task({"device": "node-0"}) == [
            "node-0"
        ]

    def test_no_device_returns_empty(self):
        assert (
            main._extract_device_names_from_autoconf_task({"vlan": {"vid": 100}}) == []
        )


class TestFilterTasksByDevice:
    """``_filter_tasks_by_device`` keeps tasks referencing a filtered device."""

    def test_keeps_only_matching_tasks(self):
        tasks = [
            {"device_interface": {"device": "node-0"}},
            {"device_interface": {"device": "node-1"}},
        ]
        assert main._filter_tasks_by_device(tasks, {"node-0"}) == [
            {"device_interface": {"device": "node-0"}}
        ]

    def test_empty_filter_keeps_nothing(self):
        tasks = [{"device_interface": {"device": "node-0"}}]
        assert main._filter_tasks_by_device(tasks, set()) == []

    def test_no_match_keeps_nothing(self):
        tasks = [{"device_interface": {"device": "node-0"}}]
        assert main._filter_tasks_by_device(tasks, {"switch-9"}) == []

    def test_keeps_matching_portchannel_lag_tasks(self):
        # The shapes `_generate_portchannel_tasks` actually emits: per-switch LAG
        # creations and member assignments, each a single-device
        # `device_interface` task. The pre-consolidation
        # `_filter_portchannel_tasks_by_device` ran on exactly this list.
        tasks = [
            {
                "device_interface": {
                    "device": "switch-1",
                    "name": "PortChannel3",
                    "type": "lag",
                }
            },
            {
                "device_interface": {
                    "device": "switch-2",
                    "name": "PortChannel3",
                    "type": "lag",
                }
            },
            {
                "device_interface": {
                    "device": "switch-1",
                    "name": "Ethernet3",
                    "lag": "PortChannel3",
                }
            },
            {
                "device_interface": {
                    "device": "switch-2",
                    "name": "Ethernet3",
                    "lag": "PortChannel3",
                }
            },
        ]
        assert main._filter_tasks_by_device(tasks, {"switch-1"}) == [
            {
                "device_interface": {
                    "device": "switch-1",
                    "name": "PortChannel3",
                    "type": "lag",
                }
            },
            {
                "device_interface": {
                    "device": "switch-1",
                    "name": "Ethernet3",
                    "lag": "PortChannel3",
                }
            },
        ]


class TestFilterTasksByTypeByDevice:
    """``_filter_tasks_by_type_by_device`` filters each resource-type bucket."""

    def test_filters_within_each_bucket_and_keeps_keys(self):
        tasks_by_type = {
            "device_interface": [
                {"device_interface": {"device": "node-0"}},
                {"device_interface": {"device": "node-1"}},
            ],
            "cable": [{"cable": {"termination_a": {"device": "node-9"}}}],
        }
        result = main._filter_tasks_by_type_by_device(tasks_by_type, {"node-0"})
        assert result == {
            "device_interface": [{"device_interface": {"device": "node-0"}}],
            # The bucket key is preserved even when nothing matches.
            "cable": [],
        }
