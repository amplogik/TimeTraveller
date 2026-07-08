"""The self-contained bootstrap restore script, written beside the backups.

`timetraveller-restore.sh` lets someone recover on a fresh machine that does
NOT have TimeTraveller installed: it needs only `bash`, `python3`, `tar`, and
`zstd` (all in the Debian/Ubuntu base or a single `apt install`). It reads the
manifest, lets the user pick a plan and a full backup, confirms a restore
target, and unpacks that full plus every incremental up to the next full using
`zstd -dc | tar -x`.

The script is stored as a string constant (not a packaged data file) so it
ships regardless of how TimeTraveller itself is installed, and is written into
each archive directory at backup time (and backfilled on --refresh-from-mount)
next to the manifest + `timetraveller.restore.json` descriptor.

Design note — the embedded Python is passed to `python3 -c`, NOT piped on
stdin, so stdin stays connected to the terminal for the interactive prompts.
"""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_NAME = "timetraveller-restore.sh"

# NOTE: kept as one r-string. The bash uses a single-quoted heredoc for the
# Python, and the Python uses only single/double quotes (never triple), so
# nothing inside closes this r'''...''' literal.
BOOTSTRAP_SCRIPT = r'''#!/usr/bin/env bash
# TimeTraveller bootstrap restore — recover WITHOUT installing TimeTraveller.
# Requires: bash, python3, tar, zstd. Run it from where your backups live:
#     ./timetraveller-restore.sh
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "TimeTraveller bootstrap restore"
echo "Backup location: $SELF_DIR"
echo

missing=()
for tool in python3 tar zstd; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Missing required tool(s): ${missing[*]}"
    echo
    echo "Install them on Debian/Ubuntu with:"
    echo "    sudo apt update && sudo apt install -y ${missing[*]}"
    echo
    echo "(zstd decompresses the archives, tar unpacks them, python3 reads the manifest.)"
    exit 1
fi

PYPROG=$(cat <<'PYEOF'
import sys, os, json, subprocess, re

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
SHARD_RE = re.compile(r'\.s\d+of\d+(?=\.pax\.)')


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except EOFError:
        return default
    return s or default


def group_id(fn):
    n = SHARD_RE.sub('', fn)
    for ext in ('.pax.zst', '.pax.gz', '.pax'):
        if n.endswith(ext):
            return n[:-len(ext)]
    return n


def find_plan_dirs(root):
    root = os.path.abspath(root)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        if 'manifest.json' in filenames:
            hits.append(dirpath)
            dirnames[:] = []   # a plan dir is a leaf; don't descend into it
            continue
        depth = dirpath[len(root):].count(os.sep)
        if depth >= 3:
            dirnames[:] = []
    return sorted(hits)


def load_manifest(pdir):
    with open(os.path.join(pdir, 'manifest.json')) as f:
        return json.load(f)


def read_descriptor(pdir):
    p = os.path.join(pdir, 'timetraveller.restore.json')
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cycles_from(archives):
    """Group archive entries into cycles (a full + its incrementals up to the
    next full). Sharded backups group by shard_group so N shards count as one."""
    sets = {}
    for e in archives:
        gid = e.get('shard_group') or group_id(e['filename'])
        sets.setdefault(gid, []).append(e)
    setlist = sorted(sets.values(), key=lambda ms: min(m.get('date_started', '') for m in ms))
    cycles = []
    cur = None
    for ms in setlist:
        kind = ms[0].get('kind', 'incr')
        complete = all(m.get('status') in ('ok', 'ok-with-warnings')
                       and not m.get('corrupt_frames') for m in ms)
        if kind == 'full' and complete:
            cur = {'id': ms[0].get('cycle_id', ''), 'full': ms, 'incrs': [], 'sets': [ms]}
            cycles.append(cur)
        else:
            if cur is None:
                cur = {'id': ms[0].get('cycle_id', ''), 'full': None, 'incrs': [], 'sets': []}
                cycles.append(cur)
            cur['incrs'].append(ms)
            cur['sets'].append(ms)
    return cycles


def human(n):
    x = float(n)
    for u in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if x < 1024:
            return ("%d %s" % (x, u)) if u == 'B' else ("%.1f %s" % (x, u))
        x /= 1024
    return "%.1f PiB" % x


def pick(items, render, prompt):
    for i, it in enumerate(items, 1):
        print("  %d) %s" % (i, render(it)))
    while True:
        s = ask(prompt)
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]
        print("  Please enter a number from the list.")


def decompressor(fn):
    if fn.endswith('.zst'):
        return ['zstd', '-dc']
    if fn.endswith('.gz'):
        return ['gzip', '-dc']
    return ['cat']


def extract(archive_path, target):
    with open(archive_path, 'rb') as af:
        p1 = subprocess.Popen(decompressor(archive_path), stdin=af, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '-xf', '-', '-C', target], stdin=p1.stdout)
        p1.stdout.close()
        rc = p2.wait()
        p1.wait()
    return rc


def main():
    plans = find_plan_dirs(ROOT)
    if not plans:
        die("No TimeTraveller backups (manifest.json) found under %s" % ROOT)
    if len(plans) == 1:
        pdir = plans[0]
    else:
        print("Backup plans found:")

        def rp(p):
            mm = load_manifest(p)
            return "%s  (%d archives)  [%s]" % (
                mm.get('plan_name') or os.path.basename(p),
                len(mm.get('archives', [])), os.path.relpath(p, ROOT))
        pdir = pick(plans, rp, "Choose a plan (number): ")

    m = load_manifest(pdir)
    desc = read_descriptor(pdir)
    cycles = [c for c in cycles_from(m.get('archives', [])) if c['full']]
    if not cycles:
        die("No complete full backups to restore from in %s." % pdir)

    print("\nPlan: %s" % (m.get('plan_name') or os.path.basename(pdir)))
    if desc and desc.get('sources'):
        print("Originally backed up from: %s" % ", ".join(desc['sources']))
    print("\nAvailable full backups (each restores that full + its incrementals):")

    def rc(c):
        full_sz = sum(x.get('size_bytes', 0) for x in c['full'])
        incr_sz = sum(x.get('size_bytes', 0) for ms in c['incrs'] for x in ms)
        bad = any(x.get('status') == 'corrupt' or x.get('corrupt_frames')
                  for ms in c['sets'] for x in ms)
        warn = "  [!] contains corrupt frames" if bad else ""
        return "%s   full %s + %d incrementals %s%s" % (
            c['id'], human(full_sz), len(c['incrs']), human(incr_sz), warn)

    c = pick(cycles, rc, "\nChoose a full backup to restore from (number): ")

    ordered = list(c['full'])
    for ms in sorted(c['incrs'], key=lambda ms: min(x.get('date_started', '') for x in ms)):
        ordered.extend(ms)

    default_target = os.path.expanduser("~/timetraveller-restore")
    print("\nThis restores the full backup plus %d incremental(s) — %d archive(s) total."
          % (len(c['incrs']), len(ordered)))
    if desc and desc.get('sources'):
        print("Original location(s): %s" % ", ".join(desc['sources']))
        print("Restoring there overwrites live files; a staging directory is safer.")
    target = os.path.abspath(os.path.expanduser(
        ask("Restore into [%s]: " % default_target, default_target)))

    print("\nAbout to restore cycle %s (%d archives) into:\n    %s" % (c['id'], len(ordered), target))
    if ask("Proceed? [y/N]: ").lower() not in ('y', 'yes'):
        die("Aborted.", 0)
    os.makedirs(target, exist_ok=True)

    failures = []
    for i, e in enumerate(ordered, 1):
        fn = e['filename']
        ap = os.path.join(pdir, fn)
        if not os.path.exists(ap):
            if os.path.exists(ap + '.failed'):
                ap = ap + '.failed'
            else:
                print("  [%d/%d] MISSING %s" % (i, len(ordered), fn))
                failures.append(fn)
                continue
        print("  [%d/%d] %s ..." % (i, len(ordered), fn), flush=True)
        if extract(ap, target) != 0:
            print("       WARNING: extraction of %s reported errors" % fn)
            failures.append(fn)

    print()
    if failures:
        print("Restore finished with %d problem archive(s):" % len(failures))
        for f in failures:
            print("    %s" % f)
        print("Some files may be missing or from an earlier point in time.")
        print("If a file is corrupt, a clean copy may exist in another full backup here.")
        sys.exit(2)
    print("Restore complete -> %s" % target)


main()
PYEOF
)

exec python3 -c "$PYPROG" "$SELF_DIR"
'''


def script_path(archive_dir: Path) -> Path:
    return archive_dir / SCRIPT_NAME


def write_bootstrap_script(archive_dir: Path) -> None:
    """Atomically write the bootstrap restore script into an archive directory
    and make it executable. Raises OSError on failure — callers treat it as
    best-effort (like the descriptor), since it is regenerable."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = script_path(archive_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(BOOTSTRAP_SCRIPT)
    os.chmod(tmp, 0o755)
    tmp.replace(path)
