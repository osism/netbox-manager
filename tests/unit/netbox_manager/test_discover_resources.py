# SPDX-License-Identifier: Apache-2.0

"""Unit tests for resource / site-folder discovery in ``netbox_manager.main``.

Tier 3 groups 3-5 of the coverage effort tracked in #232 (issue #259):
``discover_resource_files`` (top-level + numbered-subdirectory collection with
``--limit`` and ``vars``-directory exclusion), ``detect_site_folders`` (the
``full_path -> folder_name`` map of numbered site subdirectories) and
``extract_device_names_from_folder`` (the set of ``- device: {name: X}`` names
across a folder's YAML files). Everything is exercised through pytest's
``tmp_path`` -- no live NetBox, no ``pynetbox`` mocks, no ``ansible_runner`` --
with ``settings.VARS`` driven via ``monkeypatch.setattr(main.settings, "VARS",
..., raising=False)`` for the ``vars``-exclusion cases.

Assertions on cross-directory ordering are confined to single-numbered-dir
layouts, because ``os.listdir`` order across sibling directories is
filesystem-dependent; elsewhere the tests assert on set / membership. Log
assertions use the shared loguru->``caplog`` bridge from ``conftest.py``. No
new shared conftest fixtures were needed for this tier.
"""

import logging

import pytest

from netbox_manager import main


@pytest.fixture(autouse=True)
def _no_global_vars(monkeypatch):
    """Force ``settings.VARS`` to ``None`` as the hermetic baseline.

    ``discover_resource_files`` reads ``settings.VARS`` to skip a same-named
    ``vars`` subdirectory; an exported ``NETBOX_MANAGER_VARS`` would otherwise
    leak in. The vars-exclusion tests override this with their own value.
    """
    monkeypatch.setattr(main.settings, "VARS", None, raising=False)


class TestDiscoverResourceFiles:
    """Group 3 -- ``discover_resource_files`` collection / limit / exclusion."""

    def test_top_level_files_both_extensions(self, tmp_path):
        (tmp_path / "100-a.yml").write_text("[]\n")
        (tmp_path / "200-b.yaml").write_text("[]\n")

        result = main.discover_resource_files(str(tmp_path))

        # One file per extension keeps the two-pass glob order deterministic.
        assert set(result) == {
            str(tmp_path / "100-a.yml"),
            str(tmp_path / "200-b.yaml"),
        }

    def test_numbered_subdirs_descended_files_basename_sorted(self, tmp_path):
        site = tmp_path / "200-site"
        site.mkdir()
        (site / "z.yml").write_text("[]\n")
        (site / "a.yaml").write_text("[]\n")
        # A non-numbered dir and a bare-number dir (no dash) are not descended.
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "ignored.yml").write_text("[]\n")
        bare = tmp_path / "300"
        bare.mkdir()
        (bare / "ignored.yml").write_text("[]\n")

        result = main.discover_resource_files(str(tmp_path))

        assert result == [str(site / "a.yaml"), str(site / "z.yml")]

    def test_vars_dir_excluded_by_basename(self, tmp_path, monkeypatch):
        vars_dir = tmp_path / "100-vars"
        vars_dir.mkdir()
        (vars_dir / "globals.yml").write_text("[]\n")
        site = tmp_path / "200-site"
        site.mkdir()
        (site / "devices.yml").write_text("[]\n")
        monkeypatch.setattr(main.settings, "VARS", str(vars_dir), raising=False)

        result = main.discover_resource_files(str(tmp_path))

        assert result == [str(site / "devices.yml")]

    def test_vars_exclusion_noop_when_unset(self, tmp_path, monkeypatch):
        vars_dir = tmp_path / "100-vars"
        vars_dir.mkdir()
        (vars_dir / "globals.yml").write_text("[]\n")
        monkeypatch.setattr(main.settings, "VARS", None, raising=False)

        result = main.discover_resource_files(str(tmp_path))

        # With VARS unset the numbered dir is treated like any other.
        assert result == [str(vars_dir / "globals.yml")]

    def test_limit_filters_top_level_files_by_basename(self, tmp_path):
        (tmp_path / "100-a.yml").write_text("[]\n")
        (tmp_path / "300-b.yml").write_text("[]\n")

        result = main.discover_resource_files(str(tmp_path), limit="300")

        assert result == [str(tmp_path / "300-b.yml")]

    def test_limit_filters_numbered_directories(self, tmp_path):
        kept = tmp_path / "300-y"
        kept.mkdir()
        (kept / "devices.yml").write_text("[]\n")
        skipped = tmp_path / "100-x"
        skipped.mkdir()
        (skipped / "devices.yml").write_text("[]\n")

        result = main.discover_resource_files(str(tmp_path), limit="300")

        assert result == [str(kept / "devices.yml")]

    def test_missing_resources_dir_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        # glob returns [] for a missing dir and os.listdir's FileNotFoundError is
        # swallowed, so the function returns [] rather than raising.
        assert main.discover_resource_files(str(missing)) == []

    def test_aggregate_order_top_level_then_dir_files(self, tmp_path):
        (tmp_path / "500-top.yml").write_text("[]\n")
        site = tmp_path / "200-site"
        site.mkdir()
        (site / "b.yml").write_text("[]\n")
        (site / "a.yaml").write_text("[]\n")

        result = main.discover_resource_files(str(tmp_path))

        # Top-level files are appended first (even a numerically larger prefix),
        # then each numbered dir's basename-sorted files. Only one numbered dir
        # here, since os.listdir order across dirs is not guaranteed.
        assert result == [
            str(tmp_path / "500-top.yml"),
            str(site / "a.yaml"),
            str(site / "b.yml"),
        ]


