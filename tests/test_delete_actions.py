"""C: scoped delete actions (--delete-cycle / --delete-set) and the shard-aware
file sweep.

A logical backup is N shard archives. Deleting one must remove EVERY shard's
on-disk files (archive, the `.failed`-suffixed variant, `.idx.zst`,
`.frames.json`) and the matching manifest entries, and must refuse the
always-keep newest complete cycle / an incremental-bearing full unless --force.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import index as indexlib
from timetraveller import manifest as manifestlib
from timetraveller import worker
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)


def _entry(group: str, kind: str, idx: int, n: int, *, status="ok", started=""):
    suffix = "" if n == 1 else f".s{idx}of{n}"
    fname = f"{group}{suffix}.pax.zst"
    cid = group.split("_")[0]
    return manifestlib.ArchiveEntry(
        filename=fname, kind=kind, cycle_id=cid,
        date_started=started or f"{cid}T09:00:0{idx}+00:00",
        date_finished="" if status == "in-progress" else f"{cid}T09:30:00+00:00",
        size_bytes=100, status=status, hostname="h", plan_name="tp",
        shard_index=idx, shard_count=n, shard_group=group,
    )


def _set(group, kind, n, *, statuses=None, started=""):
    statuses = statuses or ["ok"] * n
    return [_entry(group, kind, i + 1, n, status=statuses[i], started=started)
            for i in range(n)]


def _plan(dest: Path) -> PlanConfig:
    return PlanConfig(
        plan_name="tp", sources=["/x"], destination=str(dest),
        include_hostname_in_path=False,
        schedule=Schedule(mode="weekly", full=FullSchedule(days=["sun"]),
                          incr=IncrSchedule(mode="except_full")),
        retention=Retention(),
    )


def _args(**kw):
    base = dict(quiet=True, log_file=None, force=False,
                delete_cycle=None, delete_set=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _touch(archive_dir: Path, entries, *, failed=False, frames=True):
    """Materialise on-disk files for each entry: archive (or `.failed`), the
    `.idx.zst` sidecar, and optionally `.frames.json`."""
    for e in entries:
        name = e.filename + ".failed" if failed else e.filename
        (archive_dir / name).write_text("x")
        (archive_dir / (e.filename + ".idx.zst")).write_text("i")
        if frames:
            (archive_dir / (e.filename + ".frames.json")).write_text("{}")


def _setup(tmp_path, monkeypatch, archives):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = _plan(tmp_path / "dest")
    archive_dir = plan.archive_dir()
    archive_dir.mkdir(parents=True)
    man = manifestlib.Manifest(plan_name="tp", archives=list(archives))
    manifestlib.save(man, manifestlib.manifest_path(archive_dir))
    return plan, archive_dir


def _reload(archive_dir):
    return manifestlib.load(manifestlib.manifest_path(archive_dir))


# ---------- delete-cycle ----------

def test_delete_cycle_removes_every_shard_file(tmp_path, monkeypatch):
    old = _set("2026-06-10_full", "full", 4, started="2026-06-10T09:00:00+00:00")
    new = _set("2026-06-12_full", "full", 2, started="2026-06-12T09:00:00+00:00")
    plan, archive_dir = _setup(tmp_path, monkeypatch, old + new)
    _touch(archive_dir, old + new)

    rc = worker.action_delete_cycle(_args(delete_cycle="2026-06-10"), plan)
    assert rc == 0

    # All 4 old shards' files (archive + idx + frames) are gone.
    assert not any(p.name.startswith("2026-06-10_full") for p in archive_dir.iterdir())
    # The newer cycle is fully intact (2 shards × 3 files).
    assert sum(p.name.startswith("2026-06-12_full") for p in archive_dir.iterdir()) == 6
    # Manifest keeps only the newer cycle's entries.
    man = _reload(archive_dir)
    assert {e.shard_group for e in man.archives} == {"2026-06-12_full"}


def test_delete_cycle_refuses_newest_complete_without_force(tmp_path, monkeypatch):
    old = _set("2026-06-10_full", "full", 2, started="2026-06-10T09:00:00+00:00")
    new = _set("2026-06-12_full", "full", 2, started="2026-06-12T09:00:00+00:00")
    plan, archive_dir = _setup(tmp_path, monkeypatch, old + new)
    _touch(archive_dir, old + new)

    rc = worker.action_delete_cycle(_args(delete_cycle="2026-06-12"), plan)
    assert rc == 1
    assert len(_reload(archive_dir).archives) == 4  # nothing removed

    rc = worker.action_delete_cycle(
        _args(delete_cycle="2026-06-12", force=True), plan)
    assert rc == 0
    assert {e.shard_group for e in _reload(archive_dir).archives} == {"2026-06-10_full"}


def test_delete_cycle_unknown_id_errors(tmp_path, monkeypatch):
    full = _set("2026-06-12_full", "full", 2)
    plan, archive_dir = _setup(tmp_path, monkeypatch, full)
    assert worker.action_delete_cycle(_args(delete_cycle="2099-01-01"), plan) == 1
    assert len(_reload(archive_dir).archives) == 2


# ---------- delete-set ----------

def test_delete_set_sweeps_failed_and_frames(tmp_path, monkeypatch):
    full = _set("2026-06-12_full", "full", 2, started="2026-06-12T09:00:00+00:00")
    # Incremental set whose 2nd shard FAILED — on disk at `.failed`, manifest at
    # the bare name. The sweep must catch the suffixed file too.
    incr = _set("2026-06-12T10_incr", "incr", 2,
                statuses=["ok", "failed"], started="2026-06-12T10:00:00+00:00")
    plan, archive_dir = _setup(tmp_path, monkeypatch, full + incr)
    _touch(archive_dir, full)
    _touch(archive_dir, [incr[0]])                 # ok shard: bare archive
    _touch(archive_dir, [incr[1]], failed=True)    # failed shard: .failed on disk

    rc = worker.action_delete_set(
        _args(delete_set="2026-06-12T10_incr"), plan)
    assert rc == 0
    # No file from the incr group survives — including the `.failed` one.
    assert not any(p.name.startswith("2026-06-12T10_incr")
                   for p in archive_dir.iterdir())
    # The full set is untouched.
    man = _reload(archive_dir)
    assert {e.shard_group for e in man.archives} == {"2026-06-12_full"}


def test_delete_set_refuses_full_with_dependents(tmp_path, monkeypatch):
    full = _set("2026-06-12_full", "full", 2, started="2026-06-12T09:00:00+00:00")
    incr = _set("2026-06-12T10_incr", "incr", 1, started="2026-06-12T10:00:00+00:00")
    plan, archive_dir = _setup(tmp_path, monkeypatch, full + incr)
    _touch(archive_dir, full + incr)

    rc = worker.action_delete_set(_args(delete_set="2026-06-12_full"), plan)
    assert rc == 1
    assert len(_reload(archive_dir).archives) == 3  # untouched

    rc = worker.action_delete_set(
        _args(delete_set="2026-06-12_full", force=True), plan)
    assert rc == 0
    assert {e.shard_group for e in _reload(archive_dir).archives} == {"2026-06-12T10_incr"}


def test_delete_set_refuses_newest_complete_full(tmp_path, monkeypatch):
    full = _set("2026-06-12_full", "full", 2, started="2026-06-12T09:00:00+00:00")
    plan, archive_dir = _setup(tmp_path, monkeypatch, full)
    _touch(archive_dir, full)

    assert worker.action_delete_set(_args(delete_set="2026-06-12_full"), plan) == 1
    assert len(_reload(archive_dir).archives) == 2
    assert worker.action_delete_set(
        _args(delete_set="2026-06-12_full", force=True), plan) == 0
    assert _reload(archive_dir).archives == []


def test_delete_set_unknown_id_errors(tmp_path, monkeypatch):
    full = _set("2026-06-12_full", "full", 2)
    plan, archive_dir = _setup(tmp_path, monkeypatch, full)
    assert worker.action_delete_set(_args(delete_set="nope"), plan) == 1
    assert len(_reload(archive_dir).archives) == 2


# ---------- _sync_mirror_from_mount: manifest AND sidecars ----------

def test_sync_mirror_seeds_missing_sidecars(tmp_path, monkeypatch):
    """After a privileged op, the user mirror must get both the manifest and the
    per-shard .idx.zst sidecars — else the GUI lists the backup but can't browse
    it ('no local mirror sidecar'). Regression for the 2026-06-14 system run."""
    entries = _set("2026-06-14_full", "full", 3)
    plan, archive_dir = _setup(tmp_path, monkeypatch, entries)
    _touch(archive_dir, entries)   # archives + .idx.zst sidecars on the mount

    # User mirror starts empty.
    for e in entries:
        assert not indexlib.sidecar_mirror_path("tp", e.filename).exists()

    worker._sync_mirror_from_mount(plan)

    # Manifest mirror written, and every sidecar copied into the user mirror.
    assert manifestlib.mirror_manifest_path("tp").exists()
    for e in entries:
        assert indexlib.sidecar_mirror_path("tp", e.filename).exists()


def test_sync_mirror_refreshes_stale_sidecar(tmp_path, monkeypatch):
    """A reindex/recover rewrites a sidecar; the sync must refresh a stale mirror
    copy (newer source overwrites), not skip it."""
    import os
    entries = _set("2026-06-14_full", "full", 1)
    plan, archive_dir = _setup(tmp_path, monkeypatch, entries)
    _touch(archive_dir, entries)
    fname = entries[0].filename

    # Seed an OLD mirror sidecar, then make the on-mount one newer with new bytes.
    worker._sync_mirror_from_mount(plan)
    mirror = indexlib.sidecar_mirror_path("tp", fname)
    old_mtime = mirror.stat().st_mtime
    src = indexlib.sidecar_path(archive_dir / fname)
    src.write_text("REBUILT")
    os.utime(src, (old_mtime + 10, old_mtime + 10))

    worker._sync_mirror_from_mount(plan)
    assert mirror.read_text() == "REBUILT"
