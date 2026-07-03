# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``export_archive`` (issue #256, Tier 8 of #232).

``export_archive`` (``netbox_manager.main``) bundles the configured
devicetype / moduletype / resource directories into ``netbox-export.tar.gz``,
optionally embedding a ``COMMIT_INFO.txt`` from ``git.Repo(".")`` and,
on Linux with ``--image``, wrapping the tarball in an ext4 image via
``mkfs.ext4`` / ``e2cp``. These tests cover its branches -- the empty-directory
guard, the git-repo-vs-not split, the tar member set, the ``--image`` OS gate,
the image happy path and the outer failure wrapper -- against the shared
``mock_git`` / ``mock_subprocess`` / ``mock_platform`` fixtures and **real**
tarballs written under ``tmp_path``.

Two gotchas shape these tests:

* The command is Typer-decorated but directly callable; its parameter defaults
  are ``typer.OptionInfo`` objects (truthy), so every call passes ``image`` /
  ``image_size`` explicitly -- a bare ``export_archive()`` would take the
  ``--image`` branch spuriously.
* ``export_archive`` calls ``init_logger()``, whose ``logger.remove()`` drops
  every loguru sink (including the conftest ``caplog`` bridge). Assertions here
  are therefore behavioral -- exit codes, files on disk, recorded argv -- never
  on log text.

