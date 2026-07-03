# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``DeviceTypes`` (issue #257, Tier 9 of #232).

``netbox_manager.dtl.DeviceTypes`` owns the lookup helpers (group 3) and the
~16 component creators (groups 4-5). The creators all follow one shared
contract -- filter existing -> dedup -> batch ``*_templates.create`` ->
``log_*_ports_created`` -> ``counter["updated"]`` bump -- so groups 4-5 are
exercised with parametrized tables over that contract (``DEVICE_CREATORS`` /
``MODULE_CREATORS``) plus targeted tests for the four creators that diverge
(the ``power_port`` / ``rear_port`` id-resolution steps) and the error branch.

``DeviceTypes`` is constructed directly against the fake ``pynetbox.api`` from
``make_dtl_api`` (its ``__init__`` only calls ``dcim.device_types.all()``); the
``LogHandler`` is the call-through ``dtl_log_handler`` spy so the real
``len(created_ports)`` still drives the counter while the label and port list
stay assertable. ``upload_images`` patches ``dtl.requests.patch`` so no live
HTTP occurs.
"""

from collections import Counter
from types import SimpleNamespace

import pytest

from netbox_manager import dtl


@pytest.fixture
def make_device_types(make_dtl_api, dtl_log_handler):
    """Build a ``DeviceTypes`` bound to a fake api and the LogHandler spy.

    Returns ``(device_types, api)``; the spy is ``device_types.handle`` and the
    counter is ``device_types.counter``. Pass an ``api`` to reuse one already
    configured, or let the fixture mint a fresh one.
    """

    def _make(api=None, *, ignore_ssl=False):
        api = make_dtl_api() if api is None else api
        return dtl.DeviceTypes(api, dtl_log_handler, Counter(), ignore_ssl), api

    return _make


class TestLookupHelpers:
    """Group 3 -- ``get_*`` lookup helpers (dtl.py:386-437)."""

    def test_get_device_types_keys_by_str(self, make_device_types, make_dtl_record):
        device_types, api = make_device_types()
        model_a = make_dtl_record("ModelA", id=1)
        model_b = make_dtl_record("ModelB", id=2)
        api.dcim.device_types.all_result = [model_a, model_b]

        assert device_types.get_device_types() == {
            "ModelA": model_a,
            "ModelB": model_b,
        }

    @pytest.mark.parametrize(
        "helper, attr, kwarg",
        [
            ("get_power_ports", "power_port_templates", "device_type_id"),
            ("get_rear_ports", "rear_port_templates", "device_type_id"),
            ("get_module_power_ports", "power_port_templates", "module_type_id"),
            ("get_module_rear_ports", "rear_port_templates", "module_type_id"),
        ],
    )
    def test_port_lookup_helper_filters_and_keys_by_str(
        self, make_device_types, make_dtl_record, helper, attr, kwarg
    ):
        device_types, api = make_device_types()
        record = make_dtl_record("PX", id=9)
        getattr(api.dcim, attr).filter_result = [record]

        result = getattr(device_types, helper)(7)

        assert result == {"PX": record}
        # The device- vs module-side helpers key the filter on the right kwarg.
        assert getattr(api.dcim, attr).filter_calls == [{kwarg: 7}]

    def test_get_device_type_ports_to_create_dedups_and_stamps(self, make_device_types):
        device_types, _ = make_device_types()
        ports = [{"name": "A"}, {"name": "B"}]

        to_create = device_types.get_device_type_ports_to_create(
            ports, 42, {"A": object()}
        )

        assert [port["name"] for port in to_create] == ["B"]
        assert to_create[0]["device_type"] == 42

    def test_get_module_type_ports_to_create_dedups_and_stamps(self, make_device_types):
        device_types, _ = make_device_types()
        ports = [{"name": "A"}, {"name": "B"}]

        to_create = device_types.get_module_type_ports_to_create(
            ports, 77, {"A": object()}
        )

        assert [port["name"] for port in to_create] == ["B"]
        assert to_create[0]["module_type"] == 77

    @pytest.mark.parametrize(
        "helper", ["get_device_type_ports_to_create", "get_module_type_ports_to_create"]
    )
    def test_ports_to_create_empty_when_nothing_new(self, make_device_types, helper):
        device_types, _ = make_device_types()

        # Empty input and all-existing input both yield no work.
        assert getattr(device_types, helper)([], 1, {}) == []
        assert getattr(device_types, helper)([{"name": "A"}], 1, {"A": object()}) == []


# (method, templates endpoint, log port_type label, extra port fields).
# Front ports carry ``rear_port`` + ``type`` because a missing ``type`` would
# KeyError inside the missing-rear-port log line; keep the column.
DEVICE_CREATORS = [
    ("create_interfaces", "interface_templates", "Interface", {}),
    ("create_power_ports", "power_port_templates", "Power Port", {}),
    ("create_console_ports", "console_port_templates", "Console Port", {}),
    ("create_power_outlets", "power_outlet_templates", "Power Outlet", {}),
    (
        "create_console_server_ports",
        "console_server_port_templates",
        "Console Server Port",
        {},
    ),
    ("create_rear_ports", "rear_port_templates", "Rear Port", {}),
    (
        "create_front_ports",
        "front_port_templates",
        "Front Port",
        {"rear_port": "Rear1", "type": "8p8c"},
    ),
    ("create_device_bays", "device_bay_templates", "Device Bay", {}),
    ("create_module_bays", "module_bay_templates", "Module Bay", {}),
]

MODULE_CREATORS = [
    ("create_module_interfaces", "interface_templates", "Module Interface", {}),
    ("create_module_power_ports", "power_port_templates", "Module Power Port", {}),
    (
        "create_module_console_ports",
        "console_port_templates",
        "Module Console Port",
        {},
    ),
    (
        "create_module_power_outlets",
        "power_outlet_templates",
        "Module Power Outlet",
        {},
    ),
    (
        "create_module_console_server_ports",
        "console_server_port_templates",
        "Module Console Server Port",
        {},
    ),
    ("create_module_rear_ports", "rear_port_templates", "Module Rear Port", {}),
    (
        "create_module_front_ports",
        "front_port_templates",
        "Module Front Port",
        {"rear_port": "Rear1", "type": "8p8c"},
    ),
]

DEVICE_IDS = [row[0] for row in DEVICE_CREATORS]
MODULE_IDS = [row[0] for row in MODULE_CREATORS]


class TestDeviceComponentCreators:
    """Group 4 -- device-component creators (dtl.py:439-664)."""

    @pytest.mark.parametrize(
        "method, attr, label, extra", DEVICE_CREATORS, ids=DEVICE_IDS
    )
    def test_skips_existing_and_creates_new(
        self, make_device_types, make_dtl_record, method, attr, label, extra
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)
        # One existing port (P0) dedups away; one new port (P1) survives.
        endpoint.filter_result = [make_dtl_record("P0")]
        if "rear_port" in extra:
            api.dcim.rear_port_templates.filter_result = [
                make_dtl_record(extra["rear_port"], id=1)
            ]
        ports = [dict(name="P0", **extra), dict(name="P1", **extra)]

        getattr(device_types, method)(ports, 42)

        # Only the new port reaches create, stamped with the device type.
        created = endpoint.create_calls[0]
        assert [port["name"] for port in created] == ["P1"]
        assert all(port["device_type"] == 42 for port in created)
        assert endpoint.filter_calls == [{"device_type_id": 42}]
        # The device-side log helper is used, with the matching label.
        device_types.handle.log_device_ports_created.assert_called_once()
        assert device_types.handle.log_device_ports_created.call_args.args[1] == label
        assert device_types.counter["updated"] == 1

    @pytest.mark.parametrize(
        "method, attr, label, extra", DEVICE_CREATORS, ids=DEVICE_IDS
    )
    def test_no_create_when_nothing_new(
        self, make_device_types, make_dtl_record, method, attr, label, extra
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)

        # Empty input -> the ``if to_create:`` guard skips create entirely.
        getattr(device_types, method)([], 42)
        # All-existing input -> dedup empties the batch, same guard.
        endpoint.filter_result = [make_dtl_record("P0")]
        getattr(device_types, method)([dict(name="P0", **extra)], 42)

        assert endpoint.create_calls == []
        assert device_types.counter["updated"] == 0
        device_types.handle.log_device_ports_created.assert_not_called()

    @pytest.mark.parametrize(
        "method, attr, label, extra", DEVICE_CREATORS, ids=DEVICE_IDS
    )
    def test_request_error_logged(
        self,
        make_device_types,
        make_dtl_record,
        make_request_error,
        method,
        attr,
        label,
        extra,
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)
        endpoint.create_error = make_request_error("boom")
        if "rear_port" in extra:
            api.dcim.rear_port_templates.filter_result = [
                make_dtl_record(extra["rear_port"], id=1)
            ]

        getattr(device_types, method)([dict(name="P1", **extra)], 42)

        assert f"creating {label}" in device_types.handle.log.call_args.args[0]
        assert device_types.counter["updated"] == 0

    def test_create_power_outlets_resolves_power_port_id(
        self, make_device_types, make_dtl_record
    ):
        device_types, api = make_device_types()
        api.dcim.power_port_templates.filter_result = [make_dtl_record("PP1", id=99)]

        device_types.create_power_outlets(
            [
                {"name": "Outlet1", "power_port": "PP1"},
                {"name": "Outlet2", "power_port": "Unknown"},
            ],
            42,
        )

        created = {
            p["name"]: p for p in api.dcim.power_outlet_templates.create_calls[0]
        }
        assert created["Outlet1"]["power_port"] == 99  # resolved to the port id
        assert created["Outlet2"]["power_port"] == "Unknown"  # KeyError -> left as-is

    def test_create_front_ports_resolves_rear_port_or_logs(
        self, make_device_types, make_dtl_record
    ):
        device_types, api = make_device_types()
        api.dcim.rear_port_templates.filter_result = [make_dtl_record("Rear1", id=5)]

        device_types.create_front_ports(
            [
                {"name": "Front1", "rear_port": "Rear1", "type": "8p8c"},
                {"name": "Front2", "rear_port": "Missing", "type": "8p8c"},
            ],
            42,
        )

        created = {p["name"]: p for p in api.dcim.front_port_templates.create_calls[0]}
        assert created["Front1"]["rear_port"] == 5  # resolved to the rear port id
        assert created["Front2"]["rear_port"] == "Missing"  # missing -> logged, kept
        assert "Could not find Rear Port" in device_types.handle.log.call_args.args[0]


class TestModuleComponentCreators:
    """Group 5 -- module-component creators (dtl.py:666-845)."""

    @pytest.mark.parametrize(
        "method, attr, label, extra", MODULE_CREATORS, ids=MODULE_IDS
    )
    def test_skips_existing_and_creates_new(
        self, make_device_types, make_dtl_record, method, attr, label, extra
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)
        endpoint.filter_result = [make_dtl_record("P0")]
        if "rear_port" in extra:
            api.dcim.rear_port_templates.filter_result = [
                make_dtl_record(extra["rear_port"], id=1)
            ]
        ports = [dict(name="P0", **extra), dict(name="P1", **extra)]

        getattr(device_types, method)(ports, 77)

        created = endpoint.create_calls[0]
        assert [port["name"] for port in created] == ["P1"]
        assert all(port["module_type"] == 77 for port in created)
        # Existing ports are filtered by module_type_id, not device_type_id.
        assert endpoint.filter_calls == [{"module_type_id": 77}]
        device_types.handle.log_module_ports_created.assert_called_once()
        assert device_types.handle.log_module_ports_created.call_args.args[1] == label
        assert device_types.counter["updated"] == 1

    @pytest.mark.parametrize(
        "method, attr, label, extra", MODULE_CREATORS, ids=MODULE_IDS
    )
    def test_no_create_when_nothing_new(
        self, make_device_types, make_dtl_record, method, attr, label, extra
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)

        getattr(device_types, method)([], 77)
        endpoint.filter_result = [make_dtl_record("P0")]
        getattr(device_types, method)([dict(name="P0", **extra)], 77)

        assert endpoint.create_calls == []
        assert device_types.counter["updated"] == 0
        device_types.handle.log_module_ports_created.assert_not_called()

    @pytest.mark.parametrize(
        "method, attr, label, extra", MODULE_CREATORS, ids=MODULE_IDS
    )
    def test_request_error_logged(
        self,
        make_device_types,
        make_dtl_record,
        make_request_error,
        method,
        attr,
        label,
        extra,
    ):
        device_types, api = make_device_types()
        endpoint = getattr(api.dcim, attr)
        endpoint.create_error = make_request_error("boom")
        if "rear_port" in extra:
            api.dcim.rear_port_templates.filter_result = [
                make_dtl_record(extra["rear_port"], id=1)
            ]

        getattr(device_types, method)([dict(name="P1", **extra)], 77)

        assert f"creating {label}" in device_types.handle.log.call_args.args[0]
        assert device_types.counter["updated"] == 0

    def test_create_module_power_outlets_resolves_power_port_id(
        self, make_device_types, make_dtl_record
    ):
        device_types, api = make_device_types()
        api.dcim.power_port_templates.filter_result = [make_dtl_record("PP1", id=99)]

        device_types.create_module_power_outlets(
            [
                {"name": "Outlet1", "power_port": "PP1"},
                {"name": "Outlet2", "power_port": "Unknown"},
            ],
            77,
        )

        created = {
            p["name"]: p for p in api.dcim.power_outlet_templates.create_calls[0]
        }
        assert created["Outlet1"]["power_port"] == 99
        assert created["Outlet2"]["power_port"] == "Unknown"

    def test_create_module_front_ports_resolves_rear_port_or_logs(
        self, make_device_types, make_dtl_record
    ):
        device_types, api = make_device_types()
        api.dcim.rear_port_templates.filter_result = [make_dtl_record("Rear1", id=5)]

        device_types.create_module_front_ports(
            [
                {"name": "Front1", "rear_port": "Rear1", "type": "8p8c"},
                {"name": "Front2", "rear_port": "Missing", "type": "8p8c"},
            ],
            77,
        )

        created = {p["name"]: p for p in api.dcim.front_port_templates.create_calls[0]}
        assert created["Front1"]["rear_port"] == 5
        assert created["Front2"]["rear_port"] == "Missing"
        assert "Could not find Rear Port" in device_types.handle.log.call_args.args[0]


class TestUploadImages:
    """Group 5 -- ``upload_images`` (dtl.py:847)."""

    @pytest.mark.parametrize("ignore_ssl", [False, True])
    def test_patches_once_with_headers_files_and_verify(
        self, make_device_types, monkeypatch, tmp_path, ignore_ssl
    ):
        device_types, _ = make_device_types(ignore_ssl=ignore_ssl)
        front = tmp_path / "front.png"
        front.write_bytes(b"\x89PNG")
        rear = tmp_path / "rear.png"
        rear.write_bytes(b"\x89PNG")

        calls = []

        def fake_patch(url, headers=None, files=None, verify=None):
            # Close the file objects upload_images opened (it never does), so
            # no descriptor leaks into the rest of the suite.
            names = {}
            for key, (basename, fileobj) in files.items():
                names[key] = basename
                fileobj.close()
            calls.append(
                {"url": url, "headers": headers, "files": names, "verify": verify}
            )
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(dtl.requests, "patch", fake_patch)

        device_types.upload_images(
            "https://netbox.example.com",
            "test-token",
            {"front_image": str(front), "rear_image": str(rear)},
            42,
        )

        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://netbox.example.com/api/dcim/device-types/42/"
        assert call["headers"] == {"Authorization": "Token test-token"}
        assert call["files"] == {"front_image": "front.png", "rear_image": "rear.png"}
        assert call["verify"] is (not ignore_ssl)
        assert device_types.counter["images"] == 2