class TestDetectSiteFolders:
    """Group 4 -- ``detect_site_folders`` builds a name-sorted path->name map."""

    def test_maps_full_path_to_name_in_sorted_order(self, tmp_path):
        for name in ("300-b", "200-a", "1000-xx"):
            (tmp_path / name).mkdir()

        result = main.detect_site_folders(str(tmp_path))

        assert result == {
            str(tmp_path / "1000-xx"): "1000-xx",
            str(tmp_path / "200-a"): "200-a",
            str(tmp_path / "300-b"): "300-b",
        }
        # Insertion order follows lexicographic (string) name sort, not numeric:
        # "1000-xx" < "200-a" < "300-b".
        assert list(result.values()) == ["1000-xx", "200-a", "300-b"]

    def test_excludes_files_and_non_matching_names(self, tmp_path):
        (tmp_path / "100-file.yml").write_text("[]\n")  # a file, not a dir
        (tmp_path / "notes").mkdir()  # no number prefix
        (tmp_path / "300").mkdir()  # bare number, no dash
        (tmp_path / "12-").mkdir()  # dash but empty suffix
        (tmp_path / "200-aa").mkdir()  # the one valid site folder

        result = main.detect_site_folders(str(tmp_path))

        assert result == {str(tmp_path / "200-aa"): "200-aa"}

    def test_missing_or_file_path_returns_empty(self, tmp_path):
        assert main.detect_site_folders(str(tmp_path / "does-not-exist")) == {}

        a_file = tmp_path / "resources.yml"
        a_file.write_text("[]\n")
        assert main.detect_site_folders(str(a_file)) == {}


class TestExtractDeviceNamesFromFolder:
    """Group 5 -- ``extract_device_names_from_folder`` collects device names."""

    def test_collects_device_names_across_files_as_set(self, tmp_path):
        (tmp_path / "10-a.yml").write_text(
            "- device:\n    name: node-0\n- vlan:\n    name: OOB\n"
        )
        (tmp_path / "20-b.yml").write_text("- device:\n    name: node-1\n")

        result = main.extract_device_names_from_folder(str(tmp_path))

        assert isinstance(result, set)
        assert result == {"node-0", "node-1"}

    def test_ignores_non_matching_shapes(self, tmp_path):
        # Root is a mapping, not a list -> skipped entirely.
        (tmp_path / "10-mapping.yml").write_text("device:\n  name: ignored\n")
        # List of strings -> non-dict items are skipped.
        (tmp_path / "20-strings.yml").write_text("- just-a-string\n")
        # dict items whose device is not a dict / has no truthy name.
        (tmp_path / "30-shapes.yml").write_text(
            "- device: node-str\n"  # device value is a string
            "- device:\n    label: x\n"  # dict without name
            '- device:\n    name: ""\n'  # falsy name
        )

        assert main.extract_device_names_from_folder(str(tmp_path)) == set()

    def test_deduplicates_same_name_across_files(self, tmp_path):
        (tmp_path / "10-a.yml").write_text("- device:\n    name: node-0\n")
        (tmp_path / "20-b.yml").write_text("- device:\n    name: node-0\n")

        assert main.extract_device_names_from_folder(str(tmp_path)) == {"node-0"}

    def test_malformed_file_debug_logged_and_others_processed(self, tmp_path, caplog):
        # Sorted before the good file, so the error is hit first.
        (tmp_path / "10-broken.yml").write_text("key: [unclosed\n")
        (tmp_path / "20-good.yml").write_text("- device:\n    name: node-0\n")

        with caplog.at_level(logging.DEBUG):
            result = main.extract_device_names_from_folder(str(tmp_path))

        assert result == {"node-0"}
        assert "Error reading" in caplog.text
