# SPDX-License-Identifier: Apache-2.0

"""Tests for disambiguation of duplicate device interface labels (issue #243).

When a switch's ``device_interface_label`` (e.g. ``data1``) is applied to a node
that has multiple links to that switch, the labels collide. The helper renames
the duplicates ``data1a``, ``data1b``, ... in natural order of the node
interface name.
"""

from netbox_manager import main


def _task(device, name, label=None):
    interface = {"device": device, "name": name, "tags": ["managed-by-osism"]}
    if label is not None:
        interface["label"] = label
    return {"device_interface": interface}


def _labels(tasks):
    return [t["device_interface"].get("label") for t in tasks]


def test_label_suffix_sequence():
    assert main._label_suffix(0) == "a"
    assert main._label_suffix(1) == "b"
    assert main._label_suffix(25) == "z"
    assert main._label_suffix(26) == "aa"
    assert main._label_suffix(27) == "ab"


def test_natural_sort_orders_numerically():
    names = ["Ethernet10", "Ethernet2", "Ethernet1"]
    assert sorted(names, key=main._natural_sort_key) == [
        "Ethernet1",
        "Ethernet2",
        "Ethernet10",
    ]


def test_no_collision_leaves_labels_unchanged():
    tasks = [
        _task("node-0", "Ethernet3", "leaf1"),
        _task("node-0", "Ethernet4", "leaf2"),
    ]
    main._disambiguate_interface_labels(tasks)
    assert _labels(tasks) == ["leaf1", "leaf2"]


def test_two_way_collision_uses_node_interface_order():
    # Provided out of order; Ethernet3 must become 'a', Ethernet5 'b'.
    tasks = [
        _task("node-0", "Ethernet5", "data1"),
        _task("node-0", "Ethernet3", "data1"),
    ]
    main._disambiguate_interface_labels(tasks)
    by_name = {
        t["device_interface"]["name"]: t["device_interface"]["label"] for t in tasks
    }
    assert by_name == {"Ethernet3": "data1a", "Ethernet5": "data1b"}


def test_three_way_collision():
    tasks = [
        _task("node-0", "Ethernet3", "data1"),
        _task("node-0", "Ethernet4", "data1"),
        _task("node-0", "Ethernet5", "data1"),
    ]
    main._disambiguate_interface_labels(tasks)
    assert _labels(tasks) == ["data1a", "data1b", "data1c"]


def test_collisions_are_independent_per_device():
    tasks = [
        _task("node-0", "Ethernet3", "data1"),
        _task("node-0", "Ethernet4", "data1"),
        _task("node-1", "Ethernet3", "data1"),
        _task("node-1", "Ethernet4", "data1"),
    ]
    main._disambiguate_interface_labels(tasks)
    assert _labels(tasks) == ["data1a", "data1b", "data1a", "data1b"]


def test_different_labels_on_same_device_do_not_collide():
    tasks = [
        _task("node-0", "Ethernet3", "data1"),
        _task("node-0", "Ethernet4", "data2"),
    ]
    main._disambiguate_interface_labels(tasks)
    assert _labels(tasks) == ["data1", "data2"]


def test_natural_sort_assignment_with_double_digit_interface():
    tasks = [
        _task("node-0", "Ethernet10", "data1"),
        _task("node-0", "Ethernet2", "data1"),
    ]
    main._disambiguate_interface_labels(tasks)
    by_name = {
        t["device_interface"]["name"]: t["device_interface"]["label"] for t in tasks
    }
    assert by_name == {"Ethernet2": "data1a", "Ethernet10": "data1b"}


def test_tasks_without_label_are_untouched():
    tasks = [
        _task("node-0", "Ethernet0"),
        _task("node-0", "Ethernet3", "data1"),
        _task("node-0", "Ethernet4", "data1"),
    ]
    main._disambiguate_interface_labels(tasks)
    assert _labels(tasks) == [None, "data1a", "data1b"]


def test_overflow_beyond_26_links():
    tasks = [_task("node-0", f"Ethernet{i:02d}", "data1") for i in range(27)]
    main._disambiguate_interface_labels(tasks)
    labels = _labels(tasks)
    assert labels[0] == "data1a"
    assert labels[25] == "data1z"
    assert labels[26] == "data1aa"


def test_returns_same_list_object():
    tasks = [_task("node-0", "Ethernet3", "data1")]
    assert main._disambiguate_interface_labels(tasks) is tasks
