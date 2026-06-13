"""Tests for action_recover_failed — un-quarantine a failed-but-intact backup.

Unlike --finalize-archive, recovery actually reads the archive (write_sidecar
streams the whole thing through tarfile), so these tests build *real* framed
pax+zstd archives rather than dummy bytes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import framewriter
from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib
from timetraveller import pax as paxlib
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)
from timetraveller.worker import action_recover_failed


def _plan(destination: Path) -> PlanConfig:
    return PlanConfig(
        plan_name="testplan",
        sources=["/tmp"],
        destination=str(destination),
        include_hostname_in_path=True,
        schedule=Schedule(mode="weekly", full=FullSchedule(),
                          incr=IncrSchedule(mode="weekdays")),
        retention=Retention(),
    )


def _entry(filename: str, status: str = "failed") -> manifestlib.ArchiveEntry:
    return manifestlib.ArchiveEntry(
        filename=filename, kind="incr", cycle_id="2026-06-13",
        date_started="2026-06-13T09:00:00+00:00",
        date_finished="",
        size_bytes=0, status=status, hostname="testhost",
        plan_name="testplan",
    )


def _args(filename: str, **overrides) -> argparse.Namespace:
    defaults = dict(recover_failed=filename, force=False, quiet=True)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _build_archive(archive_dir: Path, src: Path, dest_name: str, *,
                   framed: bool = True) -> Path:
    """Write a real framed pax+zstd archive of `src` to archive_dir/dest_name."""
    src.mkdir(parents=True, exist_ok=True)
    (src / "alpha.txt").write_text("alpha contents\n")
    (src / "beta.txt").write_text("beta beta beta\n")
    (src / "sub").mkdir(exist_ok=True)
    (src / "sub" / "gamma.txt").write_text("gamma\n" * 100)

    archive_dir.mkdir(parents=True, exist_ok=True)
    inv = paxlib.PaxInvocation(
        sources=[], chdir=str(src),
        archive_path=archive_dir / dest_name,
        excludes=[], extra_mount_excludes=[], framed=framed,
    )
    files = ["./alpha.txt", "./beta.txt", "./sub/gamma.txt"]
    result = paxlib.run_with_file_list(inv, iter(files))
    assert result.status == "ok", result.status
    return inv.archive_path


def _seed_failed(tmp_path: Path, *, framed: bool = True,
                 already_bare: bool = False) -> tuple[PlanConfig, Path, str]:
    """Create a real archive, quarantine it to .failed (unless already_bare),
    and seed a manifest entry with status=failed under the bare name."""
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    fname = "2026-06-13_incr.pax.zst"

    built = _build_archive(archive_dir, tmp_path / "src", fname, framed=framed)
    # A genuinely failed backup never produced a sidecar index (the post-write /
    # inline build is skipped on failure); drop the one the successful build
    # just created so the fixture matches a real .failed state.
    indexlib.sidecar_path(built).unlink(missing_ok=True)
    if not already_bare:
        built.rename(built.with_suffix(built.suffix + ".failed"))

    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan", archives=[_entry(fname)]),
        manifestlib.manifest_path(archive_dir),
    )
    return plan, archive_dir, fname


def test_recover_intact_failed_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, archive_dir, fname = _seed_failed(tmp_path, framed=True)

    rc = action_recover_failed(_args(fname), plan)
    assert rc == 0

    bare = archive_dir / fname
    failed = bare.with_suffix(bare.suffix + ".failed")
    assert bare.exists() and not failed.exists()          # un-quarantined
    assert indexlib.sidecar_path(bare).exists()            # sidecar built

    e = manifestlib.load(manifestlib.manifest_path(archive_dir)).archives[0]
    assert e.status == "ok-with-warnings"
    assert e.date_finished != ""
    assert e.size_bytes == bare.stat().st_size
    assert e.has_sidecar is True
    assert e.has_frames is True                            # framed build

    # Mirror manifest + sidecar mirror were written.
    assert manifestlib.load(
        manifestlib.mirror_manifest_path("testplan")
    ).archives[0].status == "ok-with-warnings"
    assert indexlib.sidecar_mirror_path("testplan", fname).exists()


def test_recover_truncated_archive_rolls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, archive_dir, fname = _seed_failed(tmp_path, framed=True)

    # Corrupt the stream: keep only the first half of the .failed archive.
    failed = (archive_dir / fname).with_suffix(".zst.failed")
    data = failed.read_bytes()
    failed.write_bytes(data[: len(data) // 2])

    rc = action_recover_failed(_args(fname), plan)
    assert rc == 1
    assert "not recoverable" in capsys.readouterr().err

    bare = archive_dir / fname
    # Re-quarantined: bare gone, .failed restored, no sidecar left behind.
    assert failed.exists() and not bare.exists()
    assert not indexlib.sidecar_path(bare).exists()
    # And no orphaned temp sidecar from the aborted write.
    sc = indexlib.sidecar_path(bare)
    assert not sc.with_suffix(sc.suffix + ".tmp").exists()

    e = manifestlib.load(manifestlib.manifest_path(archive_dir)).archives[0]
    assert e.status == "failed"
    assert e.has_sidecar is False


def test_recover_idempotent_when_already_bare(tmp_path, monkeypatch):
    """A prior recovery that renamed .failed->bare but crashed before finalize
    must still recover: the file is already bare, manifest still says failed."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, archive_dir, fname = _seed_failed(tmp_path, already_bare=True)

    rc = action_recover_failed(_args(fname), plan)
    assert rc == 0

    e = manifestlib.load(manifestlib.manifest_path(archive_dir)).archives[0]
    assert e.status == "ok-with-warnings"
    assert (archive_dir / fname).exists()


def test_recover_missing_entry_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    plan.archive_dir().mkdir(parents=True)
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan", archives=[]),
        manifestlib.manifest_path(plan.archive_dir()),
    )

    rc = action_recover_failed(_args("nope.pax.zst"), plan)
    assert rc == 1
    assert "no manifest entry" in capsys.readouterr().err


def test_recover_non_failed_entry_requires_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan, archive_dir, fname = _seed_failed(tmp_path, already_bare=True)
    # Flip the seeded entry to a non-failed status.
    m = manifestlib.load(manifestlib.manifest_path(archive_dir))
    m.archives[0].status = "ok"
    manifestlib.save(m, manifestlib.manifest_path(archive_dir))

    rc = action_recover_failed(_args(fname), plan)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not 'failed'" in err and "--force" in err

    # With --force it proceeds.
    rc = action_recover_failed(_args(fname, force=True), plan)
    assert rc == 0
    e = manifestlib.load(manifestlib.manifest_path(archive_dir)).archives[0]
    assert e.status == "ok-with-warnings"


def test_recover_missing_file_errors(tmp_path, monkeypatch, capsys):
    """Manifest entry exists but neither .failed nor bare file is on disk."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "mount")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    fname = "2026-06-13_incr.pax.zst"
    manifestlib.save(
        manifestlib.Manifest(plan_name="testplan", archives=[_entry(fname)]),
        manifestlib.manifest_path(archive_dir),
    )

    rc = action_recover_failed(_args(fname), plan)
    assert rc == 1
    assert "archive not found" in capsys.readouterr().err
