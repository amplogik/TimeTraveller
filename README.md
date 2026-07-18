# TimeTraveller

A local Linux backup tool focused on **trustworthy backups** and **fast, partial recovery**.

Status: **v1.5.3** — stable.

**Highlights since v1.0:**

- **Verifiable integrity** — every archive frame carries a SHA-256 computed inline as it's written (no extra read pass), so corruption introduced anywhere from compression onward (client buffer, NFS, network, storage) is detectable. `--verify` re-checks an archive against those digests *without decompressing it*.
- **Cross-archive search & partial restore** — search a filename (or full path) across every cycle and shard of a plan at once, compare the versions that turn up, and extract just the copy you want — straight from the results.
- **Restore from anywhere** — point the GUI at a backup on a USB drive or NAS share with no local config and browse, search, and extract from it exactly as a local plan.
- **Parallel multi-shard backups** — a backup can be split into N independent shards written concurrently, for substantially higher throughput on multi-core machines. Restores transparently span the shard set.
- **Full GUI management of system-level backups** — running, deleting, reindexing and recovering `system`/`homes` plans (whose archives are root-owned) all escalate through a single Polkit prompt; no dropping to a root shell.
- **Tri-state backup status** — `ok` / `ok-with-warnings` / `failed`. Benign races (a browser rewriting its cache mid-backup, a temp file vanishing) are warnings, not failures, so a backup is only flagged failed when the archive is actually untrustworthy.
- **Archive management** — type-to-confirm delete (by cycle or single backup), group-atomic export, and recovery of a failed-but-intact backup, all from the Archives tab.

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
sudo apt install ./timetraveller_1.4.3_all.deb
```

This installs:

- `/usr/bin/timetraveller` — the GUI entry point
- `/usr/bin/timetraveller-backup` — the CLI worker
- `/usr/libexec/timetraveller-*` — a set of tightly-scoped privileged helpers (write system config, install cron, run backup, delete archives, reindex/recover) that the GUI invokes via `pkexec` for `system`/`homes` plans
- `/usr/share/polkit-1/actions/com.timetraveller.*.policy` — the matching Polkit rules, each gated by a single (5-minute cached) authentication prompt
- `/usr/share/applications/timetraveller.desktop` — menu entry under System / Utility
- `/usr/share/icons/hicolor/512x512/apps/timetraveller.png` — app icon
- `/etc/timetraveller/system.yaml` and `/etc/timetraveller/homes.yaml` — default system-class plans (preserved across upgrades as conffiles)

On removal (`apt remove`), TimeTraveller's managed cron blocks are stripped from root's crontab and any user crontab automatically.

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
- **System-class plans** (`system`, `homes`) live in `/etc/timetraveller/<name>.yaml`, run from root's crontab, and are meant for `/`, `/boot/efi`, etc. — anything you need root to read. Their archives and manifest on the backup mount are owned by root.

You don't need a root shell to manage system-class plans from the GUI. **Every operation that touches root-owned state escalates through `pkexec`** behind a single (5-minute cached) authentication prompt:

- Saving the plan's YAML to `/etc`
- Installing / suspending / removing its schedule in root's crontab
- **Run full/incr now**
- **Deleting** a cycle or a single backup
- **Reindexing** a sidecar or **recovering** a failed-but-intact backup

Each is handled by its own narrow, auditable helper under `/usr/libexec/` authorised by a matching Polkit policy — the GUI never runs an arbitrary command as root. (Scheduled backups already run as root from cron, so they were never affected.)

From the CLI, the equivalent is `sudo`, e.g. `sudo timetraveller-backup --plan system --kind full`.

State written during backups (the manifest mirror, sidecar mirror, logs) goes under `~/.local/state/timetraveller/<plan>/` for the user running the job (root's copy for root-owned runs). The GUI reads the invoking user's mirror; after a privileged operation it is resynced automatically, and `--list-archives --refresh-from-mount` rebuilds it from the on-mount manifest if it ever drifts.

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

### How to search across archives

When you can't spot a file in the tree — or you're not sure which cycle still has it — search instead of scrolling.

1. On the **Archives** tab, click **🔍 Search files…** above the file tree.
2. Type part of a name. **Filename** mode matches the last path component; **Full path** mode matches anywhere in the path. Search runs across *every* archive in the plan at once, so you find a file without knowing which cycle or shard holds it.
3. Results group by path — expand one to see every backup that holds a copy, with its size and modified-at-backup time, so you can pick the version with the content you want.
4. Double-click a result to jump straight to that file in the browse tree, **or** select one or more results and click the search panel's own **Extract selected…** to restore without leaving search. A *version* row extracts that exact copy; a *path* row extracts its newest version.

Extract always acts on the pane you're looking at: the search panel's Extract button uses your search selection (and the bottom Extract button is hidden while search is open), so you always get the file you highlighted — never a stale pick from the other view. In **Restore from location…** (source) mode, search reads the same browsed location the tree does, so the two never disagree.

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

### Restoring from a drive with no plan configured

You don't need the original machine or its config to get your files back. On any box with TimeTraveller installed, click **Restore from location…** in the toolbar and browse to where the backup lives — a USB drive, an external disk, or a mounted NAS share. TimeTraveller reads that location directly: the **Archives** tab fills with its cycles, and browsing, searching, and **Extract selected…** all work exactly as they do for a locally-configured plan.

### How to manage archives (verify, delete, export, recover)

In the **Archives** tab, right-click a cycle or a single backup for the management actions:

- **Verify** an archive against its per-frame SHA-256 digests — a fast, decompress-free integrity check that names any corrupt frames. Backups also carry zstd's own per-frame checksum, so a normal browse or restore self-verifies as it reads.
- **Delete** a cycle or a single backup. Deletion is type-to-confirm and discloses the blast radius (how many shards, and any dependent incrementals); it refuses the newest complete cycle unless you confirm.
- **Export** a whole backup or cycle as a self-contained bundle (every shard + sidecars + a manifest slice) to another directory — handy for offlining a copy to a USB drive.
- **Recover** a backup the status column shows as **failed**: if its compressed stream is actually intact (the common case is a file that vanished mid-walk), recovery re-reads it as proof, rebuilds the sidecar, and flips it back to `ok-with-warnings`.
- **Refresh from mount (rebuild local mirror)** — re-reads the backup mount and rebuilds your local manifest + sidecar mirror. Use it if the Archives tab ever lists a backup it can't browse, or shows nothing at all, for a plan you know has backups on disk (most often after a root-run `system`/`homes` backup, whose mirror lands under root). It's read-only on the mount and never needs root; it's available even on empty space so you can reach it when the list is empty.

For `system`/`homes` plans the destructive actions (delete) and maintenance (reindex/recover) go through a single Polkit prompt (see *Sudo and system plans* above); browse, export, verify, and refresh-from-mount run as your user.

### Backup status: ok / ok-with-warnings / failed

A live filesystem changes under a snapshot-less backup — a browser rewrites a cache file, a build drops a temp file — and `tar`/`pax` reports those as per-file warnings. TimeTraveller classifies a run by what actually happened to the archive:

- **ok** — clean.
- **ok-with-warnings** — some files were skipped (vanished or changed mid-read), but the archive stream is structurally valid and trustworthy. This is normal for a busy home directory.
- **failed** — the archive itself is not trustworthy (a fatal error, a compression failure, or unreadable diagnostics). Only these are treated as failures; retention won't prune around them, and the file is quarantined for `--recover-failed`.

## Architecture

- Each backup is a **pax archive compressed with zstd**, written to `<destination>/<hostname>/<plan>/<date>_<kind>.pax.zst`. A backup may be split into **shards** (`…<date>_<kind>.sNofM.pax.zst`) written concurrently — one logical backup, M files; the GUI and restore treat the set as a unit.
- The archive is a **sequence of independent 64 MiB zstd frames**. A `.frames.json` sidecar records each frame's byte offset, length, and **SHA-256** (built inline during the write — no second pass), and a `.idx.zst` sidecar holds a sorted index of every entry's name and byte offset. Together they make single-file restore read only the relevant bytes — extracting one file from a 200 GB archive takes seconds — and let `--verify` confirm integrity by re-hashing frames without decompressing.
- Cycles are tracked in a `manifest.json` next to the archives (plus a per-shard `.meta.json`). A local mirror under `~/.local/state/timetraveller/` lets the GUI draw its list without blocking on the backup mount.
- Schedules are stored in your crontab inside a managed marker block. The block is the only thing TimeTraveller edits — anything else in your crontab is preserved verbatim, and it's removed cleanly on `apt remove`.

## Command-line tool

The GUI is a wrapper around `timetraveller-backup`. Anything you can do in the GUI works from the shell — handy for scripted restores, remote machines, or a faster feedback loop. See `timetraveller-backup --help` for the full list. Common one-liners:

```bash
# Take a manual full backup
timetraveller-backup --plan home --kind full

