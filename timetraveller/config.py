"""Plan config schema, defaults, YAML load/save."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RETENTION_POLICIES = ("max_cycles", "max_age_days", "max_size_gb", "keep_all")
COMPRESSIONS = ("zstd", "gzip", "none")
SCHEDULE_MODES = ("weekly", "monthly", "archive")
INCR_MODES = ("weekdays", "except_full", "every_n_days", "disabled")

# Plans whose YAML lives at /etc/timetraveller/<name>.yaml and whose cron block
# lives in root's crontab. Routes through pkexec helpers. The libexec helpers
# hardcode the same allowlist for auditability and must be kept in sync.
SYSTEM_PLAN_NAMES = frozenset({"system", "homes"})


@dataclass
class FullSchedule:
    """When to take a full backup.

    `days` is used when the parent Schedule.mode is "weekly" — one or more
    weekday short names (mon..sun). `day_of_month` is used when mode is
    "monthly" — an integer 1..28 (capped to avoid February edge cases).
    """
    days: list[str] = field(default_factory=lambda: ["sun"])
    day_of_month: int = 1
    time: str = "02:00"


@dataclass
class IncrSchedule:
    """When to take incremental backups.

    `mode` picks the semantics:
      - "weekdays":      run on the weekdays listed in `days`.
      - "except_full":   run every weekday that ISN'T a full-backup day.
                         (Only valid in weekly schedule mode.)
      - "every_n_days":  run every Nth day-of-month (cron `*/N`).
                         (Only valid in monthly schedule mode.)
      - "disabled":      no incremental backups.
    """
    mode: str = "except_full"
    days: list[str] = field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat"]
    )
    every_n_days: int = 3
    time: str = "02:00"


@dataclass
class Schedule:
    mode: str = "weekly"
    full: FullSchedule = field(default_factory=FullSchedule)
    incr: IncrSchedule = field(default_factory=IncrSchedule)


@dataclass
class Retention:
    policy: str = "max_cycles"
    max_cycles: int = 4
    max_age_days: int | None = None
    max_size_gb: float | None = None


@dataclass
class PlanConfig:
    plan_name: str
    sources: list[str]
    excludes: list[str] = field(default_factory=list)
    destination: str = "/mnt/Backups/timetraveller"
    schedule: Schedule = field(default_factory=Schedule)
    retention: Retention = field(default_factory=Retention)
    include_hostname_in_path: bool = True
    include_removable: bool = False
    include_nfs: bool = False
    include_cifs: bool = False
    include_mounts: list[str] = field(default_factory=list)
    exclude_mounts: list[str] = field(default_factory=list)
    compression: str = "zstd"
    framed: bool = True
    extra_pax_flags: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.plan_name:
            raise ValueError("plan_name is required")
        if not self.sources:
            raise ValueError("at least one source is required")

        sch = self.schedule
        if sch.mode not in SCHEDULE_MODES:
            raise ValueError(f"schedule.mode must be one of {SCHEDULE_MODES}")

        if sch.mode == "weekly":
            if not sch.full.days:
                raise ValueError("weekly mode: schedule.full.days must list at least one day")
            seen: set[str] = set()
            for d in sch.full.days:
                if d not in WEEKDAYS:
                    raise ValueError(f"schedule.full.days contains {d!r}; must be in {WEEKDAYS}")
                if d in seen:
                    raise ValueError(f"schedule.full.days has duplicate {d!r}")
                seen.add(d)
            if sch.incr.mode not in ("weekdays", "except_full", "disabled"):
                raise ValueError(
                    f"weekly mode: schedule.incr.mode must be weekdays|except_full|disabled, "
                    f"got {sch.incr.mode!r}"
                )
            if sch.incr.mode == "weekdays":
                if not sch.incr.days:
                    raise ValueError("incr.mode=weekdays requires at least one day in incr.days")
                seen = set()
                for d in sch.incr.days:
                    if d not in WEEKDAYS:
                        raise ValueError(f"schedule.incr.days contains {d!r}; must be in {WEEKDAYS}")
                    if d in seen:
                        raise ValueError(f"schedule.incr.days has duplicate {d!r}")
                    seen.add(d)
                overlap = set(sch.full.days) & set(sch.incr.days)
                if overlap:
                    raise ValueError(
                        f"schedule.full.days and schedule.incr.days overlap on {sorted(overlap)}; "
                        f"use incr.mode=except_full to auto-skip the full day(s)"
                    )

        elif sch.mode == "monthly":
            if not (1 <= sch.full.day_of_month <= 28):
                raise ValueError("monthly mode: schedule.full.day_of_month must be 1..28")
            if sch.incr.mode not in ("weekdays", "every_n_days", "disabled"):
                raise ValueError(
                    f"monthly mode: schedule.incr.mode must be weekdays|every_n_days|disabled, "
                    f"got {sch.incr.mode!r}"
                )
            if sch.incr.mode == "weekdays":
                if not sch.incr.days:
                    raise ValueError("incr.mode=weekdays requires at least one day in incr.days")
                for d in sch.incr.days:
                    if d not in WEEKDAYS:
                        raise ValueError(f"schedule.incr.days contains {d!r}; must be in {WEEKDAYS}")
            elif sch.incr.mode == "every_n_days":
                if not (2 <= sch.incr.every_n_days <= 28):
                    raise ValueError("incr.mode=every_n_days requires every_n_days 2..28")

        if self.retention.policy not in RETENTION_POLICIES:
            raise ValueError(f"retention.policy must be one of {RETENTION_POLICIES}")
        if self.compression not in COMPRESSIONS:
            raise ValueError(f"compression must be one of {COMPRESSIONS}")

    def archive_dir(self, hostname: str | None = None) -> Path:
        """Compute destination directory for this plan, including hostname if enabled."""
        base = Path(self.destination)
        if self.include_hostname_in_path:
            host = hostname or os.uname().nodename
            return base / host / self.plan_name
        return base / self.plan_name


def load(path: Path) -> PlanConfig:
    """Load a plan config from YAML."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return _from_dict(data)


