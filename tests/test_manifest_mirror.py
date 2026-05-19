"""Tests for the on-mount + local-mirror manifest dual-write."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from timetraveller import manifest as manifestlib
from timetraveller.worker import _save_manifest


def _sample_manifest() -> manifestlib.Manifest:
    return manifestlib.Manifest(
        plan_name="testplan",
        archives=[
            manifestlib.ArchiveEntry(
                filename="2026-05-19T120000_full.pax.zst",
                kind="full",
                cycle_id="2026-05-19",
                date_started="2026-05-19T12:00:00+00:00",
                date_finished="2026-05-19T13:00:00+00:00",
                size_bytes=1024,
                status="ok",
                hostname="testhost",
                plan_name="testplan",
            ),
        ],
    )


def test_mirror_path_honors_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    p = manifestlib.mirror_manifest_path("home")
    assert p == tmp_path / "xdg" / "timetraveller" / "home" / "manifest.json"


def test_mirror_path_falls_back_to_local_state(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = manifestlib.mirror_manifest_path("home")
    assert p == tmp_path / ".local" / "state" / "timetraveller" / "home" / "manifest.json"


def test_save_manifest_writes_both(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    archive_dir = tmp_path / "mount" / "plan"
    archive_dir.mkdir(parents=True)

    m = _sample_manifest()
    _save_manifest(m, archive_dir, "testplan")

    on_mount = archive_dir / "manifest.json"
    mirror = tmp_path / "state" / "timetraveller" / "testplan" / "manifest.json"

    assert on_mount.exists(), "on-mount manifest must be written"
    assert mirror.exists(), "mirror manifest must be written"

    assert json.loads(on_mount.read_text()) == json.loads(mirror.read_text()), \
        "on-mount and mirror must have identical content"


def test_save_manifest_mirror_failure_does_not_propagate(tmp_path, monkeypatch, capsys):
    """If the mirror write fails, the on-mount write still succeeds and no
    exception is raised. The backup must not depend on the mirror."""
    # Point XDG_STATE_HOME at a regular file so any path-creation under it
    # raises NotADirectoryError.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))

    archive_dir = tmp_path / "mount" / "plan"
    archive_dir.mkdir(parents=True)

    m = _sample_manifest()
    _save_manifest(m, archive_dir, "testplan")  # must not raise

    on_mount = archive_dir / "manifest.json"
    assert on_mount.exists(), "on-mount manifest must still be written"

    captured = capsys.readouterr()
    assert "manifest mirror write failed" in captured.err
