# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the cluster loopback pipeline (issue #253, groups 2-5).

Covers, in order:

* ``_get_cluster_segment_config_context`` -- segment-context lookup by name
  (group 2), driven through the fake ``pynetbox.api`` passed as its
  ``netbox_api`` parameter.
* ``group_devices_by_cluster`` -- pure bucketing by ``device.cluster.id``
  (group 3).
* ``calculate_loopback_ips`` -- pure IPv4/IPv6 loopback arithmetic with exact
  address strings (group 4).
* ``_generate_cluster_loopback_tasks`` -- the end-to-end generator wired to the
  fake API via ``create_netbox_api`` (group 5).

The loopback-gate and role helpers are covered in ``test_loopback_gate.py`` and
``test_helpers.py`` (Tier 1, #247); only their effect inside the generator is
asserted here.
"""

import ipaddress
from types import SimpleNamespace

import pytest

from netbox_manager import main

IPV4_NETWORK = ipaddress.IPv4Network("10.10.128.0/24")
IPV6_NETWORK = ipaddress.IPv6Network("fd93:363d:dab8::/64")


class TestGetClusterSegmentConfigContext:
    """``_get_cluster_segment_config_context`` selects the context by name."""

    def test_name_match_returns_data_verbatim(
        self, make_netbox_api, make_config_context
    ):
        # Data carries _segment_loopback_network_ipv4, so the debug branch runs;
        # the returned object is the context's own data dict, unchanged.
        data = {
            "_segment_loopback_network_ipv4": "10.10.128.0/24",
            "_segment_loopback_network_ipv6": "fd93:363d:dab8::/64",
            "extra": 1,
        }
        ctx = make_config_context(name="seg-a", data=data)
        fake_api = make_netbox_api(config_contexts_by_cluster={7: [ctx]})

        result = main._get_cluster_segment_config_context(fake_api, 7, "seg-a")

        assert result is data

    def test_name_match_without_loopback_key_still_returns_data(
        self, make_netbox_api, make_config_context
    ):
        # The debug branch is skipped (no _segment_loopback_network_ipv4) but the
        # data dict is still returned verbatim.
        data = {"some_other_key": "value"}
        ctx = make_config_context(name="seg-a", data=data)
        fake_api = make_netbox_api(config_contexts_by_cluster={7: [ctx]})

        assert main._get_cluster_segment_config_context(fake_api, 7, "seg-a") is data

    @pytest.mark.parametrize("falsy_data", [None, {}])
    def test_name_match_with_falsy_data_returns_empty(
        self, make_netbox_api, make_config_context, falsy_data
    ):
        ctx = make_config_context(name="seg-a", data=falsy_data)
        fake_api = make_netbox_api(config_contexts_by_cluster={7: [ctx]})

        assert main._get_cluster_segment_config_context(fake_api, 7, "seg-a") == {}

    def test_no_name_match_returns_empty_and_filters_by_cluster_id(
        self, make_netbox_api, make_config_context
    ):
        # A differently-named context in first position proves selection is by
        # name, not by filter position.
        ctx = make_config_context(name="other-segment", data={"x": 1})
        fake_api = make_netbox_api(config_contexts_by_cluster={7: [ctx]})

        assert main._get_cluster_segment_config_context(fake_api, 7, "seg-a") == {}
        assert fake_api.extras.config_contexts.filter_calls == [{"clusters": 7}]

    def test_lookup_exception_returns_empty(self, make_netbox_api):
        fake_api = make_netbox_api(config_contexts_error=RuntimeError("boom"))

        assert main._get_cluster_segment_config_context(fake_api, 7, "seg-a") == {}


class TestGroupDevicesByCluster:
    """``group_devices_by_cluster`` buckets devices by ``cluster.id``."""

    def test_same_cluster_accumulates_in_order(self, make_device, make_cluster):
        # Two cluster objects sharing an id but distinct identities: the bucket
        # must keep the first-seen object.
        first = make_cluster(cluster_id=1, name="seg-a")
        second = make_cluster(cluster_id=1, name="seg-a-dup")
        d0 = make_device(name="d0", cluster=first)
        d1 = make_device(name="d1", cluster=second)

        result = main.group_devices_by_cluster([d0, d1])

        assert set(result) == {1}
        assert result[1]["devices"] == [d0, d1]
        assert result[1]["cluster"] is first

    def test_distinct_clusters_produce_distinct_buckets(
        self, make_device, make_cluster
    ):
        c1 = make_cluster(cluster_id=1)
        c2 = make_cluster(cluster_id=2)
        d0 = make_device(name="d0", cluster=c1)
        d1 = make_device(name="d1", cluster=c2)

        result = main.group_devices_by_cluster([d0, d1])

        assert set(result) == {1, 2}
        assert result[1]["devices"] == [d0]
        assert result[2]["devices"] == [d1]
        assert result[1]["cluster"] is c1
        assert result[2]["cluster"] is c2

    def test_empty_input_returns_empty_dict(self):
        assert main.group_devices_by_cluster([]) == {}


class TestCalculateLoopbackIps:
    """``calculate_loopback_ips`` maps a rack position to loopback addresses."""

    def test_position_none_returns_none_pair(self, make_device):
        device = make_device(name="d0", position=None)
        assert main.calculate_loopback_ips(device, IPV4_NETWORK, None, 0) == (
            None,
            None,
        )

    def test_string_position_is_coerced(self, make_device):
        # "10" -> int 10; byte_4 = 10 * 2 - 1 + 0 = 19.
        device = make_device(name="d0", position="10")
        assert main.calculate_loopback_ips(device, IPV4_NETWORK, None, 0) == (
            "10.10.128.19/32",
            None,
        )

    def test_non_convertible_position_returns_none_pair(self, make_device):
        device = make_device(name="d0", position="abc")
        assert main.calculate_loopback_ips(device, IPV4_NETWORK, None, 0) == (
            None,
            None,
        )

    def test_default_multiplicator_ipv4_only(self, make_device):
        # byte_4 = 3 * 2 - 1 + 0 = 5.
        device = make_device(name="d0", position=3)
        assert main.calculate_loopback_ips(device, IPV4_NETWORK, None, 0) == (
            "10.10.128.5/32",
            None,
        )

    def test_non_default_multiplicator_and_offset(self, make_device):
        # byte_4 = 3 * 4 - 1 + 10 = 21.
        device = make_device(name="d0", position=3)
        assert main.calculate_loopback_ips(
            device, IPV4_NETWORK, None, 10, multiplicator=4
        ) == ("10.10.128.21/32", None)

    def test_ipv6_suffix_mapping(self, make_device):
        # The four IPv4 octets (10.10.128.5) become the a:b:c:d suffix appended
        # to the ::-stripped network prefix.
        device = make_device(name="d0", position=3)
        assert main.calculate_loopback_ips(device, IPV4_NETWORK, IPV6_NETWORK, 0) == (
            "10.10.128.5/32",
            "fd93:363d:dab8:0:10:10:128:5/128",
        )

    def test_ipv4_arithmetic_error_returns_none_pair(self, make_device):
        # A network object without ``network_address`` makes the IPv4 arithmetic
        # raise; the outer except returns (None, None).
        device = make_device(name="d0", position=3)
        assert main.calculate_loopback_ips(device, None, IPV6_NETWORK, 0) == (
            None,
            None,
        )

    def test_ipv6_mapping_error_keeps_ipv4(self, make_device):
        # A truthy IPv6 stand-in without ``network_address`` makes only the inner
        # IPv6 mapping raise; the IPv4 result survives.
        device = make_device(name="d0", position=3)
        assert main.calculate_loopback_ips(
            device, IPV4_NETWORK, SimpleNamespace(), 0
        ) == ("10.10.128.5/32", None)


@pytest.fixture
def roles(monkeypatch):
    """Pin NODE_ROLES / SWITCH_ROLES so loopback eligibility is unambiguous."""
    monkeypatch.setattr(main.settings, "NODE_ROLES", ["control"], raising=False)
    monkeypatch.setattr(main.settings, "SWITCH_ROLES", ["leaf"], raising=False)


def _wire(monkeypatch, make_netbox_api, *, devices, config_contexts_by_cluster=None):
    """Point ``main.create_netbox_api`` at a fake API for the cluster generator."""
    fake_api = make_netbox_api(
        devices=devices, config_contexts_by_cluster=config_contexts_by_cluster
    )
    monkeypatch.setattr(main, "create_netbox_api", lambda: fake_api)
    return fake_api


def _ip_task(address, device):
    return {
        "ip_address": {
            "address": address,
            "assigned_object": {"name": "Loopback0", "device": device},
        }
    }


class TestGenerateClusterLoopbackTasks:
    """``_generate_cluster_loopback_tasks`` emits per-device loopback IPs."""

    def test_happy_path_ipv4_and_ipv6_in_device_order(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(
                name="node-3", role_slug="control", cluster=cluster, position=3
            ),
            make_device(
                name="node-4", role_slug="control", cluster=cluster, position=4
            ),
            # A cluster-less device must be filtered out before grouping.
            make_device(
                name="no-cluster", role_slug="control", cluster=None, position=9
            ),
        ]
        ctx = SimpleNamespace(
            name="seg-a",
            data={
                "_segment_loopback_network_ipv4": "10.10.128.0/24",
                "_segment_loopback_network_ipv6": "fd93:363d:dab8::/64",
            },
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        result = main._generate_cluster_loopback_tasks()

        assert result == {
            "ip_address": [
                _ip_task("10.10.128.5/32", "node-3"),
                _ip_task("fd93:363d:dab8:0:10:10:128:5/128", "node-3"),
                _ip_task("10.10.128.7/32", "node-4"),
                _ip_task("fd93:363d:dab8:0:10:10:128:7/128", "node-4"),
            ]
        }

    def test_empty_config_context_skips_cluster(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(name="node-3", role_slug="control", cluster=cluster, position=3)
        ]
        ctx = SimpleNamespace(name="seg-a", data={})
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {"ip_address": []}

    def test_context_without_ipv4_network_skips_cluster(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(name="node-3", role_slug="control", cluster=cluster, position=3)
        ]
        ctx = SimpleNamespace(
            name="seg-a", data={"_segment_loopback_network_ipv6": "fd93:363d:dab8::/64"}
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {"ip_address": []}

    def test_offset_and_multiplicator_are_honored(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(name="node-3", role_slug="control", cluster=cluster, position=3)
        ]
        ctx = SimpleNamespace(
            name="seg-a",
            data={
                "_segment_loopback_network_ipv4": "10.10.128.0/24",
                "_segment_loopback_offset_ipv4": 10,
                "_segment_loopback_network_multiplicator": 4,
            },
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        # byte_4 = 3 * 4 - 1 + 10 = 21.
        assert main._generate_cluster_loopback_tasks() == {
            "ip_address": [_ip_task("10.10.128.21/32", "node-3")]
        }

    def test_invalid_ipv4_network_skips_cluster(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(name="node-3", role_slug="control", cluster=cluster, position=3)
        ]
        ctx = SimpleNamespace(
            name="seg-a", data={"_segment_loopback_network_ipv4": "not-a-network"}
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {"ip_address": []}

    def test_invalid_ipv6_network_drops_ipv6_keeps_ipv4(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(name="node-3", role_slug="control", cluster=cluster, position=3)
        ]
        ctx = SimpleNamespace(
            name="seg-a",
            data={
                "_segment_loopback_network_ipv4": "10.10.128.0/24",
                "_segment_loopback_network_ipv6": "not-a-network",
            },
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {
            "ip_address": [_ip_task("10.10.128.5/32", "node-3")]
        }

    def test_ineligible_device_in_eligible_cluster_is_skipped(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(
                name="node-3", role_slug="control", cluster=cluster, position=3
            ),
            # Switch role without SONiC hwsku: ineligible for a Loopback0.
            make_device(name="leaf-0", role_slug="leaf", cluster=cluster, position=5),
        ]
        ctx = SimpleNamespace(
            name="seg-a", data={"_segment_loopback_network_ipv4": "10.10.128.0/24"}
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {
            "ip_address": [_ip_task("10.10.128.5/32", "node-3")]
        }

    def test_device_without_position_emits_nothing(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster
    ):
        cluster = make_cluster(cluster_id=1, name="seg-a")
        devices = [
            make_device(
                name="node-x", role_slug="control", cluster=cluster, position=None
            )
        ]
        ctx = SimpleNamespace(
            name="seg-a", data={"_segment_loopback_network_ipv4": "10.10.128.0/24"}
        )
        _wire(
            monkeypatch,
            make_netbox_api,
            devices=devices,
            config_contexts_by_cluster={1: [ctx]},
        )

        assert main._generate_cluster_loopback_tasks() == {"ip_address": []}

    def test_per_cluster_exception_is_swallowed_and_next_cluster_processed(
        self, roles, monkeypatch, make_netbox_api, make_device, make_cluster, mocker
    ):
        cluster_a = make_cluster(cluster_id=1, name="seg-a")
        cluster_b = make_cluster(cluster_id=2, name="seg-b")
        devices = [
            make_device(
                name="node-a", role_slug="control", cluster=cluster_a, position=3
            ),
            make_device(
                name="node-b", role_slug="control", cluster=cluster_b, position=3
            ),
        ]
        _wire(monkeypatch, make_netbox_api, devices=devices)
        # The real helper swallows its own errors, so patch it to raise for the
        # first cluster and return a valid context for the second.
        mocker.patch.object(
            main,
            "_get_cluster_segment_config_context",
            side_effect=[
                RuntimeError("boom"),
                {"_segment_loopback_network_ipv4": "10.10.128.0/24"},
            ],
        )

        assert main._generate_cluster_loopback_tasks() == {
            "ip_address": [_ip_task("10.10.128.5/32", "node-b")]
        }
