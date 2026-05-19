"""Modal dialog for extracting selected paths from an archive."""

from __future__ import annotations

import shlex
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from ..archive import build_extract_argv


class RestoreDialog(QDialog):
    """Extract specific paths from a single archive into a chosen destination."""

    def __init__(self, archive: Path, paths: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extract from archive")
        self.resize(900, 600)

        self._archive = archive
        self._paths = paths
        self._proc1: QProcess | None = None  # zstdcat
        self._proc2: QProcess | None = None  # pax -r

        layout = QVBoxLayout(self)

        # Header summary.
        info = QFormLayout()
        info.addRow("Archive:", QLabel(str(archive)))
        info.addRow("Paths:", QLabel(f"{len(paths)} selected"))
        layout.addLayout(info)

        # Paths preview (read-only list).
        self._paths_list = QListWidget()
        self._paths_list.setMaximumHeight(120)
        for p in paths:
            self._paths_list.addItem(QListWidgetItem(p))
        layout.addWidget(self._paths_list)

        # Destination row.
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        default_dest = str(Path.home() / "Restored" / archive.stem.split(".")[0])
        self._dest = QLineEdit(default_dest)
        dest_row.addWidget(self._dest, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        dest_row.addWidget(browse)
        layout.addLayout(dest_row)

        # Options.
        self._preserve = QCheckBox("Preserve ownership, permissions, and mtime (pax -p e)")
        self._preserve.setChecked(True)
        layout.addWidget(self._preserve)

        # Command preview.
        self._cmd_label = QLabel()
        self._cmd_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._cmd_label.setStyleSheet("font-family: monospace; padding: 4px;")
        self._cmd_label.setWordWrap(True)
        layout.addWidget(self._cmd_label)

        # Live output.
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._output.setFont(mono)
        layout.addWidget(self._output, 1)

        # Status + buttons.
        self._status = QLabel("Ready.")
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        self._extract_btn = QPushButton("Extract")
        self._cancel_btn = QPushButton("Cancel")
        self._close_btn = QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._buttons.addButton(self._extract_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self._buttons.addButton(self._cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self._buttons.addButton(self._close_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(self._buttons)

        self._extract_btn.clicked.connect(self._on_extract)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._close_btn.clicked.connect(self.accept)

        self._refresh_cmd_preview()
        self._dest.textChanged.connect(self._refresh_cmd_preview)
        self._preserve.toggled.connect(self._refresh_cmd_preview)

    # ---------- helpers ----------

    def _on_browse(self) -> None:
        start = self._dest.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose extraction destination", start)
        if d:
            self._dest.setText(d)

    def _refresh_cmd_preview(self) -> None:
        try:
            zstdcat, pax = build_extract_argv(
                self._archive, self._paths,
                preserve_metadata=self._preserve.isChecked(),
            )
        except ValueError as e:
            self._cmd_label.setText(f"<span style='color:#cf222e'>error: {e}</span>")
            return
        cmd = (
            f"cd {shlex.quote(self._dest.text())} && "
            f"{' '.join(shlex.quote(x) for x in zstdcat)} | "
            f"{' '.join(shlex.quote(x) for x in pax)}"
        )
        self._cmd_label.setText(f"<b>Command:</b> {cmd}")

    # ---------- extract pipeline ----------

    def _on_extract(self) -> None:
        dest = Path(self._dest.text()).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._status.setText(f"<span style='color:#cf222e'>cannot create destination: {e}</span>")
            return
        try:
            zstdcat, pax = build_extract_argv(
                self._archive, self._paths,
                preserve_metadata=self._preserve.isChecked(),
            )
        except ValueError as e:
            self._status.setText(f"<span style='color:#cf222e'>{e}</span>")
            return

        self._extract_btn.setEnabled(False)
        self._output.clear()
        self._status.setText("Extracting...")

        # QProcess pipeline: zstdcat | pax. Both run with cwd=dest.
        self._proc1 = QProcess(self)
        self._proc2 = QProcess(self)
        self._proc1.setWorkingDirectory(str(dest))
        self._proc2.setWorkingDirectory(str(dest))
        # Stitch stdout of proc1 to stdin of proc2 using setStandardOutputProcess.
        self._proc1.setStandardOutputProcess(self._proc2)

        # Merge each process's stderr into our output. (stdout of proc2 is
        # discarded; pax -r writes only diagnostics to stderr.)
        self._proc1.readyReadStandardError.connect(
            lambda: self._append_bytes(self._proc1.readAllStandardError())  # type: ignore[union-attr]
        )
        self._proc2.readyReadStandardError.connect(
            lambda: self._append_bytes(self._proc2.readAllStandardError())  # type: ignore[union-attr]
        )
        self._proc2.readyReadStandardOutput.connect(
            lambda: self._append_bytes(self._proc2.readAllStandardOutput())  # type: ignore[union-attr]
        )

        self._proc2.finished.connect(self._on_finished)

        # Start pax first (so it's ready for input), then zstdcat.
        self._proc2.start(pax[0], pax[1:])
        self._proc1.start(zstdcat[0], zstdcat[1:])

    def _append_bytes(self, data) -> None:
        text = bytes(data).decode("utf-8", errors="replace")
        self._output.appendPlainText(text.rstrip("\n"))

    def _on_finished(self, exit_code: int, _status) -> None:
        # Make sure proc1 has wrapped up too.
        if self._proc1 and self._proc1.state() != QProcess.ProcessState.NotRunning:
            self._proc1.waitForFinished(2000)
        if exit_code == 0:
            self._status.setText("<span style='color:#2da44e'>Extract OK</span>")
        else:
            self._status.setText(f"<span style='color:#cf222e'>pax exited {exit_code}</span>")
        self._cancel_btn.setEnabled(False)
        self._close_btn.setEnabled(True)
        self._close_btn.setDefault(True)

    def _on_cancel(self) -> None:
        for p in (self._proc1, self._proc2):
            if p and p.state() != QProcess.ProcessState.NotRunning:
                p.terminate()
                if not p.waitForFinished(2000):
                    p.kill()
        self.reject()
