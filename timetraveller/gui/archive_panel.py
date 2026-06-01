"""Archive browser panel.

Layout: a QSplitter with two panes —
  Left: a QTreeWidget grouped by cycle, showing archives with status badges.
  Right: a QTreeView of the selected archive's contents (from its .idx.zst sidecar).
Bottom: 'Selected: N paths' + 'Extract selected...' button.

The panel reads exclusively from the local manifest mirror and the local
sidecar mirror — it never blocks the Qt thread on the backup mount.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QModelIndex, QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QTreeView, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..archive import CycleListing, IndexNode, list_from_manifest, load_sidecar_tree
from ..config import PlanConfig
from ..index import sidecar_mirror_path
from ..manifest import ArchiveEntry
from .archive_tree_model import ArchiveTreeModel
from .reindex_tracker import ReindexTracker
from .restore_dialog import RestoreDialog
from .search_widget import SearchWidget


class _SidecarLoadWorker(QObject):
    """Loads + parses an archive's .idx.zst sidecar off the Qt UI thread.

    Sidecars for million-entry archives take a couple seconds to decompress
    and JSON-parse; doing that on the UI thread freezes the window long
    enough for the compositor to flag it as "Not Responding."
    """

    loaded = pyqtSignal(str, object)   # (archive_filename, IndexNode root)
    failed = pyqtSignal(str, str)      # (archive_filename, error message)

    def __init__(self, archive_filename: str, sidecar_path: Path):
        super().__init__()
        self._archive_filename = archive_filename
        self._sidecar_path = sidecar_path

    def run(self) -> None:
        try:
            root = load_sidecar_tree(self._sidecar_path)
        except Exception as exc:  # noqa: BLE001 - surface every failure
            self.failed.emit(self._archive_filename, f"{type(exc).__name__}: {exc}")
            return
        self.loaded.emit(self._archive_filename, root)


class ArchivePanel(QWidget):
    """Browse and restore from archives belonging to a plan."""

    restore_requested = pyqtSignal()  # emitted after a successful extract

    def __init__(self, parent=None, *, tracker: ReindexTracker | None = None):
        super().__init__(parent)
        self._plan: PlanConfig | None = None
        self._current_entry: ArchiveEntry | None = None
        self._tree_model: ArchiveTreeModel | None = None
        self._load_thread: QThread | None = None
        self._load_worker: _SidecarLoadWorker | None = None
        # Tracks the archive whose sidecar load is currently in flight, so we
        # can ignore late-arriving results from a previous selection.
        self._pending_archive: str | None = None
        self._tracker = tracker

        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: archive list.
        self._archive_list = QTreeWidget()
        self._archive_list.setHeaderLabels(["Archive", "Size", "Status"])
        self._archive_list.setRootIsDecorated(True)
        self._archive_list.setMinimumWidth(360)
        self._archive_list.itemSelectionChanged.connect(self._on_archive_selection)
        splitter.addWidget(self._archive_list)

        # Right: a stack with two pages —
        #   page 0 (browse): file tree of the selected archive
        #   page 1 (search): cross-archive file search
        # A "Search files…" button on the browse header swaps the stack
        # to page 1; SearchWidget's close button / Esc swaps it back.
        self._right_stack = QStackedWidget()

        # ----- browse page (page 0) -----
        browse_page = QWidget()
        rv = QVBoxLayout(browse_page)
        rv.setContentsMargins(0, 0, 0, 0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._archive_label = QLabel("(no archive selected)")
        self._archive_label.setStyleSheet("padding: 4px;")
        header_row.addWidget(self._archive_label, 1)
        self._reindex_btn = QPushButton("Reindex")
        self._reindex_btn.setVisible(False)
        self._reindex_btn.clicked.connect(self._on_reindex_clicked)
        header_row.addWidget(self._reindex_btn)
        self._search_btn = QPushButton("🔍 Search files…")
        self._search_btn.setToolTip(
            "Find a filename across every archive in this plan"
        )
        self._search_btn.clicked.connect(self._enter_search_mode)
        header_row.addWidget(self._search_btn)
        rv.addLayout(header_row)

        if self._tracker is not None:
            self._tracker.started.connect(self._on_reindex_started)
            self._tracker.finished.connect(self._on_reindex_finished)

        self._file_tree = QTreeView()
        self._file_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._file_tree.setUniformRowHeights(True)
        rv.addWidget(self._file_tree, 1)
        self._right_stack.addWidget(browse_page)

        # ----- search page (page 1) -----
        self._search_widget = SearchWidget()
        self._search_widget.back_requested.connect(self._exit_search_mode)
        self._search_widget.result_activated.connect(self._on_search_result_activated)
        self._right_stack.addWidget(self._search_widget)

        splitter.addWidget(self._right_stack)
        splitter.setStretchFactor(1, 1)

        # When a sidecar load completes for an archive that the user just
        # navigated to via search, scroll/highlight this path after the
        # tree model is populated.
        self._pending_highlight_path: str | None = None

        # Bottom toolbar.
        bottom = QHBoxLayout()
        self._selection_label = QLabel("Selected: 0 paths")
        bottom.addWidget(self._selection_label)
        bottom.addStretch(1)
        self._extract_btn = QPushButton("Extract selected…")
        self._extract_btn.setEnabled(False)
        self._extract_btn.clicked.connect(self._on_extract)
        bottom.addWidget(self._extract_btn)
        layout.addLayout(bottom)

    # ---------- plan switching ----------

    def load_plan(self, plan: PlanConfig) -> None:
        self._plan = plan
        self.refresh()

    def refresh(self) -> None:
        """Reload the archive list from the local manifest mirror.

        Touches no mount-backed path. If the mirror is empty (never refreshed),
        the panel shows a placeholder pointing the user at the CLI.
        """
        self._archive_list.clear()
        self._set_file_tree(None)
        if not self._plan:
            self._search_widget.set_plan("", [])
            return
        listing = list_from_manifest(self._plan.plan_name, self._plan.archive_dir())
        all_entries: list[ArchiveEntry] = []
        for c in listing.cycles:
            if c.full is not None:
                all_entries.append(c.full)
            all_entries.extend(c.incrementals)
        self._search_widget.set_plan(self._plan.plan_name, all_entries)
        if not listing.cycles:
            placeholder = QTreeWidgetItem([
                "(no archives in local mirror — run "
                "`timetraveller-backup --plan <name> --list-archives --refresh-from-mount`)",
                "", "",
            ])
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._archive_list.addTopLevelItem(placeholder)
            return
        for cycle in listing.cycles:
            self._add_cycle(cycle)
        self._archive_list.expandAll()
        for col in range(self._archive_list.columnCount()):
            self._archive_list.resizeColumnToContents(col)

    def _add_cycle(self, cycle: CycleListing) -> None:
        status = "complete" if cycle.is_complete else "INCOMPLETE"
        top = QTreeWidgetItem([f"Cycle {cycle.cycle_id}", "", status])
        if not cycle.is_complete:
            top.setForeground(0, _color("#cc8000"))
        self._archive_list.addTopLevelItem(top)

        members: list = []
        if cycle.full is not None:
            members.append(cycle.full)
        members.extend(cycle.incrementals)
        for entry in members:
            size_str = _human(entry.size_bytes)
            status_str = entry.status
            child = QTreeWidgetItem([f"{entry.kind}  {entry.filename}", size_str, status_str])
            child.setData(0, Qt.ItemDataRole.UserRole, entry)
            if entry.status == "failed":
                child.setForeground(0, _color("#cf222e"))
                child.setForeground(2, _color("#cf222e"))
            elif entry.status == "empty":
                child.setForeground(0, _color("#6e7781"))
            elif entry.status == "orphan":
                child.setForeground(0, _color("#cc8000"))
            top.addChild(child)

    # ---------- file tree ----------

    def _on_archive_selection(self) -> None:
        items = self._archive_list.selectedItems()
        entry: ArchiveEntry | None = None
        if items:
            data = items[0].data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, ArchiveEntry):
                entry = data
        if entry is None:
            self._set_file_tree(None)
            self._pending_archive = None
            return
        if self._current_entry and entry.filename == self._current_entry.filename:
            return

        if not self._plan:
            return

        if not entry.has_sidecar:
            self._show_no_sidecar(entry)
            self._set_file_tree(None)
            self._pending_archive = None
            return

        # Sidecar is recorded; hide the button used for the missing-sidecar
        # warning so it doesn't linger over a healthy archive's pane.
        self._reindex_btn.setVisible(False)

        sc = sidecar_mirror_path(self._plan.plan_name, entry.filename)
        if not sc.exists():
            self._archive_label.setText(
                f"<b>{entry.filename}</b> — sidecar missing from local mirror "
                f"(run <code>--list-archives --refresh-from-mount</code>)"
            )
            self._set_file_tree(None)
            self._pending_archive = None
            return

        # Kick the actual sidecar load onto a worker thread so the UI stays
        # responsive even on multi-million-entry sidecars.
        self._archive_label.setText(
            f"<b>{entry.filename}</b> &nbsp; — &nbsp; {_human(entry.size_bytes)} &nbsp; "
            f"<i>(loading file tree…)</i>"
        )
        self._set_file_tree(None)
        self._pending_archive = entry.filename
        # If a previous load is still running, let it complete; we'll filter
        # its result out via _pending_archive when it arrives.
        self._load_thread = QThread(self)
        self._load_worker = _SidecarLoadWorker(entry.filename, sc)
        self._load_worker.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._load_worker.run)
        self._load_worker.loaded.connect(self._on_sidecar_loaded)
        self._load_worker.failed.connect(self._on_sidecar_failed)
        self._load_worker.loaded.connect(self._load_thread.quit)
        self._load_worker.failed.connect(self._load_thread.quit)
        self._load_thread.finished.connect(self._load_worker.deleteLater)
        self._load_thread.finished.connect(self._load_thread.deleteLater)
        # Stash the entry so _on_sidecar_loaded can promote it to current.
        self._loading_entry = entry
        self._load_thread.start()

    def _on_sidecar_loaded(self, archive_filename: str, root: IndexNode) -> None:
        # Drop the result if the user clicked another archive while this one
        # was loading — the more recent click set a different _pending_archive.
        if archive_filename != self._pending_archive:
            return
        entry = self._loading_entry
        self._archive_label.setText(
            f"<b>{entry.filename}</b> &nbsp; — &nbsp; {_human(entry.size_bytes)} &nbsp; "
            f"({root.total_entries() - 1} entries)"
        )
        self._current_entry = entry
        self._set_file_tree(root)
        self._pending_archive = None
        # If the selection was driven by a search-result click, scroll the
        # newly-loaded tree to that path now that the model is in place.
        if self._pending_highlight_path is not None:
            self._scroll_to_path(self._pending_highlight_path)
            self._pending_highlight_path = None

    def _on_sidecar_failed(self, archive_filename: str, msg: str) -> None:
        if archive_filename != self._pending_archive:
            return
        sc = sidecar_mirror_path(self._plan.plan_name, archive_filename) if self._plan else "(?)"
        QMessageBox.warning(self, "Could not read sidecar", f"{sc}\n\n{msg}")
        self._set_file_tree(None)
        self._pending_archive = None

    # ---------- reindex button ----------

    def _show_no_sidecar(self, entry: ArchiveEntry) -> None:
        """Render the no-sidecar warning, swapping in the running/idle state
        from the tracker. Failed-status archives never get a Reindex button
        because their archive file is at the .failed suffix on disk and the
        worker's --reindex would fail to open it.
        """
        plan_name = self._plan.plan_name if self._plan else ""
        running = (self._tracker.running_for(plan_name, entry.filename)
                   if self._tracker else None)
        if running is not None:
            self._archive_label.setText(
                f"<b>{entry.filename}</b> — no sidecar index (Indexing now)"
            )
            self._reindex_btn.setVisible(False)
            return
        if entry.status == "failed":
            self._archive_label.setText(
                f"<b>{entry.filename}</b> — backup failed; archive incomplete"
            )
            self._reindex_btn.setVisible(False)
            return
        self._archive_label.setText(
            f"<b>{entry.filename}</b> — no sidecar index"
        )
        self._reindex_btn.setVisible(self._tracker is not None)

    def _selected_archive_entry(self) -> ArchiveEntry | None:
        items = self._archive_list.selectedItems()
        if not items:
            return None
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, ArchiveEntry) else None

    def _on_reindex_clicked(self) -> None:
        if self._plan is None or self._tracker is None:
            return
        entry = self._selected_archive_entry()
        if entry is None or entry.has_sidecar:
            return
        handle = self._tracker.start(self._plan.plan_name, entry.filename)
        if handle is None:
            QMessageBox.warning(
                self, "Reindex failed to launch",
                f"Could not start reindex for {entry.filename}.",
            )
            return
        # Repaint immediately for the user who just clicked; the tracker's
        # `started` signal will also fire and is idempotent for this archive.
        self._show_no_sidecar(entry)

    def _on_reindex_started(self, plan_name: str, archive: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        entry = self._selected_archive_entry()
        if entry is not None and entry.filename == archive and not entry.has_sidecar:
            self._show_no_sidecar(entry)

    def _on_reindex_finished(self, plan_name: str, archive: str,
                             ok: bool, log_tail: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        if not ok:
            QMessageBox.warning(
                self, f"Reindex failed: {archive}",
                f"{archive}\n\n{log_tail or '(no output captured)'}",
            )
            # Restore the idle warning so the button reappears for retry.
            entry = self._selected_archive_entry()
            if entry is not None and entry.filename == archive and not entry.has_sidecar:
                self._show_no_sidecar(entry)
            return
        # Success: --reindex flipped has_sidecar in the manifest mirror;
        # reload the listing and re-select the archive so the file tree
        # loads automatically.
        was_selected = self._selected_archive_entry()
        target = archive if (was_selected and was_selected.filename == archive) else None
        self.refresh()
        if target is not None:
            self._select_archive(target)

    def _select_archive(self, filename: str) -> None:
        for i in range(self._archive_list.topLevelItemCount()):
            top = self._archive_list.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, ArchiveEntry) and data.filename == filename:
                    # Clearing _current_entry forces _on_archive_selection
                    # to re-render rather than bailing on the same-entry guard.
                    self._current_entry = None
                    self._archive_list.setCurrentItem(child)
                    return

    # ---------- search ----------

    def _enter_search_mode(self) -> None:
        self._right_stack.setCurrentIndex(1)
        self._search_widget.focus_input()

    def _exit_search_mode(self) -> None:
        self._right_stack.setCurrentIndex(0)

    def _on_search_result_activated(self, archive: str, path: str) -> None:
        # Stash the highlight target before triggering selection so that
        # the async sidecar load can pick it up on completion.
        self._pending_highlight_path = path
        self._exit_search_mode()
        if self._current_entry is not None and self._current_entry.filename == archive:
            # Tree already loaded — scroll now, no need to wait for async load.
            self._scroll_to_path(path)
            self._pending_highlight_path = None
            return
        self._select_archive(archive)

    def _scroll_to_path(self, path: str) -> None:
        """Locate `path` (e.g. './home/kim/recipe.md') in the loaded tree
        and scroll/highlight it. Expands every ancestor along the way."""
        if self._tree_model is None:
            return
        idx = self._index_for_path(path)
        if not idx.isValid():
            return
        # Expand ancestors so the leaf is visible.
        cursor = idx.parent()
        ancestors = []
        while cursor.isValid():
            ancestors.append(cursor)
            cursor = cursor.parent()
        for anc in reversed(ancestors):
            self._file_tree.expand(anc)
        self._file_tree.scrollTo(
            idx, QAbstractItemView.ScrollHint.PositionAtCenter
        )
        sel = self._file_tree.selectionModel()
        if sel is not None:
            sel.setCurrentIndex(
                idx,
                sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows,
            )

    def _index_for_path(self, path: str) -> QModelIndex:
        """Walk the model from the root, matching each path component
        against children by name. Returns invalid QModelIndex on miss.
        """
        if self._tree_model is None:
            return QModelIndex()
        rel = path[2:] if path.startswith("./") else path.lstrip("/")
        rel = rel.rstrip("/")
        if not rel:
            return QModelIndex()
        parent_idx = QModelIndex()
        for part in rel.split("/"):
            found = QModelIndex()
            for row in range(self._tree_model.rowCount(parent_idx)):
                child_idx = self._tree_model.index(row, 0, parent_idx)
                node = self._tree_model.node_at(child_idx)
                if node is not None and node.name == part:
                    found = child_idx
                    break
            if not found.isValid():
                return QModelIndex()
            parent_idx = found
        return parent_idx

    def _set_file_tree(self, root: IndexNode | None) -> None:
        if root is None:
            self._file_tree.setModel(None)
            self._tree_model = None
            self._selection_label.setText("Selected: 0 paths")
            self._extract_btn.setEnabled(False)
            return
        self._tree_model = ArchiveTreeModel(root, parent=self)
        self._file_tree.setModel(self._tree_model)
        self._file_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._file_tree.setColumnWidth(0, 380)
        sel = self._file_tree.selectionModel()
        if sel is not None:
            sel.selectionChanged.connect(self._on_path_selection)
        self._update_selection_label()

    def _on_path_selection(self, *_args) -> None:
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        n = len(self._selected_member_paths())
        self._selection_label.setText(f"Selected: {n} path{'s' if n != 1 else ''}")
        self._extract_btn.setEnabled(n > 0 and self._current_entry is not None)

    def _selected_member_paths(self) -> list[str]:
        if not self._tree_model:
            return []
        idxs = self._file_tree.selectionModel().selectedRows(0)
        paths: list[str] = []
        for idx in idxs:
            node = self._tree_model.node_at(idx)
            if node is None:
                continue
            paths.append(node.full_path)
        return paths

    # ---------- extract ----------

    def _on_extract(self) -> None:
        if not self._current_entry or not self._tree_model or not self._plan:
            return
        paths = self._selected_member_paths()
        if not paths:
            return
        # Extraction is a deliberate, user-initiated mount touch.
        archive_path = self._plan.archive_dir() / self._current_entry.filename
        dlg = RestoreDialog(archive_path, paths, parent=self)
        dlg.exec()
        # Selection might have been invalidated by user interaction; refresh.
        self._update_selection_label()


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, divisor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < divisor * 1024:
            return f"{n/divisor:.1f} {unit}"
    return f"{n/1024**3:.1f} GB"


def _color(hex_str: str):
    from PyQt6.QtGui import QBrush, QColor
    return QBrush(QColor(hex_str))
