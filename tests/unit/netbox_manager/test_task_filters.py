# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the task-filter helpers in ``netbox_manager.main`` (issue #247).

Covers group 3 of the Tier 1 scope tracked in #232: the helpers that decide
whether a task is skipped by a ``--limit`` task-type filter or a device filter,
and the helper that extracts device names from a task. All operate on plain
dicts/lists, so they need no mocks.
"""

from netbox_manager import main


class TestShouldSkipTaskByFilter:
    """``should_skip_task_by_filter`` compares key and filter after `-`/`_`."""

    def test_exact_match_does_not_skip(self):
        assert main.should_skip_task_by_filter("device", "device") is False

    def test_dash_key_matches_underscore_filter(self):
        assert (
            main.should_skip_task_by_filter("device-interface", "device_interface")
            is False
        )

    def test_underscore_key_matches_dash_filter(self):
        assert (
            main.should_skip_task_by_filter("device_interface", "device-interface")
            is False
        )

    def test_mismatch_skips(self):
        assert main.should_skip_task_by_filter("cable", "device") is True

    def test_mismatch_skips_even_after_normalisation(self):
        assert main.should_skip_task_by_filter("device-interface", "cable") is True


class TestExtractDeviceNamesFromTask:
    """``extract_device_names_from_task`` gathers direct and nested names."""

    def test_direct_device_field(self):
        value = {"device": "node-0"}
        # Collected once directly and once by the nested search: no dedup.
        assert main.extract_device_names_from_task("device_interface", value) == [
            "node-0",
            "node-0",
        ]

    def test_device_creation_task_uses_name(self):
        value = {"name": "testbed-node-0", "site": "Discworld"}
        assert main.extract_device_names_from_task("device", value) == [
            "testbed-node-0"
        ]

    def test_device_creation_task_without_name_yields_nothing(self):
        assert (
            main.extract_device_names_from_task("device", {"site": "Discworld"}) == []
        )

    def test_nested_names_only(self):
        value = {
            "termination_a": {"device": "switch-1"},
            "termination_b": {"device": "node-0"},
        }
        assert main.extract_device_names_from_task("cable", value) == [
            "switch-1",
            "node-0",
        ]

    def test_combines_direct_and_nested_without_dedup(self):
        value = {"device": "node-0", "extra": {"device": "node-9"}}
        assert main.extract_device_names_from_task("device_interface", value) == [
            "node-0",  # direct device field
            "node-0",  # same value re-collected by the nested search
            "node-9",  # nested device
        ]


class TestShouldSkipTaskByDeviceFilter:
    """``should_skip_task_by_device_filter`` skips on empty/no-match."""

    def test_empty_device_names_skips(self):
        assert main.should_skip_task_by_device_filter([], ["node"]) is True

    def test_substring_match_does_not_skip(self):
        assert (
            main.should_skip_task_by_device_filter(["testbed-node-0"], ["node"])
            is False
        )

    def test_no_match_skips(self):
        assert main.should_skip_task_by_device_filter(["node-0"], ["switch"]) is True

    def test_any_matching_filter_does_not_skip(self):
        assert (
            main.should_skip_task_by_device_filter(["node-0"], ["switch", "node"])
            is False
        )

    def test_any_matching_device_name_does_not_skip(self):
        assert (
            main.should_skip_task_by_device_filter(["switch-1", "node-0"], ["node"])
            is False
        )
