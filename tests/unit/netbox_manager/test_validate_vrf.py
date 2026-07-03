# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``validate_vrf_consistency`` (issue #255, Tier 7 of #232).

``netbox_manager.main.validate_vrf_consistency`` is a read-only validator: it
pulls the IPs carrying a VRF via ``ipam.ip_addresses.filter(vrf_id__n="null")``
and, for each device-interface IP, re-fetches the interface through
``dcim.interfaces.get(...)`` to compare the IP's VRF against the interface's VRF.
It returns ``(validation_passed, inconsistencies)`` and never writes to NetBox.

The helper takes the ``pynetbox.api`` client as a parameter, so these tests hand
it a flat :class:`types.SimpleNamespace` bag exposing only the two endpoints it
touches, with :class:`unittest.mock.Mock` methods for call-recording and
raising. IP / VRF shapes come from the shared ``make_ip_address`` / ``make_vrf``
factories; the re-fetched interface bag is built by the local ``_interface``
helper. Assertions are on the returned ``(bool, list)`` tuple and the finding
dicts -- never on log output.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pynetbox
import pytest

from netbox_manager import main


def _api(*, ip_filter, iface_get):
    """Build a fake ``pynetbox.api`` for the VRF-consistency validator.

    ``ip_filter`` is the ``Mock`` backing ``ipam.ip_addresses.filter`` and
    ``iface_get`` the ``Mock`` backing ``dcim.interfaces.get`` -- each test
    configures the return value / side effect and asserts on the call record.
    """
    return SimpleNamespace(
        ipam=SimpleNamespace(ip_addresses=SimpleNamespace(filter=ip_filter)),
        dcim=SimpleNamespace(interfaces=SimpleNamespace(get=iface_get)),
    )


def _interface(*, vrf=None, device_name="node-0", name="Ethernet0"):
    """A re-fetched interface bag.

    Exposes ``vrf`` (a ``make_vrf`` bag or ``None``), ``device`` (read via
    ``device.name``) and ``name`` -- the attributes the validator reads off the
    interface returned by ``dcim.interfaces.get``.
    """
    return SimpleNamespace(vrf=vrf, device=SimpleNamespace(name=device_name), name=name)


def _device_assignment(*, id=11):
    """A device-interface ``assigned_object`` bag.

    Carries a truthy ``device`` (so the ``hasattr``/truthiness gate passes) plus
    the ``id`` fed to ``dcim.interfaces.get(assigned_obj.id)``.
    """
    return SimpleNamespace(device=SimpleNamespace(name="node-0"), id=id)


class TestValidateVrfConsistency:
    """Branch coverage for ``validate_vrf_consistency`` (main.py:3010)."""

    def test_matching_vrf_passes_and_pins_query_shape(self, make_ip_address, make_vrf):
        ip = make_ip_address(
            vrf=make_vrf(id=1), assigned_object=_device_assignment(id=11)
        )
        ip_filter = Mock(return_value=[ip])
        iface_get = Mock(return_value=_interface(vrf=make_vrf(id=1)))
        api = _api(ip_filter=ip_filter, iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is True
        assert issues == []
        ip_filter.assert_called_once_with(vrf_id__n="null")
        iface_get.assert_called_once_with(11)

    def test_ip_without_assigned_object_is_skipped(self, make_ip_address, make_vrf):
        ip = make_ip_address(vrf=make_vrf(id=1), assigned_object=None)
        iface_get = Mock()
        api = _api(ip_filter=Mock(return_value=[ip]), iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is True
        assert issues == []
        iface_get.assert_not_called()

    def test_vm_interface_assignment_is_skipped(self, make_ip_address, make_vrf):
        # Both sides of the `not hasattr(...) or not assigned_obj.device` guard:
        # an assigned object with no `device` attribute at all, and one whose
        # `device` is present but falsy.
        no_device_attr = SimpleNamespace(name="veth0", id=21)
        device_none = SimpleNamespace(device=None, name="veth1", id=22)
        ips = [
            make_ip_address(vrf=make_vrf(id=1), assigned_object=no_device_attr),
            make_ip_address(vrf=make_vrf(id=2), assigned_object=device_none),
        ]
        iface_get = Mock()
        api = _api(ip_filter=Mock(return_value=ips), iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is True
        assert issues == []
        iface_get.assert_not_called()

    def test_vrf_mismatch_is_recorded(self, make_ip_address, make_vrf):
        ip = make_ip_address(
            address="192.168.16.10/32",
            vrf=make_vrf(id=1, name="red"),
            assigned_object=_device_assignment(id=11),
        )
        iface_get = Mock(
            return_value=_interface(
                vrf=make_vrf(id=2, name="blue"),
                device_name="node-0",
                name="Ethernet0",
            )
        )
        api = _api(ip_filter=Mock(return_value=[ip]), iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is False
        assert issues == [
            {
                "ip_address": "192.168.16.10/32",
                "ip_vrf": "red",
                "device": "node-0",
                "interface": "Ethernet0",
                "interface_vrf": "blue",
                "reason": "VRF mismatch: IP in 'red', interface in 'blue'",
            }
        ]

    def test_mismatch_with_interface_vrf_none(self, make_ip_address, make_vrf):
        # One side of the comparison is None: IP in a VRF, interface in none.
        ip = make_ip_address(
            vrf=make_vrf(id=1, name="red"),
            assigned_object=_device_assignment(id=11),
        )
        iface_get = Mock(return_value=_interface(vrf=None))
        api = _api(ip_filter=Mock(return_value=[ip]), iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is False
        finding = issues[0]
        assert finding["interface_vrf"] == "None"
        assert finding["reason"].endswith("interface in 'None'")

    def test_interface_lookup_error_is_swallowed(self, make_ip_address, make_vrf):
        ip = make_ip_address(
            vrf=make_vrf(id=1), assigned_object=_device_assignment(id=11)
        )
        iface_get = Mock(side_effect=Exception("boom"))
        api = _api(ip_filter=Mock(return_value=[ip]), iface_get=iface_get)

        # The defensive `except Exception` continues without re-raising.
        passed, issues = main.validate_vrf_consistency(api)

        assert passed is True
        assert issues == []

    def test_falsy_interface_is_skipped(self, make_ip_address, make_vrf):
        ip = make_ip_address(
            vrf=make_vrf(id=1), assigned_object=_device_assignment(id=11)
        )
        iface_get = Mock(return_value=None)
        api = _api(ip_filter=Mock(return_value=[ip]), iface_get=iface_get)

        passed, issues = main.validate_vrf_consistency(api)

        assert passed is True
        assert issues == []

    def test_request_error_from_filter_propagates(self, make_request_error):
        ip_filter = Mock(side_effect=make_request_error())
        api = _api(ip_filter=ip_filter, iface_get=Mock())

        with pytest.raises(pynetbox.RequestError):
            main.validate_vrf_consistency(api)
