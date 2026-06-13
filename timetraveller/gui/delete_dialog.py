"""Type-to-confirm delete dialog for archive management.

The destructive guard is deliberately NOT a plain OK button — reflexive clicking
defeats that. Instead the user must type a short identifier of the SPECIFIC
target, which forces them to read *what* they are about to destroy. Matching is
normalized (case-insensitive, internal whitespace collapsed, trimmed) so it isn't
a typing-school exercise, and the exact phrase is shown on screen.

The pure helpers (`normalize`, `*_token`) live here too so the match logic is
unit-testable without a QApplication.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)


def normalize(s: str) -> str:
    """Case-insensitive, internal-whitespace-collapsed, trimmed."""
    return re.sub(r"\s+", " ", s).strip().lower()


def cycle_token(plan_name: str, cycle_id: str) -> str:
    """Confirm phrase for a whole cycle: '<plan> <date>'."""
    return f"{plan_name} {cycle_id[:10]}"


def set_token(plan_name: str, kind: str, date_started: str) -> str:
    """Confirm phrase for one logical backup: '<plan> <kind> <date>'.

    No shard index — it deliberately names the whole set, never a single shard,
    matching the "delete the set, never a shard" rule.
    """
    return f"{plan_name} {kind} {date_started[:10]}".strip()


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, divisor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < divisor * 1024:
            return f"{n/divisor:.1f} {unit}"
    return f"{n/1024**3:.1f} GB"


class DeleteConfirmDialog(QDialog):
    """Blast-radius disclosure + type-to-confirm gate. `exec()` returns Accepted
    only after the typed phrase matches (the Delete button stays disabled until
    then), so callers can safely pass --force to the worker on acceptance."""

    def __init__(self, *, title: str, summary: str, token: str,
                 files: list[str], total_bytes: int, dependents: int = 0,
                 newest_complete: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(540)
        self._token = token

        layout = QVBoxLayout(self)

        head = QLabel(summary)
        head.setWordWrap(True)
        head.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(head)

        # Blast radius — built entirely from manifest data (no mount access).
        shown = files[:12]
        more = len(files) - len(shown)
        files_html = "<br>".join(f"&nbsp;&nbsp;<code>{f}</code>" for f in shown)
        if more > 0:
            files_html += f"<br>&nbsp;&nbsp;… and {more} more"
        radius = QLabel(
            f"<b>This permanently deletes</b> {len(files)} shard archive(s) "
            f"(plus their <code>.idx.zst</code> / <code>.frames.json</code> "
            f"sidecars), freeing <b>{_human(total_bytes)}</b>:<br>{files_html}"
        )
        radius.setWordWrap(True)
        radius.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(radius)

        if dependents:
            warn = QLabel(
                f"⚠️ This full backup has <b>{dependents}</b> dependent "
                f"incremental backup(s); deleting it makes them unrestorable."
            )
            warn.setStyleSheet("color: #cc8000;")
            warn.setWordWrap(True)
            layout.addWidget(warn)
        if newest_complete:
            warn2 = QLabel(
                "⚠️ This is the <b>newest complete backup</b> — automatic "
                "retention never removes it. Deleting it leaves no current full."
            )
            warn2.setStyleSheet("color: #cf222e;")
            warn2.setWordWrap(True)
            layout.addWidget(warn2)

        prompt = QLabel(f"To confirm, type:&nbsp; <code><b>{token}</b></code>")
        prompt.setTextFormat(Qt.TextFormat.RichText)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(token)
        self._edit.textChanged.connect(self._validate)
        layout.addWidget(self._edit)

        self._match_label = QLabel(" ")
        self._match_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._match_label)

        buttons = QDialogButtonBox()
        self._cancel = QPushButton("Cancel")
        self._delete = QPushButton("Delete")
        self._delete.setStyleSheet("color: #cf222e; font-weight: bold;")
        self._delete.setEnabled(False)
        buttons.addButton(self._cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self._delete, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)

        self._cancel.clicked.connect(self.reject)
        self._delete.clicked.connect(self.accept)
        # Focus + default on Cancel; the destructive button is neither.
        self._cancel.setDefault(True)
        self._cancel.setFocus()

    def _validate(self, text: str) -> None:
        ok = normalize(text) == normalize(self._token)
        self._delete.setEnabled(ok)
        if not text:
            self._match_label.setText(" ")
        elif ok:
            self._match_label.setText(
                "<span style='color:#2da44e'>✓ matches</span>")
        else:
            self._match_label.setText(
                "<span style='color:#cf222e'>✗ does not match yet</span>")