Every test ``monkeypatch.chdir(tmp_path)`` because the command writes
``netbox-export.tar.gz`` / ``netbox-export.img`` into the current directory.
"""

import os
import tarfile
from types import SimpleNamespace

import git
import pytest
import typer

from netbox_manager import main

# settings attribute -> directory basename the export walks.
SETTINGS_DIRS = {
    "DEVICETYPE_LIBRARY": "devicetypes",
    "MODULETYPE_LIBRARY": "moduletypes",
    "RESOURCES": "resources",
}


@pytest.fixture(autouse=True)
def _reset_export_dirs(monkeypatch):
    """Force the three library/resource settings to ``None`` as the baseline.

    A developer environment exporting ``NETBOX_MANAGER_DEVICETYPE_LIBRARY`` (etc.)
    would otherwise leak real paths into ``os.path.exists`` gates. Tests that
    need a directory set it explicitly via :func:`make_library_dirs`.
    """
    for attr in SETTINGS_DIRS:
        monkeypatch.setattr(main.settings, attr, None, raising=False)


def make_library_dirs(tmp_path, monkeypatch, present):
    """Create the requested library dirs under ``tmp_path`` and point settings.

    ``present`` is an iterable of settings attribute names (a subset of
    :data:`SETTINGS_DIRS`). Each named directory is created with one marker YAML
    file (so ``tar.add`` produces a nested member) and the matching settings
    attribute is pointed at it. Attributes not in ``present`` keep the ``None``
    baseline from the autouse fixture. Returns the mapping of attribute name to
    the created :class:`pathlib.Path`.
    """
    created = {}
    for attr in present:
        directory = tmp_path / SETTINGS_DIRS[attr]
        directory.mkdir()
        (directory / "model.yml").write_text("marker\n")
        monkeypatch.setattr(main.settings, attr, str(directory), raising=False)
        created[attr] = directory
    return created


@pytest.fixture
def record_tempfile(monkeypatch):
    """Wrap ``main.tempfile.NamedTemporaryFile`` and record the names it hands out.

    ``export_archive`` writes the commit-info text to a
    ``NamedTemporaryFile(delete=False)`` and removes it after the tarball is
    built. This wrapper delegates to the real ``tempfile`` while capturing each
    created path, so the cleanup assertion can check the file is gone.
    """
    import tempfile as real_tempfile

    names = []

    def _named_temp(*args, **kwargs):
        handle = real_tempfile.NamedTemporaryFile(*args, **kwargs)
        names.append(handle.name)
        return handle

    monkeypatch.setattr(
        main, "tempfile", SimpleNamespace(NamedTemporaryFile=_named_temp)
    )
    return SimpleNamespace(names=names)


class TestNoDirectories:
    """Group 1 -- empty-``directories`` guard (main.py:1066-1068)."""

    @pytest.mark.parametrize("mode", ["all_none", "nonexistent_paths"])
    def test_no_directories_exits_before_tar_work(
        self, tmp_path, monkeypatch, mock_git, mode
    ):
        monkeypatch.chdir(tmp_path)
        if mode == "nonexistent_paths":
            # Configured, but nothing exists on disk -> os.path.exists gates fail.
            for attr in SETTINGS_DIRS:
                monkeypatch.setattr(
                    main.settings, attr, str(tmp_path / f"absent-{attr}"), raising=False
                )

        with pytest.raises(typer.Exit) as exc_info:
            main.export_archive(image=False, image_size=100)

        assert exc_info.value.exit_code == 1
        # Exit precedes every tar / git operation.
        assert not (tmp_path / "netbox-export.tar.gz").exists()
        assert mock_git.repo_calls == []


class TestGitCommitInfo:
    """Group 2 -- COMMIT_INFO.txt from git (main.py:1076-1111)."""

    def test_git_happy_path_adds_and_cleans_commit_info(
        self, tmp_path, monkeypatch, mock_git, record_tempfile
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})

        main.export_archive(image=False, image_size=100)

        assert mock_git.repo_calls == ["."]
        tar_path = tmp_path / "netbox-export.tar.gz"
        with tarfile.open(tar_path, "r:gz") as tar:
            assert "COMMIT_INFO.txt" in tar.getnames()
            content = tar.extractfile("COMMIT_INFO.txt").read().decode()
        # Hash, formatted date and branch from the fake repo land in the file.
        assert "deadbeef" * 5 in content
        assert "2024-03-14 15:09:26" in content
        assert "test-branch" in content
        # The temp commit-info file is removed after the tarball is built.
        assert record_tempfile.names
        for name in record_tempfile.names:
            assert not os.path.exists(name)

    def test_not_a_git_repo_proceeds_without_commit_info(
        self, tmp_path, monkeypatch, mock_git
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        mock_git.repo_error = git.exc.InvalidGitRepositoryError("not a repo")

        main.export_archive(image=False, image_size=100)

        tar_path = tmp_path / "netbox-export.tar.gz"
        assert tar_path.exists()
        with tarfile.open(tar_path, "r:gz") as tar:
            names = tar.getnames()
        assert "COMMIT_INFO.txt" not in names
        assert "devicetypes" in {name.split("/")[0] for name in names}

    def test_generic_git_error_proceeds_without_commit_info(
        self, tmp_path, monkeypatch, mock_git
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        mock_git.repo_error = Exception("boom")

        main.export_archive(image=False, image_size=100)

        with tarfile.open(tmp_path / "netbox-export.tar.gz", "r:gz") as tar:
            assert "COMMIT_INFO.txt" not in tar.getnames()


class TestTarContents:
    """Group 3 -- per-directory tar members (main.py:1105-1107)."""

    def test_tar_contains_each_existing_directory(
        self, tmp_path, monkeypatch, mock_git
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY", "RESOURCES"})
        # Configured but absent on disk -> skipped by the os.path.exists gate.
        monkeypatch.setattr(
            main.settings,
            "MODULETYPE_LIBRARY",
            str(tmp_path / "absent-mt"),
            raising=False,
        )

        main.export_archive(image=False, image_size=100)

        with tarfile.open(tmp_path / "netbox-export.tar.gz", "r:gz") as tar:
            names = tar.getnames()
        # One top-level entry per existing directory, plus the commit-info file.
        assert {name.split("/")[0] for name in names} == {
            "COMMIT_INFO.txt",
            "devicetypes",
            "resources",
        }
        # arcname=os.path.basename(directory) is applied recursively.
        assert "devicetypes/model.yml" in names


class TestImageOption:
    """Group 4 -- the ``--image`` OS gate and happy path (main.py:1115-1139)."""

    def test_image_on_non_linux_exits_without_subprocess(
        self, tmp_path, monkeypatch, mock_git, mock_platform, mock_subprocess
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        mock_platform.system_name = "Darwin"

        with pytest.raises(typer.Exit) as exc_info:
            main.export_archive(image=True, image_size=1)

        assert exc_info.value.exit_code == 1
        # No mkfs.ext4 / e2cp on a non-Linux host.
        assert mock_subprocess.check_call_calls == []

    def test_image_happy_path_on_linux(
        self, tmp_path, monkeypatch, mock_git, mock_platform, mock_subprocess
    ):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        mock_platform.system_name = "Linux"

        main.export_archive(image=True, image_size=1)

        image = tmp_path / "netbox-export.img"
        assert image.exists()
        assert image.stat().st_size == 1 * 1024 * 1024
        assert mock_subprocess.check_call_calls == [
            ["mkfs.ext4", "-F", "netbox-export.img"],
            ["e2cp", "netbox-export.tar.gz", "netbox-export.img:/netbox-export.tar.gz"],
        ]
        # The tarball is removed once copied into the image.
        assert not (tmp_path / "netbox-export.tar.gz").exists()


class TestFailureWrapper:
    """Group 5 -- outer failure wrapper and missing-``git`` degradation."""

    def test_failure_in_tar_creation_exits(self, tmp_path, monkeypatch, mock_git):
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        # Skip commit-info so no temp file leaks; then fail inside tarfile.open.
        mock_git.repo_error = git.exc.InvalidGitRepositoryError("no repo")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(main, "tarfile", SimpleNamespace(open=_boom))

        with pytest.raises(typer.Exit) as exc_info:
            main.export_archive(image=False, image_size=100)

        assert exc_info.value.exit_code == 1

    def test_git_module_none_exits_cleanly(self, tmp_path, monkeypatch):
        """An absent optional ``git`` degrades to a clean exit, not AttributeError."""
        monkeypatch.chdir(tmp_path)
        make_library_dirs(tmp_path, monkeypatch, {"DEVICETYPE_LIBRARY"})
        monkeypatch.setattr(main, "git", None)

        # If an AttributeError escaped the helper, pytest.raises(typer.Exit)
        # would not match and the test would fail.
        with pytest.raises(typer.Exit) as exc_info:
            main.export_archive(image=False, image_size=100)

        assert exc_info.value.exit_code == 1
