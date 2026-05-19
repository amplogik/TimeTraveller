#!/usr/bin/env python3
"""Frame-correctness test: framed vs monolithic on IDENTICAL input bytes.

Builds a tar stream from a tmp fixture, fans the bytes out to plain
`zstd -3` and to `frame_write.py` concurrently, then compares the
decompressed outputs.

Isolates frame_write's correctness from any source-side drift — which
is what invalidated the byte-identity check in run_ab.py on 2026-05-19
(WinBoat briefly touched data.img between the baseline and framed runs,
bumping its mtime in the tar header).

Exit 0 = framed and monolithic decompress to byte-identical streams.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

FRAME_SIZE = "1M"      # multiple of 512; small for a fast test
CHUNK = 64 * 1024      # fanout granularity — small enough to not stall a pipe


def build_fixture(d: Path) -> int:
    """Create files of varied size/entropy under d. Returns total bytes."""
    sizes = {
        "small.bin":        100 * 1024,         # 100 KiB random
        "big.bin":          5 * 1024 * 1024,    # 5 MiB random — spans multiple frames
        "compressible.bin": 200 * 1024,         # 200 KiB of constant data
    }
    total = 0
    for name, n in sizes.items():
        if name == "compressible.bin":
            (d / name).write_bytes(b"A" * n)
        else:
            (d / name).write_bytes(secrets.token_bytes(n))
        total += n
    return total


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="frame_test_") as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        src.mkdir()
        in_bytes = build_fixture(src)

        mono = tmp / "mono.zst"
        framed = tmp / "framed.zst"
        index = tmp / "framed.frames.json"

        tar_argv = ["tar", "--format=pax", "-C", str(tmp), "-c", "src"]
        zstd_argv = ["zstd", "-3", "-q", "-c"]
        framer_argv = [
            sys.executable, str(Path(__file__).parent / "frame_write.py"),
            "--frame-size", FRAME_SIZE,
            "--zstd-level", "3",
            "--index-out", str(index),
            str(framed),
        ]

        tar = subprocess.Popen(tar_argv, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
        with open(mono, "wb") as mono_f:
            zstd = subprocess.Popen(zstd_argv, stdin=subprocess.PIPE,
                                    stdout=mono_f)
            framer = subprocess.Popen(framer_argv, stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)

            assert tar.stdout is not None
            assert zstd.stdin is not None and framer.stdin is not None
            try:
                while True:
                    chunk = tar.stdout.read(CHUNK)
                    if not chunk:
                        break
                    zstd.stdin.write(chunk)
                    framer.stdin.write(chunk)
            finally:
                zstd.stdin.close()
                framer.stdin.close()

            zstd_rc = zstd.wait()
            framer_rc = framer.wait()
            tar_rc = tar.wait()

        for name, rc in (("tar", tar_rc), ("zstd", zstd_rc), ("framer", framer_rc)):
            if rc != 0:
                print(f"FAIL: {name} exited rc={rc}", file=sys.stderr)
                return 1

        cmp_proc = subprocess.run(
            ["bash", "-c", f"cmp <(zstdcat {mono}) <(zstdcat {framed})"],
            capture_output=True, text=True,
        )

        mono_sz = mono.stat().st_size
        framed_sz = framed.stat().st_size
        print(f"input:       {in_bytes:,} bytes ({in_bytes/1024**2:.2f} MiB) "
              f"across 3 files", file=sys.stderr)
        print(f"frame size:  {FRAME_SIZE}", file=sys.stderr)
        print(f"monolithic:  {mono_sz:,} bytes", file=sys.stderr)
        print(f"framed:      {framed_sz:,} bytes", file=sys.stderr)
        print(f"size delta:  {framed_sz - mono_sz:+,} bytes "
              f"({100*(framed_sz-mono_sz)/mono_sz:+.2f}%)", file=sys.stderr)

        if cmp_proc.returncode == 0:
            print("PASS: decompressed streams are byte-identical", file=sys.stderr)
            return 0
        print(f"FAIL: cmp rc={cmp_proc.returncode}", file=sys.stderr)
        if cmp_proc.stdout:
            print(f"stdout: {cmp_proc.stdout}", file=sys.stderr)
        if cmp_proc.stderr:
            print(f"stderr: {cmp_proc.stderr}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
