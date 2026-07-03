# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_write_autoconf_files`` in ``netbox_manager.main`` (#259).

Tier 3 group 6 of the coverage effort tracked in #232. ``_write_autoconf_files``
writes each non-empty ``tasks_by_type`` bucket to its own file and returns the
count of files written. These tests exercise only the file-writing behavior --
the output filename mapping (underscores in the resource type become hyphens),
the empty-bucket skip, the target-directory selection chain (explicit
``resources_dir`` -> ``settings.RESOURCES`` fallback -> bare filename in the
current directory), the ``os.makedirs(..., exist_ok=True)`` call, and the
return count -- all through pytest's ``tmp_path`` with ``settings.RESOURCES``
driven via ``monkeypatch.setattr(main.settings, "RESOURCES", ...,
raising=False)``.

The dump-format correctness of ``ProperIndentDumper`` and the ``yaml.dump(...)``
signature are Tier 2 / #249 and out of scope here: these tests may
``yaml.safe_load`` a written file to confirm the content round-trips, but do not
re-assert dumper formatting. No live NetBox, ``pynetbox`` or ``ansible_runner``
is involved, and no new shared conftest fixtures were needed for this tier.
"""

import os

import pytest
import yaml

from netbox_manager import main


@pytest.fixture(autouse=True)
def _no_settings_resources(monkeypatch):
    """Force ``settings.RESOURCES`` to ``None`` as the hermetic baseline.

    Individual tests that exercise the fallback path override this with their
    own ``tmp_path`` subdirectory; the explicit-``resources_dir`` and
    bare-filename tests rely on the ``None`` baseline.
    """
    monkeypatch.setattr(main.settings, "RESOURCES", None, raising=False)


class TestWriteAutoconfFiles:
    """Group 6 -- ``_write_autoconf_files`` path selection and file writing."""

    def test_filename_maps_underscores_to_hyphens(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        tasks_by_type = {
            "device_interface": [{"device_interface": {"device": "node-0"}}],
            "device": [{"device": {"name": "node-0"}}],
        }

        written = main._write_autoconf_files(
            tasks_by_type, "299-autoconf", resources_dir=str(out)
        )

        assert written == 2
        assert (out / "299-autoconf-device-interface.yml").is_file()
        assert (out / "299-autoconf-device.yml").is_file()

    def test_empty_bucket_skipped_and_not_counted(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        tasks_by_type = {
            "device": [],  # empty -> skipped before counting or writing
            "vlan": [{"vlan": {"name": "OOB"}}],
        }

        written = main._write_autoconf_files(
            tasks_by_type, "299-autoconf", resources_dir=str(out)
        )

        assert written == 1
        assert (out / "299-autoconf-vlan.yml").is_file()
        assert not (out / "299-autoconf-device.yml").exists()

    def test_explicit_resources_dir_wins_over_settings(self, tmp_path, monkeypatch):
        fallback = tmp_path / "fallback"
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        monkeypatch.setattr(main.settings, "RESOURCES", str(fallback), raising=False)

        written = main._write_autoconf_files(
            {"device": [{"device": {"name": "node-0"}}]},
            "299-autoconf",
            resources_dir=str(explicit),
        )

        assert written == 1
        assert (explicit / "299-autoconf-device.yml").is_file()
        # The settings fallback is never touched when resources_dir is given.
        assert not fallback.exists()

    def test_falls_back_to_settings_resources(self, tmp_path, monkeypatch):
        fallback = tmp_path / "fallback"
        monkeypatch.setattr(main.settings, "RESOURCES", str(fallback), raising=False)

        written = main._write_autoconf_files(
            {"device": [{"device": {"name": "node-0"}}]},
            "299-autoconf",
            resources_dir=None,
        )

        assert written == 1
        assert (fallback / "299-autoconf-device.yml").is_file()

    def test_bare_filename_in_cwd_when_neither_resolves(self, tmp_path, monkeypatch):
        # Neither an explicit dir nor settings.RESOURCES resolves -> the file is
        # written to the bare filename in the current working directory.
        monkeypatch.setattr(main.settings, "RESOURCES", None, raising=False)
        monkeypatch.chdir(tmp_path)

        written = main._write_autoconf_files(
            {"device": [{"device": {"name": "node-0"}}]},
            "299-autoconf",
            resources_dir=None,
        )

        assert written == 1
        assert (tmp_path / "299-autoconf-device.yml").is_file()

    def test_creates_missing_target_dir(self, tmp_path):
        # A nested, not-yet-existing target directory is created before writing.
        target = tmp_path / "not" / "yet"
        assert not target.exists()

        written = main._write_autoconf_files(
            {"device": [{"device": {"name": "node-0"}}]},
            "299-autoconf",
            resources_dir=str(target),
        )

        assert written == 1
        assert os.path.isdir(target)
        assert (target / "299-autoconf-device.yml").is_file()

    def test_return_count_and_content_round_trips(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        tasks = [
            {"device_interface": {"device": "node-0", "name": "Ethernet0"}},
            {"device_interface": {"device": "node-1", "name": "Ethernet0"}},
        ]

        written = main._write_autoconf_files(
            {"device_interface": tasks}, "299-autoconf", resources_dir=str(out)
        )

        assert written == 1
        path = out / "299-autoconf-device-interface.yml"
        with open(path) as f:
            assert yaml.safe_load(f) == tasks
