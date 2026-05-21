"""Schedule editor.

Layout:

    Schedule mode: ( Weekly ) ( Monthly )

    [Weekly group, visible when mode=weekly]
      Full backup days: [Mon][Tue][Wed][Thu][Fri][Sat][Sun]
      Full backup time: HH:MM

      Incremental mode: ( Every day except full ) ( Specific weekdays ) ( Disabled )
        days (if specific):  [Mon][Tue][Wed][Thu][Fri][Sat][Sun]
        time: HH:MM

    [Monthly group, visible when mode=monthly]
      Full backup day of month: [N] (1-28)
      Full backup time: HH:MM

      Incremental mode: ( Every N days ) ( Specific weekdays ) ( Disabled )
        N (if every-N):        [N] (2-28)
        days (if specific):    [Mon][Tue][Wed][Thu][Fri][Sat][Sun]
        time: HH:MM

    Cron preview: [read-only multi-line]
    [Install schedule] [Uninstall schedule]
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QTime, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QStackedWidget,
    QTimeEdit, QVBoxLayout, QWidget,
)

from ..config import SYSTEM_PLAN_NAMES, WEEKDAYS, FullSchedule, IncrSchedule, PlanConfig, Schedule
from ..schedule import find_block, is_block_suspended, render_block

DAY_LABELS = [("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"),
              ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun")]


# Section-header style: bold, ~25% larger than the default control font.
# Applied via QLabel.setStyleSheet so the two section headers ("Full backup"
# and "Incremental backup") match visually regardless of the system theme.
_SECTION_HEADER_CSS = "font-weight: bold; font-size: 13pt;"

# Vertical breathing room above each section header.
_SECTION_SPACING = 12


def _style_section_header_label(label: QLabel) -> None:
    label.setStyleSheet(_SECTION_HEADER_CSS)


def _make_weekday_row(parent_layout) -> dict[str, QCheckBox]:
    row = QHBoxLayout()
    boxes: dict[str, QCheckBox] = {}
    for code, label in DAY_LABELS:
        cb = QCheckBox(label)
        row.addWidget(cb)
        boxes[code] = cb
    row.addStretch(1)
    parent_layout.addLayout(row)
    return boxes


class _WeeklyPage(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Full backup contents — unboxed; the SchedulePanel hoists the "Full
        # backup" header out so it can sit above the schedule-mode radio.
        full_w = QWidget()
        fl = QVBoxLayout(full_w)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(QLabel("Run on these weekday(s):"))
        self.full_days = _make_weekday_row(fl)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Time:"))
        self.full_time = QTimeEdit()
        self.full_time.setDisplayFormat("HH:mm")
        time_row.addWidget(self.full_time)
        time_row.addStretch(1)
        fl.addLayout(time_row)
        layout.addWidget(full_w)

        # Incremental backup section. Parallel to Full backup: a styled label
        # header (with breathing room above) and unframed contents below.
        incr_header = QLabel("Incremental backup")
        _style_section_header_label(incr_header)
        layout.addSpacing(_SECTION_SPACING)
        layout.addWidget(incr_header)
        incr_w = QWidget()
        il = QVBoxLayout(incr_w)
        il.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Run:"))
        self.incr_except = QRadioButton("Every day except full days")
        self.incr_weekdays = QRadioButton("Specific weekdays")
        self.incr_disabled = QRadioButton("Disabled")
        self._incr_group = QButtonGroup(self)
        for btn in (self.incr_except, self.incr_weekdays, self.incr_disabled):
            self._incr_group.addButton(btn)
            mode_row.addWidget(btn)
        mode_row.addStretch(1)
        il.addLayout(mode_row)
        self._incr_days_label = QLabel("Days:")
        il.addWidget(self._incr_days_label)
        self.incr_days = _make_weekday_row(il)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Time:"))
        self.incr_time = QTimeEdit()
        self.incr_time.setDisplayFormat("HH:mm")
        time_row.addWidget(self.incr_time)
        time_row.addStretch(1)
        il.addLayout(time_row)
        layout.addWidget(incr_w)
        layout.addStretch(1)

        # Wire change signals.
        for cb in self.full_days.values():
            cb.toggled.connect(self.changed.emit)
        for cb in self.incr_days.values():
            cb.toggled.connect(self.changed.emit)
        self.full_time.timeChanged.connect(self.changed.emit)
        self.incr_time.timeChanged.connect(self.changed.emit)
        for btn in (self.incr_except, self.incr_weekdays, self.incr_disabled):
            btn.toggled.connect(self._on_incr_mode)
            btn.toggled.connect(self.changed.emit)

        self.incr_except.setChecked(True)
        self._on_incr_mode()

    def _on_incr_mode(self) -> None:
        weekdays_visible = self.incr_weekdays.isChecked()
        time_enabled = not self.incr_disabled.isChecked()
        self._incr_days_label.setVisible(weekdays_visible)
        for cb in self.incr_days.values():
            cb.setVisible(weekdays_visible)
        self.incr_time.setEnabled(time_enabled)


class _MonthlyPage(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Full backup contents — unboxed; the SchedulePanel hoists the "Full
        # backup" header out so it can sit above the schedule-mode radio.
        full_w = QWidget()
        fl = QFormLayout(full_w)
        fl.setContentsMargins(0, 0, 0, 0)
        self.full_dom = QSpinBox()
        self.full_dom.setRange(1, 28)
        self.full_dom.setSuffix("  (1-28; capped to avoid month-end surprises)")
        self.full_time = QTimeEdit()
        self.full_time.setDisplayFormat("HH:mm")
        fl.addRow("Day of month:", self.full_dom)
        fl.addRow("Time:", self.full_time)
        layout.addWidget(full_w)

        # Incremental backup section. Parallel to Full backup: a styled label
        # header (with breathing room above) and unframed contents below.
        incr_header = QLabel("Incremental backup")
        _style_section_header_label(incr_header)
        layout.addSpacing(_SECTION_SPACING)
        layout.addWidget(incr_header)
        incr_w = QWidget()
        il = QVBoxLayout(incr_w)
        il.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Run:"))
        self.incr_every_n = QRadioButton("Every N days")
        self.incr_weekdays = QRadioButton("Specific weekdays")
        self.incr_disabled = QRadioButton("Disabled")
        self._incr_group = QButtonGroup(self)
        for btn in (self.incr_every_n, self.incr_weekdays, self.incr_disabled):
            self._incr_group.addButton(btn)
            mode_row.addWidget(btn)
        mode_row.addStretch(1)
        il.addLayout(mode_row)

        n_row = QHBoxLayout()
        self._n_label = QLabel("N:")
        n_row.addWidget(self._n_label)
        self.every_n = QSpinBox()
        self.every_n.setRange(2, 28)
        self.every_n.setValue(3)
        n_row.addWidget(self.every_n)
        n_row.addWidget(QLabel("  (uses cron */N for day-of-month)"))
        n_row.addStretch(1)
        il.addLayout(n_row)

        self._incr_days_label = QLabel("Days:")
        il.addWidget(self._incr_days_label)
        self.incr_days = _make_weekday_row(il)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Time:"))
        self.incr_time = QTimeEdit()
        self.incr_time.setDisplayFormat("HH:mm")
        time_row.addWidget(self.incr_time)
        time_row.addStretch(1)
        il.addLayout(time_row)
        layout.addWidget(incr_w)
        layout.addStretch(1)

        self.full_dom.valueChanged.connect(self.changed.emit)
        self.full_time.timeChanged.connect(self.changed.emit)
        self.every_n.valueChanged.connect(self.changed.emit)
        self.incr_time.timeChanged.connect(self.changed.emit)
        for cb in self.incr_days.values():
            cb.toggled.connect(self.changed.emit)
        for btn in (self.incr_every_n, self.incr_weekdays, self.incr_disabled):
            btn.toggled.connect(self._on_incr_mode)
            btn.toggled.connect(self.changed.emit)

        self.incr_every_n.setChecked(True)
        self._on_incr_mode()

    def _on_incr_mode(self) -> None:
        n_visible = self.incr_every_n.isChecked()
        weekdays_visible = self.incr_weekdays.isChecked()
        time_enabled = not self.incr_disabled.isChecked()
        self._n_label.setVisible(n_visible)
        self.every_n.setVisible(n_visible)
        self._incr_days_label.setVisible(weekdays_visible)
        for cb in self.incr_days.values():
            cb.setVisible(weekdays_visible)
        self.incr_time.setEnabled(time_enabled)


class SchedulePanel(QWidget):
    changed = pyqtSignal()
    install_requested = pyqtSignal(bool)  # True=install, False=uninstall
    suspend_requested = pyqtSignal(bool)  # True=suspend, False=resume

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: PlanConfig | None = None

        layout = QVBoxLayout(self)

        # Section header for the Full backup area. Hoisted up here (above the
        # Schedule mode radio) so users see "Full backup" before being asked
        # how its cadence should be picked. Hidden when an archive plan is
        # loaded — see load_plan.
        self._full_header = QLabel("Full backup")
        _style_section_header_label(self._full_header)
        layout.addWidget(self._full_header)

        # Top: mode radio. Hidden when an archive plan is loaded.
        self._mode_row_w = QWidget()
        mode_row = QHBoxLayout(self._mode_row_w)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(QLabel("Schedule mode:"))
        self.mode_weekly = QRadioButton("Weekly")
        self.mode_monthly = QRadioButton("Monthly")
        self._mode_group = QButtonGroup(self)
        for btn in (self.mode_weekly, self.mode_monthly):
            self._mode_group.addButton(btn)
            mode_row.addWidget(btn)
        mode_row.addStretch(1)
        layout.addWidget(self._mode_row_w)

        # Archive-plan notice — shown in place of the schedule editor when
        # plan.schedule.mode == "archive".
        self._archive_notice = QLabel(
            "<b>Archive plan</b> — no schedule. Backups run only when invoked "
            "manually (CLI <code>--kind full</code> / <code>--kind incr</code>, "
            "or the Run button on the Archives tab).<br>"
            "<i>To put this plan back on a schedule, switch its type to "
            "Active on the Plan tab.</i>"
        )
        self._archive_notice.setWordWrap(True)
        self._archive_notice.setVisible(False)
        self._archive_notice.setStyleSheet("padding: 12px;")
        layout.addWidget(self._archive_notice)

        # Stacked weekly/monthly pages.
        self._stack = QStackedWidget()
        self._weekly = _WeeklyPage()
        self._monthly = _MonthlyPage()
        self._stack.addWidget(self._weekly)
        self._stack.addWidget(self._monthly)
        layout.addWidget(self._stack)

        # Preview.
        self._preview_box = QGroupBox("Cron entries that will be installed")
        pl = QVBoxLayout(self._preview_box)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._preview.setFont(mono)
        self._preview.setMaximumHeight(150)
        pl.addWidget(self._preview)
        layout.addWidget(self._preview_box)

        # Status indicator (computed from user crontab for home plan).
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("<b>Status:</b>"))
        self._status_label = QLabel("(unknown)")
        status_row.addWidget(self._status_label)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        # Install / Uninstall / Suspend / Resume.
        btn_row = QHBoxLayout()
        self._install_btn = QPushButton("Install schedule")
        self._uninstall_btn = QPushButton("Uninstall schedule")
        self._suspend_btn = QPushButton("Suspend")
        self._resume_btn = QPushButton("Resume")
        for b in (self._install_btn, self._uninstall_btn, self._suspend_btn, self._resume_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # Wiring.
        self.mode_weekly.toggled.connect(self._on_mode_change)
        self.mode_monthly.toggled.connect(self._on_mode_change)
        self._weekly.changed.connect(self._on_changed)
        self._monthly.changed.connect(self._on_changed)
        self._install_btn.clicked.connect(lambda: self.install_requested.emit(True))
        self._uninstall_btn.clicked.connect(lambda: self.install_requested.emit(False))
        self._suspend_btn.clicked.connect(lambda: self.suspend_requested.emit(True))
        self._resume_btn.clicked.connect(lambda: self.suspend_requested.emit(False))

        self.mode_weekly.setChecked(True)

    # ---------- load / save ----------

    def load_plan(self, plan: PlanConfig) -> None:
        self._plan = plan
        sch = plan.schedule

        # Archive plans replace the entire schedule editor with a notice and
        # disable the install/suspend buttons (cron is not used).
        is_archive = sch.mode == "archive"
        self._full_header.setVisible(not is_archive)
        self._mode_row_w.setVisible(not is_archive)
        self._stack.setVisible(not is_archive)
        self._preview_box.setVisible(not is_archive)
        self._archive_notice.setVisible(is_archive)
        for b in (self._install_btn, self._uninstall_btn,
                  self._suspend_btn, self._resume_btn):
            b.setEnabled(not is_archive)
        if is_archive:
            self._status_label.setText("<i>Archive plan — no schedule installed</i>")
            return

        # Mode.
        if sch.mode == "monthly":
            self.mode_monthly.setChecked(True)
            self._stack.setCurrentWidget(self._monthly)
        else:
            self.mode_weekly.setChecked(True)
            self._stack.setCurrentWidget(self._weekly)

        # Weekly page.
        full_set = set(sch.full.days)
        for code, cb in self._weekly.full_days.items():
            cb.setChecked(code in full_set)
        h, m = self._parse_time(sch.full.time)
        self._weekly.full_time.setTime(QTime(h, m))

        incr_mode = sch.incr.mode
        if incr_mode == "except_full":
            self._weekly.incr_except.setChecked(True)
        elif incr_mode == "disabled":
            self._weekly.incr_disabled.setChecked(True)
        elif incr_mode == "weekdays" and sch.mode == "weekly":
            self._weekly.incr_weekdays.setChecked(True)
        else:
            # incr_mode might be every_n_days (only valid in monthly) — when
            # loading a monthly plan, just default the weekly page's incr UI
            # to "except_full" so the page is internally consistent.
            self._weekly.incr_except.setChecked(True)

        incr_set = set(sch.incr.days)
        for code, cb in self._weekly.incr_days.items():
            cb.setChecked(code in incr_set)
        h, m = self._parse_time(sch.incr.time)
        self._weekly.incr_time.setTime(QTime(h, m))

        # Monthly page.
        self._monthly.full_dom.setValue(sch.full.day_of_month)
        h, m = self._parse_time(sch.full.time)
        self._monthly.full_time.setTime(QTime(h, m))

        if incr_mode == "every_n_days":
            self._monthly.incr_every_n.setChecked(True)
        elif incr_mode == "weekdays" and sch.mode == "monthly":
            self._monthly.incr_weekdays.setChecked(True)
        elif incr_mode == "disabled":
            self._monthly.incr_disabled.setChecked(True)
        else:
            self._monthly.incr_every_n.setChecked(True)
        self._monthly.every_n.setValue(max(2, int(sch.incr.every_n_days or 3)))

        for code, cb in self._monthly.incr_days.items():
            cb.setChecked(code in incr_set)
        h, m = self._parse_time(sch.incr.time)
        self._monthly.incr_time.setTime(QTime(h, m))

        self._refresh_preview()
        self.refresh_status()

    def to_plan(self) -> PlanConfig:
        assert self._plan is not None
        # Archive plans pin the schedule; the editor controls are hidden and
        # have no meaningful values to read back.
        if self._plan.schedule.mode == "archive":
            return self._plan
        if self.mode_weekly.isChecked():
            full = FullSchedule(
                days=self._collect_weekdays(self._weekly.full_days),
                day_of_month=self._monthly.full_dom.value(),
                time=self._weekly.full_time.time().toString("HH:mm"),
            )
            if self._weekly.incr_except.isChecked():
                incr_mode = "except_full"
            elif self._weekly.incr_disabled.isChecked():
                incr_mode = "disabled"
            else:
                incr_mode = "weekdays"
            incr = IncrSchedule(
                mode=incr_mode,
                days=self._collect_weekdays(self._weekly.incr_days),
                every_n_days=self._monthly.every_n.value(),
                time=self._weekly.incr_time.time().toString("HH:mm"),
            )
            schedule = Schedule(mode="weekly", full=full, incr=incr)
        else:
            full = FullSchedule(
                days=self._collect_weekdays(self._weekly.full_days) or ["sun"],
                day_of_month=self._monthly.full_dom.value(),
                time=self._monthly.full_time.time().toString("HH:mm"),
            )
            if self._monthly.incr_every_n.isChecked():
                incr_mode = "every_n_days"
            elif self._monthly.incr_disabled.isChecked():
                incr_mode = "disabled"
            else:
                incr_mode = "weekdays"
            incr = IncrSchedule(
                mode=incr_mode,
                days=self._collect_weekdays(self._monthly.incr_days),
                every_n_days=self._monthly.every_n.value(),
                time=self._monthly.incr_time.time().toString("HH:mm"),
            )
            schedule = Schedule(mode="monthly", full=full, incr=incr)
        return replace(self._plan, schedule=schedule)

    # ---------- helpers ----------

    @staticmethod
    def _collect_weekdays(boxes: dict[str, QCheckBox]) -> list[str]:
        return [code for code in WEEKDAYS if boxes.get(code) and boxes[code].isChecked()]

    @staticmethod
    def _parse_time(hhmm: str) -> tuple[int, int]:
        parts = hhmm.split(":")
        return int(parts[0]), int(parts[1])

    def _on_mode_change(self) -> None:
        if self.mode_weekly.isChecked():
            self._stack.setCurrentWidget(self._weekly)
        else:
            self._stack.setCurrentWidget(self._monthly)
        self._on_changed()

    def _on_changed(self) -> None:
        self._refresh_preview()
        self.changed.emit()

    def _refresh_preview(self) -> None:
        if not self._plan:
            return
        try:
            proposed = self.to_plan()
            block = render_block(proposed, "/usr/local/bin/timetraveller-backup")
            self._preview.setPlainText(block)
        except Exception as e:  # noqa: BLE001
            self._preview.setPlainText(f"(invalid schedule: {e})")

    # ---------- status ----------

    def refresh_status(self) -> None:
        """Inspect the user crontab for the home plan, update label + button states.

        For the system plan we'd need root to read root's crontab; for now we
        just show 'Unknown' and leave all buttons enabled.
        """
        if not self._plan:
            return
        if self._plan.plan_name in SYSTEM_PLAN_NAMES:
            self._status_label.setText("<i>Unknown</i> (root-owned crontab; "
                                       "use the buttons to manage)")
            for b in (self._install_btn, self._uninstall_btn,
                      self._suspend_btn, self._resume_btn):
                b.setEnabled(True)
            return

        import subprocess
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        text = r.stdout if r.returncode == 0 else ""
        block = find_block(text, self._plan.plan_name)
        if block is None:
            self._status_label.setText("<i>Not installed</i>")
            self._install_btn.setEnabled(True)
            self._uninstall_btn.setEnabled(False)
            self._suspend_btn.setEnabled(False)
            self._resume_btn.setEnabled(False)
            return
        suspended = is_block_suspended(text, self._plan.plan_name)
        if suspended is True:
            self._status_label.setText("<span style='color:#cc8000'>Suspended</span>")
            self._install_btn.setEnabled(True)
            self._uninstall_btn.setEnabled(True)
            self._suspend_btn.setEnabled(False)
            self._resume_btn.setEnabled(True)
        elif suspended is False:
            self._status_label.setText("<span style='color:#2da44e'>Active</span>")
            self._install_btn.setEnabled(True)
            self._uninstall_btn.setEnabled(True)
            self._suspend_btn.setEnabled(True)
            self._resume_btn.setEnabled(False)
        else:
            self._status_label.setText("Installed (no entries detected)")
            self._install_btn.setEnabled(True)
            self._uninstall_btn.setEnabled(True)
            self._suspend_btn.setEnabled(False)
            self._resume_btn.setEnabled(False)
