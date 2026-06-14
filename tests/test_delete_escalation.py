"""The worker routes system-class deletes through the pkexec helper as a
non-root user, and does the delete directly when already root."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import worker
from timetraveller.config import defaults_system


def _home_plan():
    p = defaults_system()
    p.plan_name = "home"   # a user-class plan name (not in SYSTEM_PLAN_NAMES)
    return p


def test_needs_escalation_for_system_as_nonroot(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    assert worker._needs_root_escalation(defaults_system()) is True


def test_no_escalation_when_already_root(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    assert worker._needs_root_escalation(defaults_system()) is False


def test_no_escalation_for_user_plan(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    assert worker._needs_root_escalation(_home_plan()) is False


def test_delete_cycle_routes_to_pkexec_for_system(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    seen = {}

    def fake_pkexec(args, plan, action_flag, ident):
        seen["call"] = (plan.plan_name, action_flag, ident)
        return 0

    monkeypatch.setattr(worker, "_delete_via_pkexec", fake_pkexec)
    args = argparse.Namespace(delete_cycle="2026-06-14", force=True)
    rc = worker.action_delete_cycle(args, defaults_system())
    assert rc == 0
    assert seen["call"] == ("system", "--delete-cycle", "2026-06-14")


def test_delete_set_routes_to_pkexec_for_system(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    seen = {}

    def fake_pkexec(a, p, flag, ident):
        seen["c"] = (flag, ident)
        return 0

    monkeypatch.setattr(worker, "_delete_via_pkexec", fake_pkexec)
    args = argparse.Namespace(delete_set="2026-06-14_full", force=True)
    rc = worker.action_delete_set(args, defaults_system())
    assert rc == 0
    assert seen["c"] == ("--delete-set", "2026-06-14_full")


def test_pkexec_forwards_force_only_when_set(monkeypatch):
    """--force reaches the helper iff the caller passed it (preserves the
    newest-complete-cycle guard for un-forced ad-hoc CLI deletes)."""
    captured = {}
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(worker, "_sync_mirror_from_mount", lambda plan: None)
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    args = argparse.Namespace(delete_cycle="2026-06-14", force=False, quiet=True)
    worker._delete_via_pkexec(args, defaults_system(), "--delete-cycle", "2026-06-14")
    assert "--force" not in captured["cmd"]

    args = argparse.Namespace(delete_cycle="2026-06-14", force=True, quiet=True)
    worker._delete_via_pkexec(args, defaults_system(), "--delete-cycle", "2026-06-14")
    assert captured["cmd"][-1] == "--force"


# ---------- backup escalation ----------

def test_backup_routes_to_pkexec_for_system(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    seen = {}

    def fake_backup(a, p):
        seen["plan"] = p.plan_name
        return 0

    monkeypatch.setattr(worker, "_backup_via_pkexec", fake_backup)
    args = argparse.Namespace(kind="full", manual=True)
    rc = worker.action_backup(args, defaults_system())
    assert rc == 0
    assert seen["plan"] == "system"


def test_backup_does_not_escalate_for_user_plan(monkeypatch):
    """A user-class plan must NOT route to pkexec (it would run the real backup;
    we only assert the escalation branch is skipped)."""
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    called = {"pkexec": False}
    monkeypatch.setattr(worker, "_backup_via_pkexec",
                        lambda a, p: called.__setitem__("pkexec", True) or 0)
    assert worker._needs_root_escalation(_home_plan()) is False
    assert called["pkexec"] is False


def test_backup_pkexec_forwards_kind_and_manual(monkeypatch):
    captured = {}
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(worker, "_sync_mirror_from_mount", lambda plan: None)

    class _R:
        returncode = 0

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.update(cmd=cmd) or _R())

    args = argparse.Namespace(kind="incr", manual=True, quiet=True)
    worker._backup_via_pkexec(args, defaults_system())
    assert captured["cmd"][:3] == ["pkexec", worker.BACKUP_HELPER_PATH, "system"]
    assert captured["cmd"][3:5] == ["--kind", "incr"]
    assert captured["cmd"][-1] == "--manual"

    captured.clear()
    args = argparse.Namespace(kind="full", manual=False, quiet=True)
    worker._backup_via_pkexec(args, defaults_system())
    assert "--manual" not in captured["cmd"]


# ---------- reindex / recover-failed escalation ----------

def test_reindex_routes_to_pkexec_for_system(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    seen = {}

    def fake_maint(a, p, flag, ident):
        seen["c"] = (p.plan_name, flag, ident)
        return 0

    monkeypatch.setattr(worker, "_maint_via_pkexec", fake_maint)
    args = argparse.Namespace(reindex="2026-06-14_full.s1of8.pax.zst")
    rc = worker.action_reindex(args, defaults_system())
    assert rc == 0
    assert seen["c"] == ("system", "--reindex", "2026-06-14_full.s1of8.pax.zst")


def test_recover_failed_routes_to_pkexec_for_system(monkeypatch):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    seen = {}

    def fake_maint(a, p, flag, ident):
        seen["c"] = (flag, ident)
        return 0

    monkeypatch.setattr(worker, "_maint_via_pkexec", fake_maint)
    args = argparse.Namespace(recover_failed="2026-06-14_full.s3of4.pax.zst", force=False)
    rc = worker.action_recover_failed(args, defaults_system())
    assert rc == 0
    assert seen["c"] == ("--recover-failed", "2026-06-14_full.s3of4.pax.zst")


def test_maint_pkexec_command_shape(monkeypatch):
    captured = {}
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(worker, "_sync_mirror_from_mount", lambda plan: None)

    class _R:
        returncode = 0

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.update(cmd=cmd) or _R())
    args = argparse.Namespace(quiet=True)
    worker._maint_via_pkexec(args, defaults_system(), "--reindex", "*")
    assert captured["cmd"] == ["pkexec", worker.MAINT_HELPER_PATH, "system", "--reindex", "*"]
