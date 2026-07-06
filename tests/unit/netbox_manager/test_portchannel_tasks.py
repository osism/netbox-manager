# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_generate_portchannel_tasks`` (issue #253, group 7).

The generator finds switch-to-switch connections, dedups them by endpoint id,
and -- for each switch pair with two or more connections -- emits a LAG-creation
task per switch plus one member-assignment task per member interface. The
PortChannel number is derived per switch from the lowest port number (the second
number of a slashed name, otherwise the first). Roles are pinned via
``main.settings`` so switch selection is explicit.

``_connect``/``_link`` cross-links two interface bags so both switch perspectives
see the same NetBox interface ids, exercising the dedup path realistically: each
interface's ``device`` is its own home switch, and it appears in the other end's
``connected_endpoints``.
"""

import pytest

from netbox_manager import main


@pytest.fixture
def roles(monkeypatch):
    """Pin SWITCH_ROLES / NODE_ROLES so only ``leaf`` devices are switches."""
    monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)
    monkeypatch.setattr(main.settings, "NODE_ROLES", ["control"], raising=False)


def _wire(monkeypatch, make_netbox_api, *, devices, interfaces_by_device):
    """Point ``main.create_netbox_api`` at a fake API for the generator."""
    fake_api = make_netbox_api(
        devices=devices, interfaces_by_device=interfaces_by_device
    )
    monkeypatch.setattr(main, "create_netbox_api", lambda: fake_api)
    return fake_api


def _iface(make_interface, *, device, name, interface_id):
    return make_interface(name=name, interface_id=interface_id, device=device)


def _link(a, b, *, cabled=True):
    """Cross-link two interface bags so each end sees the other as an endpoint."""
    a.connected_endpoints = [b]
    b.connected_endpoints = [a]
    if cabled:
        a.cable = True
        b.cable = True


def _lag_task(device, name):
    return {
        "device_interface": {
            "device": device,
            "name": name,
            "type": "lag",
            "tags": ["managed-by-osism"],
        }
    }


def _member_task(device, name, lag):
    return {
        "device_interface": {
            "device": device,
            "name": name,
            "lag": lag,
            "tags": ["managed-by-osism"],
        }
    }


def test_two_connections_emit_lags_members_dedup_and_numbering(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    a1 = _iface(make_interface, device=sw_a, name="Eth1/3/1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Eth1/4/1", interface_id=102)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet48", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet49", interface_id=202)
    _link(a1, b1)
    _link(a2, b2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b],
        interfaces_by_device={1: [a1, a2], 2: [b1, b2]},
    )

    tasks = main._generate_portchannel_tasks()

    # Eth1/3/1 -> 3, Eth1/4/1 -> 4 => min 3 => PortChannel3 on sw-a;
    # Ethernet48/49 => min 48 => PortChannel48 on sw-b. Four sightings dedup to
    # two connections. LAG tasks precede members, each sorted by (device, name).
    assert tasks == [
        _lag_task("sw-a", "PortChannel3"),
        _lag_task("sw-b", "PortChannel48"),
        _member_task("sw-a", "Eth1/3/1", "PortChannel3"),
        _member_task("sw-a", "Eth1/4/1", "PortChannel3"),
        _member_task("sw-b", "Ethernet48", "PortChannel48"),
        _member_task("sw-b", "Ethernet49", "PortChannel48"),
    ]


def test_single_connection_emits_nothing(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    _link(a1, b1)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b],
        interfaces_by_device={1: [a1], 2: [b1]},
    )

    assert main._generate_portchannel_tasks() == []


def test_only_switch_roles_are_queried(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    node = make_device(name="node-0", role_slug="control", device_id=99)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    n1 = _iface(make_interface, device=node, name="Ethernet0", interface_id=901)
    _link(a1, n1)
    fake_api = _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, node],
        interfaces_by_device={1: [a1], 99: [n1]},
    )

    assert main._generate_portchannel_tasks() == []
    queried = [call["device_id"] for call in fake_api.dcim.interfaces.filter_calls]
    assert queried == [1]


def test_switch_to_node_connections_are_ignored(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # Two cables from a switch to a node: the connected role is not a switch
    # role, so no connection is collected even though the count would meet the
    # threshold.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    node = make_device(name="node-0", role_slug="control", device_id=99)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    n1 = _iface(make_interface, device=node, name="Ethernet0", interface_id=901)
    n2 = _iface(make_interface, device=node, name="Ethernet1", interface_id=902)
    _link(a1, n1)
    _link(a2, n2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, node],
        interfaces_by_device={1: [a1, a2], 99: [n1, n2]},
    )

    assert main._generate_portchannel_tasks() == []


def test_uncabled_interface_does_not_count(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # One good link plus a second link whose interfaces are wired but not
    # cabled: the cable gate keeps the pair at one connection, below threshold.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    _link(a1, b1)
    _link(a2, b2, cabled=False)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b],
        interfaces_by_device={1: [a1, a2], 2: [b1, b2]},
    )

    assert main._generate_portchannel_tasks() == []


def test_interface_without_endpoints_does_not_count(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # One good link plus a second link whose interfaces are cabled but expose no
    # connected endpoints: the endpoints gate keeps the pair below threshold.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    a2 = make_interface(name="Ethernet2", interface_id=102, device=sw_a, cable=True)
    b2 = make_interface(name="Ethernet2", interface_id=202, device=sw_b, cable=True)
    _link(a1, b1)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b],
        interfaces_by_device={1: [a1, a2], 2: [b1, b2]},
    )

    assert main._generate_portchannel_tasks() == []


def test_portchannel_number_zero_when_no_digits(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    a1 = _iface(make_interface, device=sw_a, name="uplinkA", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="uplinkB", interface_id=102)
    b1 = _iface(make_interface, device=sw_b, name="uplinkX", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="uplinkY", interface_id=202)
    _link(a1, b1)
    _link(a2, b2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b],
        interfaces_by_device={1: [a1, a2], 2: [b1, b2]},
    )

    tasks = main._generate_portchannel_tasks()

    lag_names = {
        (t["device_interface"]["device"], t["device_interface"]["name"])
        for t in tasks
        if t["device_interface"].get("type") == "lag"
    }
    assert lag_names == {("sw-a", "PortChannel0"), ("sw-b", "PortChannel0")}


def _lag_names_for(tasks, device):
    """Return the sorted PortChannel names of LAG-creation tasks on ``device``."""
    return sorted(
        t["device_interface"]["name"]
        for t in tasks
        if t["device_interface"].get("type") == "lag"
        and t["device_interface"]["device"] == device
    )


def test_shared_switch_colliding_slashed_names_get_distinct_lags(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # sw-c sits in two pairs. Arista/Dell modular naming makes both pairs'
    # members on sw-c extract to the same second number (1), so the per-pair
    # numbering wants PortChannel1 on sw-c twice -- a name collision on one
    # device. The two LAGs must instead be disambiguated to distinct names.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    c_a1 = _iface(make_interface, device=sw_c, name="Ethernet3/1", interface_id=331)
    c_a2 = _iface(make_interface, device=sw_c, name="Ethernet3/2", interface_id=332)
    c_b1 = _iface(make_interface, device=sw_c, name="Ethernet4/1", interface_id=341)
    c_b2 = _iface(make_interface, device=sw_c, name="Ethernet4/2", interface_id=342)
    _link(a1, c_a1)
    _link(a2, c_a2)
    _link(b1, c_b1)
    _link(b2, c_b2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b, sw_c],
        interfaces_by_device={
            1: [a1, a2],
            2: [b1, b2],
            3: [c_a1, c_a2, c_b1, c_b2],
        },
    )

    tasks = main._generate_portchannel_tasks()

    # Pairs are processed in sorted order: (sw-a, sw-c) keeps the natural
    # PortChannel1; (sw-b, sw-c) collides and is bumped to PortChannel2. Each
    # peer keeps its own independent PortChannel1.
    assert tasks == [
        _lag_task("sw-a", "PortChannel1"),
        _lag_task("sw-b", "PortChannel1"),
        _lag_task("sw-c", "PortChannel1"),
        _lag_task("sw-c", "PortChannel2"),
        _member_task("sw-a", "Ethernet1", "PortChannel1"),
        _member_task("sw-a", "Ethernet2", "PortChannel1"),
        _member_task("sw-b", "Ethernet1", "PortChannel1"),
        _member_task("sw-b", "Ethernet2", "PortChannel1"),
        _member_task("sw-c", "Ethernet3/1", "PortChannel1"),
        _member_task("sw-c", "Ethernet3/2", "PortChannel1"),
        _member_task("sw-c", "Ethernet4/1", "PortChannel2"),
        _member_task("sw-c", "Ethernet4/2", "PortChannel2"),
    ]


def test_shared_switch_colliding_cisco_names_get_distinct_lags(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # Cisco stackable naming: the second number is the (identical) linecard
    # index 0, so both pairs on sw-c want PortChannel0. They must resolve to two
    # distinct names.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    c_a1 = _iface(
        make_interface, device=sw_c, name="GigabitEthernet1/0/1", interface_id=311
    )
    c_a2 = _iface(
        make_interface, device=sw_c, name="GigabitEthernet1/0/2", interface_id=312
    )
    c_b1 = _iface(
        make_interface, device=sw_c, name="GigabitEthernet2/0/1", interface_id=321
    )
    c_b2 = _iface(
        make_interface, device=sw_c, name="GigabitEthernet2/0/2", interface_id=322
    )
    _link(a1, c_a1)
    _link(a2, c_a2)
    _link(b1, c_b1)
    _link(b2, c_b2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b, sw_c],
        interfaces_by_device={
            1: [a1, a2],
            2: [b1, b2],
            3: [c_a1, c_a2, c_b1, c_b2],
        },
    )

    tasks = main._generate_portchannel_tasks()

    assert _lag_names_for(tasks, "sw-c") == ["PortChannel0", "PortChannel1"]


def test_shared_switch_colliding_digitless_names_get_distinct_lags(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # The review's literal case, now actually shared on one switch: digit-less
    # names all extract to 0, so both pairs on sw-c want PortChannel0.
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    c_a1 = _iface(make_interface, device=sw_c, name="uplinkA", interface_id=311)
    c_a2 = _iface(make_interface, device=sw_c, name="uplinkB", interface_id=312)
    c_b1 = _iface(make_interface, device=sw_c, name="uplinkC", interface_id=321)
    c_b2 = _iface(make_interface, device=sw_c, name="uplinkD", interface_id=322)
    _link(a1, c_a1)
    _link(a2, c_a2)
    _link(b1, c_b1)
    _link(b2, c_b2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b, sw_c],
        interfaces_by_device={
            1: [a1, a2],
            2: [b1, b2],
            3: [c_a1, c_a2, c_b1, c_b2],
        },
    )

    tasks = main._generate_portchannel_tasks()

    assert _lag_names_for(tasks, "sw-c") == ["PortChannel0", "PortChannel1"]


def test_existing_lag_number_is_reused_for_surviving_channel(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # sw-c sits in two pairs. The sw-a<->sw-c pair is removed; only sw-b<->sw-c
    # remains. sw-c's surviving members (Ethernet4/1, Ethernet4/2) previously
    # belonged to PortChannel2. Without stateful numbering they would derive 1
    # (second number in the slashed name) and take PortChannel1, silently
    # shifting the name. With stateful numbering the existing LAG number is
    # reused and the name stays PortChannel2.
    from types import SimpleNamespace

    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    pc = make_interface(
        name="PortChannel2", type_value="lag", interface_id=390, device=sw_c
    )
    c1 = make_interface(
        name="Ethernet4/1",
        interface_id=341,
        device=sw_c,
        lag=SimpleNamespace(name="PortChannel2"),
    )
    c2 = make_interface(
        name="Ethernet4/2",
        interface_id=342,
        device=sw_c,
        lag=SimpleNamespace(name="PortChannel2"),
    )
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    _link(b1, c1)
    _link(b2, c2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_b, sw_c],
        interfaces_by_device={2: [b1, b2], 3: [c1, c2, pc]},
    )

    tasks = main._generate_portchannel_tasks()

    assert _lag_names_for(tasks, "sw-c") == ["PortChannel2"]


def test_new_channel_avoids_existing_lag_number(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # sw-c already has PortChannel1 (no members participating this run).
    # A newly cabled sw-b<->sw-c pair whose sw-c members carry no lag yet and
    # extract to 1 must bump past PortChannel1 to PortChannel2.
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    pc = make_interface(
        name="PortChannel1", type_value="lag", interface_id=391, device=sw_c
    )
    c1 = make_interface(name="Ethernet1", interface_id=311, device=sw_c)
    c2 = make_interface(name="Ethernet2", interface_id=312, device=sw_c)
    b1 = _iface(make_interface, device=sw_b, name="Ethernet1", interface_id=201)
    b2 = _iface(make_interface, device=sw_b, name="Ethernet2", interface_id=202)
    _link(b1, c1)
    _link(b2, c2)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_b, sw_c],
        interfaces_by_device={2: [b1, b2], 3: [c1, c2, pc]},
    )

    tasks = main._generate_portchannel_tasks()

    lag_tasks_sw_c = _lag_names_for(tasks, "sw-c")
    assert len(lag_tasks_sw_c) == 1
    assert lag_tasks_sw_c == ["PortChannel2"]


def test_lag_tasks_precede_members_each_sorted_by_device_and_name(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    sw_a = make_device(name="sw-a", role_slug="leaf", device_id=1)
    sw_b = make_device(name="sw-b", role_slug="leaf", device_id=2)
    sw_c = make_device(name="sw-c", role_slug="leaf", device_id=3)
    a1 = _iface(make_interface, device=sw_a, name="Ethernet1", interface_id=101)
    a2 = _iface(make_interface, device=sw_a, name="Ethernet2", interface_id=102)
    b3 = _iface(make_interface, device=sw_b, name="Ethernet3", interface_id=203)
    b4 = _iface(make_interface, device=sw_b, name="Ethernet4", interface_id=204)
    c10 = _iface(make_interface, device=sw_c, name="Ethernet10", interface_id=310)
    c11 = _iface(make_interface, device=sw_c, name="Ethernet11", interface_id=311)
    c20 = _iface(make_interface, device=sw_c, name="Ethernet20", interface_id=320)
    c21 = _iface(make_interface, device=sw_c, name="Ethernet21", interface_id=321)
    _link(a1, c10)
    _link(a2, c11)
    _link(b3, c20)
    _link(b4, c21)
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[sw_a, sw_b, sw_c],
        interfaces_by_device={
            1: [a1, a2],
            2: [b3, b4],
            3: [c10, c11, c20, c21],
        },
    )

    tasks = main._generate_portchannel_tasks()

    lag_tasks = [t for t in tasks if t["device_interface"].get("type") == "lag"]
    member_tasks = [t for t in tasks if "lag" in t["device_interface"]]
    # LAG tasks come first as a contiguous block.
    split = len(lag_tasks)
    assert tasks[:split] == lag_tasks
    assert tasks[split:] == member_tasks

    def key(task):
        di = task["device_interface"]
        return (di["device"], di["name"])

    assert [key(t) for t in lag_tasks] == [
        ("sw-a", "PortChannel1"),
        ("sw-b", "PortChannel3"),
        ("sw-c", "PortChannel10"),
        ("sw-c", "PortChannel20"),
    ]
    assert [key(t) for t in member_tasks] == [
        ("sw-a", "Ethernet1"),
        ("sw-a", "Ethernet2"),
        ("sw-b", "Ethernet3"),
        ("sw-b", "Ethernet4"),
        ("sw-c", "Ethernet10"),
        ("sw-c", "Ethernet11"),
        ("sw-c", "Ethernet20"),
        ("sw-c", "Ethernet21"),
    ]
