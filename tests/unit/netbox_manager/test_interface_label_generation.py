# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_generate_device_interface_labels`` (issue #253, group 6).

The generator selects switch / router / firewall source devices that resolve a
non-empty interface label, walks each source interface's connected endpoints,
and emits a ``device_interface`` label task for every endpoint that lands on a
node interface. The label-resolution helper (``_resolve_device_interface_label``)
and the disambiguation helper (``_disambiguate_interface_labels``) have their own
Tier 1 / #243 / #220 coverage; here only their effect inside the generator is
asserted. Roles are pinned via ``main.settings`` -- crucially NODE_ROLES is set
to a list that excludes ``router`` / ``firewall`` (which the in-module defaults
treat as node roles) so source-vs-node selection is unambiguous.
"""

from types import SimpleNamespace

import pytest

from netbox_manager import main


@pytest.fixture
def roles(monkeypatch):
    """Pin roles so ``router`` / ``firewall`` are sources, not nodes."""
    monkeypatch.setattr(main.settings, "NODE_ROLES", ["control"], raising=False)
    monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)


def _wire(
    monkeypatch,
    make_netbox_api,
    *,
    devices,
    interfaces_by_device=None,
    config_contexts_by_cluster=None,
):
    """Point ``main.create_netbox_api`` at a fake API for the label generator."""
    fake_api = make_netbox_api(
        devices=devices,
        interfaces_by_device=interfaces_by_device,
        config_contexts_by_cluster=config_contexts_by_cluster,
    )
    monkeypatch.setattr(main, "create_netbox_api", lambda: fake_api)
    return fake_api


def _label_task(device, name, label, custom_fields=None):
    task = {
        "device": device,
        "name": name,
        "label": label,
        "tags": ["managed-by-osism"],
    }
    if custom_fields is not None:
        task["custom_fields"] = custom_fields
    return {"device_interface": task}


def test_emits_label_task_for_node_endpoint(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    node = make_device(name="node-0", role_slug="control")
    endpoint = make_interface(name="Ethernet5", device=node)
    source_iface = make_interface(name="Ethernet1", connected_endpoints=[endpoint])
    source = make_device(
        name="switch-0",
        role_slug="leaf",
        device_id=10,
        custom_fields={"device_interface_label": "data1"},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source, node],
        interfaces_by_device={10: [source_iface]},
    )

    tasks = main._generate_device_interface_labels()

    assert tasks == [_label_task("node-0", "Ethernet5", "data1")]


@pytest.mark.parametrize("role_slug", ["router", "firewall"])
def test_router_and_firewall_are_sources(
    roles, monkeypatch, make_netbox_api, make_device, make_interface, role_slug
):
    node = make_device(name="node-0", role_slug="control")
    endpoint = make_interface(name="Ethernet5", device=node)
    source_iface = make_interface(name="Ethernet1", connected_endpoints=[endpoint])
    source = make_device(
        name="src",
        role_slug=role_slug,
        device_id=20,
        custom_fields={"device_interface_label": "data1"},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source, node],
        interfaces_by_device={20: [source_iface]},
    )

    assert main._generate_device_interface_labels() == [
        _label_task("node-0", "Ethernet5", "data1")
    ]


def test_node_role_device_is_never_a_source(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    # A node-role device carrying a label and a connection must not act as a
    # source (only switches / routers / firewalls do).
    other_node = make_device(name="node-1", role_slug="control")
    endpoint = make_interface(name="Ethernet5", device=other_node)
    node_iface = make_interface(name="Ethernet1", connected_endpoints=[endpoint])
    node = make_device(
        name="node-0",
        role_slug="control",
        device_id=30,
        custom_fields={"device_interface_label": "data1"},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[node, other_node],
        interfaces_by_device={30: [node_iface]},
    )

    assert main._generate_device_interface_labels() == []


def test_segment_config_context_provides_label(
    roles, monkeypatch, make_netbox_api, make_device, make_interface, make_cluster
):
    # Empty custom fields but a cluster whose segment context carries the label:
    # integration through the real _resolve_device_interface_label +
    # _get_cluster_segment_config_context.
    node = make_device(name="node-0", role_slug="control")
    endpoint = make_interface(name="Ethernet5", device=node)
    source_iface = make_interface(name="Ethernet1", connected_endpoints=[endpoint])
    cluster = make_cluster(cluster_id=5, name="seg-x")
    source = make_device(
        name="switch-0", role_slug="leaf", device_id=10, cluster=cluster
    )
    ctx = SimpleNamespace(
        name="seg-x", data={"_segment_device_interface_label": "seg1"}
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source, node],
        interfaces_by_device={10: [source_iface]},
        config_contexts_by_cluster={5: [ctx]},
    )

    assert main._generate_device_interface_labels() == [
        _label_task("node-0", "Ethernet5", "seg1")
    ]


def test_source_without_label_emits_nothing_and_is_not_queried(
    roles, monkeypatch, make_netbox_api, make_device
):
    # Switch role but no label from any source: never becomes a source device,
    # so its interfaces are never fetched.
    source = make_device(name="switch-0", role_slug="leaf", device_id=10)
    fake_api = _wire(monkeypatch, make_netbox_api, devices=[source])

    assert main._generate_device_interface_labels() == []
    assert fake_api.dcim.interfaces.filter_calls == []


def test_frr_local_pref_is_added_to_task(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    node = make_device(name="node-0", role_slug="control")
    endpoint = make_interface(name="Ethernet5", device=node)
    source_iface = make_interface(name="Ethernet1", connected_endpoints=[endpoint])
    source = make_device(
        name="switch-0",
        role_slug="leaf",
        device_id=10,
        custom_fields={"device_interface_label": "data1", "frr_local_pref": 200},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source, node],
        interfaces_by_device={10: [source_iface]},
    )

    assert main._generate_device_interface_labels() == [
        _label_task("node-0", "Ethernet5", "data1", {"frr_local_pref": 200})
    ]


@pytest.mark.parametrize(
    "endpoint_kwargs, iface_kwargs, description",
    [
        (None, {"connected_endpoints": None}, "no connected endpoints"),
        ({"name": "Ethernet5", "device_role": None}, {}, "endpoint without device"),
        ({"name": "Ethernet5", "device_role": "leaf"}, {}, "non-node endpoint device"),
        ({"name": None, "device_role": "control"}, {}, "endpoint without name"),
    ],
)
def test_skip_branches_emit_no_task(
    roles,
    monkeypatch,
    make_netbox_api,
    make_device,
    make_interface,
    endpoint_kwargs,
    iface_kwargs,
    description,
):
    if endpoint_kwargs is None:
        # The interface itself has no connected endpoints.
        source_iface = make_interface(name="Ethernet1", **iface_kwargs)
    else:
        far_role = endpoint_kwargs["device_role"]
        far_device = (
            make_device(name="far-0", role_slug=far_role)
            if far_role is not None
            else None
        )
        endpoint = make_interface(name=endpoint_kwargs["name"], device=far_device)
        source_iface = make_interface(
            name="Ethernet1", connected_endpoints=[endpoint], **iface_kwargs
        )

    source = make_device(
        name="switch-0",
        role_slug="leaf",
        device_id=10,
        custom_fields={"device_interface_label": "data1"},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source],
        interfaces_by_device={10: [source_iface]},
    )

    assert main._generate_device_interface_labels() == [], description


def test_duplicate_labels_on_one_node_are_disambiguated(
    roles, monkeypatch, make_netbox_api, make_device, make_interface
):
    node = make_device(name="node-0", role_slug="control")
    endpoint_a = make_interface(name="Ethernet3", device=node)
    endpoint_b = make_interface(name="Ethernet5", device=node)
    iface_a = make_interface(name="Ethernet1", connected_endpoints=[endpoint_a])
    iface_b = make_interface(name="Ethernet2", connected_endpoints=[endpoint_b])
    source = make_device(
        name="switch-0",
        role_slug="leaf",
        device_id=10,
        custom_fields={"device_interface_label": "data1"},
    )
    _wire(
        monkeypatch,
        make_netbox_api,
        devices=[source, node],
        interfaces_by_device={10: [iface_a, iface_b]},
    )

    tasks = main._generate_device_interface_labels()

    by_name = {
        t["device_interface"]["name"]: t["device_interface"]["label"] for t in tasks
    }
    assert by_name == {"Ethernet3": "data1a", "Ethernet5": "data1b"}
