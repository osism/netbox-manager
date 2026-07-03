# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the autoconf collectors in ``netbox_manager.main`` (#254).

Tier 6 groups 1-3 of the coverage effort tracked in #232. These cover the
functions that walk a (mocked) NetBox API to collect per-interface MAC
assignments and per-device IP assignments, and the ``_generate_autoconf_tasks``
orchestrator that assembles them into the ``tasks_by_type`` payload:

* ``collect_interface_assignments`` -- MAC resolution (direct vs. fallback),
  the virtual-interface skip, the emitted task shape and name-sorted ordering.
* ``collect_ip_assignments_by_interface`` -- OOB vs. primary IPv4/IPv6 routing,
  per-device grouping and merging, empty/no-match handling, and ordering.
* ``_generate_autoconf_tasks`` -- the switch/non-switch split, the interface
  and device buckets, the OOB+primary per-device merge, and the returned bucket
  keys.

The collectors read from a live ``pynetbox.api`` client, so the fake
``make_autoconf_api`` client from ``conftest`` (extending the Tier 1 #247
``make_device`` / ``make_interface`` factories) stands in for NetBox;
``_generate_autoconf_tasks`` additionally calls ``create_netbox_api`` and is
tested by patching ``main.create_netbox_api``.

Out of scope here: the pure helpers ``is_virtual_interface`` /
``get_device_role_slug`` / ``get_switch_roles`` (Tier 1, #247), the generators
feeding the dispatcher (Tier 5, #253), the ``_write_autoconf_files`` internals
(Tier 3, #259) and the ``_run_autoconf_for_devices`` dispatcher and CLI wiring
(covered in ``test_autoconf_dispatch.py`` / Tier 10). No live NetBox or
``ansible_runner`` is involved.
"""

import logging
from types import SimpleNamespace

from netbox_manager import main


def _ip(address):
    """Build an IP-address record bag exposing only ``.address``."""
    return SimpleNamespace(address=address)


class TestCollectInterfaceAssignments:
    """Group 1 -- ``collect_interface_assignments`` MAC collection."""

    def test_virtual_interface_is_skipped(
        self, make_autoconf_api, make_device, make_interface
    ):
        # Both the value- and label-detected virtual interfaces are skipped even
        # though they carry a MAC that would otherwise be emitted.
        devices = {1: make_device(name="node-0")}
        interfaces = [
            make_interface(
                name="Vlan1", type_value="virtual", mac_address="aa:bb:cc:00:00:01"
            ),
            make_interface(
                name="Vlan2", type_label="Virtual", mac_address="aa:bb:cc:00:00:02"
            ),
        ]
        api = make_autoconf_api(interfaces_by_device={1: interfaces})

        assert main.collect_interface_assignments(api, devices) == []

    def test_direct_mac_address_is_used(
        self, make_autoconf_api, make_device, make_interface, make_dtl_record
    ):
        # A truthy `mac_address` wins and is coerced with `str(...)`; a non-str
        # record object pins that coercion.
        devices = {1: make_device(name="node-0")}
        mac_record = make_dtl_record("18:C0:86:D4:E2:F7")
        interface = make_interface(name="Ethernet0", mac_address=mac_record)
        api = make_autoconf_api(interfaces_by_device={1: [interface]})

        tasks = main.collect_interface_assignments(api, devices)

        assert (
            tasks[0]["device_interface"]["primary_mac_address"] == "18:C0:86:D4:E2:F7"
        )
        assert isinstance(tasks[0]["device_interface"]["primary_mac_address"], str)

    def test_falls_back_to_first_mac_addresses_entry(
        self, make_autoconf_api, make_device, make_interface
    ):
        # With no direct MAC, the first `mac_addresses` entry is used.
        devices = {1: make_device(name="node-0")}
        interface = make_interface(
            name="Ethernet0",
            mac_address=None,
            mac_addresses=[
                SimpleNamespace(mac_address="aa:bb:cc:00:00:01"),
                SimpleNamespace(mac_address="aa:bb:cc:00:00:02"),
            ],
        )
        api = make_autoconf_api(interfaces_by_device={1: [interface]})

        tasks = main.collect_interface_assignments(api, devices)

        assert (
            tasks[0]["device_interface"]["primary_mac_address"] == "aa:bb:cc:00:00:01"
        )

    def test_interface_without_mac_emits_no_task(
        self, make_autoconf_api, make_device, make_interface
    ):
        # Neither source yields a MAC -> nothing is appended.
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Ethernet0", mac_address=None, mac_addresses=[])
        api = make_autoconf_api(interfaces_by_device={1: [interface]})

        assert main.collect_interface_assignments(api, devices) == []

    def test_emitted_task_shape(self, make_autoconf_api, make_device, make_interface):
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:01")
        api = make_autoconf_api(interfaces_by_device={1: [interface]})

        tasks = main.collect_interface_assignments(api, devices)

        assert tasks == [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "Ethernet0",
                    "primary_mac_address": "aa:bb:cc:00:00:01",
                }
            }
        ]

    def test_devices_iterated_in_name_sorted_order(
        self, make_autoconf_api, make_device, make_interface
    ):
        # Devices are iterated in name-sorted order regardless of dict key order.
        devices = {
            1: make_device(name="b-node"),
            2: make_device(name="a-node"),
        }
        interfaces_by_device = {
            1: [make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:0b")],
            2: [make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:0a")],
        }
        api = make_autoconf_api(interfaces_by_device=interfaces_by_device)

        tasks = main.collect_interface_assignments(api, devices)

        assert [task["device_interface"]["device"] for task in tasks] == [
            "a-node",
            "b-node",
        ]


class TestCollectIpAssignmentsByInterface:
    """Group 2 -- ``collect_ip_assignments_by_interface`` IP collection."""

    def test_seeds_entry_with_device_name(
        self, make_autoconf_api, make_device, make_interface
    ):
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="eth0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("172.16.0.10/20")]},
        )

        result = main.collect_ip_assignments_by_interface(api, devices, "eth0", "OOB")

        assert result["node-0"]["name"] == "node-0"

    def test_oob_type_writes_oob_ip(
        self, make_autoconf_api, make_device, make_interface
    ):
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="eth0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("172.16.0.10/20")]},
        )

        result = main.collect_ip_assignments_by_interface(api, devices, "eth0", "OOB")

        assert result["node-0"]["oob_ip"] == "172.16.0.10/20"
        assert "primary_ip4" not in result["node-0"]

    def test_primary_ipv4_written_for_dotted_address(
        self, make_autoconf_api, make_device, make_interface
    ):
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Loopback0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("192.168.16.10/32")]},
        )

        result = main.collect_ip_assignments_by_interface(
            api, devices, "Loopback0", "Primary"
        )

        assert result["node-0"]["primary_ip4"] == "192.168.16.10/32"
        assert "primary_ip6" not in result["node-0"]

    def test_primary_ipv6_written_for_colon_address(
        self, make_autoconf_api, make_device, make_interface
    ):
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Loopback0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("fda6:f659:8c2b::192:168:16:10/128")]},
        )

        result = main.collect_ip_assignments_by_interface(
            api, devices, "Loopback0", "Primary"
        )

        assert result["node-0"]["primary_ip6"] == "fda6:f659:8c2b::192:168:16:10/128"
        assert "primary_ip4" not in result["node-0"]

    def test_multiple_ips_accumulate_per_device(
        self, make_autoconf_api, make_device, make_interface
    ):
        # An IPv4 and an IPv6 on the same interface merge into one assignment.
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Loopback0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("192.168.16.10/32"), _ip("fda6::10/128")]},
        )

        result = main.collect_ip_assignments_by_interface(
            api, devices, "Loopback0", "Primary"
        )

        assert result["node-0"] == {
            "name": "node-0",
            "primary_ip4": "192.168.16.10/32",
            "primary_ip6": "fda6::10/128",
        }

    def test_no_matching_interface_or_empty_input(
        self, make_autoconf_api, make_device, make_interface
    ):
        # A device whose named interface is absent contributes no key.
        devices = {1: make_device(name="node-0")}
        interface = make_interface(name="Ethernet0", interface_id=10)
        api = make_autoconf_api(
            interfaces_by_device={1: [interface]},
            ips_by_interface={10: [_ip("172.16.0.10/20")]},
        )
        assert (
            main.collect_ip_assignments_by_interface(api, devices, "eth0", "OOB") == {}
        )

        # No devices at all -> empty result.
        empty_api = make_autoconf_api()
        assert (
            main.collect_ip_assignments_by_interface(empty_api, {}, "eth0", "OOB") == {}
        )

    def test_result_keys_in_name_sorted_order(
        self, make_autoconf_api, make_device, make_interface
    ):
        devices = {
            1: make_device(name="node-b"),
            2: make_device(name="node-a"),
        }
        interfaces_by_device = {
            1: [make_interface(name="eth0", interface_id=11)],
            2: [make_interface(name="eth0", interface_id=12)],
        }
        ips_by_interface = {
            11: [_ip("172.16.0.11/20")],
            12: [_ip("172.16.0.12/20")],
        }
        api = make_autoconf_api(
            interfaces_by_device=interfaces_by_device,
            ips_by_interface=ips_by_interface,
        )

        result = main.collect_ip_assignments_by_interface(api, devices, "eth0", "OOB")

        assert list(result.keys()) == ["node-a", "node-b"]


class TestGenerateAutoconfTasks:
    """Group 3 -- ``_generate_autoconf_tasks`` orchestration."""

    def test_switch_split_is_logged(
        self, monkeypatch, caplog, make_autoconf_api, make_device
    ):
        monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)
        devices = [
            make_device(name="switch-0", device_id=1, role_slug="leaf"),
            make_device(name="node-0", device_id=2, role_slug="control"),
        ]
        api = make_autoconf_api(devices=devices)
        monkeypatch.setattr(main, "create_netbox_api", lambda: api)

        with caplog.at_level(logging.INFO):
            main._generate_autoconf_tasks()

        assert "2 total devices (1 non-switch, 1 switches)" in caplog.text

    def test_interface_bucket_includes_switch_macs(
        self, monkeypatch, make_autoconf_api, make_device, make_interface
    ):
        # `collect_interface_assignments` runs over ALL devices, so a switch's
        # MAC lands in the device_interface bucket too.
        monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)
        devices = [
            make_device(name="switch-0", device_id=1, role_slug="leaf"),
            make_device(name="node-0", device_id=2, role_slug="control"),
        ]
        interfaces_by_device = {
            1: [make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:01")],
            2: [make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:02")],
        }
        api = make_autoconf_api(
            devices=devices, interfaces_by_device=interfaces_by_device
        )
        monkeypatch.setattr(main, "create_netbox_api", lambda: api)

        result = main._generate_autoconf_tasks()

        names = {
            task["device_interface"]["device"] for task in result["device_interface"]
        }
        assert names == {"switch-0", "node-0"}

    def test_oob_and_primary_merge_by_device_name(
        self, monkeypatch, make_autoconf_api, make_device, make_interface
    ):
        # eth0 (OOB) and Loopback0 (IPv4 + IPv6) merge into one device task.
        devices = [make_device(name="node-0", device_id=1, role_slug="control")]
        interfaces_by_device = {
            1: [
                make_interface(name="eth0", interface_id=10),
                make_interface(name="Loopback0", interface_id=11),
            ]
        }
        ips_by_interface = {
            10: [_ip("172.16.0.10/20")],
            11: [_ip("192.168.16.10/32"), _ip("fda6::10/128")],
        }
        api = make_autoconf_api(
            devices=devices,
            interfaces_by_device=interfaces_by_device,
            ips_by_interface=ips_by_interface,
        )
        monkeypatch.setattr(main, "create_netbox_api", lambda: api)

        result = main._generate_autoconf_tasks()

        assert result["device"] == [
            {
                "device": {
                    "name": "node-0",
                    "oob_ip": "172.16.0.10/20",
                    "primary_ip4": "192.168.16.10/32",
                    "primary_ip6": "fda6::10/128",
                }
            }
        ]
        # The MAC-less interfaces leave the interface bucket empty.
        assert result["device_interface"] == []

    def test_device_bucket_sorted_by_device_name(
        self, monkeypatch, make_autoconf_api, make_device, make_interface
    ):
        # Devices are returned in reverse-name order but the device bucket is
        # emitted in sorted-name order.
        devices = [
            make_device(name="node-b", device_id=1, role_slug="control"),
            make_device(name="node-a", device_id=2, role_slug="control"),
        ]
        interfaces_by_device = {
            1: [make_interface(name="eth0", interface_id=11)],
            2: [make_interface(name="eth0", interface_id=12)],
        }
        ips_by_interface = {
            11: [_ip("172.16.0.11/20")],
            12: [_ip("172.16.0.12/20")],
        }
        api = make_autoconf_api(
            devices=devices,
            interfaces_by_device=interfaces_by_device,
            ips_by_interface=ips_by_interface,
        )
        monkeypatch.setattr(main, "create_netbox_api", lambda: api)

        result = main._generate_autoconf_tasks()

        assert [task["device"]["name"] for task in result["device"]] == [
            "node-a",
            "node-b",
        ]

    def test_returned_bucket_keys_and_empty_ip_address(
        self, monkeypatch, make_autoconf_api, make_device, make_interface
    ):
        devices = [make_device(name="node-0", device_id=1, role_slug="control")]
        interfaces_by_device = {
            1: [make_interface(name="Ethernet0", mac_address="aa:bb:cc:00:00:01")]
        }
        api = make_autoconf_api(
            devices=devices, interfaces_by_device=interfaces_by_device
        )
        monkeypatch.setattr(main, "create_netbox_api", lambda: api)

        result = main._generate_autoconf_tasks()

        assert set(result.keys()) == {"device", "device_interface", "ip_address"}
        # No `ip_address` tasks are ever produced by this function.
        assert result["ip_address"] == []
