# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``process_device_and_module_types`` (issue #256, Tier 8 of #232).

``process_device_and_module_types`` (``netbox_manager.main``) is the dispatch
seam between the CLI and the Device Type Library importer: it resolves a library
path off settings, constructs ``Repo`` / ``NetBox``, then routes parsed data to
either ``create_device_types`` or ``create_module_types``. These tests cover its
branches -- the skip / missing-library early return, the devicetypes-vs-
moduletypes selection and the ``FileNotFoundError`` logging -- with the real dtl
classes replaced at their ``main`` import sites (``main.Repo`` / ``main.NetBox``);
the classes themselves are exercised in Tier 9 (#257), not here.

``process_device_and_module_types`` does not call ``init_logger()``, so the
conftest loguru->``caplog`` bridge survives and the error-log assertion uses
``caplog``. ``NetBox(settings)`` is checked by identity against the module-level
``main.settings`` object.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from netbox_manager import main


def install_dtl_mocks(
    monkeypatch,
    *,
    files=None,
    vendors=None,
    types_data=None,
    get_devices_error=None,
    parse_files_error=None,
):
    """Install ``Mock`` ``Repo`` / ``NetBox`` classes on ``main`` and return them.

    The fake ``Repo`` instance's ``get_devices()`` returns ``(files, vendors)``
    and ``parse_files(files)`` returns ``types_data`` (or raises the given error);
    the fake ``NetBox`` instance carries ``create_*`` spies. Returns a namespace
    exposing ``repo_cls`` / ``repo`` / ``netbox_cls`` / ``netbox`` for assertions.
    """
    repo = Mock()
    if get_devices_error is not None:
        repo.get_devices.side_effect = get_devices_error
    else:
        repo.get_devices.return_value = (
            files if files is not None else ["file.yml"],
            vendors if vendors is not None else ["Vendor"],
        )
    if parse_files_error is not None:
        repo.parse_files.side_effect = parse_files_error
    else:
        repo.parse_files.return_value = (
            types_data if types_data is not None else [{"model": "X"}]
        )
    repo_cls = Mock(return_value=repo)

    netbox = Mock()
    netbox_cls = Mock(return_value=netbox)

    monkeypatch.setattr(main, "Repo", repo_cls)
    monkeypatch.setattr(main, "NetBox", netbox_cls)
    return SimpleNamespace(
        repo_cls=repo_cls, repo=repo, netbox_cls=netbox_cls, netbox=netbox
    )


class TestEarlyReturn:
    """Group 1 -- skip / missing-library early return (main.py:650-652)."""

    def test_missing_library_returns_early(self, monkeypatch):
        dtl = install_dtl_mocks(monkeypatch)
        monkeypatch.setattr(main.settings, "DEVICETYPE_LIBRARY", None, raising=False)

        result = main.process_device_and_module_types(
            "DEVICETYPE_LIBRARY", False, "devicetypes"
        )

        assert result is None
        dtl.repo_cls.assert_not_called()
        dtl.netbox_cls.assert_not_called()

    def test_skip_flag_returns_early(self, monkeypatch):
        dtl = install_dtl_mocks(monkeypatch)
        # Library path present, but the skip flag short-circuits before any work.
        monkeypatch.setattr(
            main.settings, "DEVICETYPE_LIBRARY", "/lib/devicetypes", raising=False
        )

        result = main.process_device_and_module_types(
            "DEVICETYPE_LIBRARY", True, "devicetypes"
        )

        assert result is None
        dtl.repo_cls.assert_not_called()
        dtl.netbox_cls.assert_not_called()


class TestBranchSelection:
    """Group 2 -- devicetypes-vs-moduletypes dispatch (main.py:655-667)."""

    def test_devicetypes_branch(self, monkeypatch):
        dtl = install_dtl_mocks(
            monkeypatch, vendors=["Arista"], types_data=[{"model": "Node"}]
        )
        monkeypatch.setattr(
            main.settings, "DEVICETYPE_LIBRARY", "/lib/devicetypes", raising=False
        )

        main.process_device_and_module_types("DEVICETYPE_LIBRARY", False, "devicetypes")

        dtl.repo_cls.assert_called_once_with("/lib/devicetypes")
        assert dtl.netbox_cls.call_count == 1
        assert dtl.netbox_cls.call_args.args[0] is main.settings
        dtl.netbox.create_manufacturers.assert_called_once_with(["Arista"])
        dtl.netbox.create_device_types.assert_called_once_with([{"model": "Node"}])
        dtl.netbox.create_module_types.assert_not_called()

    def test_moduletypes_branch(self, monkeypatch):
        dtl = install_dtl_mocks(
            monkeypatch, vendors=["Foo"], types_data=[{"model": "Mod"}]
        )
        monkeypatch.setattr(
            main.settings, "MODULETYPE_LIBRARY", "/lib/moduletypes", raising=False
        )

        main.process_device_and_module_types("MODULETYPE_LIBRARY", False, "moduletypes")

        dtl.repo_cls.assert_called_once_with("/lib/moduletypes")
        dtl.netbox.create_manufacturers.assert_called_once_with(["Foo"])
        dtl.netbox.create_module_types.assert_called_once_with([{"model": "Mod"}])
        dtl.netbox.create_device_types.assert_not_called()


class TestFileNotFound:
    """Group 3 -- FileNotFoundError logged, not raised (main.py:669-670)."""

    @pytest.mark.parametrize("failing", ["get_devices", "parse_files"])
    def test_file_not_found_logged_not_raised(self, monkeypatch, caplog, failing):
        dtl = install_dtl_mocks(
            monkeypatch, **{f"{failing}_error": FileNotFoundError("missing")}
        )
        monkeypatch.setattr(
            main.settings, "DEVICETYPE_LIBRARY", "/lib/devicetypes", raising=False
        )

        # A FileNotFoundError from either repo call is caught, not propagated.
        result = main.process_device_and_module_types(
            "DEVICETYPE_LIBRARY", False, "devicetypes"
        )

        assert result is None
        assert "Could not load devicetypes in /lib/devicetypes" in caplog.text
        # Both repo calls precede create_manufacturers, so it is never reached.
        dtl.netbox.create_manufacturers.assert_not_called()
