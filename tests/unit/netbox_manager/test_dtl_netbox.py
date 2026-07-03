# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``NetBox`` API wrapper (issue #257, Tier 9 of #232).

``netbox_manager.dtl.NetBox`` wraps ``pynetbox.api``: it connects, checks the
server version, indexes existing manufacturers/types and dispatches parsed
device/module types into the ``DeviceTypes`` component creators. These tests
inject the fake ``pynetbox.api`` client from the shared ``make_dtl_api`` fixture
via ``monkeypatch.setattr(dtl.pynetbox, "api", ...)`` -- no live NetBox.

``NetBox.__init__`` is eager (it connects, reads ``version`` and calls
``manufacturers.all()`` / ``device_types.all()`` immediately), so every endpoint
is configured on the fake api *before* ``NetBox`` is constructed. Where a test
only cares about routing, ``nb.device_types`` is replaced with a ``Mock`` after
construction so the real component creators (groups 4-5) are not re-run here.
``LogHandler`` prints via ``print``, so log output is asserted through
``capsys``.
"""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from netbox_manager import dtl


def make_netbox(monkeypatch, api, *, ignore_ssl=False, verbose=False):
    """Construct ``dtl.NetBox`` against the fake ``api``.

    Patches ``dtl.pynetbox.api`` to hand back ``api`` and record the
    ``(url, token)`` it was called with, feeds a ``SimpleNamespace`` settings
    stub (the shape ``NetBox.__init__`` / ``LogHandler`` read) and returns the
    constructed wrapper together with the recorded api calls.
    """
    calls = []

    def _fake_api(url, token):
        calls.append((url, token))
        return api

    monkeypatch.setattr(dtl.pynetbox, "api", _fake_api)
    settings = SimpleNamespace(
        URL="https://netbox.example.com",
        TOKEN="test-token",
        IGNORE_SSL_ERRORS=ignore_ssl,
        verbose=verbose,
    )
    netbox = dtl.NetBox(settings)
    return netbox, calls


class TestConnectApi:
    """``connect_api`` (dtl.py:174)."""

    def test_builds_api_and_leaves_verify_untouched(self, monkeypatch, make_dtl_api):
        api = make_dtl_api()

        _, calls = make_netbox(monkeypatch, api, ignore_ssl=False)

        assert calls == [("https://netbox.example.com", "test-token")]
        # No-ignore branch does not touch the session's verify flag.
        assert api.http_session.verify == "untouched"

    def test_ignore_ssl_disables_verify_and_logs(
        self, monkeypatch, make_dtl_api, capsys
    ):
        api = make_dtl_api()

        make_netbox(monkeypatch, api, ignore_ssl=True, verbose=True)

        assert api.http_session.verify is False
        assert "IGNORE_SSL_ERRORS is True" in capsys.readouterr().out


class TestVerifyCompatibility:
    """``verify_compatibility`` (dtl.py:192) -- the ``modules`` flag."""

    @pytest.mark.parametrize(
        "version, expected",
        [("3.1", False), ("3.2", True), ("4.0", True)],
    )
    def test_modules_flag_by_version(
        self, monkeypatch, make_dtl_api, version, expected
    ):
        api = make_dtl_api(version=version)

        netbox, _ = make_netbox(monkeypatch, api)

        assert netbox.modules is expected


class TestGetManufacturers:
    """``get_manufacturers`` (dtl.py:201)."""

    def test_keys_by_str(self, monkeypatch, make_dtl_api, make_dtl_record):
        arista = make_dtl_record("Arista", id=1)
        cisco = make_dtl_record("Cisco", id=2)
        api = make_dtl_api()
        api.dcim.manufacturers.all_result = [arista, cisco]

        netbox, _ = make_netbox(monkeypatch, api)

        assert netbox.existing_manufacturers == {"Arista": arista, "Cisco": cisco}


class TestCreateManufacturers:
    """``create_manufacturers`` (dtl.py:204) -- idempotent creation."""

    def test_creates_only_missing(self, monkeypatch, make_dtl_api, make_dtl_record):
        api = make_dtl_api()
        api.dcim.manufacturers.all_result = [
            make_dtl_record("Arista", name="Arista", id=1)
        ]
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_manufacturers(
            [
                {"name": "Arista", "slug": "arista"},
                {"name": "FS", "slug": "fs"},
            ]
        )

        # Only the absent vendor is queued for creation.
        assert api.dcim.manufacturers.create_calls == [[{"name": "FS", "slug": "fs"}]]
        assert netbox.counter["manufacturer"] == 1

    def test_all_existing_no_create(self, monkeypatch, make_dtl_api, make_dtl_record):
        api = make_dtl_api()
        api.dcim.manufacturers.all_result = [
            make_dtl_record("Arista", name="Arista", id=1),
            make_dtl_record("FS", name="FS", id=2),
        ]
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_manufacturers(
            [{"name": "Arista", "slug": "arista"}, {"name": "FS", "slug": "fs"}]
        )

        assert api.dcim.manufacturers.create_calls == []
        assert netbox.counter["manufacturer"] == 0

    def test_request_error_logged_not_raised(
        self, monkeypatch, make_dtl_api, make_request_error, capsys
    ):
        api = make_dtl_api()
        api.dcim.manufacturers.create_error = make_request_error("boom")
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_manufacturers([{"name": "FS", "slug": "fs"}])

        assert "Error creating manufacturers" in capsys.readouterr().out
        assert netbox.counter["manufacturer"] == 0


def _device_type(**overrides):
    """A parsed device-type dict (post ``parse_files``) for create tests."""
    device_type = {
        "model": "Node",
        "slug": "node",
        "manufacturer": {"name": "Arista", "slug": "arista"},
        "src": "/lib/devicetypes/Arista/node.yaml",
    }
    device_type.update(overrides)
    return device_type


class TestCreateDeviceTypes:
    """``create_device_types`` (dtl.py:234)."""

    def test_creates_new_and_pops_src_and_images(self, monkeypatch, make_dtl_api):
        api = make_dtl_api()
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_device_types([_device_type(front_image=False)])

        assert len(api.dcim.device_types.create_calls) == 1
        payload = api.dcim.device_types.create_calls[0]
        for stripped in ("src", "front_image", "rear_image"):
            assert stripped not in payload
        assert netbox.counter["added"] == 1

    def test_reuses_existing(self, monkeypatch, make_dtl_api, make_dtl_record):
        existing = make_dtl_record(
            "Node", model="Node", manufacturer=SimpleNamespace(name="Arista"), id=5
        )
        api = make_dtl_api()
        api.dcim.device_types.all_result = [existing]
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_device_types([_device_type()])

        assert api.dcim.device_types.create_calls == []
        assert netbox.counter["added"] == 0

    def test_request_error_skips_component_dispatch(
        self, monkeypatch, make_dtl_api, make_request_error, capsys
    ):
        api = make_dtl_api()
        api.dcim.device_types.create_error = make_request_error("nope")
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        mock_dt.existing_device_types = {}
        netbox.device_types = mock_dt

        netbox.create_device_types([_device_type(interfaces=[{"name": "Ethernet1"}])])

        # The RequestError branch logs and ``continue``s before dispatching.
        assert "creating device type" in capsys.readouterr().out
        mock_dt.create_interfaces.assert_not_called()
        assert netbox.counter["added"] == 0

    def test_dispatches_each_present_key(self, monkeypatch, make_dtl_api):
        api = make_dtl_api()  # version 4.2 -> modules True
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        mock_dt.existing_device_types = {}
        netbox.device_types = mock_dt

        device_type = _device_type(
            interfaces=["iface"],
            **{
                "power-ports": ["pp"],
                "power-port": ["pp-singular"],
                "console-ports": ["cp"],
                "power-outlets": ["po"],
                "console-server-ports": ["csp"],
                "rear-ports": ["rp"],
                "front-ports": ["fp"],
                "device-bays": ["db"],
                "module-bays": ["mb"],
            },
        )
        netbox.create_device_types([device_type])

        # device_types is a fresh endpoint, so the created record's id is 1.
        mock_dt.create_interfaces.assert_called_once_with(["iface"], 1)
        assert mock_dt.create_power_ports.call_args_list == [
            call(["pp"], 1),
            call(["pp-singular"], 1),
        ]
        mock_dt.create_console_ports.assert_called_once_with(["cp"], 1)
        mock_dt.create_power_outlets.assert_called_once_with(["po"], 1)
        mock_dt.create_console_server_ports.assert_called_once_with(["csp"], 1)
        mock_dt.create_rear_ports.assert_called_once_with(["rp"], 1)
        mock_dt.create_front_ports.assert_called_once_with(["fp"], 1)
        mock_dt.create_device_bays.assert_called_once_with(["db"], 1)
        mock_dt.create_module_bays.assert_called_once_with(["mb"], 1)
        # No images resolved -> no upload.
        mock_dt.upload_images.assert_not_called()

    def test_module_bays_gated_by_modules_flag(self, monkeypatch, make_dtl_api):
        api = make_dtl_api(version="3.1")  # modules False
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        mock_dt.existing_device_types = {}
        netbox.device_types = mock_dt

        netbox.create_device_types(
            [_device_type(interfaces=["iface"], **{"module-bays": ["mb"]})]
        )

        # Other keys still dispatch, but module-bays is gated on self.modules.
        mock_dt.create_interfaces.assert_called_once_with(["iface"], 1)
        mock_dt.create_module_bays.assert_not_called()

    def test_resolves_images_and_uploads(
        self, monkeypatch, make_dtl_api, make_dtl_tree
    ):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    "node.yaml": {
                        "manufacturer": "Arista",
                        "model": "Node",
                        "slug": "node",
                        "front_image": True,
                    }
                }
            },
            images={"Arista": ["node.front.png"]},
        )
        repo = dtl.Repo(base)
        files, _ = repo.get_devices()
        types_data = repo.parse_files(files)

        api = make_dtl_api()
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        mock_dt.existing_device_types = {}
        netbox.device_types = mock_dt

        netbox.create_device_types(types_data)

        mock_dt.upload_images.assert_called_once()
        url, token, images, device_type_id = mock_dt.upload_images.call_args.args
        assert url == "https://netbox.example.com"
        assert token == "test-token"
        assert device_type_id == 1
        assert set(images) == {"front_image"}
        assert images["front_image"].endswith("node.front.png")

    def test_missing_image_logged_and_no_upload(
        self, monkeypatch, make_dtl_api, make_dtl_tree, capsys
    ):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    "node.yaml": {
                        "manufacturer": "Arista",
                        "model": "Node",
                        "slug": "node",
                        "front_image": True,
                    }
                }
            },
        )
        repo = dtl.Repo(base)
        files, _ = repo.get_devices()
        types_data = repo.parse_files(files)

        api = make_dtl_api()
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        mock_dt.existing_device_types = {}
        netbox.device_types = mock_dt

        netbox.create_device_types(types_data)

        assert "Error locating image file" in capsys.readouterr().out
        mock_dt.upload_images.assert_not_called()
        # The image key is stripped from the create payload regardless.
        assert "front_image" not in api.dcim.device_types.create_calls[0]


def _module_type(**overrides):
    """A parsed module-type dict for create tests."""
    module_type = {
        "model": "NewModule",
        "manufacturer": {"name": "Arista", "slug": "arista"},
    }
    module_type.update(overrides)
    return module_type


class TestCreateModuleTypes:
    """``create_module_types`` (dtl.py:313)."""

    def test_creates_new_and_reuses_existing(
        self, monkeypatch, make_dtl_api, make_dtl_record
    ):
        existing = make_dtl_record(
            "ExistingModule",
            model="ExistingModule",
            manufacturer=SimpleNamespace(name="Arista", slug="arista"),
            id=7,
        )
        api = make_dtl_api()
        api.dcim.module_types.all_result = [existing]
        netbox, _ = make_netbox(monkeypatch, api)

        netbox.create_module_types(
            [
                _module_type(model="ExistingModule"),
                _module_type(model="NewModule"),
            ]
        )

        # Existing module type is reused; only the new one is created.
        assert api.dcim.module_types.create_calls == [_module_type(model="NewModule")]
        assert netbox.counter["module_added"] == 1

    def test_request_error_skips_component_dispatch(
        self, monkeypatch, make_dtl_api, make_request_error, capsys
    ):
        api = make_dtl_api()
        api.dcim.module_types.create_error = make_request_error("bad")
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        netbox.device_types = mock_dt

        netbox.create_module_types([_module_type(interfaces=[{"name": "Ethernet1"}])])

        # The RequestError branch logs and ``continue``s before dispatching.
        assert "creating module type" in capsys.readouterr().out
        mock_dt.create_module_interfaces.assert_not_called()
        assert netbox.counter["module_added"] == 0

    def test_dispatches_each_present_key(self, monkeypatch, make_dtl_api):
        api = make_dtl_api()
        netbox, _ = make_netbox(monkeypatch, api)
        mock_dt = Mock()
        netbox.device_types = mock_dt

        module_type = _module_type(
            interfaces=["iface"],
            **{
                "power-ports": ["pp"],
                "console-ports": ["cp"],
                "power-outlets": ["po"],
                "console-server-ports": ["csp"],
                "rear-ports": ["rp"],
                "front-ports": ["fp"],
            },
        )
        netbox.create_module_types([module_type])

        mock_dt.create_module_interfaces.assert_called_once_with(["iface"], 1)
        mock_dt.create_module_power_ports.assert_called_once_with(["pp"], 1)
        mock_dt.create_module_console_ports.assert_called_once_with(["cp"], 1)
        mock_dt.create_module_power_outlets.assert_called_once_with(["po"], 1)
        mock_dt.create_module_console_server_ports.assert_called_once_with(["csp"], 1)
        mock_dt.create_module_rear_ports.assert_called_once_with(["rp"], 1)
        mock_dt.create_module_front_ports.assert_called_once_with(["fp"], 1)
