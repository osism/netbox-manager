# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the filesystem helpers in ``netbox_manager.main`` (issue #259).

Tier 3 groups 1-2 of the coverage effort tracked in #232:
``find_yaml_files`` (the ``*.yml`` / ``*.yaml`` glob-and-sort helper) and
``load_global_vars`` (which reads and ``deep_merge``s every YAML file in
``settings.VARS``). Both are exercised entirely through pytest's ``tmp_path`` --
no live NetBox, no ``pynetbox`` mocks, no ``ansible_runner`` -- with the
dynaconf settings stub driven via ``monkeypatch.setattr(main.settings, "VARS",
..., raising=False)`` (``raising=False`` because dynaconf resolves keys through
``__getattr__`` rather than real attributes).

The ``FileNotFoundError`` and generic-``Exception`` branches of
``load_global_vars`` are reached without monkeypatching ``os`` / ``glob`` /
``open`` (per the issue's constraint) via POSIX-specific on-disk layouts: a
dangling symlink whose ``open`` raises ``FileNotFoundError``, and a directory
named ``*.yml`` whose ``open`` raises ``IsADirectoryError`` (a sibling of
``FileNotFoundError`` under ``OSError``, so it falls through to the generic
handler). Both CI (Ubuntu) and local dev (macOS) are POSIX, so this is safe.

Log assertions use the shared loguru->``caplog`` bridge from ``conftest.py``
(whose docstring names #259 as a consumer). Where a PyYAML error message is
asserted, only stable substrings are pinned -- the exact ``problem`` /
``context`` wording varies across PyYAML versions. No new shared conftest
fixtures were needed for this tier; the two-line settings override lives
module-local.
"""

import logging
import os

import pytest

from netbox_manager import main


@pytest.fixture(autouse=True)
def _no_global_vars(monkeypatch):
    """Force ``settings.VARS`` to ``None`` as the hermetic baseline.

    A developer environment exporting ``NETBOX_MANAGER_VARS`` would otherwise
    make ``load_global_vars`` read files from a real directory. Each
    ``load_global_vars`` test that needs a ``VARS`` directory overrides this with
    its own ``tmp_path`` subdirectory.
    """
    monkeypatch.setattr(main.settings, "VARS", None, raising=False)


class TestFindYamlFiles:
    """Group 1 -- ``find_yaml_files`` globs both extensions and sorts."""

    def test_globs_both_extensions_filename_sorted(self, tmp_path):
        # Two glob passes (*.yml then *.yaml) are concatenated and re-sorted, so
        # the result is filename-sorted regardless of extension: a.yaml < b.yml.
        (tmp_path / "b.yml").write_text("b: 1\n")
        (tmp_path / "a.yaml").write_text("a: 1\n")
        (tmp_path / "c.yaml").write_text("c: 1\n")

        result = main.find_yaml_files(str(tmp_path))

        assert result == [
            str(tmp_path / "a.yaml"),
            str(tmp_path / "b.yml"),
            str(tmp_path / "c.yaml"),
        ]

    def test_empty_or_yaml_free_directory_returns_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert main.find_yaml_files(str(empty)) == []

        no_yaml = tmp_path / "no_yaml"
        no_yaml.mkdir()
        (no_yaml / "notes.txt").write_text("nothing to see\n")
        assert main.find_yaml_files(str(no_yaml)) == []

    def test_non_yaml_and_dot_files_excluded(self, tmp_path):
        (tmp_path / "a.yml").write_text("a: 1\n")
        (tmp_path / "b.txt").write_text("text\n")
        (tmp_path / "c.json").write_text("{}\n")
        # glob's *.yml never matches a dot-prefixed entry.
        (tmp_path / ".hidden.yml").write_text("hidden: 1\n")

        assert main.find_yaml_files(str(tmp_path)) == [str(tmp_path / "a.yml")]


class TestLoadGlobalVars:
    """Group 2 -- ``load_global_vars`` merge and error-branch behavior."""

    def _vars_dir(self, tmp_path, monkeypatch):
        """Create a ``vars`` dir under ``tmp_path`` and point ``settings.VARS``."""
        vars_dir = tmp_path / "vars"
        vars_dir.mkdir()
        monkeypatch.setattr(main.settings, "VARS", str(vars_dir), raising=False)
        return vars_dir

    def test_vars_unset_returns_empty(self, monkeypatch):
        # The autouse fixture already set VARS to None; be explicit anyway.
        monkeypatch.setattr(main.settings, "VARS", None, raising=False)
        assert main.load_global_vars() == {}

    def test_vars_dir_missing_returns_empty_and_debug_logs(
        self, tmp_path, monkeypatch, caplog
    ):
        missing = tmp_path / "novars"  # never created on disk
        monkeypatch.setattr(main.settings, "VARS", str(missing), raising=False)

        with caplog.at_level(logging.DEBUG):
            assert main.load_global_vars() == {}

        assert "does not exist, skipping global vars" in caplog.text

    def test_multi_file_deep_merge_in_sorted_order(self, tmp_path, monkeypatch):
        vars_dir = self._vars_dir(tmp_path, monkeypatch)
        (vars_dir / "10-base.yml").write_text(
            "a: 1\nnested:\n  keep: true\n  winner: first\n"
        )
        (vars_dir / "20-override.yml").write_text(
            "a: 2\nnested:\n  winner: second\n  extra: 1\n"
        )

        # Files merge in sorted-filename order (later wins). The nested merge is
        # deep: keep survives from the first file, winner is overwritten by the
        # second. Flipping the order would change the result, which pins it.
        assert main.load_global_vars() == {
            "a": 2,
            "nested": {"keep": True, "winner": "second", "extra": 1},
        }

    def test_empty_file_skipped(self, tmp_path, monkeypatch):
        vars_dir = self._vars_dir(tmp_path, monkeypatch)
        # safe_load of a comments-only file returns None -> falsy -> skipped.
        (vars_dir / "10-empty.yml").write_text("# only a comment\n")
        (vars_dir / "20-real.yml").write_text("a: 1\n")

        assert main.load_global_vars() == {"a": 1}

    def test_malformed_yaml_logged_not_raised(self, tmp_path, monkeypatch, caplog):
        vars_dir = self._vars_dir(tmp_path, monkeypatch)
        # Unclosed flow sequence -> MarkedYAMLError, exercising the problem_mark /
        # problem / context enrichment branches.
        (vars_dir / "10-broken.yml").write_text("key: [unclosed\n")
        (vars_dir / "20-good.yml").write_text("a: 1\n")

        with caplog.at_level(logging.ERROR):
            result = main.load_global_vars()

        # The good file still processes despite the earlier malformed one.
        assert result == {"a": 1}
        assert "Invalid YAML syntax in vars file" in caplog.text
        assert "at line" in caplog.text

    def test_missing_file_logged_not_raised(self, tmp_path, monkeypatch, caplog):
        vars_dir = self._vars_dir(tmp_path, monkeypatch)
        # A dangling symlink: glob lists the entry, but open() raises
        # FileNotFoundError on the missing target.
        os.symlink(str(vars_dir / "does-not-exist"), str(vars_dir / "10-dangling.yml"))
        (vars_dir / "20-good.yml").write_text("a: 1\n")

        with caplog.at_level(logging.ERROR):
            result = main.load_global_vars()

        assert result == {"a": 1}
        assert "Vars file not found" in caplog.text

    def test_generic_error_logged_and_remaining_files_processed(
        self, tmp_path, monkeypatch, caplog
    ):
        vars_dir = self._vars_dir(tmp_path, monkeypatch)
        # A directory named *.yml: glob lists it, open() raises IsADirectoryError,
        # which is not FileNotFoundError, so the generic Exception branch fires.
        (vars_dir / "10-dir.yml").mkdir()
        (vars_dir / "20-good.yml").write_text("a: 1\n")

        with caplog.at_level(logging.ERROR):
            result = main.load_global_vars()

        assert result == {"a": 1}
        assert "Error loading vars from" in caplog.text
