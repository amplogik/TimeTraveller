"""QAbstractItemModel wrapping a parsed IndexNode tree.

Columns: Name | Size | Modified | Owner | Mode
"""

from __future__ import annotations

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt

from ..archive import IndexNode


COLUMNS = ("Name", "Size", "Modified", "Owner", "Mode")


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, divisor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4)):
        if n < divisor * 1024:
            return f"{n/divisor:.1f} {unit}"
    return f"{n/1024**4:.1f} TB"


class _Row:
    """Wraps an IndexNode + its parent _Row, caching the sorted children list."""
    __slots__ = ("node", "parent", "_children")

    def __init__(self, node: IndexNode, parent: "_Row | None"):
        self.node = node
        self.parent = parent
        self._children: list["_Row"] | None = None

    def children(self) -> list["_Row"]:
        if self._children is None:
            self._children = [_Row(c, self) for c in self.node.sorted_children()]
        return self._children


class ArchiveTreeModel(QAbstractItemModel):
    def __init__(self, root: IndexNode, parent=None):
        super().__init__(parent)
        # Wrap the real root in a synthetic row so its children are accessible
        # via index(0, 0, QModelIndex()).
        self._root = _Row(root, None)

    # ---------- Qt model interface ----------

    def columnCount(self, _parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def rowCount(self, parent=QModelIndex()) -> int:
        row = self._row(parent)
        return len(row.children())

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        parent_row = self._row(parent)
        children = parent_row.children()
        if 0 <= row < len(children):
            return self.createIndex(row, column, children[row])
        return QModelIndex()

    def parent(self, idx: QModelIndex) -> QModelIndex:
        if not idx.isValid():
            return QModelIndex()
        row: _Row = idx.internalPointer()
        if row.parent is None or row.parent is self._root:
            return QModelIndex()
        grandparent = row.parent.parent
        if grandparent is None:
            return QModelIndex()
        siblings = grandparent.children()
        i = siblings.index(row.parent)
        return self.createIndex(i, 0, row.parent)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(COLUMNS):
                return COLUMNS[section]
        return None

    def data(self, idx: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not idx.isValid():
            return None
        row: _Row = idx.internalPointer()
        node = row.node
        if role == Qt.ItemDataRole.DisplayRole:
            col = idx.column()
            if col == 0:
                return node.name
            if col == 1:
                return "" if node.is_dir else _format_size(node.size)
            if col == 2:
                return node.mtime
            if col == 3:
                return f"{node.owner}:{node.group}" if node.owner else ""
            if col == 4:
                return node.perms
        if role == Qt.ItemDataRole.TextAlignmentRole and idx.column() == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return node
        return None

    def hasChildren(self, parent=QModelIndex()) -> bool:
        return self.rowCount(parent) > 0

    # ---------- public helpers ----------

    def node_at(self, idx: QModelIndex) -> IndexNode | None:
        if not idx.isValid():
            return None
        row: _Row = idx.internalPointer()
        return row.node

    # ---------- internals ----------

    def _row(self, idx: QModelIndex) -> _Row:
        if not idx.isValid():
            return self._root
        return idx.internalPointer()
