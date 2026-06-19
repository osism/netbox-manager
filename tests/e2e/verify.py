#!/usr/bin/env python3
"""Assert the live NetBox state after ``netbox-manager run`` (phase 3a).

This standalone script connects to the NetBox instance provisioned by
``deploy_netbox.sh`` using the same credentials as ``netbox-manager``
(``NETBOX_MANAGER_URL`` / ``NETBOX_MANAGER_TOKEN``) and checks that the
objects, counts, and relations pinned in :mod:`expected` were created
from the bundled ``example/`` data.

It is intentionally not a ``pytest`` test so that ``pytest tests/unit``
(the fast CI gate) never collects it. It exits ``0`` when every
assertion passes and ``1`` otherwise.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List, Tuple

import pynetbox
import urllib3

import expected

# The deploy is ephemeral and uses a self-signed certificate behind the
# port-forward; mirror ``NETBOX_MANAGER_IGNORE_SSL_ERRORS=true``.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Accumulated results: (name, passed, detail).
Result = Tuple[str, bool, str]


def _record(results: List[Result], name: str, passed: bool, detail: str = "") -> None:
    """Append a single assertion outcome to ``results``."""
    results.append((name, passed, detail))


def _choice_value(value: Any) -> Any:
    """Return the raw value of a pynetbox choice field.

    pynetbox renders choice fields (e.g. interface ``mode``) as a record
    exposing ``value``; fall back to the value itself for plain strings
    or ``None``.
    """
    return getattr(value, "value", value)


def build_api() -> pynetbox.api:
    """Create a pynetbox API client from the environment.

    Raises:
        SystemExit: if ``NETBOX_MANAGER_URL`` or ``NETBOX_MANAGER_TOKEN``
            is missing.
    """
    url = os.environ.get("NETBOX_MANAGER_URL")
    token = os.environ.get("NETBOX_MANAGER_TOKEN")
    if not url or not token:
        sys.exit(
            "NETBOX_MANAGER_URL and NETBOX_MANAGER_TOKEN must be set "
            "to run the E2E verification."
        )
    api = pynetbox.api(url, token=token)
    api.http_session.verify = False
    return api


def _endpoint(api: pynetbox.api, path: str) -> Any:
    """Resolve a dotted ``app.endpoint`` path to a pynetbox endpoint."""
    app_name, endpoint_name = path.split(".", 1)
    return getattr(getattr(api, app_name), endpoint_name)


def check_counts(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the exact object count for every pinned endpoint."""
    for path, want in expected.EXPECTED_COUNTS.items():
        got = _endpoint(api, path).count()
        _record(results, f"count {path}", got == want, f"expected {want}, got {got}")


def check_devices(api: pynetbox.api, results: List[Result]) -> None:
    """Assert each expected device exists by name."""
    for name in expected.DEVICE_NAMES:
        device = api.dcim.devices.get(name=name)
        _record(results, f"device {name}", device is not None, "not found")


