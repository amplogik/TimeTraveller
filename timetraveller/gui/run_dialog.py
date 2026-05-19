"""Modal dialog that runs the worker as a subprocess and streams output live.

Used for Run Now (full/incr), Dry Run, Show Mounts, List Files, Show Schedule
— any worker invocation where the user wants to see what happened.
"""

from __future__ import annotations

from PyQt6.QtCore import QProcess, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .worker_runner import build_qprocess, worker_args, worker_program


class WorkerRunDialog(QDialog):
    """Run `timetraveller-backup` with the given args and show output live."""

    def __init__(self, title: str, args: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        self._args = args
        self._proc: QProcess | None = None

        layout = QVBoxLayout(self)

        cmd_label = QLabel(f"<b>Command:</b> timetraveller-backup {' '.join(args)}")
        cmd_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cmd_label.setWordWrap(True)
        layout.addWidget(cmd_label)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._output.setFont(mono)
        layout.addWidget(self._output, 1)

        self._status = QLabel("Starting...")
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        self._cancel_btn = QPushButton("Cancel")
        self._close_btn = QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._buttons.addButton(self._cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self._buttons.addButton(self._close_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(self._buttons)

        self._cancel_btn.clicked.connect(self._cancel)
        self._close_btn.clicked.connect(self.accept)

    def start(self) -> None:
        self._proc = build_qprocess(self)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.start(worker_program(), worker_args(self._args))
        self._status.setText("Running...")

    def _on_output(self) -> None:
        if not self._proc:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._output.appendPlainText(data.rstrip("\n"))

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        self._cancel_btn.setEnabled(False)
        self._close_btn.setEnabled(True)
        self._close_btn.setDefault(True)
        if exit_code == 0:
            self._status.setText(f"<span style='color: #2da44e'>Finished OK (exit 0)</span>")
        else:
            self._status.setText(f"<span style='color: #cf222e'>Failed (exit {exit_code})</span>")

    def _on_error(self, err) -> None:
        self._output.appendPlainText(f"\n[QProcess error: {err}]")

    def _cancel(self) -> None:
        if self._proc and self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.terminate()
            if not self._proc.waitForFinished(2000):
                self._proc.kill()
        self.reject()
