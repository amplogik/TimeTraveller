"""The naive (no-sidecar) extract path must not report success on failure.

It used to set `stderr=DEVNULL`, ignore tar's exit status entirely (the check was
literally `pass`), and then tally results by walking the destination directory —
so a wholly failed extract into a non-empty directory returned a large
`bytes_written` and read as a clean restore. That matters most for exactly the
case this path is reached in: a privileged restore of root-owned files, where
"silently dropped everything" is far worse than a hard error.

A member simply not being present is still benign — a logical backup is sharded
and callers extract the same pattern list from every shard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import extract as extractlib
from timetraveller import framewriter as fwlib

from tests.test_extract import _make_framed_archive


def _naive_archive(tmp_path: Path) -> Path:
    """A framed archive with its frames sidecar removed, forcing the naive path."""
    archive = _make_framed_archive(tmp_path)
    fwlib.sidecar_path(archive).unlink()
    return archive


def test_counts_only_what_was_extracted_not_preexisting_files(tmp_path):
    """The regression that made a failed restore look successful."""
    archive = _naive_archive(tmp_path)
    into = tmp_path / "out"
    into.mkdir()
    # Pre-existing junk in the destination, as a real restore target would have.
    (into / "unrelated.bin").write_bytes(b"Z" * 100_000)
    (into / "also-unrelated.bin").write_bytes(b"Z" * 100_000)

    stats = extractlib.extract_files(archive, ["./src/small.txt"], into=into)

    assert stats.fallback_naive
    # Exactly one file extracted — the pre-existing 200 KB must not be counted.
    assert stats.matched_files == 1
    assert stats.bytes_written == len(b"hello world\n" * 10)


def test_missing_member_is_benign_and_reports_no_match(tmp_path):
    """tar exits 2 with 'Not found in archive'; that is not a failure."""
    archive = _naive_archive(tmp_path)
    into = tmp_path / "out"

    stats = extractlib.extract_files(archive, ["./src/does-not-exist"], into=into)

    assert stats.fallback_naive
    assert stats.matched_files == 0
    assert stats.bytes_written == 0


def test_permission_failure_raises_instead_of_reporting_success(tmp_path):
    """An unwritable destination must surface as an error, not a clean result."""
    archive = _naive_archive(tmp_path)
    into = tmp_path / "locked"
    into.mkdir()
    # Pre-fill so a destination-walking tally would have something to count.
    (into / "decoy.bin").write_bytes(b"Z" * 50_000)
    into.chmod(0o500)            # readable + searchable, not writable
    try:
        with pytest.raises(extractlib.ExtractError) as ei:
            extractlib.extract_files(archive, ["./src/small.txt"], into=into)
        assert "tar exited" in str(ei.value)
    finally:
        into.chmod(0o700)


def test_corrupt_archive_raises_on_naive_path(tmp_path):
    """A truncated/garbage archive must not come back as a successful extract."""
    archive = _naive_archive(tmp_path)
    archive.write_bytes(b"this is not a zstd stream at all")
    into = tmp_path / "out"

    with pytest.raises(extractlib.ExtractError):
        extractlib.extract_files(archive, ["./src/small.txt"], into=into)


def test_subtree_extract_counts_dirs_and_files(tmp_path):
    archive = _naive_archive(tmp_path)
    into = tmp_path / "out"

    stats = extractlib.extract_files(archive, ["./src/sub/"], into=into)

    assert stats.fallback_naive
    assert (into / "src" / "sub" / "nested.txt").read_bytes() == b"nested\n" * 50
    # The directory entry is tallied separately from its contents.
    assert stats.matched_dirs >= 1
    assert stats.matched_files >= 1


def test_benign_stderr_classifier():
    ok = ("tar: ./src/nope: Not found in archive\n"
          "tar: Exiting with failure status due to previous errors")
    assert extractlib._tar_stderr_is_only_benign(ok) is True

    for bad in (
        "tar: ./src/x: Cannot open: Permission denied\n"
        "tar: Exiting with failure status due to previous errors",
        "tar: ./src/x: Cannot write: No space left on device",
        "tar: Unexpected EOF in archive",
    ):
        assert extractlib._tar_stderr_is_only_benign(bad) is False

    # No diagnostic at all is not "benign" — a non-zero exit with empty stderr
    # is unexplained and must be treated as a failure.
    assert extractlib._tar_stderr_is_only_benign("") is False
