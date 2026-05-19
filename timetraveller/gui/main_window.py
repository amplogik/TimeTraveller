"""TimeTraveller main window.

Left sidebar: list of plans whose configs exist (plus a "+ New plan" entry).
Center: tabbed editor for the selected plan (Plan, Schedule).
Toolbar: Run Full, Run Incr, Dry Run, Show Mounts, Save.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QInputDialog, QListWidget, QMainWindow, QMessageBox, QSplitter, QStatusBar,
    QTabWidget, QToolBar, QWidget,
)

from .. import config as configlib
from ..config import PlanConfig
from .archive_panel import ArchivePanel
from .plan_panel import PlanPanel
from .run_dialog import WorkerRunDialog
from .schedule_panel import SchedulePanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TimeTraveller")
        self.resize(1100, 720)

        self._plans: dict[str, PlanConfig] = {}
        self._current_plan_name: str | None = None
        self._dirty = False

        # ----- toolbar -----
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._save_act = QAction("Save", self)
        self._save_act.setShortcut(QKeySequence.StandardKey.Save)
        self._save_act.triggered.connect(self._save_current_plan)
        tb.addAction(self._save_act)

        tb.addSeparator()

        self._run_full_act = QAction("Run full now", self)
        self._run_full_act.triggered.connect(lambda: self._run_now("full"))
        tb.addAction(self._run_full_act)

        self._run_incr_act = QAction("Run incr now", self)
        self._run_incr_act.triggered.connect(lambda: self._run_now("incr"))
        tb.addAction(self._run_incr_act)

        tb.addSeparator()

        self._dry_run_act = QAction("Dry run", self)
        self._dry_run_act.triggered.connect(lambda: self._spawn_worker(
            "Dry run", ["--plan", self._current_plan_name or "",
                        "--config", str(self._config_path_for(self._current_plan_name or "")),
                        "--dry-run", "--kind", "auto"]))
        tb.addAction(self._dry_run_act)

        self._mounts_act = QAction("Show mounts", self)
        self._mounts_act.triggered.connect(lambda: self._spawn_worker(
            "Show mounts", ["--plan", self._current_plan_name or "",
                            "--config", str(self._config_path_for(self._current_plan_name or "")),
                            "--show-mounts"]))
        tb.addAction(self._mounts_act)

        self._archives_act = QAction("List archives", self)
        self._archives_act.triggered.connect(lambda: self._spawn_worker(
            "List archives", ["--plan", self._current_plan_name or "",
                              "--config", str(self._config_path_for(self._current_plan_name or "")),
                              "--list-archives"]))
        tb.addAction(self._archives_act)

        tb.addSeparator()

        self._new_plan_act = QAction("New plan…", self)
        self._new_plan_act.triggered.connect(self._new_plan)
        tb.addAction(self._new_plan_act)

        # ----- central splitter -----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        self._plan_list = QListWidget()
        self._plan_list.setMaximumWidth(220)
        self._plan_list.currentTextChanged.connect(self._on_plan_selected)
        splitter.addWidget(self._plan_list)

        self._tabs = QTabWidget()
        self._plan_panel = PlanPanel()
        self._schedule_panel = SchedulePanel()
        self._archive_panel = ArchivePanel()
        self._tabs.addTab(self._plan_panel, "Plan")
        self._tabs.addTab(self._schedule_panel, "Schedule")
        self._tabs.addTab(self._archive_panel, "Archives")
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(1, 1)

        self._plan_panel.changed.connect(self._mark_dirty)
        self._schedule_panel.changed.connect(self._mark_dirty)
        self._schedule_panel.install_requested.connect(self._on_install_request)
        self._schedule_panel.suspend_requested.connect(self._on_suspend_request)
        # Refresh the archive list when the user switches back to the Archives tab.
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # ----- status bar -----
        self.setStatusBar(QStatusBar())
        self._update_status()

        # Initial load.
        self._discover_plans()
        if self._plan_list.count():
            self._plan_list.setCurrentRow(0)
        else:
            self._set_actions_enabled(False)

    # ---------- plan discovery + selection ----------

    def _config_path_for(self, plan_name: str) -> Path:
        if plan_name == "system":
            return configlib.system_config_path(plan_name)
        return configlib.user_config_path(plan_name)

    def _discover_plans(self) -> None:
        self._plan_list.clear()
        self._plans.clear()
        # User configs.
        user_dir = configlib.user_config_path("dummy").parent
        if user_dir.exists():
            for f in sorted(user_dir.glob("*.yaml")):
                try:
                    plan = configlib.load(f)
                    self._plans[plan.plan_name] = plan
                    self._plan_list.addItem(plan.plan_name)
                except Exception as e:  # noqa: BLE001
                    self.statusBar().showMessage(f"Skipped {f}: {e}", 5000)
        # System config.
        sysp = configlib.system_config_path("system")
        if sysp.exists() and os.access(sysp, os.R_OK):
            try:
                plan = configlib.load(sysp)
                self._plans[plan.plan_name] = plan
                self._plan_list.addItem(plan.plan_name)
            except Exception as e:  # noqa: BLE001
                self.statusBar().showMessage(f"Skipped {sysp}: {e}", 5000)

    def _on_plan_selected(self, name: str) -> None:
        if not name:
            return
        if self._dirty and self._current_plan_name and self._current_plan_name != name:
            if not self._confirm_discard():
                # Revert selection.
                items = self._plan_list.findItems(self._current_plan_name, Qt.MatchFlag.MatchExactly)
                if items:
                    self._plan_list.setCurrentItem(items[0])
                return
        self._current_plan_name = name
        plan = self._plans.get(name)
        if plan is None:
            return
        self._plan_panel.load_plan(plan)
        self._schedule_panel.load_plan(plan)
        self._archive_panel.load_plan(plan)
        self._dirty = False
        self._update_status()
        self._set_actions_enabled(True)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for act in (self._save_act, self._run_full_act, self._run_incr_act,
                    self._dry_run_act, self._mounts_act, self._archives_act):
            act.setEnabled(enabled)

    # ---------- save / dirty ----------

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_status()

    def _update_status(self) -> None:
        if not self._current_plan_name:
            self.statusBar().showMessage("No plan selected.")
            return
        suffix = "  •  unsaved changes" if self._dirty else ""
        self.statusBar().showMessage(f"Plan: {self._current_plan_name}{suffix}")

    def _save_current_plan(self) -> None:
        if not self._current_plan_name:
            return
        from dataclasses import replace
        try:
            merged = self._plan_panel.to_plan()
            merged = replace(merged, schedule=self._schedule_panel.to_plan().schedule)
            merged.validate()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Validation error", str(e))
            return
        path = self._config_path_for(merged.plan_name)
        if merged.plan_name == "system" and not os.access(path.parent, os.W_OK):
            QMessageBox.warning(
                self, "Cannot save system plan",
                f"This GUI cannot write {path} (root-owned). For now, edit it as root.\n"
                f"In a future release a pkexec helper will handle this.",
            )
            return
        try:
            configlib.save(merged, path)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self._plans[merged.plan_name] = merged
        self._dirty = False
        self.statusBar().showMessage(f"Saved {path}", 4000)
        self._update_status()

    def _confirm_discard(self) -> bool:
        r = QMessageBox.question(
            self, "Discard changes?",
            "There are unsaved changes. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return r == QMessageBox.StandardButton.Discard

    # ---------- run-now ----------

    def _run_now(self, kind: str) -> None:
        if not self._current_plan_name:
            return
        args = [
            "--plan", self._current_plan_name,
            "--config", str(self._config_path_for(self._current_plan_name)),
            "--kind", kind, "--manual",
        ]
        self._spawn_worker(f"Run {kind} now", args)

    def _spawn_worker(self, title: str, args: list[str]) -> None:
        dlg = WorkerRunDialog(title, args, parent=self)
        dlg.start()
        dlg.exec()
        # A backup/prune/reindex may have changed the archive list — refresh
        # so the Archives tab reflects reality if the user is on it next.
        self._archive_panel.refresh()

    def _on_tab_changed(self, idx: int) -> None:
        # Cheap refresh whenever the user lands on Archives; the list is small.
        if self._tabs.widget(idx) is self._archive_panel:
            self._archive_panel.refresh()

    # ---------- schedule install ----------

    def _on_install_request(self, install: bool) -> None:
        if not self._current_plan_name:
            return
        if self._dirty:
            if not self._confirm_save_before_install():
                return
        args = [
            "--plan", self._current_plan_name,
            "--config", str(self._config_path_for(self._current_plan_name)),
        ]
        if install:
            # Pass the path to the dev shim explicitly so the cron entries
            # use a path that exists and passes the helper's allowlist.
            # (`--dev-binary-path` would give us worker.py here since we
            # invoke the worker via `python -m`.)
            repo_root = Path(__file__).resolve().parents[2]
            dev_shim = repo_root / "bin" / "timetraveller-backup"
            args += ["--install-schedule", "--binary-path", str(dev_shim)]
            title = "Install schedule"
        else:
            args += ["--uninstall-schedule"]
            title = "Uninstall schedule"
        self._spawn_worker(title, args)
        self._schedule_panel.refresh_status()

    def _on_suspend_request(self, suspend: bool) -> None:
        if not self._current_plan_name:
            return
        args = [
            "--plan", self._current_plan_name,
            "--config", str(self._config_path_for(self._current_plan_name)),
            "--suspend-schedule" if suspend else "--resume-schedule",
        ]
        title = "Suspend schedule" if suspend else "Resume schedule"
        self._spawn_worker(title, args)
        # Refresh status afterwards.
        self._schedule_panel.refresh_status()

    def _confirm_save_before_install(self) -> bool:
        r = QMessageBox.question(
            self, "Save first?",
            "You have unsaved changes. Save before installing the schedule?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if r == QMessageBox.StandardButton.Save:
            self._save_current_plan()
            return not self._dirty
        return False

    # ---------- new plan ----------

    def _new_plan(self) -> None:
        choices = ["home (default)", "system (default)", "custom…"]
        choice, ok = QInputDialog.getItem(
            self, "New plan", "Create a plan from:", choices, 0, editable=False,
        )
        if not ok:
            return
        if choice.startswith("home"):
            plan = configlib.defaults_home()
        elif choice.startswith("system"):
            plan = configlib.defaults_system()
        else:
            name, ok = QInputDialog.getText(self, "Plan name", "Plan name (letters/digits/-/_):")
            if not ok or not name.strip():
                return
            plan = configlib.defaults_home()
            plan.plan_name = name.strip()

        if plan.plan_name in self._plans:
            QMessageBox.warning(self, "Plan exists",
                                f"Plan {plan.plan_name!r} already exists.")
            return

        path = self._config_path_for(plan.plan_name)
        if plan.plan_name == "system" and not os.access(path.parent.parent, os.W_OK):
            QMessageBox.warning(
                self, "Cannot create system plan",
                f"This GUI cannot write {path}. Create it as root for now.",
            )
            return
        try:
            configlib.save(plan, path)
        except OSError as e:
            QMessageBox.critical(self, "Create failed", str(e))
            return
        self._discover_plans()
        items = self._plan_list.findItems(plan.plan_name, Qt.MatchFlag.MatchExactly)
        if items:
            self._plan_list.setCurrentItem(items[0])

    # ---------- close handling ----------

    def closeEvent(self, event):
        if self._dirty:
            if not self._confirm_discard():
                event.ignore()
                return
        super().closeEvent(event)