# List cycles on disk
timetraveller-backup --plan home --list-archives

# Verify an archive's integrity (decompress-free, per-frame SHA-256).
# Accepts a whole sharded backup by its stem, e.g. 2026-06-14_full
timetraveller-backup --plan home --verify 2026-06-14_full

# Apply retention now (without taking a new backup)
timetraveller-backup --plan home --prune

# Restore a single path (resolves across shards automatically)
timetraveller-backup --plan home --extract 2026-06-14_full ./path/within --into /tmp/restored

# Delete one cycle, or export a whole backup as a self-contained bundle
timetraveller-backup --plan home --delete-cycle 2026-06-14 --force
timetraveller-backup --plan home --export-set 2026-06-14_full --into /mnt/usb

# Recover a backup marked "failed" whose stream is actually intact
timetraveller-backup --plan home --recover-failed 2026-06-14_full.s3of4.pax.zst

# Dry-run: walk the source tree and report what would be backed up
timetraveller-backup --plan home --dry-run --kind full

# Show which filesystems are visible under your sources
timetraveller-backup --plan home --show-mounts
```

Shard count is set per plan (the **Streams** control on the Plan tab, or `shards:` in the YAML — an integer or `auto`). More shards trade CPU for throughput up to your storage backend's write ceiling.

## Troubleshooting

- **Plan doesn't appear in the sidebar** — check that the YAML in `~/.config/timetraveller/` (or `/etc/timetraveller/` for `system`) parses cleanly. The GUI's status bar shows which files were skipped on startup.
- **Schedule won't install** — check `crontab -l` for a managed block. Plan names must match `[A-Za-z0-9_-]+` to install (the New Plan dialog enforces this).
- **Restore says "archive not found"** — the file on the backup mount was deleted outside TimeTraveller while the manifest still references it. Restore from another backup, or run `--list-archives --refresh-from-mount` to reconcile the manifest with what's actually on the mount.
- **Archives tab empty for a `system`/`homes` plan** ("no archives in local mirror") — these plans back up as root, so their local manifest mirror belongs to root, not your GUI user. Run `timetraveller-backup --plan system --list-archives --refresh-from-mount` once to rebuild your user's mirror from the on-mount manifest (it reads the mount; it never writes the root-owned copy). The GUI also resyncs automatically after any privileged operation.
- **GUI hangs on slow NFS** — the GUI is designed to avoid blocking on the backup mount, but a stalled mount can still affect things like the Archives tab refresh. Local mirror state is the fallback path.

## Contributing

Issues and PRs welcome at <https://github.com/amplogik/TimeTraveller>.

When running long backup or restore operations during development, prefer `tmux` / `nohup` — some Wayland desktops have unrelated stability issues that can interrupt long-running GUI processes.

## License

GPL-3.0 — see [LICENSE](LICENSE).
