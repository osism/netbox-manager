# SPDX-License-Identifier: Apache-2.0

from netbox_manager import dtl, main


def test_dtl_module_imports():
    assert hasattr(dtl, "Repo")
    assert hasattr(dtl, "NetBox")
    assert hasattr(dtl, "DeviceTypes")


def test_deep_merge_smoke():
    merged = main.deep_merge(
        {"a": 1, "nested": {"x": 1}},
        {"b": 2, "nested": {"y": 2}},
    )
    assert merged == {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}


def test_get_leading_number_smoke():
    assert main.get_leading_number("100-foo.yml") == "100"
    assert main.get_leading_number("/abs/path/300-devices.yml") == "300"
    assert main.get_leading_number("noprefix.yml") == "noprefix.yml"
