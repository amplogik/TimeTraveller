"""Archive browser panel.

Layout: a QSplitter with two panes —
  Left: a QTreeWidget grouped by cycle, showing archives with status badges.
  Right: a QTreeView of the selected archive's contents (from its .idx.zst sidecar).
Bottom: 'Selected: N paths' + 'Extract selected...' button.

The panel reads exclusively from the local manifest mirror and the local
sidecar mirror — it never blocks the Qt thread on the backup mount.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSplitter, QTreeView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..archive import CycleListing, IndexNode, list_from_manifest, load_sidecar_tree
from ..config import PlanConfig
from ..index import sidecar_mirror_path
from ..manifest import ArchiveEntry
from .archive_tree_model import ArchiveTreeModel
from .restore_dialog import RestoreDialog


class ArchivePanel(QWidget):
    """Browse and restore from archives belonging to a plan."""

    restore_requested = pyqtSignal()  # emitted after a successful extract

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: PlanConfig | None = None
        self._current_entry: ArchiveEntry | None = None
        self._tree_model: ArchiveTreeModel | None = None

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

        # Right: file tree.
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        self._archive_label = QLabel("(no archive selected)")
        self._archive_label.setStyleSheet("padding: 4px;")
        rv.addWidget(self._archive_label)

        self._file_tree = QTreeView()
        self._file_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._file_tree.setUniformRowHeights(True)
        rv.addWidget(self._file_tree, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

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
            return
        listing = list_from_manifest(self._plan.plan_name, self._plan.archive_dir())
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
            return
        if self._current_entry and entry.filename == self._current_entry.filename:
            return

        if not self._plan:
            return

        if not entry.has_sidecar:
            self._archive_label.setText(
                f"<b>{entry.filename}</b> — no sidecar index (run --reindex)"
            )
            self._set_file_tree(None)
            return

        sc = sidecar_mirror_path(self._plan.plan_name, entry.filename)
        if not sc.exists():
            self._archive_label.setText(
                f"<b>{entry.filename}</b> — sidecar missing from local mirror "
                f"(run <code>--list-archives --refresh-from-mount</code>)"
            )
            self._set_file_tree(None)
            return

        try:
            root = load_sidecar_tree(sc)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Could not read sidecar", f"{sc}\n\n{e}")
            self._set_file_tree(None)
            return

        self._archive_label.setText(
            f"<b>{entry.filename}</b> &nbsp; — &nbsp; {_human(entry.size_bytes)} &nbsp; "
            f"({root.total_entries() - 1} entries)"
        )
        self._current_entry = entry
        self._set_file_tree(root)

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
