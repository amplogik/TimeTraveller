# Design note: Inline index build — eliminate the post-write sidecar re-read

Status: **SHIPPED v1.0.7** (2026-06-13). Filed 2026-06-08 after observing the index
cost on a 753 GiB `home` full; prototyped + validated (byte-identity + throughput)
under `prototype/inline_index.py`, then integrated via Option A. The prototype has
been retired now that the logic lives in `index.InlineIndexWriter`. Mirrors the entry
in the seekable-archive roadmap.

Implementation: `index.InlineIndexWriter` builds `<archive>.idx.zst` in a background
thread fed the uncompressed tar stream by `framewriter.write_framed` (new
`index_writer=` arg); `pax.run_with_file_list` wires it for framed runs and surfaces
`RunResult.index_built`; `worker.action_backup` skips the post-write `write_sidecar`
when the inline build succeeded. Fallbacks preserved: `--no-framed` and any inline
failure degrade to the post-write pass; `--reindex` remains the repair path. Validated
byte-identical to `write_sidecar` (incl. long-name/symlink/hardlink) and against a real
fast single-file extract.

## Background / motivation

Building `<name>.pax.zst.idx.zst` is currently a **second full pass over the archive**.
On large, poorly-compressible data this roughly **doubles** backup wall time and is
NFS-read-bound.

Measured 2026-06-08, `home` full to TrueNAS over 10GbE:

| Phase | Time | Notes |
|---|---|---|
| Archive write (pass 1) | **1h 32m** | 752.7 GiB written, ~1.23× ratio |
| Index re-read (pass 2) | **~28 min** | worker pinned `D`-state, fd read-only on the `.pax.zst`, ~370 MB/s NFS read |
| **Total** | **2h 0m** | index pass = ~23% of wall time |

(Contrast the Phase-C 67 GB system smoke: 72s — it read far faster / was warm. The
penalty scales with archive size and with NFS read latency, so it only gets worse on
the big home/media sets this tool targets.)

## Current architecture (verified, for reference)

Two passes, only the first of which needs to touch the archive bytes:

**Pass 1 — write (single read of the source, archive + frame map produced inline):**
- `pax.run_with_file_list` (`pax.py:375`) runs `pax | zstd` as subprocesses; a
  background thread consumes pax's **uncompressed** stdout.
- `framewriter.write_framed` (`framewriter.py:64`) chunks that stream into 64 MiB
  independent zstd frames and emits `<name>.pax.zst.frames.json` **inline**, with
  incremental flush to `.frames.json.partial` + atomic rename on clean exit
  (`framewriter.py:50`).

**Pass 2 — index (a full re-read + re-decompress of the archive just written):**
- After the stream, `action_backup` calls `index.write_sidecar(archive_path)`
  (`worker.py:1254`).
- `index.write_sidecar` (`index.py:118`) re-opens the `.pax.zst` and streams it through
  `zstd.ZstdDecompressor().stream_reader(...)` into `tarfile.open(fileobj=..., mode="r|")`
  (`index.py:142-149`), iterating **every member** to read `TarInfo.offset` /
  `.offset_data` and emit the v2 JSONL `.idx.zst`.
- The only reason for the re-read: `tarfile` is the only thing that exposes the
  uncompressed header/data byte offsets Phase D needs; `tar -tvf` cannot.

Finalize ordering (`action_backup`): status stamped at stream-completion
(`worker.py:1229`) → `write_sidecar` → `action_prune` (`worker.py:1263`) →
`date_finished` **re-stamped after the sidecar** (`worker.py:1268`) → "Backup complete:
Xh Ym total." So the reported total *does* include pass 2 (the earlier `date_finished`
timing wart is resolved).

**Key invariant this design exploits:** the uncompressed tar byte stream **already flows
through the framewriter thread in pass 1** — the exact bytes pass 2 pays to reproduce.

## Proposal — capture offsets inline during pass 1

Record each member's `(header_offset, data_offset)` *while the uncompressed stream is
already in hand*, and emit `.idx.zst` alongside `.frames.json`. No second NFS read, no
second decompress.

Two implementation shapes (decide in prototype):

- **A. Tee the uncompressed stream to a header-only `tarfile` reader.** Feed the same
  pax-stdout bytes the framewriter chunks into a `tarfile.open(mode="r|")` that reads
  headers and *skips bodies by their known size*, emitting the **identical v2 records**
  `index.write_sidecar` produces today — just fed live instead of from a re-read. Reuses
  the proven offset logic; lowest correctness risk.
- **B. Inline tar-header parser inside `framewriter`.** Track 512-byte record boundaries
  + pax extended headers directly. Avoids a second stream consumer but re-implements
  part of `tarfile`; only worth it if option A backpressures the writer.

Output stays the **v2 JSONL `.idx.zst`** format (`index.py`), so `archive.parse_index`
and `extract.py` readers are untouched.

## Caveats / correctness

- **Pax extended headers** spanning frame boundaries — a noted Phase-C concern, but
  offsets are positions in the *uncompressed* stream, which the inline reader sees
  contiguously; frame boundaries are irrelevant to offset capture. Inline is arguably
  *more* robust here than the re-read.
- **Hard links, symlinks, sparse, long-name (pax) headers** — must match `tarfile`'s
  handling; option A inherits it for free.
- **Crash-resilience** — incremental flush + atomic rename, mirroring the
  `.frames.json.partial` pattern (`framewriter.py:50`). A mid-run crash should leave a
  regenerable partial; `--reindex` (`worker.py:544`) remains the rebuild path.
- **Throughput guardrail** — inline parsing adds CPU to the stream consumer; if it can't
  keep up it backpressures pax and slows pass 1. Measure against the existing +11%
  framing budget. If option A's tee can't keep pace, run the header reader on its own
  thread off a bounded queue, or fall back to option B.

## Backward / forward compatibility

- On-disk format unchanged (v2 JSONL). Old archives unaffected.
- `--reindex` stays as the rebuild/repair path for archives written before this lands,
  or with missing/corrupt sidecars.
- `--no-framed` (no frame thread) keeps today's post-write `write_sidecar` pass as the
  fallback. Any inline-build failure should degrade to the same post-write pass rather
  than fail the backup.

## Suggested sequencing

1. Prototype in `prototype/`: tee pax stdout → header-only `tarfile` reader → emit v2
   `.idx.zst`; `cmp` records against `index.write_sidecar` output on a real archive for
   byte-identity.
2. Measure write-pass slowdown vs. the current framed write; confirm within the framing
   budget.
3. Integrate into the `framewriter`/`paxlib` write path behind the `framed` flag; flip
   `has_sidecar` inline.
4. Keep `index.write_sidecar` + `--reindex` as fallback/repair.

Tie-ins: this effectively folds Phase C's offset capture into Phase B's write pass
(see the seekable-archive roadmap). Relates to the finalize-ordering notes and the
prototype-first discipline.
