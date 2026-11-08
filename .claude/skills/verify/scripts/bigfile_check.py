"""Large-file round trip through PixelCrypt, measuring peak memory and speed.

Creates a random file, encodes it to a video and decodes it back. Each step
runs in its own child process, so its peak RSS can be measured on its own.
Then the SHA-256 hashes are compared. Exit code 0 means the round trip was
byte-exact and every step stayed under the memory budget.

    .venv/bin/python .claude/skills/verify/scripts/bigfile_check.py --size-mib 1024

Compare against another engine, such as the one on main, with --engine:

    mkdir -p build && git show main:engine.py > build/engine_main.py
    .venv/bin/python .claude/skills/verify/scripts/bigfile_check.py --size-mib 256 \\
        --engine build/engine_main.py --max-rss-mib 100000

Linux and macOS only, because it uses the resource module. Needs about 3.2x
--size-mib of free disk space. The files go in build/bigfile-check/ (gitignored)
by default, not /tmp: on many Linux systems /tmp is a RAM-backed tmpfs, which
is small and would also distort the memory numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
MIB = 1 << 20

# Runs in a child process: python -c CHILD engine.py op src dst
CHILD = r"""
import importlib.util, json, os, resource, sys, time
engine_path, op, src, dst = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("engine", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
password = os.environ.get("PIXELCRYPT_CHECK_PASSWORD") or None
result = {}
start = time.perf_counter()
try:
    if op == "encode":
        result["frames"] = engine.encode_file_to_video(src, dst, password=password)
    elif op == "decode":
        result["path"] = engine.decode_video_to_file(src, dst, password=password)
    elif op == "wrong-password":
        try:
            engine.decode_video_to_file(src, dst, password="not the password")
            result["rejected"] = False
        except engine.WrongPasswordError:
            result["rejected"] = True
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
result["seconds"] = time.perf_counter() - start
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
result["peak_rss_mib"] = rss / (1 << 20) if sys.platform == "darwin" else rss / 1024
print(json.dumps(result))
"""


def run_child(engine_path: str, op: str, src: str, dst: str, env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, engine_path, op, src, dst],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(engine_path),
    )
    lines = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not lines:
        raise SystemExit(f"{op} child process failed:\n{proc.stderr}")
    return json.loads(lines[-1])


def write_random_file(path: str, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "wb") as f:
        remaining = size
        while remaining:
            chunk = os.urandom(min(4 * MIB, remaining))
            f.write(chunk)
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * MIB), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Large-file PixelCrypt round trip.")
    parser.add_argument("--size-mib", type=int, default=1024, help="file size (default: 1024)")
    parser.add_argument(
        "--engine", default=os.path.join(REPO, "engine.py"),
        help="engine.py to test (default: the repo's)",
    )
    parser.add_argument(
        "--work-dir", default=os.path.join(REPO, "build", "bigfile-check"),
        help="where to put the temporary files (default: build/bigfile-check/ in the repo)",
    )
    parser.add_argument(
        "--max-rss-mib", type=float, default=400,
        help="fail if a step's peak RSS is above this (default: 400)",
    )
    parser.add_argument("--no-password", action="store_true", help="test without encryption")
    parser.add_argument(
        "--wrong-password", action="store_true",
        help="also check that a wrong password is rejected quickly and leaves no files",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary files")
    args = parser.parse_args()

    if sys.platform == "win32":
        print("This check uses the resource module, which Windows doesn't have.", file=sys.stderr)
        return 2
    engine_path = os.path.abspath(args.engine)
    size = args.size_mib * MIB
    work_parent = args.work_dir
    os.makedirs(work_parent, exist_ok=True)
    free = shutil.disk_usage(work_parent).free
    if free < size * 3.2:
        print(f"Not enough disk space in {work_parent}: need about "
              f"{size * 3.2 / MIB:.0f} MiB, have {free / MIB:.0f} MiB.", file=sys.stderr)
        return 2

    env = dict(os.environ)
    if args.no_password:
        env.pop("PIXELCRYPT_CHECK_PASSWORD", None)
    else:
        env["PIXELCRYPT_CHECK_PASSWORD"] = "bigfile check password"

    work = tempfile.mkdtemp(prefix="pixelcrypt-bigfile-", dir=args.work_dir)
    ok = True
    try:
        print(f"PixelCrypt large-file check: {args.size_mib} MiB, "
              f"{'plain' if args.no_password else 'encrypted'}, engine {engine_path}")
        src = os.path.join(work, "input.bin")
        t = time.perf_counter()
        src_hash = write_random_file(src, size)
        print(f"  wrote input in {time.perf_counter() - t:.1f} s")

        video = os.path.join(work, "input.bin.mkv")
        steps = [
            ("encode", run_child(engine_path, "encode", src, video, env)),
            ("decode", run_child(engine_path, "decode", video, os.path.join(work, "restored"), env)),
        ]
        for name, result in steps:
            if "error" in result:
                print(f"  {name:<7} FAILED: {result['error']}")
                ok = False
                continue
            rate = args.size_mib / result["seconds"] if result["seconds"] else float("inf")
            extra = ""
            if name == "encode":
                extra = f"  ({result['frames']} frames, video {os.path.getsize(video) / MIB:.0f} MiB)"
            over = result["peak_rss_mib"] > args.max_rss_mib
            ok &= not over
            print(f"  {name:<7} {result['seconds']:7.1f} s  {rate:7.1f} MiB/s  "
                  f"peak RSS {result['peak_rss_mib']:7.1f} MiB"
                  f"{'  OVER BUDGET' if over else ''}{extra}")

        decoded = steps[1][1]
        if "path" in decoded:
            match = sha256_file(decoded["path"]) == src_hash
            ok &= match
            print(f"  sha256  {'match' if match else 'MISMATCH'} ({src_hash[:16]}…)")

        if args.wrong_password and not args.no_password and os.path.exists(video):
            wrong_dir = os.path.join(work, "wrong")
            result = run_child(engine_path, "wrong-password", video, wrong_dir, env)
            left = os.listdir(wrong_dir) if os.path.isdir(wrong_dir) else []
            good = result.get("rejected") is True and not left and result["seconds"] < 10
            ok &= good
            print(f"  wrong password: {'rejected' if result.get('rejected') else 'NOT rejected'} "
                  f"in {result['seconds']:.1f} s, files left: {left or 'none'}"
                  f"{'' if good else '  FAILED'}")
    finally:
        if args.keep:
            print(f"  kept files in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
