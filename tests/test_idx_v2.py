"""Tests for the v2 (JSONL) sidecar format — generation, reading, and
backward-compat with the legacy v1 (plain-text) format.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import archive as archivelib
from timetraveller import index as indexlib


def _make_simple_archive(tmp_path: Path) -> Path:
    """Build a tiny .pax.zst with a couple regular files, a dir, and a symlink."""
    import zstandard as zstd

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_bytes(b"hello world\n" * 50)
    (src_dir / "b.txt").write_bytes(b"goodbye\n" * 100)
    (src_dir / "sub").mkdir()
    (src_dir / "sub" / "c.txt").write_bytes(b"nested\n" * 200)
    (src_dir / "sub" / "link").symlink_to("../a.txt")

    archive = tmp_path / "test.pax.zst"
    with open(archive, "wb") as raw_out:
        cctx = zstd.ZstdCompressor(level=3)
        with cctx.stream_writer(raw_out) as zstd_out:
            with tarfile.open(fileobj=zstd_out, mode="w|", format=tarfile.PAX_FORMAT) as tf:
                tf.add(str(src_dir), arcname="./src", recursive=True)
    return archive


def test_v2_sidecar_schema(tmp_path):
    archive = _make_simple_archive(tmp_path)
    sidecar = indexlib.write_sidecar(archive)

    raw = subprocess.check_output(["zstdcat", str(sidecar)], text=True)
    lines = [l for l in raw.splitlines() if l.strip()]
    assert len(lines) >= 2

    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["archive"] == "test.pax.zst"
    assert "created_at" in header

    for line in lines[1:]:
        rec = json.loads(line)
        for key in ("name", "type", "size", "mode", "mtime",
                    "uname", "gname", "header_offset", "data_offset"):
            assert key in rec, f"missing key {key!r} in record {rec!r}"


def test_v2_sidecar_offsets_locate_file_data(tmp_path):
    """The data_offset in v2 records must point at the actual file content
    in the uncompressed tar stream. This is the core Phase D enabler."""
    import zstandard as zstd

    archive = _make_simple_archive(tmp_path)
    sidecar = indexlib.write_sidecar(archive)
    raw = subprocess.check_output(["zstdcat", str(sidecar)], text=True)
    records = [json.loads(l) for l in raw.splitlines() if l.strip()]

    # Decompress whole archive into memory
    dctx = zstd.ZstdDecompressor()
    with open(archive, "rb") as f:
        uncompressed = dctx.stream_reader(f).read()

    expected = {
        "./src/a.txt": b"hello world\n" * 50,
        "./src/b.txt": b"goodbye\n" * 100,
        "./src/sub/c.txt": b"nested\n" * 200,
    }
    for path, body in expected.items():
        rec = next(r for r in records if r.get("name") == path)
        slice_ = uncompressed[rec["data_offset"]: rec["data_offset"] + rec["size"]]
        assert slice_ == body, f"data at offset for {path} doesn't match"


def test_v2_sidecar_symlink_link_target(tmp_path):
    archive = _make_simple_archive(tmp_path)
    sidecar = indexlib.write_sidecar(archive)
    raw = subprocess.check_output(["zstdcat", str(sidecar)], text=True)
    records = [json.loads(l) for l in raw.splitlines() if l.strip()]

    link_rec = next(r for r in records if r.get("name") == "./src/sub/link")
    assert link_rec["type"] == "l"
    assert link_rec["link_target"] == "../a.txt"


def test_v2_tree_parses_via_load_sidecar(tmp_path):
    archive = _make_simple_archive(tmp_path)
    sidecar = indexlib.write_sidecar(archive)
    tree = archivelib.load_sidecar_tree(sidecar)

    src = tree.children["src"]
    assert src.is_dir
    a = src.children["a.txt"]
    assert not a.is_dir
    assert a.size == len(b"hello world\n" * 50)
    assert a.data_offset > 0
    assert a.header_offset > 0
    assert a.data_offset > a.header_offset

    link = src.children["sub"].children["link"]
    assert link.symlink_target == "../a.txt"


def test_v1_text_format_still_parses():
    """Legacy v1 sidecars (plain-text from `tar -tv`) must keep working
    so archives taken before the v2 cutover stay browsable.
    """
    v1_text = (
        "drwxr-xr-x kim/kim           0 2026-05-17 04:52 ./src/\n"
        "-rw-r--r-- kim/kim          16 2026-05-17 04:52 ./src/file.txt\n"
        "lrwxrwxrwx kim/kim           0 2026-05-17 04:52 ./src/link -> file.txt\n"
    )
    tree = archivelib.parse_index(v1_text)
    src = tree.children["src"]
    assert src.is_dir
    assert src.children["file.txt"].size == 16
    assert src.children["link"].symlink_target == "file.txt"
    # v1 has no offset info → fields stay at the zero default
    assert src.children["file.txt"].header_offset == 0
    assert src.children["file.txt"].data_offset == 0


def test_v2_format_autodetect_via_first_char():
    """The reader's format autodetect should pick v2 when the input starts
    with `{`, even with leading whitespace.
    """
    v2_minimal = (
        '{"version": 2, "archive": "x.pax.zst", "created_at": "2026-01-01T00:00:00+00:00"}\n'
        '{"name": "./x", "type": "f", "size": 0, "mode": 420, "mtime": 0,'
        ' "uname": "kim", "gname": "kim", "header_offset": 0, "data_offset": 512}\n'
    )
    # Leading whitespace shouldn't fool the detector.
    tree = archivelib.parse_index("\n  \n" + v2_minimal)
    assert "x" in tree.children


def test_v2_handles_empty_records_gracefully():
    """Blank lines in the JSONL should be skipped without erroring."""
    payload = (
        '{"version": 2, "archive": "x", "created_at": "2026-01-01T00:00:00+00:00"}\n'
        "\n"
        '{"name": "./a", "type": "f", "size": 1, "mode": 420, "mtime": 0,'
        ' "uname": "k", "gname": "k", "header_offset": 0, "data_offset": 512}\n'
        "\n"
    )
    tree = archivelib.parse_index(payload)
    assert "a" in tree.children


def test_v2_with_long_path_pax_extended_header(tmp_path):
    """pax extended headers (used for paths >100 chars) shouldn't confuse
    the offset accounting. Python's tarfile resolves the extended header
    automatically; the TarInfo.offset should point at the START of the
    extended-header block, not at the regular ustar header that follows it."""
    import zstandard as zstd

    # A path > 100 chars triggers a pax-extended-header pair.
    long_name = "./" + "x" * 200 + ".txt"

    archive = tmp_path / "long.pax.zst"
    with open(archive, "wb") as raw_out:
        cctx = zstd.ZstdCompressor(level=3)
        with cctx.stream_writer(raw_out) as zstd_out:
            with tarfile.open(fileobj=zstd_out, mode="w|",
                              format=tarfile.PAX_FORMAT) as tf:
                ti = tarfile.TarInfo(name=long_name)
                body = b"x" * 1024
                ti.size = len(body)
                ti.mode = 0o644
                ti.mtime = 1716000000
                ti.uname = "kim"
                ti.gname = "kim"
                import io
                tf.addfile(ti, io.BytesIO(body))

    sidecar = indexlib.write_sidecar(archive)
    raw = subprocess.check_output(["zstdcat", str(sidecar)], text=True)
    records = [json.loads(l) for l in raw.splitlines() if l.strip()]
    rec = next(r for r in records if r.get("name") == long_name)

    # The offsets must still bound the file data correctly.
    dctx = zstd.ZstdDecompressor()
    with open(archive, "rb") as f:
        uncompressed = dctx.stream_reader(f).read()
    slice_ = uncompressed[rec["data_offset"]: rec["data_offset"] + rec["size"]]
    assert slice_ == b"x" * 1024