def check_tenant_site_location(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the tenant, site, and location exist with their slugs."""
    tenant = api.tenancy.tenants.get(slug=expected.TENANT_SLUG)
    _record(results, f"tenant {expected.TENANT_SLUG}", tenant is not None, "not found")

    site = api.dcim.sites.get(slug=expected.SITE_SLUG)
    _record(results, f"site {expected.SITE_SLUG}", site is not None, "not found")

    location = api.dcim.locations.get(slug=expected.LOCATION_SLUG)
    _record(
        results,
        f"location {expected.LOCATION_SLUG}",
        location is not None,
        "not found",
    )


def check_vlan(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the OOB VLAN exists with the right vid, name, and role."""
    vlan = api.ipam.vlans.get(vid=expected.VLAN_VID)
    if vlan is None:
        _record(results, f"vlan vid {expected.VLAN_VID}", False, "not found")
        return
    _record(results, "vlan name", vlan.name == expected.VLAN_NAME, str(vlan.name))
    role = getattr(vlan.role, "name", None)
    _record(results, "vlan role", role == expected.VLAN_ROLE, str(role))


def check_prefixes(api: pynetbox.api, results: List[Result]) -> None:
    """Assert all four prefixes exist."""
    for prefix in expected.PREFIXES:
        obj = api.ipam.prefixes.get(prefix=prefix)
        _record(results, f"prefix {prefix}", obj is not None, "not found")


def check_interface(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the access-port interface and its untagged VLAN."""
    iface = api.dcim.interfaces.get(
        device=expected.IFACE_DEVICE, name=expected.IFACE_NAME
    )
    label = f"{expected.IFACE_DEVICE}:{expected.IFACE_NAME}"
    if iface is None:
        _record(results, f"interface {label}", False, "not found")
        return
    mode = _choice_value(iface.mode)
    _record(results, f"interface {label} mode", mode == expected.IFACE_MODE, str(mode))
    untagged = getattr(iface.untagged_vlan, "name", None)
    _record(
        results,
        f"interface {label} untagged vlan",
        untagged == expected.IFACE_UNTAGGED_VLAN,
        str(untagged),
    )


def check_ip_address(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the loopback IP is assigned to the right interface."""
    ip = api.ipam.ip_addresses.get(address=expected.IP_ADDRESS)
    if ip is None:
        _record(results, f"ip {expected.IP_ADDRESS}", False, "not found")
        return
    assigned = ip.assigned_object
    iface_name = getattr(assigned, "name", None)
    device_name = getattr(getattr(assigned, "device", None), "name", None)
    detail = f"{device_name}:{iface_name}"
    passed = iface_name == expected.IP_INTERFACE and device_name == expected.IP_DEVICE
    _record(results, f"ip {expected.IP_ADDRESS} assignment", passed, detail)


def check_mac_address(api: pynetbox.api, results: List[Result]) -> None:
    """Assert the MAC address is assigned to the right interface."""
    mac = api.dcim.mac_addresses.get(mac_address=expected.MAC_ADDRESS)
    if mac is None:
        _record(results, f"mac {expected.MAC_ADDRESS}", False, "not found")
        return
    assigned = mac.assigned_object
    iface_name = getattr(assigned, "name", None)
    device_name = getattr(getattr(assigned, "device", None), "name", None)
    detail = f"{device_name}:{iface_name}"
    passed = iface_name == expected.MAC_INTERFACE and device_name == expected.MAC_DEVICE
    _record(results, f"mac {expected.MAC_ADDRESS} assignment", passed, detail)


def check_primary_ips(api: pynetbox.api, results: List[Result]) -> None:
    """Assert primary IPv4/IPv6 and OOB IP wiring on the spot-check device."""
    device = api.dcim.devices.get(name=expected.PRIMARY_DEVICE)
    if device is None:
        _record(results, f"device {expected.PRIMARY_DEVICE}", False, "not found")
        return
    for field, want in (
        ("primary_ip4", expected.PRIMARY_IP4),
        ("primary_ip6", expected.PRIMARY_IP6),
        ("oob_ip", expected.OOB_IP),
    ):
        got = getattr(getattr(device, field, None), "address", None)
        _record(
            results,
            f"{expected.PRIMARY_DEVICE}.{field}",
            got == want,
            f"expected {want}, got {got}",
        )


def main() -> int:
    """Run every check and return a process exit code."""
    api = build_api()
    results: List[Result] = []

    try:
        check_counts(api, results)
        check_devices(api, results)
        check_tenant_site_location(api, results)
        check_vlan(api, results)
        check_prefixes(api, results)
        check_interface(api, results)
        check_ip_address(api, results)
        check_mac_address(api, results)
        check_primary_ips(api, results)
    except pynetbox.RequestError as exc:
        print(f"NetBox API error: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        suffix = f" ({detail})" if not passed and detail else ""
        print(f"[{status}] {name}{suffix}")
        if not passed:
            failed += 1

    total = len(results)
    print(f"\n{total - failed}/{total} checks passed.")
    if failed:
        print(f"{failed} check(s) FAILED.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
