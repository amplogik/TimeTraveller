"""Cross-archive file search for the Archives panel.

UI: a search bar + grouped results tree. Each matching file path is a
top-level row; under it, one row per archive that contains a copy, with
size + mtime at backup time so the user can pick the version with the
content they want. Activating a version row emits `result_activated`;
ArchivePanel listens and navigates the file tree to that path.

Worker thread: scans all of a plan's mirrored sidecars off the UI thread
(see `timetraveller.search`). Cancellation is cooperative — a long sweep
of many sidecars yields to a fresh search after each archive completes.

The widget reads only from the local sidecar mirror, never the NFS mount.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import search as searchlib
from ..index import sidecar_mirror_path
from ..manifest import ArchiveEntry


# Cap on distinct matching paths. A bare-substring search like "log" on a
# million-entry archive could match thousands of files; 500 is enough for
# the user to recognize the cluster and refine, and keeps the tree snappy.
PATH_CAP = 500

# Debounce window for typing — 300 ms is the established sweet spot for
# "I'm done typing" without making the user wait noticeably.
DEBOUNCE_MS = 300


class _SearchWorker(QObject):
    """Runs the search off the UI thread.

    Cancellation is cooperative: the orchestrator sets `_cancelled = True`
    from the main thread; the worker checks between archives. iter_matches
    itself doesn't yield to cancel mid-sidecar, which is acceptable because
    a single sidecar is <1 s to scan.
    """

    progress = pyqtSignal(int, int)       # (scanned, total)
    finished = pyqtSignal(dict, bool)     # ({path: [Match,...]}, truncated)

    def __init__(self, sidecar_paths: list[Path], term: str, mode: str):
        super().__init__()
        self._sidecars = sidecar_paths
        self._term = term
        self._mode = mode
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        results: dict[str, list[searchlib.Match]] = {}
        total = len(self._sidecars)
        for i, sc in enumerate(self._sidecars):
            if self._cancelled:
                break
            try:
                for m in searchlib.iter_matches(sc, self._term, mode=self._mode):
                    results.setdefault(m.path, []).append(m)
                    if len(results) >= PATH_CAP:
                        self.finished.emit(results, True)
                        return
            except OSError:
                # Sidecar unreadable — skip with no surface noise. The
                # broken file is visible in the archive panel's no-sidecar
                # warning if the user cares to investigate.
                pass
            self.progress.emit(i + 1, total)
        self.finished.emit(results, False)


class SearchWidget(QWidget):
    """Sidecar-driven cross-archive search."""

    # (archive_filename, archive_path) — the path begins with "./" per sidecar
    # convention. ArchivePanel consumes this to navigate its file tree.
    result_activated = pyqtSignal(str, str)
    # Fired when the user dismisses search (close button or Esc). ArchivePanel
    # listens to switch the right-pane stack back to the file-tree view.
    back_requested = pyqtSignal()
    # A list of (archive_filename, archive_path) pairs the user asked to extract
    # straight from the search results. ArchivePanel resolves each to its logical
    # backup's shards and opens the RestoreDialog — same path as the browse-tree
    # "Extract selected…" button, so the file that comes out is the one that was
    # picked here (not whatever happened to be selected in the hidden file tree).
    extract_requested = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan_name = ""
        self._scannable: list[tuple[ArchiveEntry, Path]] = []
        self._entries_by_filename: dict[str, ArchiveEntry] = {}
        self._worker: _SearchWorker | None = None
        self._thread: QThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            f"Find files in this plan's archives… (min {searchlib.MIN_SEARCH_LEN} chars)"
        )
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._on_text_changed)
        bar.addWidget(self._input, 1)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Filename", searchlib.MODE_BASENAME)
        self._mode_combo.addItem("Full path", searchlib.MODE_PATH)
        self._mode_combo.currentIndexChanged.connect(self._kick)
        bar.addWidget(self._mode_combo)

        self._close_btn = QPushButton("✕")
        self._close_btn.setToolTip("Close search (Esc)")
        self._close_btn.setFixedWidth(28)
        self._close_btn.clicked.connect(self.back_requested.emit)
        bar.addWidget(self._close_btn)
        layout.addLayout(bar)

        self._results = QTreeWidget()
        self._results.setHeaderLabels(["Path / archive", "Kind", "Size",
                                       "Modified (at backup)"])
        self._results.setRootIsDecorated(True)
        self._results.setUniformRowHeights(True)
        self._results.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.itemActivated.connect(self._on_item_activated)
        self._results.itemSelectionChanged.connect(self._update_extract_state)
        self._results.setColumnWidth(0, 420)
        layout.addWidget(self._results, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #6e7781; padding: 2px 4px;")
        layout.addWidget(self._status)

        # Extract straight from the results, so the file that comes out is the
        # one selected here. Mirrors the browse tab's bottom "Extract selected…".
        extract_row = QHBoxLayout()
        self._sel_label = QLabel("")
        self._sel_label.setStyleSheet("color: #6e7781; padding: 2px 4px;")
        extract_row.addWidget(self._sel_label)
        extract_row.addStretch(1)
        self._extract_btn = QPushButton("Extract selected…")
        self._extract_btn.setEnabled(False)
        self._extract_btn.setToolTip(
            "Extract the file(s) selected here from the archive that holds them"
        )
        self._extract_btn.clicked.connect(self._on_extract_clicked)
        extract_row.addWidget(self._extract_btn)
        layout.addLayout(extract_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._kick)

    # ---------- public API ----------

    def set_plan(self, plan_name: str, entries: list[ArchiveEntry]) -> None:
        """Hand the widget the plan's manifest entries.

        Builds the scannable list (entries with has_sidecar=True AND a
        mirrored sidecar file actually on disk). If a search is already
        active, re-runs it against the new sidecar set.
        """
        self._plan_name = plan_name
        scannable: list[tuple[ArchiveEntry, Path]] = []
        by_name: dict[str, ArchiveEntry] = {}
        for e in entries:
            by_name[e.filename] = e
            if not e.has_sidecar:
                continue
            sc = sidecar_mirror_path(plan_name, e.filename)
            if sc.exists():
                scannable.append((e, sc))
        self._scannable = scannable
        self._entries_by_filename = by_name
        if self._input.text().strip():
            self._kick()

    def clear(self) -> None:
        """Drop search state and results (e.g. on plan switch)."""
        self._cancel_active()
        self._input.clear()
        self._results.clear()
        self._status.setText("")
        self._update_extract_state()

    def focus_input(self) -> None:
        self._input.setFocus()
        self._input.selectAll()

    def keyPressEvent(self, event):  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.back_requested.emit()
            return
        super().keyPressEvent(event)

    # ---------- internal ----------

    def _on_text_changed(self, _text: str) -> None:
        self._debounce.start()

    def _cancel_active(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        # The thread is allowed to finish naturally; it'll be deleted via
        # the deleteLater connections wired in _kick().

    def _kick(self) -> None:
        self._cancel_active()
        term = self._input.text().strip()
        if not term:
            self._results.clear()
            self._status.setText("")
            return
        if len(term) < searchlib.MIN_SEARCH_LEN:
            self._results.clear()
            self._status.setText(
                f"Type at least {searchlib.MIN_SEARCH_LEN} characters."
            )
            return
        if not self._scannable:
            self._results.clear()
            self._status.setText(
                "No mirrored sidecars to search. Run "
                "`timetraveller-backup --plan <name> --list-archives "
                "--refresh-from-mount` to populate the mirror."
            )
            return

        mode = self._mode_combo.currentData()
        self._results.clear()
        self._status.setText(f"Searching… (0 of {len(self._scannable)})")

        thread = QThread(self)
        worker = _SearchWorker([sc for _, sc in self._scannable], term, mode)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _on_progress(self, scanned: int, total: int) -> None:
        self._status.setText(f"Searching… ({scanned} of {total})")

    def _on_finished(self, results_by_path: dict, truncated: bool) -> None:
        self._worker = None
        self._populate(results_by_path)
        n_paths = len(results_by_path)
        n_versions = sum(len(v) for v in results_by_path.values())
        if truncated:
            self._status.setText(
                f"Showing first {PATH_CAP} matching paths "
                f"({n_versions} versions). Narrow the search for more."
            )
        elif n_paths == 0:
            self._status.setText("No matches.")
        else:
            self._status.setText(
                f"{n_paths} matching path{'s' if n_paths != 1 else ''} "
                f"({n_versions} version{'s' if n_versions != 1 else ''})."
            )

    def _populate(self, results_by_path: dict[str, list[searchlib.Match]]) -> None:
        self._results.clear()
        # Sort paths alphabetically; within a path, sort matches by mtime
        # descending so newest version reads first.
        for path in sorted(results_by_path):
            matches = results_by_path[path]
            matches.sort(key=lambda m: m.mtime, reverse=True)
            top = QTreeWidgetItem([
                path, "", "", f"{len(matches)} version"
                f"{'s' if len(matches) != 1 else ''}",
            ])
            top.setFirstColumnSpanned(False)
            top.setData(0, Qt.ItemDataRole.UserRole, ("path", path))
            self._results.addTopLevelItem(top)
            for m in matches:
                entry = self._entries_by_filename.get(m.archive)
                kind = entry.kind if entry else ""
                cycle = entry.cycle_id if entry else ""
                child_label = m.archive if not cycle else f"{cycle}  {m.archive}"
                child = QTreeWidgetItem([
                    child_label,
                    kind,
                    _human_size(m.size),
                    _fmt_mtime(m.mtime),
                ])
                child.setData(0, Qt.ItemDataRole.UserRole,
                              ("version", m.archive, m.path))
                top.addChild(child)
            top.setExpanded(True)
        # Refit columns to content for the first 3, leave last col fluid.
        for col in (1, 2):
            self._results.resizeColumnToContents(col)
        # Fresh result set starts with nothing selected.
        self._update_extract_state()

    def _on_item_activated(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or data[0] != "version":
            # Top-level path rows: just toggle expansion. Default tree
            # behavior handles that — emit nothing.
            return
        _, archive, path = data
        self.result_activated.emit(archive, path)

    # ---------- extract-from-search ----------

    def _selected_extract_pairs(self) -> list[tuple[str, str]]:
        """The (archive, path) pairs to extract for the current selection.

        A selected *version* row extracts that exact copy. A selected *path*
        row (the parent, with N versions) resolves to its newest version — the
        first child, since `_populate` sorts versions mtime-descending. Pairs
        are de-duplicated, preserving selection order.
        """
        pairs: list[tuple[str, str]] = []
        for item in self._results.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(data, tuple):
                continue
            if data[0] == "version":
                pairs.append((data[1], data[2]))
            elif data[0] == "path" and item.childCount() > 0:
                child = item.child(0).data(0, Qt.ItemDataRole.UserRole)
                if isinstance(child, tuple) and child[0] == "version":
                    pairs.append((child[1], child[2]))
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for pr in pairs:
            if pr not in seen:
                seen.add(pr)
                out.append(pr)
        return out

    def _update_extract_state(self) -> None:
        pairs = self._selected_extract_pairs()
        self._extract_btn.setEnabled(bool(pairs))
        if pairs:
            self._sel_label.setText(
                f"{len(pairs)} file{'s' if len(pairs) != 1 else ''} selected"
            )
        else:
            self._sel_label.setText("")

    def _on_extract_clicked(self) -> None:
        pairs = self._selected_extract_pairs()
        if pairs:
            self.extract_requested.emit(pairs)


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, divisor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < divisor * 1024:
            return f"{n / divisor:.1f} {unit}"
    return f"{n / 1024**3:.1f} GB"


def _fmt_mtime(epoch: int) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
