"""Inline index build (Phase B): the .idx.zst produced during a framed write
must be byte-identical (records) to a post-write index.write_sidecar of the
same archive, and --no-framed must report no inline index so the caller falls
back to the post-write pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import index as indexlib
from timetraveller import pax as paxlib


def _make_src(src: Path) -> list[str]:
    src.mkdir(parents=True, exist_ok=True)
    rel = []
    for i in range(40):
        p = src / f"f{i:03d}.txt"
        p.write_text(f"contents {i}\n" * (i + 1))
        rel.append(f"./f{i:03d}.txt")
    # A long name (pax extended header), a symlink and a hardlink.
    deep = src / ("d" * 80)
    deep.mkdir(exist_ok=True)
    longf = deep / ("n" * 130 + ".txt")
    longf.write_text("payload\n")
    rel.append("./" + str(longf.relative_to(src)))
    tgt = src / "target.bin"
    tgt.write_bytes(b"\x00" * 4096)
    rel.append("./target.bin")
    (src / "sym").symlink_to("target.bin")
    rel.append("./sym")
    os.link(tgt, src / "hard")
    rel.append("./hard")
    return rel


def _records(idx_path: Path) -> list[dict]:
    raw = subprocess.run(["zstdcat", str(idx_path)], capture_output=True,
                         check=True).stdout.decode()
    # Drop the header line (line 0); its created_at is timestamp-dependent.
    return [json.loads(l) for l in raw.splitlines()[1:]]


def test_inline_index_matches_write_sidecar(tmp_path):
    src = tmp_path / "src"
    rel = _make_src(src)

    archive = tmp_path / "a.pax.zst"
    inv = paxlib.PaxInvocation(sources=[], chdir=str(src), archive_path=archive,
                               excludes=[], extra_mount_excludes=[], framed=True)
    result = paxlib.run_with_file_list(inv, iter(rel))

    assert result.status == "ok"
    assert result.index_built is True
    inline_sidecar = indexlib.sidecar_path(archive)
    assert inline_sidecar.exists()
    inline_recs = _records(inline_sidecar)

    # Rebuild from scratch with the post-write path and compare records.
    inline_sidecar.unlink()
    indexlib.write_sidecar(archive)
    ref_recs = _records(inline_sidecar)

    assert inline_recs == ref_recs
    assert len(inline_recs) == len(rel)
    # Edge cases actually present.
    assert any(len(r["name"]) > 100 for r in inline_recs)   # long name
    assert any(r["type"] == "l" for r in inline_recs)        # symlink
    assert any(r["type"] == "h" for r in inline_recs)        # hardlink
    assert all(r["header_offset"] is not None
               and r["data_offset"] is not None for r in inline_recs)


def test_no_framed_reports_no_inline_index(tmp_path):
    src = tmp_path / "src"
    rel = _make_src(src)
    archive = tmp_path / "b.pax.zst"
    inv = paxlib.PaxInvocation(sources=[], chdir=str(src), archive_path=archive,
                               excludes=[], extra_mount_excludes=[], framed=False)
    result = paxlib.run_with_file_list(inv, iter(rel))

    assert result.status == "ok"
    assert result.index_built is False
    # No inline sidecar; the post-write pass still builds a valid one.
    assert not indexlib.sidecar_path(archive).exists()
    indexlib.write_sidecar(archive)
    assert indexlib.sidecar_path(archive).exists()
    assert len(_records(indexlib.sidecar_path(archive))) == len(rel)
