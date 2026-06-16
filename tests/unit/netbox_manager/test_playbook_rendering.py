# SPDX-License-Identifier: Apache-2.0

"""Unit tests for playbook rendering in ``netbox_manager.main`` (issue #249).

Covers groups 3 and 4 of the Tier 2 scope tracked in #232:
``create_ansible_playbook`` (which renders ``playbook_template`` into a single
play) and ``ProperIndentDumper`` (the YAML dumper that indents nested sequences
under their key). Both are deterministic transformations of plain Python data;
the round-trip assertions parse the rendered/dumped strings back with
``yaml.safe_load`` -- no filesystem is involved.
"""

import yaml

from netbox_manager import main


class TestCreateAnsiblePlaybook:
    """``create_ansible_playbook`` renders one play from vars and tasks."""

    def test_single_play_with_basename_and_connection_settings(self):
        result = main.create_ansible_playbook(
            "/some/dir/300-devices.yml", {"site": "Disc"}, []
        )
        plays = yaml.safe_load(result)
        assert isinstance(plays, list)
        assert len(plays) == 1
        play = plays[0]
        # The play name carries only the basename, not the directory part.
        assert "300-devices.yml" in play["name"]
        assert "/some/dir" not in play["name"]
        assert play["connection"] == "local"
        assert play["hosts"] == "localhost"
        assert play["gather_facts"] is False

    def test_vars_and_tasks_round_trip(self):
        template_vars = {"site": "Disc", "nested": {"a": 1, "b": [2, 3]}}
        # Build a real task via the Tier-2 builder, from a fresh dict (it mutates
        # its input). This exercises a nested module envelope through indent(4).
        template_tasks = [main.create_netbox_task("vlan", {"name": "OOB", "vid": 100})]

        result = main.create_ansible_playbook("x.yml", template_vars, template_tasks)
        play = yaml.safe_load(result)[0]

        # indent(4) must not corrupt the nested structures on the way through.
        assert play["vars"] == template_vars
        assert play["tasks"] == template_tasks

    def test_empty_vars_renders_parseable_yaml(self):
        template_tasks = [main.create_netbox_task("vlan", {"name": "OOB"})]
        play = yaml.safe_load(
            main.create_ansible_playbook("x.yml", {}, template_tasks)
        )[0]
        assert play["vars"] == {}
        assert play["tasks"] == template_tasks

    def test_empty_tasks_renders_parseable_yaml(self):
        play = yaml.safe_load(
            main.create_ansible_playbook("x.yml", {"site": "Disc"}, [])
        )[0]
        assert play["tasks"] == []
        assert play["vars"] == {"site": "Disc"}

    def test_empty_vars_and_tasks_render_parseable_yaml(self):
        play = yaml.safe_load(main.create_ansible_playbook("x.yml", {}, []))[0]
        assert play["vars"] == {}
        assert play["tasks"] == []


class TestProperIndentDumper:
    """``ProperIndentDumper`` indents nested sequences under their parent key."""

    def test_nested_sequence_is_indented(self):
        data = {"name": "x", "tags": ["a", "b"]}

        dumped = yaml.dump(
            data, Dumper=main.ProperIndentDumper, default_flow_style=False
        )
        # The sequence is indented under `tags:` rather than aligned with it.
        assert "  - a" in dumped

        # Contrast: the stock dumper produces indentless sequences for the same
        # input, so the bullet sits at the parent key's column.
        default_dumped = yaml.dump(data, default_flow_style=False)
        assert "\n- a" in default_dumped

    def test_round_trip_with_autoconf_dump_signature(self):
        # Nested task shapes `_write_autoconf_files` emits: a device_interface
        # task with a nested `data` dict carrying `tagged_vlans`/`tags` lists,
        # and a cable task with nested termination dicts.
        data = [
            {
                "name": "Manage NetBox resource Ethernet1 of type device_interface",
                "netbox.netbox.netbox_device_interface": {
                    "data": {
                        "device": "switch-0",
                        "name": "Ethernet1",
                        "mode": "tagged",
                        "tagged_vlans": [
                            {"name": "Management"},
                            {"name": "External"},
                        ],
                        "tags": ["managed-by-osism"],
                    },
                    "state": "present",
                },
            },
            {
                "name": "Manage NetBox resource of type cable",
                "netbox.netbox.netbox_cable": {
                    "data": {
                        "type": "cat6a",
                        "termination_a": {"device": "switch-0", "name": "Ethernet1"},
                        "termination_b": {"device": "node-0", "name": "Ethernet0"},
                    },
                    "state": "present",
                },
            },
        ]

        dumped = yaml.dump(
            data,
            Dumper=main.ProperIndentDumper,
            default_flow_style=False,
            sort_keys=False,
            explicit_start=True,
        )
        assert yaml.safe_load(dumped) == data
