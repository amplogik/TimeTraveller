# TimeTraveller

A local Linux backup tool focused on **trustworthy backups** and **fast, partial recovery**.

Status: **v1.0** — initial release.

## Why TimeTraveller

TimeTraveller started after frustration with the existing open-source landscape:

- **Timeshift**, the closest equivalent, had buggy edge cases that occasionally left backups in inconsistent states and offered limited control over what gets included.
- Other tools couldn't do **partial recovery** — they treated each backup as an opaque blob, so getting one file back meant extracting the whole thing.
- Most didn't play nicely with **NAS devices over NFS** — they'd block indefinitely on a slow mount or silently skip remote destinations.
- Many couldn't even **see their mounts** correctly — backing up `/` would either walk into NFS shares you didn't want, or skip ones you did.
- **Incremental backups** with sane retention were either missing or required hand-rolled scripts.
- **Glob patterns** for excludes (`**/.cache/`, `**/node_modules/`) were rarely supported, forcing you to list paths one by one.

The underlying tools — `pax` and `zstd` — already do all of this well. TimeTraveller is the thin coordination layer that wires them together with cycle management, a manifest, indexed sidecars for random-access extraction, and a GUI that doesn't get in the way.

## Requirements

- Linux with Python 3.11 or newer
- `pax`, `zstd` (in your distro's base or `apt install pax zstd`)
- `python3-zstandard` (Python zstd bindings)
- `python3-yaml`
- `python3-pyqt6` (for the GUI; the CLI runs without it)
- For system-wide plans: `policykit-1` / `polkitd` (already present on most desktops)

## Installation

### Option 1 — Debian/Ubuntu package (recommended)

Download the `.deb` from the [latest release](https://github.com/amplogik/TimeTraveller/releases) and install:

```bash
sudo apt install ./timetraveller_1.0.0_all.deb
```

This installs:

- `/usr/bin/timetraveller` — the GUI entry point
- `/usr/bin/timetraveller-backup` — the CLI worker
- `/usr/libexec/timetraveller-install-system-cron` — privileged helper for `system` plans
- `/usr/share/polkit-1/actions/com.timetraveller.install-system-crontab.policy` — Polkit rule that lets the GUI install root crontab entries with one authentication prompt
- `/usr/share/applications/timetraveller.desktop` — menu entry under System / Utility
- `/usr/share/icons/hicolor/256x256/apps/timetraveller.png` — app icon
- `/etc/timetraveller/` and `/var/lib/timetraveller/` — empty directories for system-wide config and state

After installation, launch from your application menu or run `timetraveller` from a terminal.

### Option 2 — Dev install from a checkout

Clone the repo and run the included installer:

```bash
git clone https://github.com/amplogik/TimeTraveller.git
cd TimeTraveller
sudo ./install.sh
```

This places **symlinks** from `/usr/local/bin/` and `/usr/libexec/` back to the checkout, plus copies the Polkit policy. Edits to the working copy take effect immediately — no rebuild step.

Install the runtime Python deps separately:

```bash
sudo apt install python3-zstandard python3-yaml python3-pyqt6
```

To remove the dev install: `sudo ./install.sh --uninstall`.

### Building the .deb yourself

If you want to build the Debian package from source rather than using the prebuilt release:

```bash
# Build-time deps (one-off, in addition to the runtime deps above)
sudo apt install debhelper dh-python python3-all python3-setuptools pybuild-plugin-pyproject

# Build (produces ../timetraveller_<version>_all.deb)
dpkg-buildpackage -us -uc -b
```

The resulting `.deb` lands in the parent directory.

### Option 3 — Run from a checkout without installing

Pure local — useful for hacking:

```bash
sudo apt install python3-zstandard python3-yaml python3-pyqt6
./bin/timetraveller            # GUI
./bin/timetraveller-backup --help   # CLI
```

`system` plans won't be installable to root's crontab in this mode (no Polkit policy registered).

## Quick start

1. Launch the GUI (`timetraveller` from the menu, or `./bin/timetraveller` from a checkout).
2. Click **+ New plan…** in the top-right of the toolbar. Pick **home (default)** to back up `/home`.
3. Tweak sources and excludes on the **Plan** tab. Pick your backup destination — a local path, or an NFS-mounted NAS share like `/mnt/Backups/`.
4. Click **Save Plan**.
5. Switch to the **Schedule** tab to put it on a cadence, then **Install schedule** — or click **Run full now** on the toolbar to take a one-off backup immediately.

To restore later, pick the plan, switch to the **Archives** tab, browse to the archive you want, select files/directories in the tree, and click **Extract selected…**.

## Sudo and system plans

TimeTraveller distinguishes two scopes:

- **User plans** (e.g. `home`) live in `~/.config/timetraveller/<name>.yaml` and run from your user crontab. No root needed.
- **The system plan** (`system`) lives in `/etc/timetraveller/system.yaml`, runs from root's crontab, and is meant for `/`, `/boot/efi`, etc. — anything you need root to read.

The GUI can edit a system plan, but writing it back to `/etc` requires root. Two ways to handle this:

- **From the GUI**: install/uninstall of the system plan's cron entries goes through `pkexec` via the Polkit policy. You'll get a single authentication prompt, then a 5-minute cached auth so consecutive operations don't re-prompt. Editing the system plan's YAML in `/etc` is not yet wired through pkexec — for now, edit `/etc/timetraveller/system.yaml` as root in a text editor and the GUI will pick it up on next launch.
- **From the CLI**: `sudo timetraveller-backup --plan system --kind full` runs the backup as root with the system plan loaded from `/etc`.

State written during backups (the on-mount manifest mirror, sidecar mirror, logs) goes to `/var/lib/timetraveller/<plan>/` for root-owned runs and `~/.local/state/timetraveller/<plan>/` for user-owned runs. Both are managed automatically.

## Help

### What is a plan?

A plan is a saved configuration describing one backup job — which directories to back up, where to write them, when to run, and how many cycles to keep. Each plan lives in its own YAML file under `~/.config/timetraveller/` (or `/etc/timetraveller/` for system plans). The **Backup Plans** sidebar on the left of the GUI shows every plan TimeTraveller can see; clicking one loads it into the editor tabs on the right.

### Plan types

TimeTraveller has two plan types:

- **Active plans** run on a schedule (e.g. weekly fulls + daily incrementals) and prune old cycles automatically when they exceed your retention policy. Use these for data that changes regularly.
- **Archive plans** are not scheduled. You run them manually whenever you want to capture a new snapshot, and `keep_all` retention means cycles never expire. Use these for write-once-read-rarely data: LLM model checkpoints, a music library, container images.

Flip a plan between types via the **Change…** button on the Plan tab. The dialog explains what will happen — for Active→Archive, all cycles except the newest are deleted (irreversible).

### How to create a new plan

1. Click **+ New plan…** in the top-right of the toolbar.
2. Pick a starting template (`home (default)`, `system (default)`, or `custom…`).
3. The new plan appears in the sidebar and is loaded into the editor. Tweak sources, excludes, destination, and retention.
4. Click **Save Plan** to write it to disk.
5. If you want it scheduled, switch to the **Schedule** tab, pick weekly or monthly, set the cadence, and click **Install schedule**.

### How to view or edit a plan

1. Click the plan's name in the **Backup Plans** sidebar.
2. The three tabs in the centre show its current state:
   - **Plan** — sources, excludes, destination, retention, mount options. The "Plan type: Active / Archive" row at the top shows its category.
   - **Schedule** — when fulls and incrementals run.
   - **Archives** — the cycles already on disk for this plan.
3. Edit fields in place; the title bar shows "• unsaved changes" until you click **Save Plan**.

### How to browse an archive

1. Pick a plan in the sidebar, then switch to the **Archives** tab.
2. Cycles are listed on the left. Clicking a cycle expands it to show the individual archives.
3. Click an archive to load its contents into the file tree on the right — sourced from the archive's seekable sidecar, so it's fast even for huge archives.
4. Drill into directories as you would in a file manager.

### How to restore from an archive

1. Browse to the archive (see above).
2. In the file tree, select what you want back:
   - **A single file** — click it.
   - **A whole directory** — click the directory.
   - **Multiple files or directories** — Ctrl-click or Shift-click.
3. The bottom of the panel shows **"Selected: N paths"**. Click **Extract selected…**.
4. In the Restore dialog, the destination defaults to `~/Restored/<archive-name>/`. Change it to any writable path.
5. Click **Extract**. The output window reports the extraction mode: **fast (sidecar-based)** when the sidecar is usable, **naïve (whole-archive scan)** as a slower-but-correct fallback when it isn't.

To restore an *entire* cycle, the command line is more comfortable:

```bash
timetraveller-backup --plan <name> --extract <archive>.pax.zst --into /restore/path .
```

## Architecture

- Each backup is a **pax archive compressed with zstd**, written to `<destination>/<hostname>/<plan>/<date>_<kind>.pax.zst`.
- Alongside each archive sits a **sidecar** (`.idx.zst`) holding a sorted index of every entry's name and byte offset. Single-file restore reads only the relevant bytes — extracting one file from a 200 GB archive takes seconds, not minutes.
- Cycles are tracked in a `manifest.json` next to the archives. A local mirror under `~/.local/state/timetraveller/` (or `/var/lib/timetraveller/` for system plans) lets the GUI draw its list without blocking on the backup mount.
- Schedules are stored in your crontab inside a managed marker block. The block is the only thing TimeTraveller edits — anything else in your crontab is preserved verbatim.

## Command-line tool

The GUI is a wrapper around `timetraveller-backup`. Anything you can do in the GUI works from the shell — handy for scripted restores, remote machines, or a faster feedback loop. See `timetraveller-backup --help` for the full list. Common one-liners:

```bash
# Take a manual full backup
timetraveller-backup --plan home --kind full

# List cycles on disk
timetraveller-backup --plan home --list-archives

# Apply retention now (without taking a new backup)
timetraveller-backup --plan home --prune

# Restore a single path
timetraveller-backup --plan home --extract <archive>.pax.zst ./path/within --into /tmp/restored

# Dry-run: walk the source tree and report what would be backed up
timetraveller-backup --plan home --dry-run --kind full

# Show which filesystems are visible under your sources
timetraveller-backup --plan home --show-mounts
```

## Troubleshooting

- **Plan doesn't appear in the sidebar** — check that the YAML in `~/.config/timetraveller/` (or `/etc/timetraveller/` for `system`) parses cleanly. The GUI's status bar shows which files were skipped on startup.
- **Schedule won't install** — check `crontab -l` for a managed block. Plan names must match `[A-Za-z0-9_-]+` to install (the New Plan dialog enforces this).
- **Restore says "archive not found"** — the file on the backup mount was deleted outside TimeTraveller. The manifest still references it. Restore from another backup, or remove the manifest entry manually (a future release will detect and surface this in the GUI).
- **GUI hangs on slow NFS** — the GUI is designed to avoid blocking on the backup mount, but a stalled mount can still affect things like the Archives tab refresh. Local mirror state is the fallback path.

## Contributing

Issues and PRs welcome at <https://github.com/amplogik/TimeTraveller>.

When running long backup or restore operations during development, prefer `tmux` / `nohup` — some Wayland desktops have unrelated stability issues that can interrupt long-running GUI processes.

## License

GPL-3.0 — see [LICENSE](LICENSE).
