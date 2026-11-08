---
name: verify
description: Run PixelCrypt's full verification. That's the pytest suite (engine round trips, the golden v1 format fixtures, and the GUI tests on a virtual display) plus a large-file streaming round trip that checks byte-for-byte equality and peak memory. Use this before every commit or PR, after any change to engine.py, app.py, the tests or the dependencies, and whenever someone asks to run the tests, check that large files still work, confirm memory stays bounded, or prove a change didn't break the round-trip guarantee.
---

# Verify PixelCrypt

PixelCrypt promises that a decoded file is byte-for-byte identical to the original, at any size, and that videos written by older versions still decode. Each step below checks one part of that promise. A skipped step is not a pass, so report it as skipped.

## 1. Test suite (always)

```bash
xvfb-run -a .venv/bin/python -m pytest -q        # Linux: GUI tests on a virtual display
.venv/bin/python -m pytest -q                    # macOS/Windows, or a Linux desktop session
```

What the suite covers:

- `tests/test_engine.py`:
  - Round trips across edge-case sizes, with and without a password.
  - Wrong, missing and tampered passwords.
  - Progress, cancel and temp-file cleanup.
  - A `tracemalloc` memory bound.
  - **The format pins:** `tests/fixtures/v1_*.mkv` (written by the original engine) must still decode, and `build_stream` must still produce the golden hex.
- `tests/test_app.py`: the GUI driven through its real widgets and worker thread. It skips itself when there's no tkinter or no display.

Read the summary line. If you see `s` (skipped) for `test_app.py`, the GUI wasn't tested, usually because Xvfb or `python3-tk` is missing, so say so. If a fixture or golden test fails, don't "fix" it by regenerating the fixture or the hex. Those tests exist to catch exactly that kind of change. Read the "on-disk format is versioned" rule in CLAUDE.md first.

The suite takes about 30-60 s. Most of that is PBKDF2 at 600k iterations, which is deliberate.

## 2. Large-file streaming check (when engine.py, the dependencies or the frame logic changed)

```bash
.venv/bin/python .claude/skills/verify/scripts/bigfile_check.py --size-mib 1024 --wrong-password
```

The script:

- Writes a random file and encodes it, then decodes it, each in a child process so peak RSS is measured per step.
- Compares SHA-256 hashes.
- With `--wrong-password`, checks that a wrong password is rejected in seconds and leaves no files behind.

It exits 0 only if everything passes. Peak RSS should stay around 100-200 MiB at any file size. On the reference machine, a 1 GiB file took 16 s to encode and 38 s to decode, at 166 MiB and 87 MiB peak RSS. It needs about 3.2x the file size in free disk space. The script checks that and deletes its files afterwards.

The files go in `build/bigfile-check/` (gitignored) by default. Don't point `--work-dir` at `/tmp` or a scratch folder under it. On many Linux systems `/tmp` is a small, RAM-backed tmpfs: a 1 GiB run fails there with "Disk quota exceeded", and files in RAM skew the memory numbers.

To show a before/after memory comparison against `main`, use 256 MiB. The old in-memory engine needs several times the file size in RAM, so 1 GiB could exhaust memory:

```bash
mkdir -p build && git show main:engine.py > build/engine_main.py
.venv/bin/python .claude/skills/verify/scripts/bigfile_check.py --size-mib 256 \
  --engine build/engine_main.py --max-rss-mib 100000
```

## 3. Review of format and crypto changes (when engine.py changed)

Ask the `format-guardian` subagent to review the diff (for example `main...HEAD`). It checks the format, crypto and temp-file invariants that tests can't fully pin down, such as nonce handling and what gets authenticated.

## 4. Report

Give the user:

- The pytest counts, including what was skipped and why.
- The large-file numbers: size, seconds and MiB/s for each step, peak RSS, whether the hashes matched, and the wrong-password result.
- Any format-guardian findings.

If anything failed, quote the failing test or output line instead of summarizing it away.
