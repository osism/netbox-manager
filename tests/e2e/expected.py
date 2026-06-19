"""Pinned expected NetBox state for the end-to-end test.

These fixtures describe the objects, counts, and relations that
``netbox-manager run`` must create when applied against the bundled
``example/`` data. They are consumed by :mod:`verify.py` (phase 3a).

The values are derived from ``example/`` and confirmed against a
known-good baseline run (see ``tests/e2e/README.md`` for how to
re-pin them when ``example/`` changes).
"""

from __future__ import annotations

# Exact object counts, keyed by the pynetbox endpoint path
# (``app.endpoint``). Every count maps one-to-one to a stanza in
# ``example/`` except ``dcim.interfaces``: NetBox auto-instantiates one
# interface per device-type interface template on device creation
# (250 template interfaces across the 16 devices) and the resources add
# 17 net-new interfaces (16 ``Loopback0`` plus the manager ``vlan100``),
# for 267 total.
EXPECTED_COUNTS: dict[str, int] = {
    "tenancy.tenants": 1,
    "dcim.sites": 1,
    "dcim.locations": 1,
    "dcim.racks": 1,
    "dcim.devices": 16,
    "dcim.interfaces": 267,
    "dcim.cables": 44,
    "dcim.mac_addresses": 60,
    "ipam.vlans": 1,
    "ipam.prefixes": 4,
    "ipam.ip_addresses": 51,
}

# The 16 unique devices defined in example/resources.
DEVICE_NAMES: list[str] = [
    "testbed-manager",
    *[f"testbed-node-{i}" for i in range(10)],
    *[f"testbed-switch-{i}" for i in range(4)],
    "testbed-switch-oob",
]

# Tenant / site / location, asserted by slug.
TENANT_SLUG = "testbed"
SITE_SLUG = "discworld"
LOCATION_SLUG = "ankh-morpork"

# The single VLAN.
VLAN_VID = 100
VLAN_NAME = "OOB Testbed"
VLAN_ROLE = "OOB"

# The four prefixes.
PREFIXES: list[str] = [
    "172.16.0.0/20",
    "192.168.16.0/20",
    "fda6:f659:8c2b::/48",
    "192.168.112.0/20",
]

# Interface spot-check: testbed-node-0:Ethernet0 is an access port with
# the OOB Testbed VLAN untagged.
IFACE_DEVICE = "testbed-node-0"
IFACE_NAME = "Ethernet0"
IFACE_MODE = "access"
IFACE_UNTAGGED_VLAN = "OOB Testbed"

# IP address spot-check: 192.168.16.10/32 is assigned to
# testbed-node-0:Loopback0.
IP_ADDRESS = "192.168.16.10/32"
IP_DEVICE = "testbed-node-0"
IP_INTERFACE = "Loopback0"

# MAC address spot-check: 18:C0:86:3A:B7:F1 sits on testbed-node-0:Ethernet0.
MAC_ADDRESS = "18:C0:86:3A:B7:F1"
MAC_DEVICE = "testbed-node-0"
MAC_INTERFACE = "Ethernet0"

# Primary / OOB IP wiring for testbed-node-0.
PRIMARY_DEVICE = "testbed-node-0"
PRIMARY_IP4 = "192.168.16.10/32"
PRIMARY_IP6 = "fda6:f659:8c2b::192:168:16:10/128"
OOB_IP = "172.16.0.10/20"
