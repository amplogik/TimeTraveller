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
    """Runs extract_files() off the Qt thread; emits results via signals.

    Extracts from one or more shard archives (the shards of one logical
    backup) and sums the stats — each requested member lives in exactly one
    shard, so a shard that holds none of them simply contributes nothing.
    """

    finished = pyqtSignal(object)   # aggregated ExtractStats
    failed = pyqtSignal(str)

    def __init__(self, archives: list[Path], paths: list[str], into: Path):
        super().__init__()
        self._archives = archives
        self._paths = paths
        self._into = into

    def run(self) -> None:
        try:
            agg = ExtractStats(requested_patterns=len(self._paths), matched_files=0,
                               matched_dirs=0, matched_symlinks=0, matched_hardlinks=0,
                               frames_read=0, nfs_bytes_read=0, bytes_written=0,
                               seconds_total=0.0, fallback_naive=False)
            for archive in self._archives:
                st = extract_files(archive, self._paths, into=self._into)
                agg.matched_files += st.matched_files
                agg.matched_dirs += st.matched_dirs
                agg.matched_symlinks += st.matched_symlinks
                agg.matched_hardlinks += st.matched_hardlinks
                agg.frames_read += st.frames_read
                agg.nfs_bytes_read += st.nfs_bytes_read
                agg.bytes_written += st.bytes_written
                agg.seconds_total += st.seconds_total
                agg.fallback_naive = agg.fallback_naive or st.fallback_naive
        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(agg)


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
    """Extract specific paths from a backup (one or more shard archives) into a
    chosen destination."""

    def __init__(self, archives, paths: list[str], parent=None, *, label: str = "",
                 original_sources: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Extract from backup")
        self.resize(900, 600)

        # Accept a single Path (legacy) or a list of shard archive paths.
        self._archives = [archives] if isinstance(archives, Path) else list(archives)
        self._paths = paths
        # Original filesystem root(s) the files came from (from the plan/descriptor).
        # Archive members are stored root-rooted (./home/kim/…), so restoring to
        # the original location means extracting into "/".
        self._original_sources = original_sources or []
        self._thread: QThread | None = None
        self._worker: _ExtractWorker | None = None

        layout = QVBoxLayout(self)

        info = QFormLayout()
        shown = label or (str(self._archives[0]) if len(self._archives) == 1
                          else f"{len(self._archives)} shards")
        info.addRow("Backup:", QLabel(shown))
        info.addRow("Paths:", QLabel(f"{len(paths)} selected"))
        layout.addLayout(info)

        self._paths_list = QListWidget()
        self._paths_list.setMaximumHeight(120)
        for p in paths:
            self._paths_list.addItem(QListWidgetItem(p))
        layout.addWidget(self._paths_list)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        default_dest = str(Path.home() / "Restored"
                           / self._archives[0].stem.split(".")[0])
        self._dest = QLineEdit(default_dest)
        dest_row.addWidget(self._dest, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        dest_row.addWidget(browse)
        if self._original_sources:
            original = QPushButton("Original location…")
            original.setToolTip(
                "Restore files back to where they came from ("
                + ", ".join(self._original_sources) + ") — overwrites live files.")
            original.clicked.connect(self._on_use_original)
            dest_row.addWidget(original)
        layout.addLayout(dest_row)
        if self._original_sources:
            hint = QLabel("These files were originally under: <b>"
                          + ", ".join(self._original_sources) + "</b>")
            hint.setStyleSheet("color: #57606a; padding: 0 4px;")
            layout.addWidget(hint)

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

    def _on_use_original(self) -> None:
        """Set the destination to the filesystem root so members restore to their
        original absolute paths — with a clear overwrite warning first."""
        from PyQt6.QtWidgets import QMessageBox
        r = QMessageBox.warning(
            self, "Restore to original location?",
            "This restores the selected files to their <b>original locations</b> under "
            + ", ".join(self._original_sources)
            + ", <b>overwriting</b> any existing files there.<br><br>"
            "System paths may require running TimeTraveller as root. To stage the "
            "files somewhere safe instead, use <b>Browse…</b>.<br><br>Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._dest.setText("/")

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
        self._worker = _ExtractWorker(list(self._archives), list(self._paths), dest)
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
