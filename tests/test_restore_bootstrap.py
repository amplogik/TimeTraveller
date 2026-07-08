"""Tests for the self-contained bootstrap restore script (restore_bootstrap.py).

Exercises the real script via subprocess (bash + zstd + tar), the way a user on
a fresh machine would run it — no TimeTraveller import in the restore path."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import framewriter as fwlib
from timetraveller import manifest as manifestlib
from timetraveller import restore_bootstrap as bs
from timetraveller import restore_source as rs

FRAME = 64 * 1024


def _tar_to_framed(src: Path, archive: Path) -> None:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        with os.fdopen(w, "wb") as fw:
            with tarfile.open(fileobj=fw, mode="w|", format=tarfile.PAX_FORMAT) as tf:
                tf.add(str(src), arcname="./src", recursive=True)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as stream:
        fwlib.write_framed(stream, archive, frame_size=FRAME)
    os.waitpid(pid, 0)


def _entry(fn, kind, cid, started, sz):
    return manifestlib.ArchiveEntry(
        filename=fn, kind=kind, cycle_id=cid, date_started=started,
        date_finished=started, size_bytes=sz, status="ok", hostname="bast",
        plan_name="home", has_sidecar=True, has_frames=True)


def _make_backup_dir(root: Path) -> Path:
    """A plan dir with a full (small.txt=v1 + big.bin) and an incremental that
    updates small.txt to v2 — so a correct restore shows v2 + big.bin."""
    ad = root / "bast" / "home"
    ad.mkdir(parents=True)

    fsrc = root / "full_src"
    fsrc.mkdir()
    (fsrc / "small.txt").write_bytes(b"version one\n")
    (fsrc / "big.bin").write_bytes(b"Z" * (2 * FRAME))
    _tar_to_framed(fsrc, ad / "2026-06-28_full.pax.zst")   # added as ./src

    isrc = root / "incr_src"
    isrc.mkdir()
    (isrc / "small.txt").write_bytes(b"version two\n")
    _tar_to_framed(isrc, ad / "2026-06-29_incr.pax.zst")

    manifestlib.save(manifestlib.Manifest(plan_name="home", archives=[
        _entry("2026-06-28_full.pax.zst", "full", "2026-06-28",
               "2026-06-28T02:00:00+00:00", (ad / "2026-06-28_full.pax.zst").stat().st_size),
        _entry("2026-06-29_incr.pax.zst", "incr", "2026-06-28",
               "2026-06-29T02:00:00+00:00", (ad / "2026-06-29_incr.pax.zst").stat().st_size),
    ]), manifestlib.manifest_path(ad))
    rs.write_descriptor(ad, rs.RestoreDescriptor(
        plan_name="home", sources=["/home/kim"], hostname="bast"))
    bs.write_bootstrap_script(ad)
    return ad


def _run(script: Path, feed: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script)], input=feed, text=True,
                          capture_output=True, cwd=str(cwd), timeout=60)


def test_script_written_executable(tmp_path):
    ad = tmp_path / "d"
    ad.mkdir()
    bs.write_bootstrap_script(ad)
    p = bs.script_path(ad)
    assert p.exists() and os.access(p, os.X_OK)
    assert p.read_text().startswith("#!/usr/bin/env bash")


def test_bootstrap_restores_full_and_incremental(tmp_path):
    ad = _make_backup_dir(tmp_path)
    target = tmp_path / "restored"
    r = _run(bs.script_path(ad), f"1\n{target}\ny\n", cwd=ad)
    assert r.returncode == 0, r.stdout + r.stderr
    # incremental overlaid the full: small.txt is v2, big.bin came from the full.
    assert (target / "src" / "small.txt").read_bytes() == b"version two\n"
    assert (target / "src" / "big.bin").read_bytes() == b"Z" * (2 * FRAME)


def test_bootstrap_default_target_and_abort(tmp_path):
    ad = _make_backup_dir(tmp_path)
    # Answer 'n' at the confirm prompt → nothing extracted, clean exit.
    r = _run(bs.script_path(ad), f"1\n{tmp_path}/x\nn\n", cwd=ad)
    assert r.returncode == 0
    assert "Aborted" in (r.stdout + r.stderr)
    assert not (tmp_path / "x").exists()


def test_bootstrap_no_backups(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    bs.write_bootstrap_script(empty)
    r = _run(bs.script_path(empty), "", cwd=empty)
    assert r.returncode != 0
    assert "No TimeTraveller backups" in (r.stdout + r.stderr)
