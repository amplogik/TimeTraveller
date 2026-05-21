"""Plan editor: sources, excludes, destination, retention, mount options."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..config import RETENTION_POLICIES, PlanConfig, Retention
from ..mounts import find_nested_mounts


class _StringListEditor(QWidget):
    """A QListWidget with Add/Browse/Edit/Remove buttons. Emits changed() on edit.

    `browse_mode`:
      - None             — no Browse button
      - "source_dir"     — directory picker; result added as-is
      - "exclude_dir"    — directory picker; result has trailing "/" appended so
                           our glob translator treats it as "this dir + contents"
    """
    changed = pyqtSignal()

    def __init__(self, add_prompt: str, parent=None,
                 browse_mode: str | None = None,
                 browse_title: str = "Select…"):
        super().__init__(parent)
        self._add_prompt = add_prompt
        self._browse_mode = browse_mode
        self._browse_title = browse_title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget()
        layout.addWidget(self._list, 1)
        btn_row = QHBoxLayout()
        self._add = QPushButton("Add…")
        self._browse = QPushButton("Browse…") if browse_mode else None
        self._edit = QPushButton("Edit…")
        self._remove = QPushButton("Remove")
        btn_row.addWidget(self._add)
        if self._browse is not None:
            btn_row.addWidget(self._browse)
        btn_row.addWidget(self._edit)
        btn_row.addWidget(self._remove)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._add.clicked.connect(self._on_add)
        if self._browse is not None:
            self._browse.clicked.connect(self._on_browse)
        self._remove.clicked.connect(self._on_remove)
        self._edit.clicked.connect(self._on_edit)
        self._list.itemDoubleClicked.connect(lambda _: self._on_edit())

    def items(self) -> list[str]:
        return [self._list.item(i).text() for i in range(self._list.count())]

    def set_items(self, values: list[str]) -> None:
        self._list.clear()
        for v in values:
            self._list.addItem(QListWidgetItem(v))

    def _on_add(self) -> None:
        text, ok = QInputDialog.getText(self, "Add", self._add_prompt)
        if ok and text.strip():
            self._list.addItem(QListWidgetItem(text.strip()))
            self.changed.emit()

    def _on_remove(self) -> None:
        for it in self._list.selectedItems():
            self._list.takeItem(self._list.row(it))
        self.changed.emit()

    def _on_edit(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        text, ok = QInputDialog.getText(self, "Edit", self._add_prompt, text=item.text())
        if ok and text.strip():
            item.setText(text.strip())
            self.changed.emit()

    def _on_browse(self) -> None:
        start = str(Path.home())
        d = QFileDialog.getExistingDirectory(self, self._browse_title, start)
        if not d:
            return
        if self._browse_mode == "exclude_dir":
            # Trailing slash → our glob translator drops the dir and everything beneath.
            d = d.rstrip("/") + "/"
        self._list.addItem(QListWidgetItem(d))
        self.changed.emit()


class _NestedMountsBox(QGroupBox):
    """Detects mounts under the plan's sources and lets the user tick to include.

    Local mounts under a source are skipped by pax `-X`. NFS/CIFS/removable
    mounts are also dropped by our default filter. This widget surfaces them
    so the user can decide explicitly. Ticking adds the path as a new top-
    level source; unticking removes it.
    """

    sources_changed = pyqtSignal(list)  # full new sources list

    def __init__(self, parent=None):
        super().__init__("Nested filesystems under sources", parent)
        self._current_sources: list[str] = []
        self._suppress = False

        layout = QVBoxLayout(self)
        info = QLabel(
            "<b>NOT backed up by default.</b> "
            "Tick a row to include — the path is added as an extra source. "
            "<span style='color:#cc8000'>Orange</span> = removable / NFS / "
            "CIFS (worth a second look). "
            "<a href='#why'>Why?</a>"
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(False)
        info.linkActivated.connect(self._show_help)
        info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        info.setMinimumHeight(48)
        info.setStyleSheet("padding: 2px;")
        layout.addWidget(info)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Backup?", "Path", "Kind", "Filesystem"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setMinimumHeight(110)
        self._tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        rescan = QPushButton("Re-scan")
        rescan.clicked.connect(lambda: self.refresh(self._current_sources))
        btn_row.addWidget(rescan)
        layout.addLayout(btn_row)

    def refresh(self, sources: list[str]) -> None:
        self._current_sources = list(sources)
        nested = find_nested_mounts(sources)
        current_set = {str(s) for s in sources}
        self._suppress = True
        try:
            self._tree.clear()
            if not nested:
                placeholder = QTreeWidgetItem(["", "(no nested filesystems detected)", "", ""])
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self._tree.addTopLevelItem(placeholder)
                return
            for cm in nested:
                badge = " ⚠" if cm.kind in ("removable", "nfs", "cifs") else ""
                item = QTreeWidgetItem(["", cm.target, cm.kind + badge, cm.fstype])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if cm.target in current_set:
                    item.setCheckState(0, Qt.CheckState.Checked)
                else:
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                if cm.kind in ("removable", "nfs", "cifs"):
                    item.setForeground(2, QBrush(QColor("#cc8000")))
                self._tree.addTopLevelItem(item)
            for col in range(self._tree.columnCount()):
                self._tree.resizeColumnToContents(col)
        finally:
            self._suppress = False

    def _show_help(self, _href: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("Nested filesystems — why")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            "<p>By default, pax runs with <code>-X</code> (one-filesystem "
            "mode) so the backup won't accidentally walk into NFS shares, "
            "removable media, or other mounts you didn't intend to "
            "include.</p>"
            "<p>To include a nested filesystem, tick its row in the table "
            "above. The path is added as an additional top-level source in "
            "your plan. Untick to remove.</p>"
            "<p><b>Orange-coded rows</b> are unusual mount kinds (removable, "
            "NFS, CIFS) that warrant a closer look — for example, a USB "
            "drive might be mounted right now but absent at the next "
            "scheduled backup, leaving an awkward gap.</p>"
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress or column != 0:
            return
        path = item.text(1)
        if not path:
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        new_sources = list(self._current_sources)
        if checked and path not in new_sources:
            new_sources.append(path)
        elif not checked and path in new_sources:
            new_sources.remove(path)
        if new_sources == self._current_sources:
            return
        self._current_sources = new_sources
        self.sources_changed.emit(new_sources)


class PlanPanel(QWidget):
    """Edits one PlanConfig. Emits changed() when the user touches anything."""

    changed = pyqtSignal()
    switch_type_requested = pyqtSignal()  # user clicked "Change…" next to plan type

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: PlanConfig | None = None

        layout = QFormLayout(self)
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Plan name (read-only; you create a new plan to change it).
        self._name = QLabel("(no plan loaded)")
        layout.addRow("Plan name:", self._name)

        # Plan type — Active vs Archive. Read-only label + Change… button.
        # The actual switch goes through a confirm dialog (see MainWindow wiring).
        type_box = QWidget()
        th = QHBoxLayout(type_box)
        th.setContentsMargins(0, 0, 0, 0)
        self._type_label = QLabel("(unknown)")
        self._type_change = QPushButton("Change…")
        self._type_change.setToolTip(
            "Switch between Active (scheduled, with retention) and Archive "
            "(manual-only, keeps everything forever)."
        )
        self._type_change.clicked.connect(self.switch_type_requested.emit)
        th.addWidget(self._type_label, 1)
        th.addWidget(self._type_change)
        layout.addRow("Plan type:", type_box)

        # Sources.
        self._sources = _StringListEditor(
            "Source path (e.g. /home):",
            browse_mode="source_dir",
            browse_title="Select source directory",
        )
        sources_box = QGroupBox("Sources")
        sl = QVBoxLayout(sources_box)
        sl.addWidget(self._sources)
        layout.addRow(sources_box)

        # Nested filesystems under sources.
        self._nested = _NestedMountsBox()
        layout.addRow(self._nested)

        # Excludes.
        self._excludes = _StringListEditor(
            "Exclude glob (e.g. **/.cache/  or  /var/cache/**):",
            browse_mode="exclude_dir",
            browse_title="Select directory to exclude",
        )
        excl_box = QGroupBox("Excludes")
        el = QVBoxLayout(excl_box)
        el.addWidget(self._excludes)
        layout.addRow(excl_box)

        # Destination.
        dest_box = QWidget()
        dh = QHBoxLayout(dest_box)
        dh.setContentsMargins(0, 0, 0, 0)
        self._dest = QLineEdit()
        self._dest_browse = QPushButton("Browse…")
        dh.addWidget(self._dest, 1)
        dh.addWidget(self._dest_browse)
        layout.addRow("Destination:", dest_box)

        self._hostname_path = QCheckBox("Include hostname in destination path")
        layout.addRow("", self._hostname_path)

        # Retention. Hidden when the loaded plan is an Archive plan — those
        # use the keep_all policy which has no knobs.
        self._ret_box = QGroupBox("Retention")
        rl = QFormLayout(self._ret_box)
        # Only the rotating-retention policies appear in the combobox; keep_all
        # is a property of Archive plans and is set via the Plan type switch,
        # not picked here.
        self._policy = QComboBox()
        self._policy.addItems([p for p in RETENTION_POLICIES if p != "keep_all"])
        self._max_cycles = QSpinBox()
        self._max_cycles.setRange(1, 999)
        self._max_age = QSpinBox()
        self._max_age.setRange(1, 9999)
        self._max_age.setSuffix(" days")
        self._max_size = QDoubleSpinBox()
        self._max_size.setRange(0.1, 999_999.0)
        self._max_size.setSuffix(" GiB")
        self._max_size.setDecimals(1)
        rl.addRow("Policy:", self._policy)
        rl.addRow("Max cycles:", self._max_cycles)
        rl.addRow("Max age:", self._max_age)
        rl.addRow("Max size:", self._max_size)
        layout.addRow(self._ret_box)

        # Shown in place of the Retention box for archive plans.
        self._archive_note = QLabel(
            "<i>Archive plans keep all cycles forever. Prune manually with "
            "<code>timetraveller-backup --plan &lt;name&gt; --prune</code> "
            "if needed.</i>"
        )
        self._archive_note.setWordWrap(True)
        self._archive_note.setVisible(False)
        layout.addRow(self._archive_note)

        # Mount options.
        mnt_box = QGroupBox("Mount options")
        ml = QVBoxLayout(mnt_box)
        self._include_removable = QCheckBox("Include removable media")
        self._include_nfs = QCheckBox("Include NFS mounts")
        self._include_cifs = QCheckBox("Include CIFS mounts (other than destination)")
        for w in (self._include_removable, self._include_nfs, self._include_cifs):
            ml.addWidget(w)
        layout.addRow(mnt_box)

        # Wire change signals.
        self._dest.textEdited.connect(self.changed.emit)
        self._dest_browse.clicked.connect(self._on_browse)
        self._hostname_path.toggled.connect(self.changed.emit)
        self._policy.currentTextChanged.connect(self._on_policy_changed)
        self._max_cycles.valueChanged.connect(self.changed.emit)
        self._max_age.valueChanged.connect(self.changed.emit)
        self._max_size.valueChanged.connect(self.changed.emit)
        self._include_removable.toggled.connect(self.changed.emit)
        self._include_nfs.toggled.connect(self.changed.emit)
        self._include_cifs.toggled.connect(self.changed.emit)
        self._sources.changed.connect(self.changed.emit)
        self._sources.changed.connect(self._on_sources_edited)
        self._excludes.changed.connect(self.changed.emit)
        self._nested.sources_changed.connect(self._on_nested_changed)

        self._update_retention_enabled()

    def _on_sources_edited(self) -> None:
        self._nested.refresh(self._sources.items())

    def _on_nested_changed(self, new_sources: list[str]) -> None:
        self._sources.set_items(new_sources)
        self.changed.emit()

    def load_plan(self, plan: PlanConfig) -> None:
        self._plan = plan
        self._name.setText(plan.plan_name)
        self._sources.set_items(plan.sources)
        self._nested.refresh(plan.sources)
        self._excludes.set_items(plan.excludes)
        self._dest.setText(plan.destination)
        self._hostname_path.setChecked(plan.include_hostname_in_path)
        is_archive = plan.schedule.mode == "archive"
        self._type_label.setText("<b>Archive</b> (manual only, keeps all cycles)"
                                 if is_archive
                                 else "<b>Active</b> (scheduled, with retention)")
        self._ret_box.setVisible(not is_archive)
        self._archive_note.setVisible(is_archive)
        if not is_archive:
            # Only meaningful for Active plans; combobox doesn't include keep_all.
            self._policy.setCurrentText(plan.retention.policy)
        self._max_cycles.setValue(plan.retention.max_cycles)
        self._max_age.setValue(plan.retention.max_age_days or 30)
        self._max_size.setValue(plan.retention.max_size_gb or 100.0)
        self._include_removable.setChecked(bool(plan.include_removable))
        self._include_nfs.setChecked(bool(plan.include_nfs))
        self._include_cifs.setChecked(bool(plan.include_cifs))
        self._update_retention_enabled()

    def to_plan(self) -> PlanConfig:
        """Return a PlanConfig reflecting the panel's current state."""
        assert self._plan is not None
        # Archive plans pin retention to keep_all; the editor's combobox /
        # spinboxes are hidden and irrelevant.
        if self._plan.schedule.mode == "archive":
            retention = Retention(policy="keep_all")
        else:
            retention = Retention(
                policy=self._policy.currentText(),
                max_cycles=self._max_cycles.value(),
                max_age_days=self._max_age.value() if self._policy.currentText() == "max_age_days" else None,
                max_size_gb=self._max_size.value() if self._policy.currentText() == "max_size_gb" else None,
            )
        return replace(
            self._plan,
            sources=self._sources.items(),
            excludes=self._excludes.items(),
            destination=self._dest.text().strip(),
            retention=retention,
            include_hostname_in_path=self._hostname_path.isChecked(),
            include_removable=self._include_removable.isChecked(),
            include_nfs=self._include_nfs.isChecked(),
            include_cifs=self._include_cifs.isChecked(),
        )

    def _on_browse(self) -> None:
        start = self._dest.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose destination directory", start)
        if d:
            self._dest.setText(d)
            self.changed.emit()

    def _on_policy_changed(self, _value: str) -> None:
        self._update_retention_enabled()
        self.changed.emit()

    def _update_retention_enabled(self) -> None:
        p = self._policy.currentText() if self._policy.count() else "max_cycles"
        self._max_cycles.setEnabled(p == "max_cycles")
        self._max_age.setEnabled(p == "max_age_days")
        self._max_size.setEnabled(p == "max_size_gb")
