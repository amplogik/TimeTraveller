# Archive integrity: per-frame checksums + verify

Status: SHIPPED v1.4.0.

## Problem

TimeTraveller writes each archive as a stream and judged success only from the
process exit codes and tar/pax stderr (see [`pax.py`](../../timetraveller/pax.py)
`RunResult.status`). Nothing ever read the *persisted* compressed bytes back, so
corruption introduced anywhere after compression — the client write buffer, the
NFS client, the network, or the storage server before it commits — passed
undetected until a restore, the worst possible moment to discover it.

This was not hypothetical. The 2026-06-14 `home` full wrote shard 3 with **8 of
4273 frames corrupt**, scattered from the 70 GB to the 208 GB mark. The archive
was byte-complete (not truncated); individual 64 MiB zstd frames simply failed
to decode. The only reason it surfaced at all was that a *separate* false-failure
(see below) triggered a `--recover-failed`, which happens to re-read the whole
archive and choked on the bad frames.

Because the backup target is **ZFS/raidz2**, which already checksums and
scrub-repairs every block at rest, eight scattered corruptions almost certainly
did **not** rot on the platters — they entered in the client→NFS→server write
path and ZFS faithfully stored the bytes it was handed. ZFS protects against
media rot; it does not protect against corrupt data handed to it.

## Design

The fix records integrity **at write time, for free**, and decouples *checking*
it from the backup:

1. **Per-frame SHA-256, computed inline.** The framed writer already streams
   every byte through one pass to produce independent 64 MiB zstd frames and the
   `.idx.zst` index — the bytes are already in hand. We add the SHA-256 of each
   frame's *compressed* bytes to the `frames.json` record (`csum`), bumping the
   sidecar to **version 2** (`csum_algo: "sha256"`). Hashing the *compressed*
   bytes (the exact `[co, co+cl)` range on disk) lets verification skip
   decompression entirely.

2. **zstd's own frame checksum** (`ZstdCompressor(write_checksum=True)`). A
   4-byte content checksum is embedded in every frame, so *every* decompression
   — restore, browse, `--recover-failed` — self-verifies automatically, with no
   sidecar needed.

3. **`--verify`** re-reads each frame's persisted bytes and compares the hash to
   the recorded digest. No decompression; it is I/O-bound on the read. It is
   shard-group aware (verify a whole logical backup by its stem), includes
   quarantined `.failed` archives, and reports the exact corrupt frames. Older
   (v1) archives transparently fall back to a full `zstdcat | tar -tf` decompress.

### Why not verify-after-write?

A mandatory re-read after each backup would re-read ~800 GB over NFS (~35–45 min
for a `home` full) — handing back exactly the post-write read that the inline-
index work was built to eliminate. Instead, recording the digest is free and
verification is a separate, opt-in operation you run when convenient (and could
eventually run NAS-side, reading the pool locally and skipping NFS).

### Cost (measured, Ryzen 9 9950X, SHA-NI)

SHA-256 runs at ~2.7 GB/s (≈24.5 ms per 64 MiB frame — and *faster* than MD5 on
this CPU thanks to SHA-NI). A shard produces a frame roughly every ~500 ms
(tar-bound), so hashing consumes ~5% of the frame thread's otherwise-idle time.
Net wall-clock impact on a backup: effectively zero.

## Limitation: corruption before the hash (non-ECC RAM)

The SHA-256 is taken on the client, immediately after compression. It therefore
catches corruption from that point onward (write buffer → NFS → network →
storage), which is the failure class observed above. It **cannot** catch
corruption that occurs *before* the digest is taken — most notably a bit flip in
RAM on a **non-ECC** host — because the corrupt bytes are hashed faithfully and
will verify clean against their own (corrupt) digest. ZFS downstream is in the
same position.

This is a hardware problem that wants a hardware solution (ECC memory), and it is
deliberately out of scope:

- ECC DDR5 is currently cost-prohibitive, and this workstation is specced to sit
  between consumer and server hardware on purpose — to develop against what a
  typical user actually has, with headroom, rather than against a server build.
  An ECC-only guarantee would not be representative of the target environment.
- In-RAM corruption is statistically rare; if it is a recurring problem on a given
  machine, that machine has a fault to fix, not something a backup format can
  paper over.

If you do run on ECC hardware, the per-frame SHA-256 then gives you an
end-to-end guarantee from source read to restore.

## Related: benign file-race warnings are not failures

Separately from corruption, a snapshot-less backup of a live home directory
routinely races volatile files: a browser's automatic cache/Safe-Browsing
cleanup deletes or rewrites store files *while tar is reading them*, even when
nobody is actively browsing — an idle-but-open browser at 03:00 is the common
case, not the exception. GNU tar reports these as `Cannot stat: No such file or
directory` (a vanished file, exit 2) and `file changed as we read it` (exit 1).
Both leave a structurally valid archive with the affected member simply skipped.

`pax.py` classifies a run whose only diagnostics are these benign races as
`ok-with-warnings`, not `failed`. A regression where a `file changed as we read
it` line co-occurring with a vanished-file line tipped the whole run to `failed`
was fixed in v1.4.0 (the "file changed" wording had been missing from the
benign-stderr matcher). This integrity work is orthogonal: the classifier decides
whether *skipped* files are tolerable; the checksums decide whether the bytes we
*did* write are intact.