def save(plan: PlanConfig, path: Path) -> None:
    """Atomically save a plan config to YAML."""
    plan.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(asdict(plan), f, sort_keys=False, default_flow_style=False)
    tmp.replace(path)


def _from_dict(data: dict) -> PlanConfig:
    sched_raw = dict(data.get("schedule", {}))
    full_raw = dict(sched_raw.get("full", {}))
    incr_raw = dict(sched_raw.get("incr", {}))

    # Backward compat: old configs used `full.day: <name>` (singular) and had
    # no schedule.mode. Convert to the new `full.days: [<name>]` form.
    if "day" in full_raw and "days" not in full_raw:
        full_raw["days"] = [full_raw.pop("day")]
    # Strip any keys we no longer recognise (avoid accidental TypeErrors when
    # the new code drops a field).
    full_raw = {k: v for k, v in full_raw.items()
                if k in {"days", "day_of_month", "time"} and v is not None}
    incr_raw = {k: v for k, v in incr_raw.items()
                if k in {"mode", "days", "every_n_days", "time"} and v is not None}

    # Backward compat: pre-mode configs had no incr.mode; their behaviour was
    # "run on these specific weekdays." Preserve that semantically instead of
    # silently switching to except_full and changing behaviour if the user
    # ever edits full.days later.
    if "mode" not in sched_raw and "mode" not in incr_raw:
        incr_raw.setdefault("mode", "weekdays")

    schedule = Schedule(
        mode=sched_raw.get("mode", "weekly"),
        full=FullSchedule(**full_raw),
        incr=IncrSchedule(**incr_raw),
    )
    ret = data.get("retention", {})
    retention = Retention(**{k: v for k, v in ret.items() if v is not None})
    kwargs = {k: v for k, v in data.items() if k not in ("schedule", "retention")}
    plan = PlanConfig(schedule=schedule, retention=retention, **kwargs)
    plan.validate()
    return plan


# ---------- defaults ----------

# Common cache/garbage patterns we never want to back up.
_HOME_EXCLUDES = [
    "**/.cache/",
    "**/Cache/",
    "**/CachedData/",
    "**/.thumbnails/",
    "**/.local/share/Trash/",
    "**/snap/*/common/.cache/",
    "**/node_modules/",
    "**/.gradle/",
    "**/.npm/",
    "**/lost+found/",
]

# System exclusions. Pseudo-filesystems and the home tree are excluded;
# mount-type filtering (NFS/CIFS/removable) is enforced at runtime, not here.
_SYSTEM_EXCLUDES = [
    "/home/**",
    "/mnt/**",
    "/media/**",
    "/cdrom/**",
    "/snap/**",
    "/proc/**",
    "/sys/**",
    "/dev/**",
    "/run/**",
    "/tmp/**",
    "/var/tmp/**",
    "/var/cache/**",
    "/var/log/journal/**",
    "/swapfile",
    "/swap.img",
    "/lost+found",
]


def defaults_home() -> PlanConfig:
    """Per-user default: just /home/$USER, runs as the invoking user."""
    return PlanConfig(
        plan_name="home",
        sources=[str(Path.home())],
        excludes=list(_HOME_EXCLUDES),
    )


def defaults_homes() -> PlanConfig:
    """All-users-home default: /home, runs as root via the pkexec helpers."""
    return PlanConfig(
        plan_name="homes",
        sources=["/home"],
        excludes=list(_HOME_EXCLUDES),
    )


def defaults_system() -> PlanConfig:
    """OS-only default: / and /boot/efi, runs as root, excludes /home/**."""
    return PlanConfig(
        plan_name="system",
        sources=["/", "/boot/efi"],
        excludes=list(_SYSTEM_EXCLUDES),
    )


# ---------- config file location ----------

def user_config_path(plan_name: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "timetraveller" / f"{plan_name}.yaml"


def system_config_path(plan_name: str) -> Path:
    return Path("/etc/timetraveller") / f"{plan_name}.yaml"


def resolve_config_path(plan_name: str) -> Path:
    """Find a plan config. Looks at /etc first for system-class plans, user path for others."""
    if plan_name in SYSTEM_PLAN_NAMES:
        sp = system_config_path(plan_name)
        if sp.exists():
            return sp
    up = user_config_path(plan_name)
    if up.exists():
        return up
    sp = system_config_path(plan_name)
    if sp.exists():
        return sp
    raise FileNotFoundError(f"No config found for plan {plan_name!r} at {up} or {sp}")
