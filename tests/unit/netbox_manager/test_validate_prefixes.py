# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``validate_ip_addresses_have_prefixes`` (issue #255, Tier 7).

``netbox_manager.main.validate_ip_addresses_have_prefixes`` is a read-only
validator: it pulls every IP via ``ipam.ip_addresses.all()``, parses each
address with ``ipaddress.ip_network(..., strict=False)`` and looks for a
containing prefix in the same VRF through ``ipam.prefixes.filter(...)``. It
returns ``(validation_passed, orphaned_ips)`` and never writes back to NetBox.

The helper takes the ``pynetbox.api`` client as a parameter, so these tests hand
it a flat :class:`types.SimpleNamespace` bag exposing only the two endpoints it
touches, with :class:`unittest.mock.Mock` methods for call-recording and
raising. IP / VRF shapes come from the shared ``make_ip_address`` / ``make_vrf``
factories and the nested device bag from ``make_device`` (conftest). Assertions
are on the returned ``(bool, list)`` tuple and the finding dicts -- never on log
output.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pynetbox
import pytest

from netbox_manager import main


def _api(*, all_ips, prefix_filter):
    """Build a fake ``pynetbox.api`` for the IP-prefix validator.

    ``all_ips`` is the ``Mock`` backing ``ipam.ip_addresses.all`` and
    ``prefix_filter`` the ``Mock`` backing ``ipam.prefixes.filter`` -- each test
    configures the return value / side effect and asserts on the call record.
    """
    return SimpleNamespace(
        ipam=SimpleNamespace(
            ip_addresses=SimpleNamespace(all=all_ips),
            prefixes=SimpleNamespace(filter=prefix_filter),
        )
    )


def _assigned(*, make_device, device_name="node-0", interface_name="Ethernet0"):
    """A device-interface ``assigned_object`` bag with truthy ``device``.

    Exposes ``device`` (a ``make_device`` bag, read via ``device.name``) and
    ``name`` (the interface name) -- the two attributes the validator extracts.
    """
    return SimpleNamespace(device=make_device(name=device_name), name=interface_name)


class TestValidateIpAddressesHavePrefixes:
    """Branch coverage for ``validate_ip_addresses_have_prefixes`` (main.py:2899)."""

    def test_valid_ip_with_matching_prefix_passes(self, make_ip_address):
        ip = make_ip_address(address="192.168.16.10/32", vrf=None)
        prefix_filter = Mock(return_value=[object()])
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is True
        assert orphans == []
        prefix_filter.assert_called_once()

    def test_invalid_ip_format_is_orphaned_and_skips_prefix_query(
        self, make_ip_address, make_device
    ):
        ip = make_ip_address(
            address="not-an-ip",
            vrf=None,
            assigned_object=_assigned(make_device=make_device),
        )
        prefix_filter = Mock()
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is False
        assert len(orphans) == 1
        orphan = orphans[0]
        assert orphan["reason"].startswith("Invalid IP address format:")
        assert orphan["device"] == "node-0"
        assert orphan["interface"] == "Ethernet0"
        # The parse failure short-circuits before any prefix lookup.
        prefix_filter.assert_not_called()

    def test_no_matching_prefix_is_orphaned(self, make_ip_address):
        ip = make_ip_address(address="192.168.16.10/32", vrf=None, assigned_object=None)
        prefix_filter = Mock(return_value=[])
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is False
        assert len(orphans) == 1
        orphan = orphans[0]
        assert set(orphan.keys()) == {
            "address",
            "vrf",
            "device",
            "interface",
            "assigned_object",
            "reason",
        }
        assert orphan["reason"] == "No matching prefix found in same VRF"
        assert orphan["address"] == "192.168.16.10/32"
        assert orphan["device"] is None
        assert orphan["interface"] is None
        assert orphan["assigned_object"] == "Unassigned"

    def test_vrf_scoped_query_uses_vrf_id_and_vrf_name(self, make_ip_address, make_vrf):
        ip = make_ip_address(address="192.168.16.10/32", vrf=make_vrf(id=7, name="red"))
        prefix_filter = Mock(return_value=[])
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is False
        prefix_filter.assert_called_once_with(contains="192.168.16.10", vrf_id=7)
        assert orphans[0]["vrf"] == "red"

    def test_global_query_uses_null_vrf(self, make_ip_address):
        # strict=False keeps a host-bearing address parseable; the query keys on
        # the derived network_address (10.0.0.5/24 -> 10.0.0.0).
        ip = make_ip_address(address="10.0.0.5/24", vrf=None)
        prefix_filter = Mock(return_value=[])
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is False
        prefix_filter.assert_called_once_with(contains="10.0.0.0", vrf_id="null")
        assert orphans[0]["vrf"] == "Global"

    def test_assigned_object_populates_device_and_interface(
        self, make_ip_address, make_device
    ):
        ip = make_ip_address(
            address="192.168.16.10/32",
            vrf=None,
            assigned_object=_assigned(make_device=make_device),
        )
        prefix_filter = Mock(return_value=[])
        api = _api(all_ips=Mock(return_value=[ip]), prefix_filter=prefix_filter)

        passed, orphans = main.validate_ip_addresses_have_prefixes(api)

        assert passed is False
        orphan = orphans[0]
        assert orphan["device"] == "node-0"
        assert orphan["interface"] == "Ethernet0"
        assert orphan["assigned_object"] != "Unassigned"

    def test_request_error_from_all_propagates(self, make_request_error):
        api = _api(
            all_ips=Mock(side_effect=make_request_error("boom")),
            prefix_filter=Mock(),
        )

        with pytest.raises(pynetbox.RequestError):
            main.validate_ip_addresses_have_prefixes(api)
