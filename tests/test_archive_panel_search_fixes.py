"""Regression tests for the v1.5.2 Archives-panel cleanup pass:

  * source-mode search/browse divergence — SearchWidget must read the SAME
    sidecar source the browse tree uses (a resolver), and stat the file rather
    than trust the manifest's has_sidecar flag;
  * the file-tree must not get stranded blank — blanking it clears _current_set
    so a reselect reloads instead of hitting the same-set early-return guard;
  * the bottom (browse) Extract button is hidden while the search page shows,
    so the only exposed extract matches what the user sees highlighted.

Runs headless under the offscreen Qt platform.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6.QtWidgets import QApplication

from timetraveller.archive import IndexNode
from timetraveller.gui.archive_panel import ArchivePanel
from timetraveller.gui.search_widget import SearchWidget
from timetraveller.manifest import ArchiveEntry


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


def _entry(filename: str, *, has_sidecar: bool) -> ArchiveEntry:
    # Only the fields SearchWidget.set_plan touches need to be meaningful.
    return ArchiveEntry(
        filename=filename, kind="full", cycle_id="2026-01-01",
        date_started="2026-01-01T00:00:00", date_finished="2026-01-01T00:00:00",
        size_bytes=1, status="ok", hostname="h", plan_name="plan",
        has_sidecar=has_sidecar,
    )


def test_search_uses_resolver_not_mirror(app, tmp_path):
    """set_plan must scan the sidecars its resolver points at (browse source),
    not the local mirror — this is the source-mode divergence fix."""
    src = tmp_path / "browsed"
    src.mkdir()
    # Real sidecars in the browsed location, named as the panel would resolve.
    (src / "a.pax.zst.idx.zst").write_bytes(b"x")
    (src / "b.pax.zst.idx.zst").write_bytes(b"x")

    def resolve(fn: str) -> Path:
        return src / (fn + ".idx.zst")

    w = SearchWidget()
    entries = [_entry("a.pax.zst", has_sidecar=True),
               _entry("b.pax.zst", has_sidecar=True)]
    w.set_plan("plan", entries, sidecar_for=resolve)

    scanned = {p.name for _, p in w._scannable}
    assert scanned == {"a.pax.zst.idx.zst", "b.pax.zst.idx.zst"}
    assert all(str(src) in str(p) for _, p in w._scannable)


def test_search_stats_file_not_has_sidecar_flag(app, tmp_path):
    """A browsed manifest's has_sidecar can be stale. If the sidecar file is
    really there, it's scannable regardless of the flag; if it's absent, it
    isn't — even when the flag says True."""
    src = tmp_path / "browsed"
    src.mkdir()
    # Present on disk but flag says False (stale-low) -> must be scannable.
    (src / "present.pax.zst.idx.zst").write_bytes(b"x")

    def resolve(fn: str) -> Path:
        return src / (fn + ".idx.zst")

    w = SearchWidget()
    entries = [
        _entry("present.pax.zst", has_sidecar=False),   # flag lies low
        _entry("absent.pax.zst", has_sidecar=True),     # flag lies high
    ]
    w.set_plan("plan", entries, sidecar_for=resolve)

    scanned = {p.name for _, p in w._scannable}
    assert scanned == {"present.pax.zst.idx.zst"}


def test_set_file_tree_none_clears_current_set(app):
    """Blanking the tree must forget the shown backup, or a later reselect of
    the same set hits the same-set early-return guard and stays blank."""
    panel = ArchivePanel()
    root = IndexNode(name="", full_path=".", is_dir=True)
    root.children["etc"] = IndexNode(name="etc", full_path="./etc", is_dir=True)

    panel._set_file_tree(root)
    panel._current_set = object()      # stand in for a loaded ShardSet
    assert panel._tree_model is not None

    panel._set_file_tree(None)
    assert panel._tree_model is None
    assert panel._current_set is None  # the fix: not stranded


def test_bottom_extract_bar_hidden_in_search_mode(app):
    """The browse Extract bar is hidden while the search page shows (search has
    its own Extract button) and restored on exit."""
    panel = ArchivePanel()
    panel.show()  # visibility is only meaningful once shown

    panel._enter_search_mode()
    assert panel._right_stack.currentIndex() == 1
    assert not panel._browse_extract_bar.isVisible()

    panel._exit_search_mode()
    assert panel._right_stack.currentIndex() == 0
    assert panel._browse_extract_bar.isVisible()


def test_exit_search_relayout_is_safe_when_blank(app):
    """Exiting search must not blow up when there's no tree model loaded."""
    panel = ArchivePanel()
    panel._set_file_tree(None)
    panel._exit_search_mode()  # should be a no-op relayout, not a crash
