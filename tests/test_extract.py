"""Tests for the fast-extract module."""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import extract as extractlib
from timetraveller import framewriter as fwlib
from timetraveller import index as indexlib


def _stream_tar_to_framed(src: Path, archive: Path, *, frame_size: int) -> None:
    """Helper: tar(src) → framewriter → archive. Forks so tar can stream
    into a pipe that the parent reads via framewriter."""
    tar_pipe_r, tar_pipe_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(tar_pipe_r)
        with os.fdopen(tar_pipe_w, "wb") as fw:
            with tarfile.open(fileobj=fw, mode="w|", format=tarfile.PAX_FORMAT) as tf:
                tf.add(str(src), arcname="./src", recursive=True)
        os._exit(0)
    os.close(tar_pipe_w)
    with os.fdopen(tar_pipe_r, "rb") as tar_stream:
        fwlib.write_framed(tar_stream, archive, frame_size=frame_size)
    os.waitpid(pid, 0)


def _make_framed_archive(tmp_path: Path, *, frame_size: int = 1024 * 1024) -> Path:
    """Build a mixed framed .pax.zst: small files, a subtree, a symlink, and
    a multi-frame-spanning big file."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "small.txt").write_bytes(b"hello world\n" * 10)
    (src / "another.txt").write_bytes(b"second file\n" * 20)
    (src / "sub").mkdir()
    (src / "sub" / "nested.txt").write_bytes(b"nested\n" * 50)
    (src / "sub" / "link").symlink_to("../small.txt")
    (src / "big.bin").write_bytes(b"X" * (3 * frame_size + 12345))

    archive = tmp_path / "test.pax.zst"
    _stream_tar_to_framed(src, archive, frame_size=frame_size)
    indexlib.write_sidecar(archive)
    return archive


def _make_small_framed_archive(tmp_path: Path, *, frame_size: int = 1024 * 1024) -> Path:
    """A framed archive with only tiny files — all entries fit in frame 0.
    Used to test coalescing without a big.bin separating the small files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"aaa" * 10)
    (src / "b.txt").write_bytes(b"bbb" * 20)
    (src / "c.txt").write_bytes(b"ccc" * 30)

    archive = tmp_path / "small.pax.zst"
    _stream_tar_to_framed(src, archive, frame_size=frame_size)
    indexlib.write_sidecar(archive)
    return archive


def test_extract_single_file(tmp_path):
    archive = _make_framed_archive(tmp_path)
    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./src/small.txt"], into=into)

    assert stats.matched_files == 1
    assert not stats.fallback_naive
    extracted = into / "src" / "small.txt"
    assert extracted.exists()
    assert extracted.read_bytes() == b"hello world\n" * 10


def test_extract_subtree(tmp_path):
    archive = _make_framed_archive(tmp_path)
    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./src/sub/"], into=into)

    assert stats.matched_files >= 1
    assert (into / "src" / "sub" / "nested.txt").read_bytes() == b"nested\n" * 50
    # Symlink should also be restored
    link = into / "src" / "sub" / "link"
    assert link.is_symlink()
    assert os.readlink(str(link)) == "../small.txt"


def test_extract_coalesces_same_frame(tmp_path):
    """Three small files all in frame 0 should result in exactly one
    decompress, not three."""
    archive = _make_small_framed_archive(tmp_path)
    into = tmp_path / "out"
    stats = extractlib.extract_files(
        archive, ["./src/a.txt", "./src/b.txt", "./src/c.txt"], into=into,
    )
    assert stats.matched_files == 3
    assert stats.frames_read == 1


def test_extract_multi_frame_file(tmp_path):
    archive = _make_framed_archive(tmp_path)
    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./src/big.bin"], into=into)

    assert stats.matched_files == 1
    extracted = (into / "src" / "big.bin").read_bytes()
    expected = b"X" * (3 * 1024 * 1024 + 12345)
    assert extracted == expected
    # big.bin spans at least 4 frames (data_offset + 3*frame_size + 12345)
    assert stats.frames_read >= 4


def test_extract_restores_mode_and_mtime(tmp_path):
    """Verify the extract path preserves a file's mode and mtime."""
    src = tmp_path / "src"
    src.mkdir()
    perms = src / "perms.txt"
    perms.write_bytes(b"hello")
    os.chmod(perms, 0o640)
    os.utime(perms, (1700000000, 1700000000))

    archive = tmp_path / "perms.pax.zst"
    _stream_tar_to_framed(src, archive, frame_size=1024 * 1024)
    indexlib.write_sidecar(archive)

    into = tmp_path / "out"
    extractlib.extract_files(archive, ["./src/perms.txt"], into=into)
    extracted = into / "src" / "perms.txt"
    st = extracted.stat()
    assert (st.st_mode & 0o777) == 0o640
    assert int(st.st_mtime) == 1700000000


def test_path_traversal_rejected(tmp_path):
    """A record name containing `..` must be refused to prevent writing
    outside the destination directory."""
    bad = {"name": "./a/../../etc/passwd", "type": "f", "size": 0, "mode": 420,
           "mtime": 0, "uname": "k", "gname": "k", "header_offset": 0,
           "data_offset": 512}
    with pytest.raises(ValueError, match="\\.\\."):
        extractlib._safe_out_path(bad["name"], tmp_path)


def test_fallback_when_frames_index_missing(tmp_path):
    archive = _make_framed_archive(tmp_path)
    # Remove the frames sidecar to force the naive fallback
    fwlib.sidecar_path(archive).unlink()

    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./src/small.txt"], into=into)

    assert stats.fallback_naive
    assert (into / "src" / "small.txt").read_bytes() == b"hello world\n" * 10


def test_fallback_when_sidecar_is_v1(tmp_path):
    archive = _make_framed_archive(tmp_path)
    # Overwrite the v2 sidecar with a fake v1 (plain-text) one.
    import zstandard as zstd
    fake_v1 = "drwxr-xr-x kim/kim 0 2026-01-01 00:00 ./src/\n" \
              "-rw-r--r-- kim/kim 120 2026-01-01 00:00 ./src/small.txt\n"
    sc = indexlib.sidecar_path(archive)
    cctx = zstd.ZstdCompressor(level=3)
    sc.write_bytes(cctx.compress(fake_v1.encode()))

    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./src/small.txt"], into=into)
    assert stats.fallback_naive
    assert (into / "src" / "small.txt").read_bytes() == b"hello world\n" * 10


def test_no_match_returns_empty_stats(tmp_path):
    archive = _make_framed_archive(tmp_path)
    into = tmp_path / "out"
    stats = extractlib.extract_files(archive, ["./does/not/exist"], into=into)
    assert stats.matched_files == 0
    assert stats.matched_dirs == 0
    assert stats.matched_symlinks == 0
    assert not stats.fallback_naive


def test_dedup_when_pattern_matches_same_file_multiple_times(tmp_path):
    archive = _make_framed_archive(tmp_path)
    into = tmp_path / "out"
    # The exact match and the subtree both cover small.txt
    stats = extractlib.extract_files(
        archive, ["./src/small.txt", "./src/"], into=into,
    )
    # small.txt should only be extracted once (and counted once)
    assert stats.matched_files == 4   # small.txt, another.txt, sub/nested.txt, big.bin
