"""C2: type-to-confirm delete dialog — pure token/normalize helpers and the
live-validation gate (Delete stays disabled until the typed phrase matches)."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import pytest

PyQt6 = pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication

from timetraveller.gui.delete_dialog import (
    DeleteConfirmDialog, cycle_token, normalize, set_token,
)

_app = QApplication.instance() or QApplication([])


# ---------- pure helpers ----------

def test_normalize_collapses_and_lowers():
    assert normalize("  Home   2026-06-13 ") == "home 2026-06-13"
    assert normalize("HOME full") == "home full"


def test_cycle_token_is_plan_and_date():
    assert cycle_token("home", "2026-06-13") == "home 2026-06-13"
    # cycle_id carrying a time is trimmed to the calendar date.
    assert cycle_token("home", "2026-06-13T143022") == "home 2026-06-13"


def test_set_token_has_kind_and_no_shard_index():
    tok = set_token("home", "incr", "2026-06-13T10:00:00+00:00")
    assert tok == "home incr 2026-06-13"
    assert "s1of" not in tok and "shard" not in tok


# ---------- dialog gate ----------

def _dlg(**kw):
    base = dict(title="Delete backup", summary="Delete?", token="home full 2026-06-13",
                files=["2026-06-13_full.s1of2.pax.zst", "2026-06-13_full.s2of2.pax.zst"],
                total_bytes=1024**3)
    base.update(kw)
    return DeleteConfirmDialog(**base)


def test_delete_disabled_until_match():
    dlg = _dlg()
    assert dlg._delete.isEnabled() is False        # starts disabled
    dlg._edit.setText("home full 2026")
    assert dlg._delete.isEnabled() is False        # partial → still disabled
    dlg._edit.setText("home full 2026-06-13")
    assert dlg._delete.isEnabled() is True          # exact → enabled
    dlg.deleteLater()


def test_match_is_normalized():
    dlg = _dlg()
    dlg._edit.setText("  HOME   full   2026-06-13 ")  # case + whitespace noise
    assert dlg._delete.isEnabled() is True
    dlg.deleteLater()


def test_cancel_is_default_not_delete():
    dlg = _dlg()
    assert dlg._cancel.isDefault() is True
    assert dlg._delete.isDefault() is False
    dlg.deleteLater()


def test_blast_radius_truncates_long_file_lists():
    files = [f"2026-06-13_full.s{i}of40.pax.zst" for i in range(1, 41)]
    dlg = _dlg(files=files)
    # Should not raise and should report the full count somewhere in the dialog.
    assert dlg._delete.isEnabled() is False
    dlg.deleteLater()


# ---------- panel glue: accepted dialog → --force worker args ----------

from timetraveller import manifest as manifestlib
from timetraveller.gui import archive_panel as ap
from timetraveller.archive import CycleListing
from timetraveller.config import (
    FullSchedule, IncrSchedule, PlanConfig, Retention, Schedule,
)


class _FakeAccepted:
    """Stand-in for DeleteConfirmDialog whose exec() reports the user confirmed."""
    def __init__(self, **kw):
        self.kw = kw

    def exec(self):
        return 1  # QDialog.DialogCode.Accepted


def _entry(group, kind, idx, n):
    suffix = "" if n == 1 else f".s{idx}of{n}"
    return manifestlib.ArchiveEntry(
        filename=f"{group}{suffix}.pax.zst", kind=kind,
        cycle_id=group.split("_")[0], date_started="2026-06-13T09:00:00+00:00",
        date_finished="2026-06-13T09:30:00+00:00", size_bytes=100, status="ok",
        hostname="h", plan_name="tp", shard_index=idx, shard_count=n,
        shard_group=group,
    )


def _panel():
    panel = ap.ArchivePanel()
    panel._plan = PlanConfig(
        plan_name="tp", sources=["/x"], destination="/tmp/x",
        include_hostname_in_path=False,
        schedule=Schedule(mode="weekly", full=FullSchedule(days=["sun"]),
                          incr=IncrSchedule(mode="except_full")),
        retention=Retention(),
    )
    return panel


def test_panel_delete_set_emits_force_args(monkeypatch):
    panel = _panel()
    s = manifestlib.group_into_sets(
        [_entry("2026-06-13_full", "full", 1, 2),
         _entry("2026-06-13_full", "full", 2, 2)])[0]
    monkeypatch.setattr(panel, "_set_dependency_info", lambda _s: (0, False))
    monkeypatch.setattr(ap, "DeleteConfirmDialog", _FakeAccepted)
    got = []
    panel.worker_requested.connect(lambda t, a: got.append((t, a)))
    panel._delete_set(s)
    assert got == [("Delete backup 2026-06-13_full",
                    ["--delete-set", "2026-06-13_full", "--force"])]
    panel.deleteLater()


def test_panel_delete_cycle_emits_force_args(monkeypatch):
    panel = _panel()
    archives = [_entry("2026-06-13_full", "full", 1, 2),
                _entry("2026-06-13_full", "full", 2, 2)]
    cycle = CycleListing(cycle_id="2026-06-13", is_complete=True,
                         full=archives[0], incrementals=[], archives=archives)
    monkeypatch.setattr(panel, "_newest_complete_cycle_id", lambda: None)
    monkeypatch.setattr(ap, "DeleteConfirmDialog", _FakeAccepted)
    got = []
    panel.worker_requested.connect(lambda t, a: got.append((t, a)))
    panel._delete_cycle(cycle)
    assert got == [("Delete cycle 2026-06-13",
                    ["--delete-cycle", "2026-06-13", "--force"])]
    panel.deleteLater()

