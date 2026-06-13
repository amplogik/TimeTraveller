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
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMenu, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTreeView,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import manifest as manifestlib
from ..archive import (
    CycleListing, IndexNode, list_from_manifest, load_sidecar_tree,
    merge_sidecar_trees,
)
from ..config import PlanConfig
from ..index import sidecar_mirror_path
from ..manifest import ArchiveEntry
from .archive_tree_model import ArchiveTreeModel
from .delete_dialog import DeleteConfirmDialog, cycle_token, set_token
from .reindex_tracker import RecoverTracker, ReindexTracker
from .restore_dialog import RestoreDialog
from .search_widget import SearchWidget


class _SidecarLoadWorker(QObject):
    """Loads + parses a logical backup's .idx.zst sidecar(s) off the Qt UI
    thread, merging the shards' trees into one.

    Sidecars for million-entry archives take a couple seconds to decompress
    and JSON-parse; doing that on the UI thread freezes the window long
    enough for the compositor to flag it as "Not Responding."
    """

    loaded = pyqtSignal(str, object)   # (group_id, merged IndexNode root)
    failed = pyqtSignal(str, str)      # (group_id, error message)

    def __init__(self, group_id: str, sidecar_paths: list[Path]):
        super().__init__()
        self._group_id = group_id
        self._sidecar_paths = sidecar_paths

    def run(self) -> None:
        try:
            roots = [load_sidecar_tree(p) for p in self._sidecar_paths]
            root = merge_sidecar_trees(roots)
        except Exception as exc:  # noqa: BLE001 - surface every failure
            self.failed.emit(self._group_id, f"{type(exc).__name__}: {exc}")
            return
        self.loaded.emit(self._group_id, root)


