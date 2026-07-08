"""Restore-from-a-location dialog.

The config-less recovery entry point: point TimeTraveller at a folder that
holds backups (a mounted USB drive, an external disk, an NFS/SMB share) and it
reads them directly — discovering the plans/cycles from the manifest + the
portable `timetraveller.restore.json` descriptor, with no matching local config
required. Browsing + extraction reuse the ArchivePanel in its source mode.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from ..restore_source import BackupLocation, discover_backup_locations
from .archive_panel import ArchivePanel


class RestoreFromLocationDialog(QDialog):
    """Browse a backup directory and restore from it without any local config."""

    def __init__(self, parent=None, *, initial_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Restore from a backup location")
        self.resize(1000, 680)
        self._locations: list[BackupLocation] = []

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Point TimeTraveller at a folder that holds your backups — a mounted "
            "USB drive, an external disk, or a network share. It reads the backups "
            "directly; no matching plan configuration is needed."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Location:"))
        self._path = QLineEdit()
        self._path.setReadOnly(True)
        row.addWidget(self._path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        row.addWidget(browse)
        layout.addLayout(row)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("Backup set:"))
        self._combo = QComboBox()
        self._combo.setEnabled(False)
        self._combo.currentIndexChanged.connect(self._on_pick)
        prow.addWidget(self._combo, 1)
        layout.addLayout(prow)

        self._panel = ArchivePanel()
        layout.addWidget(self._panel, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(close)
        layout.addLayout(brow)

        if initial_dir:
            self._path.setText(initial_dir)
            self._discover(Path(initial_dir))

    def _on_browse(self) -> None:
        start = self._path.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose your backup location", start)
        if not d:
            return
        self._path.setText(d)
        self._discover(Path(d))

    def _discover(self, root: Path) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._locations = discover_backup_locations(root)
        except OSError as e:
            self._locations = []
            QMessageBox.warning(self, "Could not read location", f"{root}\n\n{e}")
        finally:
            QApplication.restoreOverrideCursor()

        self._combo.blockSignals(True)
        self._combo.clear()
        if not self._locations:
            self._combo.setEnabled(False)
            self._combo.blockSignals(False)
            # Show the empty-state placeholder in the panel too.
            self._panel.load_source(root, "", None)
            QMessageBox.information(
                self, "No backups found",
                "No TimeTraveller backups were found there. If your backups are in "
                "a subfolder (for example <i>&lt;hostname&gt;/&lt;plan&gt;</i>), pick "
                "the folder that contains them, or a parent folder that holds "
                "everything.",
            )
            return

        for loc in self._locations:
            host = f"{loc.hostname}/" if loc.hostname else ""
            name = loc.plan_name or loc.archive_dir.name
            tag = "" if loc.has_descriptor else "  (no descriptor — restore-to path unknown)"
            self._combo.addItem(f"{host}{name}  —  {loc.n_archives} archive(s){tag}")
        self._combo.setEnabled(len(self._locations) > 1)
        self._combo.blockSignals(False)
        self._combo.setCurrentIndex(0)
        self._on_pick(0)

    def _on_pick(self, idx: int) -> None:
        if not (0 <= idx < len(self._locations)):
            return
        loc = self._locations[idx]
        self._panel.load_source(loc.archive_dir, loc.plan_name, loc.sources)
