"""TimeTraveller main window.

Left sidebar: list of plans whose configs exist (plus a "+ New plan" entry).
Center: tabbed editor for the selected plan (Plan, Schedule).
Toolbar: Run Full, Run Incr, Dry Run, Show Mounts, Save.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QBrush, QColor, QKeySequence
from PyQt6.QtWidgets import (
    QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox, QSizePolicy,
    QSplitter, QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from .. import config as configlib
from ..config import PlanConfig
from .archive_panel import ArchivePanel
from .help_dialog import HelpDialog
from .plan_panel import PlanPanel
from .reindex_tracker import RecoverTracker, ReindexTracker
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

        self._save_act = QAction("Save Plan", self)
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

        self._help_act = QAction("Help", self)
        self._help_act.setShortcut(QKeySequence.StandardKey.HelpContents)
        self._help_act.triggered.connect(self._show_help)
        tb.addAction(self._help_act)

        self._remove_plan_act = QAction("Remove Plan…", self)
        self._remove_plan_act.triggered.connect(self._on_remove_plan_request)
        tb.addAction(self._remove_plan_act)
        # Red tint so the destructive action is unmistakable.
        remove_btn = tb.widgetForAction(self._remove_plan_act)
        if remove_btn is not None:
            remove_btn.setStyleSheet(
                "QToolButton { color: #cf222e; font-weight: bold; }"
                "QToolButton:hover { background: rgba(207, 34, 46, 0.12); }"
            )

        # Push New plan to the right edge — it's the one action that doesn't
        # operate on the currently-selected plan, so visually setting it apart
        # helps avoid accidental clicks.
        tb_spacer = QWidget()
        tb_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(tb_spacer)

        self._new_plan_act = QAction("+ New plan…", self)
        self._new_plan_act.triggered.connect(self._new_plan)
        tb.addAction(self._new_plan_act)
        # Green tint so the create action is unmistakable in the toolbar.
        new_plan_btn = tb.widgetForAction(self._new_plan_act)
        if new_plan_btn is not None:
            new_plan_btn.setStyleSheet(
                "QToolButton { color: #2da44e; font-weight: bold; }"
                "QToolButton:hover { background: rgba(45, 164, 78, 0.12); }"
            )

        # ----- central splitter -----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        self._plan_list = QListWidget()
        self._plan_list.currentTextChanged.connect(self._on_plan_selected)

        plans_container = QWidget()
        plans_container.setMaximumWidth(220)
        pcl = QVBoxLayout(plans_container)
        pcl.setContentsMargins(0, 0, 0, 0)
        plans_header = QLabel("<b>Backup Plans</b>")
        plans_header.setStyleSheet("padding: 6px 8px;")
        pcl.addWidget(plans_header)
        pcl.addWidget(self._plan_list, 1)
        splitter.addWidget(plans_container)

        self._tabs = QTabWidget()
        self._plan_panel = PlanPanel()
        self._schedule_panel = SchedulePanel()
        self._reindex_tracker = ReindexTracker(self)
        self._recover_tracker = RecoverTracker(self)
        self._archive_panel = ArchivePanel(tracker=self._reindex_tracker,
                                           recover_tracker=self._recover_tracker)
        self._tabs.addTab(self._plan_panel, "Plan")
        self._tabs.addTab(self._schedule_panel, "Schedule")
        self._tabs.addTab(self._archive_panel, "Archives")
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(1, 1)

        self._archive_panel.worker_requested.connect(self._on_archive_worker_requested)
        self._plan_panel.changed.connect(self._mark_dirty)
        self._plan_panel.switch_type_requested.connect(self._on_switch_type_request)
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
        if plan_name in configlib.SYSTEM_PLAN_NAMES:
            return configlib.system_config_path(plan_name)
        return configlib.user_config_path(plan_name)

    def _discover_plans(self) -> None:
        self._plan_list.clear()
        self._plans.clear()
        # Build a name → effective path map mirroring resolve_config_path:
        # for system-class plan names, the /etc path wins; for every other
        # plan name, the user path wins. Without this dedupe a user-level
        # system.yaml and an /etc/timetraveller/system.yaml both showed up in
        # the list pointing at the same selection.
        candidates: dict[str, Path] = {}
        user_dir = configlib.user_config_path("dummy").parent
        if user_dir.exists():
            for f in sorted(user_dir.glob("*.yaml")):
                candidates[f.stem] = f
        for name in sorted(configlib.SYSTEM_PLAN_NAMES):
            sysp = configlib.system_config_path(name)
            if sysp.exists() and os.access(sysp, os.R_OK):
                shadowed = candidates.get(name)
                candidates[name] = sysp
                if shadowed is not None and shadowed != sysp:
                    self.statusBar().showMessage(
                        f"Note: {shadowed} is shadowed by {sysp}", 5000,
                    )
        for name in sorted(candidates):
            path = candidates[name]
            try:
                plan = configlib.load(path)
                self._plans[plan.plan_name] = plan
                self._plan_list.addItem(plan.plan_name)
                if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
                    item = self._plan_list.item(self._plan_list.count() - 1)
                    # Warning-orange to flag plans that require root auth to
                    # save or schedule. Tooltip explains.
                    item.setForeground(QBrush(QColor("#cc7a00")))
                    item.setToolTip(
                        "Runs as root — saving and scheduling require admin "
                        "authentication (pkexec)."
                    )
            except Exception as e:  # noqa: BLE001
                self.statusBar().showMessage(f"Skipped {path}: {e}", 5000)

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
        # Pick up any reindex/recovery launched by a prior GUI session for this
        # plan before the panel renders, so the "Indexing/Recovering now" label
        # shows up on first selection rather than after a refresh.
        self._reindex_tracker.adopt(plan.plan_name)
        self._recover_tracker.adopt(plan.plan_name)
        self._archive_panel.load_plan(plan)
        self._dirty = False
        self._update_status()
        self._set_actions_enabled(True)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for act in (self._save_act, self._remove_plan_act,
                    self._run_full_act, self._run_incr_act,
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
        if merged.plan_name in configlib.SYSTEM_PLAN_NAMES and not os.access(path.parent, os.W_OK):
            if not self._save_system_plan_via_pkexec(merged):
                return
        else:
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

    def _on_archive_worker_requested(self, title: str, action_args: list) -> None:
        """Run a delete (or other scoped) worker action the Archives panel asked
        for, injecting --plan/--config from the active plan."""
        if not self._current_plan_name:
            return
        args = [
            "--plan", self._current_plan_name,
            "--config", str(self._config_path_for(self._current_plan_name)),
            *action_args,
        ]
        self._spawn_worker(title, args)

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
            args += ["--install-schedule"]
            override = self._cron_binary_override()
            if override is not None:
                args += ["--binary-path", override]
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

    def _cron_binary_override(self) -> str | None:
        """Return a --binary-path to pass to --install-schedule, or None.

        For system-class plans we always defer to the worker's auto-detection —
        the pkexec helper only accepts canonical installed paths.

        For user-crontab plans we ALSO prefer the worker default when an
        installed shim exists. Only when running from a bare checkout (no
        install.sh, no .deb) do we fall back to the repo's bin/ shim so the
        user crontab entry points at a real, executable script.
        """
        if self._current_plan_name in configlib.SYSTEM_PLAN_NAMES:
            return None
        for path in ("/usr/bin/timetraveller-backup",
                     "/usr/local/bin/timetraveller-backup"):
            if Path(path).exists():
                return None
        repo_root = Path(__file__).resolve().parents[2]
        return str(repo_root / "bin" / "timetraveller-backup")

    # Path to the pkexec helper that writes /etc/timetraveller/<plan>.yaml.
    # Hardcoded — the polkit policy authorises this exact path.
    _WRITE_CONFIG_HELPER = "/usr/libexec/timetraveller-write-system-config"

    def _save_system_plan_via_pkexec(self, plan: PlanConfig) -> bool:
        """Save a system-class plan by piping its YAML through pkexec.

        Returns True on success, False on error or user cancel. Blocks the GUI
        thread for the duration of the pkexec auth prompt; the helper itself
        finishes in well under a second.
        """
        # Validate before invoking pkexec so we don't auth-prompt just to fail
        # at the helper's shape check.
        try:
            plan.validate()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Validation error", str(e))
            return False
        body = yaml.safe_dump(asdict(plan), sort_keys=False, default_flow_style=False)
        r = subprocess.run(
            ["pkexec", self._WRITE_CONFIG_HELPER, plan.plan_name],
            input=body, text=True, capture_output=True,
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip() or f"helper exited {r.returncode}"
            QMessageBox.critical(
                self, f"Save {plan.plan_name} plan failed",
                f"pkexec helper exited {r.returncode}.\n\n{detail}",
            )
            return False
        return True

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

    # ---------- plan type switch ----------

    def _on_switch_type_request(self) -> None:
        """Handle the Plan-type Change… button from PlanPanel."""
        if not self._current_plan_name:
            return
        plan = self._plans.get(self._current_plan_name)
        if plan is None:
            return

        # Switching rewrites the YAML on disk, so any unsaved in-panel edits
        # would either be overwritten or fight with the new state. Require a
        # clean state first.
        if self._dirty:
            QMessageBox.information(
                self, "Save or discard first",
                "Please save or discard your unsaved edits before switching "
                "the plan type.",
            )
            return

        is_archive = plan.schedule.mode == "archive"
        if is_archive:
            self._switch_to_active(plan)
        else:
            self._switch_to_archive(plan)

    def _switch_to_archive(self, plan: configlib.PlanConfig) -> None:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Switch to Archive plan?")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"<b>Switch plan {plan.plan_name!r} to Archive?</b>"
        )
        msg.setInformativeText(
            "All cycles <b>except the most recent</b> will be permanently "
            "deleted from disk. The remaining cycle becomes the archive basis. "
            "<br><br>"
            "After the switch:"
            "<ul>"
            "<li>The plan will not be scheduled (no cron entries).</li>"
            "<li>Retention is set to <i>keep_all</i> — no automatic pruning.</li>"
            "<li>You can still run manual fulls and incrementals.</li>"
            "</ul>"
            "<b>This cannot be undone.</b>"
        )
        proceed_btn = msg.addButton("Switch to Archive", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        if msg.clickedButton() is not proceed_btn:
            return

        args = [
            "--plan", plan.plan_name,
            "--config", str(self._config_path_for(plan.plan_name)),
            "--switch-to-archive",
        ]
        self._spawn_worker(f"Switch {plan.plan_name} to Archive", args)
        self._reload_after_switch()

    def _switch_to_active(self, plan: configlib.PlanConfig) -> None:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Switch to Active plan?")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"<b>Switch plan {plan.plan_name!r} to Active?</b>"
        )
        msg.setInformativeText(
            "The plan will be put on a weekly schedule with default retention "
            "(<i>max_cycles=4</i>). Existing cycles on disk are preserved."
            "<br><br>"
            "<b>Heads up:</b> if the existing full is old, your archive basis "
            "may age out under retention before a new full is taken. Consider "
            "running a new full backup soon after the switch."
        )
        proceed_btn = msg.addButton("Switch to Active", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        if msg.clickedButton() is not proceed_btn:
            return

        args = [
            "--plan", plan.plan_name,
            "--config", str(self._config_path_for(plan.plan_name)),
            "--switch-to-active",
        ]
        self._spawn_worker(f"Switch {plan.plan_name} to Active", args)
        self._reload_after_switch()

    def _reload_after_switch(self) -> None:
        """Re-read plans from disk and reload the panels for the current plan."""
        current = self._current_plan_name
        self._discover_plans()
        if current:
            items = self._plan_list.findItems(current, Qt.MatchFlag.MatchExactly)
            if items:
                self._plan_list.setCurrentItem(items[0])
            plan = self._plans.get(current)
            if plan is not None:
                self._plan_panel.load_plan(plan)
                self._schedule_panel.load_plan(plan)
                self._reindex_tracker.adopt(plan.plan_name)
                self._recover_tracker.adopt(plan.plan_name)
                self._archive_panel.load_plan(plan)
        self._dirty = False
        self._update_status()

    # ---------- help ----------

    def _show_help(self) -> None:
        dlg = HelpDialog(self)
        dlg.exec()

    # ---------- remove plan ----------

    def _on_remove_plan_request(self) -> None:
        if not self._current_plan_name:
            return
        plan = self._plans.get(self._current_plan_name)
        if plan is None:
            return

        # System-class plans live in /etc/timetraveller/ and require root to
        # delete. Rather than silently disabling the button, give the user a
        # concrete next step.
        if plan.plan_name in configlib.SYSTEM_PLAN_NAMES:
            path = self._config_path_for(plan.plan_name)
            QMessageBox.information(
                self, f"Cannot remove {plan.plan_name} plan",
                f"System-class plans live in <code>/etc/timetraveller/</code>. "
                f"This GUI cannot write there. Remove it as root:<br><br>"
                f"<code>sudo rm {path}</code><br><br>"
                f"Then also uninstall its cron entries:<br>"
                f"<code>sudo timetraveller-backup --plan {plan.plan_name} "
                f"--uninstall-schedule</code>",
            )
            return

        archive_dir = plan.archive_dir()
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle(f"Remove plan {plan.plan_name!r}?")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(f"<b>Remove plan {plan.plan_name!r}?</b>")
        msg.setInformativeText(
            "This will uninstall its schedule (if any), clear its local cache, "
            "and delete its config file."
            "<br><br>"
            f"Backup archives at <code>{archive_dir}</code> can be kept on "
            "disk (you can browse to them manually) or deleted now."
        )
        # Cancel | Keep backups | Delete backups (destructive).
        cancel_btn = msg.addButton(QMessageBox.StandardButton.Cancel)
        keep_btn = msg.addButton("Remove plan, keep backups",
                                 QMessageBox.ButtonRole.AcceptRole)
        delete_btn = msg.addButton("Remove plan + delete backups",
                                   QMessageBox.ButtonRole.DestructiveRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return

        remove_backups = clicked is delete_btn
        args = [
            "--plan", plan.plan_name,
            "--config", str(self._config_path_for(plan.plan_name)),
            "--remove-plan",
        ]
        if remove_backups:
            args.append("--remove-backups")
        title = (f"Remove {plan.plan_name} (+ delete backups)" if remove_backups
                 else f"Remove {plan.plan_name}")
        self._spawn_worker(title, args)
        self._reload_after_remove()

    def _reload_after_remove(self) -> None:
        """Re-discover plans after a removal. Select the next plan if any."""
        self._current_plan_name = None
        self._dirty = False
        self._discover_plans()
        if self._plan_list.count():
            self._plan_list.setCurrentRow(0)
        else:
            self._set_actions_enabled(False)
            # Clear panels so they don't display a stale plan.
            self._plan_panel._plan = None
        self._update_status()

    # ---------- new plan ----------

    def _new_plan(self) -> None:
        choices = [
            "home (default — your /home/$USER)",
            "homes (default — all users' /home, runs as root)",
            "system (default — /, runs as root, excludes /home)",
            "custom…",
        ]
        choice, ok = QInputDialog.getItem(
            self, "New plan", "Create a plan from:", choices, 0, editable=False,
        )
        if not ok:
            return
        if choice.startswith("home "):
            plan = configlib.defaults_home()
            if plan.plan_name in self._plans:
                QMessageBox.warning(self, "Plan exists",
                                    f"Plan {plan.plan_name!r} already exists.")
                return
        elif choice.startswith("homes "):
            plan = configlib.defaults_homes()
            if plan.plan_name in self._plans:
                QMessageBox.warning(self, "Plan exists",
                                    f"Plan {plan.plan_name!r} already exists.")
                return
        elif choice.startswith("system "):
            plan = configlib.defaults_system()
            if plan.plan_name in self._plans:
                QMessageBox.warning(self, "Plan exists",
                                    f"Plan {plan.plan_name!r} already exists.")
                return
        else:
            # Loop until the user picks a unique, well-formed name or cancels.
            # The regex matches what schedule.py's cron-marker parser accepts,
            # so names that pass here are safe to install in cron.
            import re
            valid = re.compile(r"^[A-Za-z0-9_-]+$")
            name = ""
            while True:
                name, ok = QInputDialog.getText(
                    self, "Plan name",
                    "Plan name (letters/digits/-/_):",
                    text=name,
                )
                if not ok:
                    return
                name = name.strip()
                if not name:
                    continue
                if not valid.match(name):
                    QMessageBox.warning(
                        self, "Invalid plan name",
                        f"{name!r} contains characters outside letters, digits, "
                        f"'-' and '_'. Choose a different name.",
                    )
                    continue
                if name in self._plans:
                    QMessageBox.warning(
                        self, "Plan exists",
                        f"A plan named {name!r} already exists. "
                        f"Choose a different name.",
                    )
                    continue
                break
            plan = configlib.defaults_home()
            plan.plan_name = name

        path = self._config_path_for(plan.plan_name)
        if plan.plan_name in configlib.SYSTEM_PLAN_NAMES and not os.access(path.parent, os.W_OK):
            if not self._save_system_plan_via_pkexec(plan):
                return
        else:
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
