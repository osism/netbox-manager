# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``import_archive`` (issue #256, Tier 8 of #232).

``import_archive`` (``netbox_manager.main``) extracts a ``netbox-export.tar.gz``
into a temporary directory and rsyncs each extracted **directory** into the
destination, skipping any top-level regular files. These tests cover its
branches -- the missing-input guard, the per-directory rsync fan-out (argv,
kwargs and call count), the non-directory skip, the rsync-failure branch, the
outer failure wrapper and the path-traversal guard on extraction -- using
**real** tarballs under ``tmp_path`` and the shared ``mock_subprocess``
recorder, so no real rsync ever runs.

As with ``export_archive``, the command is Typer-decorated but directly
callable; its defaults are ``typer.OptionInfo`` objects, so ``input_file`` /
``destination`` are always passed explicitly. ``import_archive`` also calls
``init_logger()`` (dropping loguru sinks), so assertions are behavioral. The
rsync source path lives inside an ephemeral ``tempfile.TemporaryDirectory``, so
tests match its trailing ``/{item}/`` suffix rather than a full literal path.
"""

import io
import os
import tarfile

import pytest
import typer

from netbox_manager import main


def build_archive(tmp_path, name="netbox-export.tar.gz"):
    """Write a real ``.tar.gz`` under ``tmp_path`` and return its path.

    The archive holds two directories (``devicetypes/model.yml`` and
    ``resources/100-init.yml``) plus a top-level regular ``COMMIT_INFO.txt`` --
    the plain file exercises ``import_archive``'s non-directory skip, the two
    directories drive the per-directory rsync fan-out.
    """
    stage = tmp_path / "stage"
    (stage / "devicetypes").mkdir(parents=True)
    (stage / "devicetypes" / "model.yml").write_text("marker\n")
    (stage / "resources").mkdir(parents=True)
    (stage / "resources" / "100-init.yml").write_text("[]\n")
    (stage / "COMMIT_INFO.txt").write_text("commit info\n")

    archive = tmp_path / name
    with tarfile.open(archive, "w:gz") as tar:
        for entry in sorted(os.listdir(stage)):
            tar.add(stage / entry, arcname=entry)
    return str(archive)


def build_traversal_archive(tmp_path, member_name="../pwned", name="evil.tar.gz"):
    """Write a ``.tar.gz`` whose single member escapes the extraction root.

    ``member_name`` defaults to ``../pwned`` -- a classic CVE-2007-4559 path
    that, extracted naively, lands *outside* the temporary extraction
    directory. Returns the archive path.
    """
    archive = tmp_path / name
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name=member_name)
        payload = b"pwned\n"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return str(archive)


def build_link_archive(tmp_path, link_type, name, linkname, arc="link.tar.gz"):
    """Write a ``.tar.gz`` holding a single symlink or hardlink member.

    ``link_type`` is ``tarfile.SYMTYPE`` or ``tarfile.LNKTYPE``; ``name`` is the
    member name and ``linkname`` its link target. Used to exercise the
    link-target validation in ``_safe_extractall``'s pre-PEP-706 fallback.
    Returns the archive path.
    """
    archive = tmp_path / arc
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.type = link_type
        info.linkname = linkname
        tar.addfile(info)
    return str(archive)


def build_member_archive(tmp_path, sizes, name="bomb.tar.gz"):
    """Write a ``.tar.gz`` holding one regular member per entry in ``sizes``.

    Each ``sizes`` value is a member's declared/actual byte count (members are
    named ``m0``, ``m1``, ...). Used to exercise ``_safe_extractall``'s
    decompression-bomb caps: a long ``sizes`` trips the member-count ceiling, a
    single large value trips the cumulative-size ceiling. Returns the archive
    path.
    """
    archive = tmp_path / name
    with tarfile.open(archive, "w:gz") as tar:
        for index, size in enumerate(sizes):
            info = tarfile.TarInfo(name=f"m{index}")
            info.size = size
            tar.addfile(info, io.BytesIO(b"\0" * size))
    return str(archive)


class TestExtractionCaps:
    """Group 6 -- decompression-bomb caps in ``_safe_extractall``.

    The extraction guard bounds both the member count and the cumulative
    decompressed size so a crafted archive cannot exhaust the extraction
    filesystem. The ceilings are module constants; the tests shrink them via
    ``monkeypatch`` and assert an over-cap archive is rejected with a
    ``tarfile.TarError`` *before* anything is written to the destination.
    """

    def _extract(self, archive, dest):
        with tarfile.open(archive, "r:gz") as tar:
            main._safe_extractall(tar, str(dest))

    def test_member_count_cap_rejects_archive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_MAX_EXTRACT_MEMBERS", 2)
        archive = build_member_archive(tmp_path, [0, 0, 0])
        dest = tmp_path / "extract"
        dest.mkdir()

        with pytest.raises(tarfile.TarError):
            self._extract(archive, dest)

        # Rejected during the header pre-scan, so nothing was extracted.
        assert list(dest.iterdir()) == []

    def test_cumulative_size_cap_rejects_archive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_MAX_EXTRACT_BYTES", 10)
        archive = build_member_archive(tmp_path, [20])
        dest = tmp_path / "extract"
        dest.mkdir()

        with pytest.raises(tarfile.TarError):
            self._extract(archive, dest)

        assert list(dest.iterdir()) == []

    def test_archive_within_caps_extracts(self, tmp_path, monkeypatch):
        # A member count and size under both ceilings must not be blocked.
        monkeypatch.setattr(main, "_MAX_EXTRACT_MEMBERS", 5)
        monkeypatch.setattr(main, "_MAX_EXTRACT_BYTES", 100)
        archive = build_member_archive(tmp_path, [10, 10])
        dest = tmp_path / "extract"
        dest.mkdir()

        self._extract(archive, dest)

        assert {p.name for p in dest.iterdir()} == {"m0", "m1"}


class TestMissingInput:
    """Group 1 -- missing-input guard (main.py:1168-1170)."""

    def test_missing_input_exits_before_extraction(self, tmp_path, mock_subprocess):
        missing = str(tmp_path / "no-such.tar.gz")

        with pytest.raises(typer.Exit) as exc_info:
            main.import_archive(input_file=missing, destination=str(tmp_path / "dest"))

        assert exc_info.value.exit_code == 1
        assert mock_subprocess.run_calls == []


class TestRsyncFanOut:
    """Group 2 -- per-directory rsync and non-directory skip (main.py:1180-1201)."""

    def test_rsync_invoked_per_directory(self, tmp_path, mock_subprocess):
        archive = build_archive(tmp_path)
        destination = tmp_path / "dest"

        main.import_archive(input_file=archive, destination=str(destination))

        # One rsync per extracted directory; the plain COMMIT_INFO.txt is skipped.
        assert len(mock_subprocess.run_calls) == 2
        synced = set()
        for argv, kwargs in mock_subprocess.run_calls:
            assert kwargs == {"capture_output": True, "text": True}
            assert argv[:3] == ["rsync", "-av", "--delete"]
            source, target = argv[3], argv[4]
            item = os.path.basename(target.rstrip("/"))
            assert target == f"{destination}/{item}/"
            # Source lives in the ephemeral extraction dir -> suffix match only.
            assert source.endswith(f"/{item}/")
            # Target directory was created (os.makedirs) before the rsync call.
            assert os.path.isdir(destination / item)
            synced.add(item)
        # os.listdir order is arbitrary -> compare as a set.
        assert synced == {"devicetypes", "resources"}


class TestFailureBranches:
    """Group 3 -- rsync failure and the outer wrapper (main.py:1203-1213)."""

    def test_rsync_failure_exits(self, tmp_path, mock_subprocess):
        archive = build_archive(tmp_path)
        mock_subprocess.run_returncode = 1
        mock_subprocess.run_stderr = "permission denied"

        with pytest.raises(typer.Exit) as exc_info:
            main.import_archive(input_file=archive, destination=str(tmp_path / "dest"))

        assert exc_info.value.exit_code == 1

    def test_bad_tarball_exits_via_wrapper(self, tmp_path, mock_subprocess):
        # Exists (passes the input gate) but is not a valid gzip tarball.
        bad = tmp_path / "netbox-export.tar.gz"
        bad.write_bytes(b"not a tarball at all")

        with pytest.raises(typer.Exit) as exc_info:
            main.import_archive(input_file=str(bad), destination=str(tmp_path / "dest"))

        assert exc_info.value.exit_code == 1
        # Extraction failed before any directory was reached.
        assert mock_subprocess.run_calls == []


class TestPathTraversal:
    """Group 4 -- malicious tarball is rejected on extraction (main.py:1176-1177)."""

    def test_traversal_member_is_rejected(self, tmp_path, mock_subprocess):
        archive = build_traversal_archive(tmp_path)
        destination = tmp_path / "dest"

        with pytest.raises(typer.Exit) as exc_info:
            main.import_archive(input_file=archive, destination=str(destination))

        assert exc_info.value.exit_code == 1
        # The escaping member is rejected before extraction completes, so no
        # rsync ever runs and the destination is never created.
        assert mock_subprocess.run_calls == []
        assert not destination.exists()


class TestFallbackLinkTraversal:
    """Group 5 -- link-target validation in the pre-PEP-706 fallback.

    ``_safe_extractall`` prefers the ``data`` extraction filter, but on
    interpreters lacking ``tarfile.data_filter`` it falls back to an explicit
    per-member check. These tests force that fallback (``delattr`` on
    ``tarfile.data_filter``) and assert it rejects symlink/hardlink members
    whose *targets* escape the extraction root -- a check ``member.name``
    validation alone misses, since a benign link name can still point outside.
    """

    def _extract(self, archive, dest):
        with tarfile.open(archive, "r:gz") as tar:
            main._safe_extractall(tar, str(dest))

    def test_escaping_symlink_target_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
        archive = build_link_archive(
            tmp_path, tarfile.SYMTYPE, "link", "../../../../etc/passwd"
        )
        dest = tmp_path / "extract"
        dest.mkdir()

        with pytest.raises(tarfile.TarError):
            self._extract(archive, dest)

    def test_escaping_hardlink_target_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
        archive = build_link_archive(
            tmp_path, tarfile.LNKTYPE, "hl", "../outside", arc="hl.tar.gz"
        )
        dest = tmp_path / "extract"
        dest.mkdir()

        with pytest.raises(tarfile.TarError):
            self._extract(archive, dest)

    def test_in_root_symlink_is_allowed(self, tmp_path, monkeypatch):
        # A symlink whose target stays inside the root must not be over-blocked.
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
        archive = build_link_archive(
            tmp_path, tarfile.SYMTYPE, "link", "target", arc="ok.tar.gz"
        )
        dest = tmp_path / "extract"
        dest.mkdir()

        self._extract(archive, dest)

        assert os.path.islink(dest / "link")
