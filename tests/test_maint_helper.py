"""Input-validation tests for the pkexec archive-maintenance helper
(--reindex / --recover-failed). Functions called directly, bypassing geteuid.
"""

from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HELPER = os.path.join(REPO_ROOT, "libexec", "timetraveller-maintain-system-archive")


def _import_helper():
    loader = SourceFileLoader("ttmaint", HELPER)
    spec = importlib.util.spec_from_loader("ttmaint", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_plan_and_action_allowlists():
    mod = _import_helper()
    mod.validate_plan("system")
    mod.validate_plan("homes")
    with pytest.raises(SystemExit):
        mod.validate_plan("home")
    mod.validate_action("--reindex")
    mod.validate_action("--recover-failed")
    for bad in ("--delete-cycle", "--kind", "--reindex-all"):
        with pytest.raises(SystemExit):
            mod.validate_action(bad)


def test_ident_accepts_archive_filenames():
    mod = _import_helper()
    for good in ("2026-06-14_full.s1of4.pax.zst",
                 "2026-06-14T132920_full.s8of8.pax.zst",
                 "2026-06-06_incr.pax.zst"):
        mod.validate_ident("--recover-failed", good)
        mod.validate_ident("--reindex", good)


def test_ident_star_only_for_reindex():
    mod = _import_helper()
    mod.validate_ident("--reindex", "*")            # ok
    with pytest.raises(SystemExit):
        mod.validate_ident("--recover-failed", "*")  # not allowed


def test_ident_rejects_traversal_and_non_archives():
    mod = _import_helper()
    bad = [
        "../../etc/passwd",
        "../2026-06-14_full.s1of4.pax.zst",      # leading ..
        "a/b.pax.zst",                            # slash
        "2026-06-14_full.s1of4.pax.zst.failed",  # wrong suffix
        "manifest.json",                          # not an archive
        "2026-06-14_full",                        # stem, no extension
        "..pax.zst",                              # contains ..
        "",
    ]
    for b in bad:
        with pytest.raises(SystemExit):
            mod.validate_ident("--recover-failed", b)


def test_build_command_shape(monkeypatch):
    mod = _import_helper()
    monkeypatch.setattr(mod, "canonical_binary", lambda: "/usr/bin/timetraveller-backup")
    monkeypatch.setattr(mod.os.path, "isfile", lambda p: True)
    cmd = mod.build_command("system", "--recover-failed", "2026-06-14_full.s3of4.pax.zst")
    assert cmd == [
        "/usr/bin/timetraveller-backup", "--plan", "system",
        "--config", "/etc/timetraveller/system.yaml",
        "--recover-failed", "2026-06-14_full.s3of4.pax.zst",
    ]
    cmd = mod.build_command("homes", "--reindex", "*")
    assert cmd[-2:] == ["--reindex", "*"]
