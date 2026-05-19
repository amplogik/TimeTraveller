"""Theme detection and application.

PyQt6 doesn't follow the desktop's light/dark setting under GNOME — Qt's
"system" style there is fxgtk2 which is dated, and it ignores the GNOME
color-scheme entirely. We work around this by:

  1. Forcing the Fusion style, which looks consistent across desktops.
  2. Detecting GNOME's color-scheme via gsettings (or KDE's via the env)
     and applying a custom palette if the user is in dark mode.

This is the approach recommended in Qt's own documentation for GNOME
integration.
"""

from __future__ import annotations

import os
import subprocess

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication


def is_dark_mode() -> bool:
    """Best-effort detection of the desktop's dark-mode setting."""
    # KDE sets this directly.
    if os.environ.get("KDE_SESSION_VERSION"):
        # KDE leaves Qt's default palette to follow the system, so we don't
        # need to override; we report False so we don't double-apply.
        return False

    # GNOME (Ubuntu, Fedora, etc.) — gsettings is authoritative.
    try:
        r = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            value = r.stdout.strip().strip("'\"")
            return "dark" in value.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Last resort: many freedesktop environments expose GTK_THEME.
    gtk_theme = os.environ.get("GTK_THEME", "")
    if "dark" in gtk_theme.lower():
        return True

    return False


def _dark_palette() -> QPalette:
    """Build a dark palette in the style of GNOME's Adwaita-dark."""
    p = QPalette()
    base = QColor(36, 36, 36)
    alt_base = QColor(45, 45, 45)
    text = QColor(232, 232, 232)
    window = QColor(28, 28, 28)
    highlight = QColor(74, 144, 226)
    button = QColor(48, 48, 48)
    disabled = QColor(120, 120, 120)

    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Button, button)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 60, 60))
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
    p.setColor(QPalette.ColorRole.Link, QColor(120, 170, 230))

    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    return p


def apply_theme(app: QApplication) -> None:
    """Configure the application style + palette based on desktop preferences."""
    app.setStyle("Fusion")
    if is_dark_mode():
        app.setPalette(_dark_palette())