class ArchivePanel(QWidget):
    """Browse and restore from archives belonging to a plan."""

    restore_requested = pyqtSignal()  # emitted after a successful extract
    # (dialog title, worker action args) — main_window injects --plan/--config,
    # spawns the worker off-thread, and refreshes. Used for delete actions.
    worker_requested = pyqtSignal(str, list)

    def __init__(self, parent=None, *, tracker: ReindexTracker | None = None,
                 recover_tracker: RecoverTracker | None = None):
        super().__init__(parent)
        self._plan: PlanConfig | None = None
        # The selected logical backup (a shard set; one shard for unsharded).
        self._current_set: manifestlib.ShardSet | None = None
        self._loading_set: manifestlib.ShardSet | None = None
        self._tree_model: ArchiveTreeModel | None = None
        self._load_thread: QThread | None = None
        self._load_worker: _SidecarLoadWorker | None = None
        # Group id whose (merged) sidecar load is in flight, so we can ignore
        # late-arriving results from a previous selection.
        self._pending_group: str | None = None
        self._tracker = tracker
        self._recover_tracker = recover_tracker

        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: archive list.
        self._archive_list = QTreeWidget()
        self._archive_list.setHeaderLabels(["Archive", "Size", "Status"])
        self._archive_list.setRootIsDecorated(True)
        self._archive_list.setMinimumWidth(360)
        self._archive_list.itemSelectionChanged.connect(self._on_archive_selection)
        self._archive_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._archive_list.customContextMenuRequested.connect(self._on_context_menu)
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
        self._recover_btn = QPushButton("Recover")
        self._recover_btn.setToolTip(
            "Attempt to recover this failed backup: verify the archive stream "
            "is intact, rebuild its index, and mark it ok-with-warnings."
        )
        self._recover_btn.setVisible(False)
        self._recover_btn.clicked.connect(self._on_recover_clicked)
        header_row.addWidget(self._recover_btn)
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
        if self._recover_tracker is not None:
            self._recover_tracker.started.connect(self._on_recover_started)
            self._recover_tracker.finished.connect(self._on_recover_finished)

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
            all_entries.extend(c.archives)   # every shard, so all are searchable
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
        top.setData(0, Qt.ItemDataRole.UserRole, cycle)  # for the context menu
        if not cycle.is_complete:
            top.setForeground(0, _color("#cc8000"))
        self._archive_list.addTopLevelItem(top)

        # One row per logical backup (shard set), not per shard file. A sharded
        # full collapses to a single "(N shards)" row.
        for s in manifestlib.group_into_sets(cycle.archives):
            child = QTreeWidgetItem([_set_label(s), _human(s.total_size), s.status])
            child.setData(0, Qt.ItemDataRole.UserRole, s)
            if s.status == "failed":
                child.setForeground(0, _color("#cf222e"))
                child.setForeground(2, _color("#cf222e"))
            elif s.status == "empty":
                child.setForeground(0, _color("#6e7781"))
            elif s.status == "orphan":
                child.setForeground(0, _color("#cc8000"))
            top.addChild(child)

    # ---------- file tree ----------

    def _on_archive_selection(self) -> None:
        s = self._selected_set()
        if s is None:
            self._set_file_tree(None)
            self._pending_group = None
            return
        if self._current_set and s.group_id == self._current_set.group_id:
            return
        if not self._plan:
            return

        # Every shard must be indexed before we can show the merged tree.
        if not all(m.has_sidecar for m in s.members):
            self._show_no_sidecar(s)
            self._set_file_tree(None)
            self._pending_group = None
            return

        self._reindex_btn.setVisible(False)
        self._recover_btn.setVisible(False)

        # All shards' sidecars must be in the local mirror.
        sidecars = []
        for m in s.members:
            sc = sidecar_mirror_path(self._plan.plan_name, m.filename)
            if not sc.exists():
                self._archive_label.setText(
                    f"<b>{_set_label(s)}</b> — a sidecar is missing from the local "
                    f"mirror (run <code>--list-archives --refresh-from-mount</code>)"
                )
                self._set_file_tree(None)
                self._pending_group = None
                return
            sidecars.append(sc)

        # Load + merge the shard sidecars on a worker thread (responsive UI).
        self._archive_label.setText(
            f"<b>{_set_label(s)}</b> &nbsp; — &nbsp; {_human(s.total_size)} &nbsp; "
            f"<i>(loading file tree…)</i>"
        )
        self._set_file_tree(None)
        self._pending_group = s.group_id
        self._load_thread = QThread(self)
        self._load_worker = _SidecarLoadWorker(s.group_id, sidecars)
        self._load_worker.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._load_worker.run)
        self._load_worker.loaded.connect(self._on_sidecar_loaded)
        self._load_worker.failed.connect(self._on_sidecar_failed)
        self._load_worker.loaded.connect(self._load_thread.quit)
        self._load_worker.failed.connect(self._load_thread.quit)
        self._load_thread.finished.connect(self._load_worker.deleteLater)
        self._load_thread.finished.connect(self._load_thread.deleteLater)
        self._loading_set = s
        self._load_thread.start()

    def _on_sidecar_loaded(self, group_id: str, root: IndexNode) -> None:
        # Drop the result if the user selected another backup while this one
        # was loading.
        if group_id != self._pending_group:
            return
        s = self._loading_set
        shards = f" across {s.shard_count} shards" if s.shard_count > 1 else ""
        self._archive_label.setText(
            f"<b>{_set_label(s)}</b> &nbsp; — &nbsp; {_human(s.total_size)} &nbsp; "
            f"({root.total_entries() - 1} entries{shards})"
        )
        self._current_set = s
        self._set_file_tree(root)
        self._pending_group = None
        if self._pending_highlight_path is not None:
            self._scroll_to_path(self._pending_highlight_path)
            self._pending_highlight_path = None

    def _on_sidecar_failed(self, group_id: str, msg: str) -> None:
        if group_id != self._pending_group:
            return
        QMessageBox.warning(self, "Could not read sidecar", f"{group_id}\n\n{msg}")
        self._set_file_tree(None)
        self._pending_group = None

    # ---------- reindex / recover buttons ----------

    def _show_no_sidecar(self, s: manifestlib.ShardSet) -> None:
        """Render the not-ready state for a logical backup, with the right
        button. A failed set offers Recover (its failed shards are at .failed
        on disk); an otherwise-ok set missing a sidecar offers Reindex. Both
        act on whichever shards need it.
        """
        plan_name = self._plan.plan_name if self._plan else ""
        recovering = bool(self._recover_tracker) and any(
            self._recover_tracker.running_for(plan_name, m.filename) for m in s.members)
        reindexing = bool(self._tracker) and any(
            self._tracker.running_for(plan_name, m.filename) for m in s.members)
        label = _set_label(s)

        if recovering:
            self._archive_label.setText(f"<b>{label}</b> — backup failed (Recovering now)")
            self._reindex_btn.setVisible(False)
            self._recover_btn.setVisible(False)
            return
        if reindexing:
            self._archive_label.setText(f"<b>{label}</b> — no sidecar index (Indexing now)")
            self._reindex_btn.setVisible(False)
            self._recover_btn.setVisible(False)
            return
        if s.status == "failed":
            self._archive_label.setText(f"<b>{label}</b> — backup failed; attempt recovery?")
            self._reindex_btn.setVisible(False)
            self._recover_btn.setVisible(self._recover_tracker is not None)
            return
        self._archive_label.setText(f"<b>{label}</b> — no sidecar index")
        self._recover_btn.setVisible(False)
        self._reindex_btn.setVisible(self._tracker is not None)

    def _selected_set(self) -> manifestlib.ShardSet | None:
        items = self._archive_list.selectedItems()
        if not items:
            return None
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, manifestlib.ShardSet) else None

    def _group_of(self, archive: str) -> str:
        return manifestlib._group_id_from_filename(archive)

    # ---------- context menu (delete / reindex / recover) ----------

    def _on_context_menu(self, pos) -> None:
        """Right-click a cycle node → Delete cycle; a shard-set row → Delete
        backup (plus Reindex/Recover when applicable, rehomed here from the
        contextual buttons for discoverability)."""
        if self._plan is None:
            return
        item = self._archive_list.itemAt(pos)
        if item is None:
            return
        self._archive_list.setCurrentItem(item)  # so _selected_set() resolves
        data = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if isinstance(data, manifestlib.ShardSet):
            s = data
            if s.status == "failed" and self._recover_tracker is not None:
                menu.addAction("Recover failed backup", self._on_recover_clicked)
            elif (not all(m.has_sidecar for m in s.members)
                  and self._tracker is not None):
                menu.addAction("Reindex (rebuild sidecar)", self._on_reindex_clicked)
            if not menu.isEmpty():
                menu.addSeparator()
            menu.addAction("Export backup…", lambda: self._export_set(s))
            menu.addAction("Delete backup…", lambda: self._delete_set(s))
        elif isinstance(data, CycleListing):
            menu.addAction("Export cycle…", lambda: self._export_cycle(data))
            menu.addAction("Delete cycle…", lambda: self._delete_cycle(data))
        else:
            return
        menu.exec(self._archive_list.viewport().mapToGlobal(pos))

    def _export_into(self, kind: str, ident: str, what: str) -> None:
        """Prompt for a target directory and ask the worker to copy the bundle
        there. Non-destructive, so no type-to-confirm — just a directory pick."""
        if self._plan is None:
            return
        target = QFileDialog.getExistingDirectory(
            self, f"Export {what} into…")
        if not target:
            return
        self.worker_requested.emit(
            f"Export {what}", [kind, ident, "--into", target])

    def _export_set(self, s: manifestlib.ShardSet) -> None:
        self._export_into("--export-set", s.group_id, f"backup {s.group_id}")

    def _export_cycle(self, cycle: CycleListing) -> None:
        self._export_into("--export-cycle", cycle.cycle_id, f"cycle {cycle.cycle_id}")

    def _listing(self):
        """Current archive listing from the local mirror (no mount access)."""
        return list_from_manifest(self._plan.plan_name, self._plan.archive_dir())

    def _newest_complete_cycle_id(self) -> str | None:
        complete = sorted(c.cycle_id for c in self._listing().cycles
                          if c.is_complete)
        return complete[-1] if complete else None

    def _set_dependency_info(self, s: manifestlib.ShardSet) -> tuple[int, bool]:
        """(dependent-incremental count, is-newest-complete-full) for a set,
        derived from the mirror — mirrors the worker's --force guards so the
        dialog discloses exactly what the worker would otherwise refuse."""
        newest_id = self._newest_complete_cycle_id()
        for c in self._listing().cycles:
            if c.full is not None and manifestlib.group_id_for(c.full) == s.group_id:
                deps = len(manifestlib.group_into_sets(c.incrementals))
                return deps, c.cycle_id == newest_id
        return 0, False

    def _delete_set(self, s: manifestlib.ShardSet) -> None:
        if self._plan is None:
            return
        dependents, newest = self._set_dependency_info(s)
        shards = f" ({s.shard_count} shards)" if s.shard_count > 1 else ""
        dlg = DeleteConfirmDialog(
            title="Delete backup",
            summary=f"Delete the <b>{s.kind}</b> backup "
                    f"<code>{s.group_id}</code>{shards}?",
            token=set_token(self._plan.plan_name, s.kind, s.date_started),
            files=[m.filename for m in s.members], total_bytes=s.total_size,
            dependents=dependents, newest_complete=newest, parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.worker_requested.emit(
                f"Delete backup {s.group_id}",
                ["--delete-set", s.group_id, "--force"])

    def _delete_cycle(self, cycle: CycleListing) -> None:
        if self._plan is None:
            return
        files = [a.filename for a in cycle.archives]
        n_sets = len(manifestlib.group_into_sets(cycle.archives))
        newest = (cycle.is_complete
                  and cycle.cycle_id == self._newest_complete_cycle_id())
        dlg = DeleteConfirmDialog(
            title="Delete cycle",
            summary=f"Delete <b>cycle {cycle.cycle_id}</b> "
                    f"({n_sets} backup(s), {len(files)} shard archive(s))?",
            token=cycle_token(self._plan.plan_name, cycle.cycle_id),
            files=files, total_bytes=cycle.total_size,
            dependents=0, newest_complete=newest, parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.worker_requested.emit(
                f"Delete cycle {cycle.cycle_id}",
                ["--delete-cycle", cycle.cycle_id, "--force"])

    def _on_reindex_clicked(self) -> None:
        if self._plan is None or self._tracker is None:
            return
        s = self._selected_set()
        if s is None:
            return
        targets = [m for m in s.members if not m.has_sidecar]
        if not targets:
            return
        launched = any(self._tracker.start(self._plan.plan_name, m.filename) is not None
                       for m in targets)
        if not launched:
            QMessageBox.warning(self, "Reindex failed to launch",
                                f"Could not start reindex for {s.group_id}.")
            return
        self._show_no_sidecar(s)

    def _on_reindex_started(self, plan_name: str, archive: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        s = self._selected_set()
        if s is not None and any(m.filename == archive for m in s.members):
            self._show_no_sidecar(s)

    def _on_reindex_finished(self, plan_name: str, archive: str,
                             ok: bool, log_tail: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        if not ok:
            QMessageBox.warning(
                self, f"Reindex failed: {archive}",
                f"{archive}\n\n{log_tail or '(no output captured)'}",
            )
            s = self._selected_set()
            if s is not None and any(m.filename == archive for m in s.members):
                self._show_no_sidecar(s)
            return
        # Success: --reindex flipped has_sidecar in the manifest mirror. Reload
        # and re-select the set (if some shards are still reindexing, selection
        # will just show "Indexing now" until they finish).
        s = self._selected_set()
        target = s.group_id if (s and any(m.filename == archive for m in s.members)) else None
        self.refresh()
        if target is not None:
            self._select_set(target)

    def _on_recover_clicked(self) -> None:
        if self._plan is None or self._recover_tracker is None:
            return
        s = self._selected_set()
        if s is None or s.status != "failed":
            return
        targets = [m for m in s.members if m.status == "failed"]
        launched = any(self._recover_tracker.start(self._plan.plan_name, m.filename) is not None
                       for m in targets)
        if not launched:
            QMessageBox.warning(self, "Recovery failed to launch",
                                f"Could not start recovery for {s.group_id}.")
            return
        self._show_no_sidecar(s)

    def _on_recover_started(self, plan_name: str, archive: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        s = self._selected_set()
        if s is not None and any(m.filename == archive for m in s.members):
            self._show_no_sidecar(s)

    def _on_recover_finished(self, plan_name: str, archive: str,
                             ok: bool, log_tail: str) -> None:
        if self._plan is None or plan_name != self._plan.plan_name:
            return
        if not ok:
            QMessageBox.warning(
                self, f"Recovery failed: {archive}",
                f"{archive} could not be recovered — its archive stream is "
                f"likely truncated or corrupt.\n\n{log_tail or '(no output captured)'}",
            )
            s = self._selected_set()
            if s is not None and any(m.filename == archive for m in s.members):
                self._show_no_sidecar(s)
            return
        s = self._selected_set()
        target = s.group_id if (s and any(m.filename == archive for m in s.members)) else None
        self.refresh()
        if target is not None:
            self._select_set(target)

    def _select_set(self, group_id: str) -> None:
        for i in range(self._archive_list.topLevelItemCount()):
            top = self._archive_list.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, manifestlib.ShardSet) and data.group_id == group_id:
                    # Clearing _current_set forces _on_archive_selection to
                    # re-render rather than bailing on the same-set guard.
                    self._current_set = None
                    self._archive_list.setCurrentItem(child)
                    return

    # ---------- search ----------

    def _enter_search_mode(self) -> None:
        self._right_stack.setCurrentIndex(1)
        self._search_widget.focus_input()

    def _exit_search_mode(self) -> None:
        self._right_stack.setCurrentIndex(0)

    def _on_search_result_activated(self, archive: str, path: str) -> None:
        # A search hit names one shard file; navigate to its logical backup
        # (whose merged tree contains the path regardless of owning shard).
        group = self._group_of(archive)
        self._pending_highlight_path = path
        self._exit_search_mode()
        if self._current_set is not None and self._current_set.group_id == group:
            # Tree already loaded — scroll now, no need to wait for async load.
            self._scroll_to_path(path)
            self._pending_highlight_path = None
            return
        self._select_set(group)

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
        self._extract_btn.setEnabled(n > 0 and self._current_set is not None)

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
        if not self._current_set or not self._tree_model or not self._plan:
            return
        paths = self._selected_member_paths()
        if not paths:
            return
        # Extraction is a deliberate, user-initiated mount touch. Restore from
        # all shards of the logical backup; each holds a disjoint subset.
        adir = self._plan.archive_dir()
        archive_paths = [adir / m.filename for m in self._current_set.members]
        dlg = RestoreDialog(archive_paths, paths, parent=self,
                            label=_set_label(self._current_set))
        dlg.exec()
        # Selection might have been invalidated by user interaction; refresh.
        self._update_selection_label()


def _set_label(s: manifestlib.ShardSet) -> str:
    """Row/header label for a logical backup. Sharded backups show the
    shard-group stem and a shard count; unsharded show the archive filename."""
    if s.shard_count > 1:
        return f"{s.kind}  {s.group_id}  ({s.shard_count} shards)"
    return f"{s.kind}  {s.members[0].filename}"


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
