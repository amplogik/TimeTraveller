"""Modal dialog for extracting selected paths from an archive.

Uses the Python-level fast-extract path (extract.extract_files) when the
archive has a v2 sidecar + frames index; falls back automatically to a
whole-archive scan via the same code path. Runs the extract on a worker
thread so the Qt UI stays responsive.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from ..extract import ExtractStats, extract_files


class _ExtractWorker(QObject):
    """Runs extract_files() off the Qt thread; emits results via signals."""

    finished = pyqtSignal(object)   # ExtractStats
    failed = pyqtSignal(str)

    def __init__(self, archive: Path, paths: list[str], into: Path):
        super().__init__()
        self._archive = archive
        self._paths = paths
        self._into = into

    def run(self) -> None:
        try:
            stats = extract_files(self._archive, self._paths, into=self._into)
        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(stats)


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, divisor in (("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3)):
        if n < divisor * 1024:
            return f"{n/divisor:.1f} {unit}"
    return f"{n/1024**3:.1f} GiB"


class RestoreDialog(QDialog):
    """Extract specific paths from a single archive into a chosen destination."""

    def __init__(self, archive: Path, paths: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extract from archive")
        self.resize(900, 600)

        self._archive = archive
        self._paths = paths
        self._thread: QThread | None = None
        self._worker: _ExtractWorker | None = None

        layout = QVBoxLayout(self)

        info = QFormLayout()
        info.addRow("Archive:", QLabel(str(archive)))
        info.addRow("Paths:", QLabel(f"{len(paths)} selected"))
        layout.addLayout(info)

        self._paths_list = QListWidget()
        self._paths_list.setMaximumHeight(120)
        for p in paths:
            self._paths_list.addItem(QListWidgetItem(p))
        layout.addWidget(self._paths_list)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        default_dest = str(Path.home() / "Restored" / archive.stem.split(".")[0])
        self._dest = QLineEdit(default_dest)
        dest_row.addWidget(self._dest, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        dest_row.addWidget(browse)
        layout.addLayout(dest_row)

        # Description of what the extract will do (no shell command anymore —
        # this is a direct Python call into the seekable-archive engine).
        self._mode_label = QLabel(
            "Mode: <b>auto</b> — uses the .idx.zst + .frames.json sidecars for "
            "random-access extraction when available; falls back to a "
            "whole-archive scan if either sidecar is missing or legacy."
        )
        self._mode_label.setWordWrap(True)
        self._mode_label.setStyleSheet("padding: 4px; color: #57606a;")
        layout.addWidget(self._mode_label)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._output.setFont(mono)
        layout.addWidget(self._output, 1)

        self._status = QLabel("Ready.")
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        self._extract_btn = QPushButton("Extract")
        self._close_btn = QPushButton("Close")
        self._buttons.addButton(self._extract_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self._buttons.addButton(self._close_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(self._buttons)

        self._extract_btn.clicked.connect(self._on_extract)
        self._close_btn.clicked.connect(self.accept)

    def _on_browse(self) -> None:
        start = self._dest.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose extraction destination", start)
        if d:
            self._dest.setText(d)

    def _on_extract(self) -> None:
        dest = Path(self._dest.text()).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._status.setText(f"<span style='color:#cf222e'>cannot create destination: {e}</span>")
            return

        self._extract_btn.setEnabled(False)
        self._output.clear()
        self._output.appendPlainText(f"Extracting {len(self._paths)} path(s) into {dest}…")
        self._status.setText("Extracting…")

        # Move the work to a worker thread so the Qt event loop keeps spinning.
        self._thread = QThread(self)
        self._worker = _ExtractWorker(self._archive, list(self._paths), dest)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        # Clean shutdown chain — quit the thread once the work is done, then
        # let both sides drop their references.
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_finished(self, stats: ExtractStats) -> None:
        mode = "naive (whole-archive scan)" if stats.fallback_naive else "fast (sidecar-based)"
        self._output.appendPlainText(f"  mode:          {mode}")
        self._output.appendPlainText(
            f"  matched:       {stats.matched_files} file(s), "
            f"{stats.matched_dirs} dir(s), "
            f"{stats.matched_symlinks} symlink(s)"
            + (f", {stats.matched_hardlinks} hardlinks skipped"
               if stats.matched_hardlinks else "")
        )
        if not stats.fallback_naive:
            self._output.appendPlainText(
                f"  frames read:   {stats.frames_read} "
                f"({_human_bytes(stats.nfs_bytes_read)} from archive)"
            )
        self._output.appendPlainText(f"  bytes written: {_human_bytes(stats.bytes_written)}")
        self._output.appendPlainText(f"  elapsed:       {_hms(stats.seconds_total)}")

        nothing_matched = (stats.matched_files + stats.matched_dirs
                           + stats.matched_symlinks == 0)
        if nothing_matched:
            self._status.setText(
                "<span style='color:#cc8000'>No matching entries in the archive.</span>"
            )
        else:
            self._status.setText(
                f"<span style='color:#2da44e'>Extract OK — "
                f"{_human_bytes(stats.bytes_written)} restored in {_hms(stats.seconds_total)}</span>"
            )
        self._close_btn.setDefault(True)

    def _on_failed(self, msg: str) -> None:
        self._output.appendPlainText(f"ERROR: {msg}")
        self._status.setText(f"<span style='color:#cf222e'>extract failed: {msg}</span>")
        self._close_btn.setDefault(True)
