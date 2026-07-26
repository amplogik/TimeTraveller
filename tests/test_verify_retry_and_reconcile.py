"""A frame is never condemned on a single read, and a verify result is recorded.

Both come from the same field incident (bast, 2026-07-26). 2026-07-19_full was
flagged with one corrupt frame by verify-after-write, and re-verified perfectly
clean afterwards -- 168/168 frames good on both SHA-256 and full zstd decode. So
the flag came from a transient bad read, not from damaged bytes. Two defects:

  1. `verify_frame_checksums` condemned on ONE read, turning a momentary NFS/link
     fault into a permanent "corrupt" record.
  2. `action_verify` only printed, so that false record could never be cleared
     short of hand-editing manifest.json -- and while set, it demoted the full
     out of its own cycle (see test_cycle_grouping_corrupt_full.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

from timetraveller import heal as heallib
from timetraveller import manifest as manifestlib
from timetraveller import worker
from timetraveller.config import defaults_system


# ---------- 1. retry classification ----------

def _fake_archive(tmp_path: Path, payload: bytes = b"payload-bytes"):
    """An archive plus a v2 frames sidecar describing one frame over `payload`."""
    import hashlib
    archive = tmp_path / "a.pax.zst"
    archive.write_bytes(payload)
    sidecar = tmp_path / "a.pax.zst.frames.json"
    sidecar.write_text(json.dumps({
        "version": 2, "frame_size": 1024, "csum_algo": "sha256",
        "frames": [{"id": 0, "uo": 0, "ul": len(payload), "co": 0,
                    "cl": len(payload),
                    "csum": hashlib.sha256(payload).hexdigest()}],
    }))
    return archive


def test_clean_frame_needs_no_retry(tmp_path):
    archive = _fake_archive(tmp_path)
    algo, n, bad, transient = heallib.verify_frame_checksums(archive)
    assert (n, bad, transient) == (1, [], [])


def test_frame_that_recovers_on_retry_is_transient_not_corrupt(monkeypatch, tmp_path):
    """The exact false-positive that hit bast: first read bad, retry good."""
    archive = _fake_archive(tmp_path)
    real_pread = os.pread
    calls = {"n": 0}

    def flaky(fd, length, offset):
        calls["n"] += 1
        if calls["n"] == 1:
            return b"X" * length          # one bad read
        return real_pread(fd, length, offset)

    monkeypatch.setattr(heallib.os, "pread", flaky)
    _algo, _n, bad, transient = heallib.verify_frame_checksums(archive)

    assert bad == []                       # must NOT be condemned
    assert len(transient) == 1
    assert transient[0]["anomaly"] == "read-recovered"
    assert calls["n"] >= 2                 # it actually retried


def test_consistently_bad_frame_is_still_corrupt(monkeypatch, tmp_path):
    """The retry must not make real corruption undetectable."""
    archive = _fake_archive(tmp_path)
    monkeypatch.setattr(heallib.os, "pread",
                        lambda fd, length, offset: b"X" * length)
    _algo, _n, bad, transient = heallib.verify_frame_checksums(archive)
    assert len(bad) == 1
    assert transient == []


def test_unstable_reads_are_not_condemned(monkeypatch, tmp_path):
    """Different bytes on every read means the read path is unreliable, so the
    stored content is unproven — reporting it as corrupt would be a guess."""
    archive = _fake_archive(tmp_path)
    seq = {"i": 0}

    def unstable(fd, length, offset):
        seq["i"] += 1
        return bytes([seq["i"] % 251]) * length

    monkeypatch.setattr(heallib.os, "pread", unstable)
    _algo, _n, bad, transient = heallib.verify_frame_checksums(archive)
    assert bad == []
    assert len(transient) == 1
    assert transient[0]["anomaly"] == "unstable-read"


def test_truncated_frame_is_corrupt_not_transient(monkeypatch, tmp_path):
    archive = _fake_archive(tmp_path)
    monkeypatch.setattr(heallib.os, "pread",
                        lambda fd, length, offset: b"short")
    _algo, _n, bad, transient = heallib.verify_frame_checksums(archive)
    assert len(bad) == 1 and transient == []


def test_retries_can_be_disabled(monkeypatch, tmp_path):
    archive = _fake_archive(tmp_path)
    calls = {"n": 0}
    real_pread = os.pread

    def flaky(fd, length, offset):
        calls["n"] += 1
        return b"X" * length if calls["n"] == 1 else real_pread(fd, length, offset)

    monkeypatch.setattr(heallib.os, "pread", flaky)
    _a, _n, bad, transient = heallib.verify_frame_checksums(archive, retries=0)
    assert len(bad) == 1 and transient == []      # condemned, as the old code did


# ---------- 2. manifest reconciliation ----------

def _entry(fn, **kw):
    base = dict(filename=fn, kind="full", cycle_id="2026-07-19",
                date_started="2026-07-19T02:00:00",
                date_finished="2026-07-19T03:00:00", size_bytes=1, status="ok",
                hostname="h", plan_name="system", shard_group="2026-07-19_full")
    base.update(kw)
    return manifestlib.ArchiveEntry(**base)


def _setup(tmp_path, entry):
    m = manifestlib.Manifest(plan_name="system", archives=[entry])
    manifestlib.save(m, manifestlib.manifest_path(tmp_path))
    return argparse.Namespace(quiet=True, verify="2026-07-19_full")


def _plan_nonsystem():
    p = defaults_system()
    p.plan_name = "home"          # not system-class -> no escalation attempted
    return p


def test_clean_verify_clears_a_stale_corrupt_flag(tmp_path, monkeypatch, capsys):
    """The remedy for the false positive."""
    e = _entry("2026-07-19_full.pax.zst", status="corrupt", corrupt_frames=1)
    args = _setup(tmp_path, e)
    monkeypatch.setattr(worker, "_save_manifest",
                        lambda m, d, p: manifestlib.save(m, manifestlib.manifest_path(d)))

    worker._reconcile_after_verify(args, _plan_nonsystem(), tmp_path,
                                   {"2026-07-19_full.pax.zst": True})

    got = manifestlib.load(manifestlib.manifest_path(tmp_path)).archives[0]
    assert got.status == "ok"
    assert got.corrupt_frames == 0
    assert got.verify_state == "verified"
    assert "cleared corrupt flag" in got.notes
    assert "corrupt -> ok" in capsys.readouterr().out


def test_confirmed_corruption_is_recorded_when_manifest_said_ok(tmp_path, monkeypatch):
    """Reconciliation works in both directions."""
    e = _entry("2026-07-19_full.pax.zst")
    args = _setup(tmp_path, e)
    monkeypatch.setattr(worker, "_save_manifest",
                        lambda m, d, p: manifestlib.save(m, manifestlib.manifest_path(d)))

    worker._reconcile_after_verify(args, _plan_nonsystem(), tmp_path,
                                   {"2026-07-19_full.pax.zst": False})

    got = manifestlib.load(manifestlib.manifest_path(tmp_path)).archives[0]
    assert got.status == "corrupt"


def test_no_disagreement_writes_nothing(tmp_path, monkeypatch):
    """A plain clean verify of an already-clean entry must not touch the
    manifest — and so must never provoke a password prompt."""
    e = _entry("2026-07-19_full.pax.zst", verify_state="verified")
    args = _setup(tmp_path, e)
    saved = {"n": 0}
    monkeypatch.setattr(worker, "_save_manifest",
                        lambda *a: saved.__setitem__("n", saved["n"] + 1))

    rc = worker._reconcile_after_verify(args, _plan_nonsystem(), tmp_path,
                                        {"2026-07-19_full.pax.zst": True})
    assert rc is None
    assert saved["n"] == 0


def test_system_plan_escalates_only_when_there_is_a_change(tmp_path, monkeypatch):
    e = _entry("2026-07-19_full.pax.zst", status="corrupt", corrupt_frames=1)
    args = _setup(tmp_path, e)
    monkeypatch.setattr(worker, "_needs_root_escalation", lambda p: True)
    seen = {}

    def fake_pkexec(a, p, flag, ident):
        seen["c"] = (flag, ident)
        return 0

    monkeypatch.setattr(worker, "_maint_via_pkexec", fake_pkexec)

    rc = worker._reconcile_after_verify(args, defaults_system(), tmp_path,
                                        {"2026-07-19_full.pax.zst": True})
    assert rc == 0
    assert seen["c"] == ("--verify", "2026-07-19_full")


def test_system_plan_does_not_escalate_when_nothing_changed(tmp_path, monkeypatch):
    e = _entry("2026-07-19_full.pax.zst", verify_state="verified")
    args = _setup(tmp_path, e)
    monkeypatch.setattr(worker, "_needs_root_escalation", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("must not prompt for a no-op verify")

    monkeypatch.setattr(worker, "_maint_via_pkexec", boom)
    assert worker._reconcile_after_verify(args, defaults_system(), tmp_path,
                                          {"2026-07-19_full.pax.zst": True}) is None


def test_unwritable_manifest_explains_instead_of_crashing(tmp_path, monkeypatch, capsys):
    e = _entry("2026-07-19_full.pax.zst", status="corrupt", corrupt_frames=1)
    args = _setup(tmp_path, e)
    monkeypatch.setattr(worker, "_needs_root_escalation", lambda p: False)

    def denied(*a, **k):
        raise OSError("Permission denied")

    monkeypatch.setattr(worker, "_save_manifest", denied)
    rc = worker._reconcile_after_verify(args, _plan_nonsystem(), tmp_path,
                                        {"2026-07-19_full.pax.zst": True})
    assert rc is None
    assert "sudo timetraveller-backup" in capsys.readouterr().err


def test_maintain_helper_allows_verify():
    """The reconciliation path needs --verify through the pkexec helper."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    path = os.path.join(root, "libexec", "timetraveller-maintain-system-archive")
    loader = SourceFileLoader("ttmaint", path)
    spec = importlib.util.spec_from_loader("ttmaint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.validate_action("--verify")
    with pytest.raises(SystemExit):
        mod.validate_action("--prune")
