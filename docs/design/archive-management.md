# Design note: Archive management — delete control & sidecar resilience

Status: **proposed** (2026-06-08; **revised 2026-06-13 for sharding**). Captures
decisions from the 2026-06-08 working session, updated after client-side sharding
landed (v1.1.0–v1.1.2). Prototype-first: build under `prototype/` and validate before
touching the working app. Nothing in Features 1–2 is implemented yet.

> **2026-06-13 sharding revision.** This note was written before sharding shipped, when
> one logical backup == one `.pax.zst`. That is no longer true: a backup is now **N shard
> archives** tied by `shard_group`, and the GUI already collapses them into one row
> (`manifest.ShardSet`). Every "delete a backup" / "export a backup" / "describe a
> backup" statement below now means **operate on the whole shard set atomically**, never
> a single shard. The shard-specific deltas are folded into each section and summarised
> in [Sharding model](#sharding-model-what-the-revision-must-respect).

## Background / motivation

Two gaps surfaced while cleaning up stale archive state after the QNAP→TrueNAS
migration:

1. **No targeted delete in the GUI.** The Archives panel lists cycles/archives from
   the manifest, but the only deletion paths are *Remove Plan* (all-or-nothing for a
   whole plan) and automatic retention (`max_cycles`). Removing one bad cycle on
   demand required hand-editing the manifest mirror. (Confirmed: a per-archive delete
   button never existed — not clobbered by the search commit.)

2. **Sidecars didn't travel with a hand-moved copy.** When archives were copied to an
   offline device for the migration, only the `*.pax.zst` files were taken; the
   `.idx.zst` / `.frames.json` siblings were left behind.

## Current architecture (verified, for reference)

Authoritative artifacts live **together on the destination** in `plan.archive_dir()`
(e.g. `/mnt/Backups/timetraveller/<host>/<plan>/`):

| File | Producer | Role |
|---|---|---|
| `<name>.pax.zst` | pax \| zstd pipeline | the archive (primary data) |
| `<name>.pax.zst.idx.zst` | `index.write_sidecar` (`worker.py:181`) | file→offset index |
| `<name>.pax.zst.frames.json` | `framewriter` | zstd frame map (framed archives) |
| `manifest.json` | `_save_manifest` (mount **and** mirror) | plan-wide archive list |

`~/.local/state/timetraveller/<plan>/` holds a **regenerable browse cache** (mirror of
`manifest.json` + the `.idx.zst` sidecars) whose only purpose is to keep the GUI off
the NFS mount. It is not a source of truth.

Key invariants this design relies on:
- **Sidecars are derived data.** `--reindex` rebuilds `.idx.zst` from the `.pax.zst`.
- **Restore works without sidecars.** `--extract` uses them when present and falls back
  to a whole-archive scan otherwise (`fallback_naive`, `worker.py:616`). Losing
  sidecars costs *time*, never data.
- A *directory-level* mirror (`rsync`, `cp -a`, `zfs send`) of `archive_dir` already
  captures everything host-independently. The footgun is cherry-picking `*.pax.zst`.

---

## Sharding model (what the revision must respect)

Verified against the v1.1.x code (`manifest.py`, `worker.py`):

| Concept | Code | Deletion/export relevance |
|---|---|---|
| **Shard** | one `ArchiveEntry` with `shard_index`/`shard_count`/`shard_group` | **Never** the unit of user action — shards partition the file list with no overlap, so deleting one silently loses those files from an otherwise-"present" backup. |
| **Shard set** = one logical backup | `manifest.ShardSet` (`group_id`, `members`) | **The unit for "delete this backup" / "export this backup".** Has aggregate `status`, `total_size`, `is_complete` (true iff *every* shard succeeded). Unsharded backups are a set of one — same code path. |
| **Cycle** | `manifest.Cycle` (`full_set`, `incr_sets`) | `Cycle.archives` already returns **every shard entry** (full + incrementals) and is documented as "the unit of deletion for retention." So cycle-level delete is already shard-correct at the entry level. |

Consequences for this design:

- **`_delete_cycles` already removes all shards of a cycle** — it iterates `cycle.archives`,
  which spans the set. The new *scoped* delete just needs to feed it the right cycle/set.
- **Legacy archives still group.** `manifest.group_id_for` derives a `shard_group` from the
  filename for pre-sharding entries, so grouping (and meta-based rebuild) works uniformly
  for old and new archives.
- **The `.failed`-suffix gap is real and intersects here.** `_delete_cycles` only unlinks
  `a.filename` and its sidecar — **not** the on-disk `<name>.pax.zst.failed` that a failed
  shard leaves behind (manifest keeps the bare name; the file is suffixed). A failed shard
  *set* is exactly the cleanup case the user hit. Delete must also remove `<name>.failed`
  (and its sidecars) for any member whose on-disk file is suffixed. Ties to the
  failed-backup-recovery roadmap.

---

## Feature 1 — Per-cycle / per-shard-set Delete

### Scope & unit of deletion
- **Cycle is the default unit.** A cycle (full + its incrementals, each a shard set) is
  self-consistent to remove. Deleting a single *incremental set mid-chain* breaks restore
  for every later incremental in that cycle.
- **The finest user-deletable unit is a shard SET, never a single shard.** Members of a set
  partition the file list disjointly, so removing one shard silently drops those files while
  the backup still looks present. The GUI never offers per-shard delete; it deletes the
  whole `ShardSet` (`group_id`) atomically. (Internally that's N file removals + N
  `manifest.remove(filename)` calls — see Mechanics.)
- Deleting an **individual full set** orphans its incrementals → must be surfaced loudly
  or blocked.
- **Never** quietly delete the **newest complete cycle.** `retention.py:116` already
  enforces this for retention (and `Cycle.is_complete` now means *all shards succeeded*);
  the GUI must mirror the guard (block, or require an extra-strong confirm).

### Type-to-confirm (the safety net)
A plain "OK" loses to reflexive clicking. Require the user to **type a short
identifier of the specific target** — this forces them to read *what* they're
destroying, not merely acknowledge a delete. (Generic "delete" becomes muscle memory;
the target identifier does not.)

Design decisions (from session feedback — "we're not running a typing school"):
- **Token = minimal safe identifier**, not the full filename. The `.pax.zst` suffix is
  noise and typo bait.
  - Cycle delete → `"<plan> <cycle-date>"` e.g. `home 2026-05-24`
  - Single set → `"<plan> <kind> <date>"` e.g. `home incr 2026-05-25`. The token has
    **no shard index** — it deliberately names the whole logical backup, matching the
    "delete the set, never a shard" rule. (It maps cleanly to the `shard_group` stem,
    e.g. `2026-06-13_full`.)
- **The dialog displays the exact phrase to type** (no recall; pattern is on screen).
- **Matching is normalized** to kill typo-support emails: case-insensitive, internal
  whitespace collapsed, trimmed. `Home  2026-05-24` ≡ `home 2026-05-24`. Strict only on
  the identifying tokens.
- **Live validation**: as they type, show ✓/✗; the destructive button is **disabled
  until the normalized match succeeds**. There is no "submit → error" path, so a typo
  can never produce a failed or confusing action — it just doesn't enable the button.
- Destructive-styled confirm button; **focus defaults to Cancel**; destructive button
  is not the default.

### Blast-radius disclosure
The dialog shows, concretely:
- the exact files to be removed — **for every shard in the set(s)**: each `.pax.zst`
  (or `.pax.zst.failed`) + its sidecars — and **total bytes freed** (sum across shards,
  `ShardSet.total_size`);
- the **shard count** when > 1 ("4 shards, 312 GiB") so the user sees they're removing a
  set, not a lone file;
- the **dependency warning**: "deleting this full also invalidates N dependent
  incrementals" (compute from `manifest.cycles()`, counting incr *sets*).

### Mechanics (mount-safe)
- Deletion runs **off the UI thread** via a spawned worker action (same pattern as
  Remove-Plan / Reindex / Recover), never blocking on the NFS mount.
- Reuse `worker._delete_cycles()` — it already iterates `cycle.archives` (every shard) and
  deletes archive files + sidecars + mirror + `manifest.remove()` per entry. Two gaps to
  close before reuse:
  1. **`.failed` cleanup** — also unlink `<name>.pax.zst.failed` (+ its sidecars) when the
     bare file is absent but the suffixed one exists (failed shard sets).
  2. **Set-scoped entry point** — factor a `_delete_sets(sets)` helper (delete the shards of
     given `ShardSet`s) that `_delete_cycles` can call, so set-delete and cycle-delete share
     one path.
- New scoped CLI actions the GUI invokes (note: **set**, not a single filename):
  - `--delete-cycle <cycle_id>` — the whole cycle (all sets, all shards).
  - `--delete-set <group_id>` — one logical backup (its shards). Replaces the
    originally-proposed `--delete-archive <filename>`, which is wrong post-sharding.
- Update **both** the on-mount manifest and the local mirror, plus
  `index.delete_sidecar_mirror()` for each shard in the browse cache.
- **Surface (decided 2026-06-13): context menu primary, buttons retained.** The Archives
  tree is already a `QTreeWidget` grouped by cycle (cycle nodes → shard-set rows). Add
  `CustomContextMenu`: right-click a **cycle node** → *Delete cycle* / *Export cycle*;
  right-click a **shard-set row** → *Delete backup* / *Reindex* / *Recover* / *Export*,
  each enabled only when applicable (`ShardSet.status`/`is_complete`). Keep the existing
  contextual Reindex/Recover **buttons** too — moving them into the menu fixes their
  discoverability (they're hidden unless a matching set is selected) while the buttons stay
  for users who don't think to right-click.

---

## Feature 2 — Sidecar resilience ("archives that travel")

**Decision: do NOT embed sidecars inside the `.pax.zst` stream.**
- `.frames.json` is a byte-offset map *of the compressed file itself* — it cannot live
  inside the file it measures (adding it shifts every offset it records).
- `.idx.zst` could be appended as a trailing tar member, but tar/pax has no standard
  trailing-index convention, zstd framing makes seek-to-tail awkward, and it couples
  index-format evolution to archive rewrites — defeating the seekable-sidecar approach
  (make *existing* archives seekable without rewriting them).

Instead, two co-located, plain-file improvements (the session's chosen path — #1 + #2):

### 2.1 Per-shard metadata sidecar — `<name>.pax.zst.meta.json`
A one-entry manifest fragment written next to **each shard** archive: `kind`, `cycle_id`,
`date_started/finished`, incr window, `size_bytes`, `status`, `hostname`, a **checksum** of
that shard's `.pax.zst`, **and the shard fields `shard_index` / `shard_count` /
`shard_group`** (so a bare directory regroups into sets via `manifest.group_into_sets`).
It's essentially one serialized `ArchiveEntry` per shard. Effect:
- each shard becomes **self-describing**, and a full set is reconstructable from its
  members' meta even when detached from `manifest.json`;
- a bare directory of archives can **rebuild a manifest** — group the `.meta.json` files by
  `shard_group`, emit one `ArchiveEntry` per shard (extends `--reindex` /
  `--refresh-from-mount` to seed from `.meta.json` when the manifest is absent). Legacy
  archives without shard fields fall back to `group_id_for`'s filename derivation;
- the per-shard checksum makes post-move integrity verification cheap (vs. re-streaming
  through `--verify`), and surfaces a half-copied set (missing a shard's meta).

### 2.2 "Export bundle" action (GUI + CLI)
Select cycle(s) or set(s) → copy, as one atomic bundle, into a target dir / removable:
for **every shard in the selection**, `.pax.zst` + `.idx.zst` + `.frames.json` +
`.meta.json`, plus a manifest slice. Post-sharding the footgun is doubled — "forgot the
sidecars" *and* "forgot some shards" — so export must be **group-atomic**: it expands the
selection through `group_into_sets` and refuses to write a partial set. Directly prevents
both mistakes for the offline-copy workflow. CLI verbs e.g.
`--export-cycle <cycle_id> --into <dir>` and `--export-set <group_id> --into <dir>`.

### 2.3 (Optional, low priority) Self-priming restore
When restore hits a missing sidecar, regenerate it next to the archive (today it
naive-scans without persisting) so the next access is fast. Pure optimization.

---

## Suggested sequencing
1. Prototype the **delete dialog** (type-to-confirm + shard-aware blast radius: all shard
   files, summed bytes, shard count, dependency disclosure) against a throwaway manifest;
   nail the normalized-match UX.
2. Factor `_delete_sets()` out of `_delete_cycles`; add `.failed`-suffix cleanup; add the
   scoped `--delete-cycle` / `--delete-set` actions.
3. Wire the GUI **context menu** (primary) on the Archives tree + keep contextual buttons;
   spawn the delete action off-thread (Remove-Plan/Recover pattern).
4. Per-**shard** `.meta.json` (write on backup, one per shard incl. shard fields; backfill
   via `--reindex`).
5. Manifest-rebuild-from-directory (group `.meta.json` by `shard_group` → sets).
6. Export-bundle action (`--export-cycle` / `--export-set`, group-atomic).

Tie-ins: relates to the failed-backup-recovery roadmap (the `.failed`/manifest
mismatch + no-GUI-delete gap) and the seekable-archive sidecar design.
