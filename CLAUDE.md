# PixelCrypt

PixelCrypt is a desktop tool that encrypts any file with AES-256-GCM, packs the
encrypted bytes into RGB pixels, and saves them as frames of a lossless FFV1
video in an `.mkv` container. Decoding restores the original file byte for byte.
Files of any size are processed one frame at a time.

## Layout

- `engine.py`: the core engine.
  - The public API is `encode_file_to_video()` and `decode_video_to_file()`. Both take an optional `progress` callback, and decode also takes `overwrite`.
  - The byte layout and the streaming design are documented at the top of the file.
- `app.py`: the tkinter desktop app. It has Encode and Decode tabs, a progress bar, Cancel, and optional drag-and-drop. `python app.py --self-test` runs a round trip without opening a window.
- `tests/test_engine.py`: round trips, format pins, progress, cancel, temp-file cleanup, and a memory bound.
- `tests/test_app.py`: drives the real GUI. It skips itself when there's no tkinter or no display.
- `tests/fixtures/v1_*.mkv`: videos written by the original engine. They must always decode.
- `pixelcrypt.spec` and `requirements-build.txt`: the PyInstaller build.
- `.github/workflows/tests.yml`: CI on Ubuntu (under Xvfb), macOS and Windows, with Python 3.10 and 3.13. `build.yml` builds binaries for all three OSes, on a manual run or a `v*` tag.
- `.claude/`: the project skills, the reviewer subagent and the permissions allowlist (see "Claude tooling" below).

## Commands

```bash
sudo apt install python3-tk python3-venv xvfb     # Debian/Ubuntu: tkinter and Xvfb aren't pip packages
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
xvfb-run -a .venv/bin/python -m pytest -q         # Linux; plain pytest elsewhere
.venv/bin/python app.py                           # run the app
.venv/bin/pip install -r requirements-build.txt && .venv/bin/pyinstaller --clean --noconfirm pixelcrypt.spec
```

The pip package is `av` (PyAV), not `pyav`. It bundles its own FFmpeg
libraries, so the engine needs no system FFmpeg.

## Rules

- **Byte-for-byte round trips are the core guarantee.** Every change must keep
  `pytest` green, and new behaviour needs a round-trip test.
- **The on-disk format is versioned.** If you change the stream layout, bump
  `FORMAT_VERSION` and keep decoding older versions.
  - `tests/fixtures/v1_*.mkv` and the golden hex test pin format v1. Never regenerate them to make a test pass.
  - Streaming didn't change the layout, so the format is still v1.
- **Pixel format:** FFV1 rejects 8-bit `rgb24` and `gbrp`. Frames are written as
  `bgr0` (3 data bytes plus one zero byte per pixel) straight into the plane
  buffer, because PyAV's `from_ndarray` doesn't support `bgr0`. Don't route the
  data through swscale or any colour conversion.
- **Crypto:** PBKDF2-HMAC-SHA256 with 600k iterations (the count is stored in
  the header), and AES-256-GCM with the whole cleartext header as associated
  data. Don't weaken any of these.
- **The stored length matters.** The header records `BODY_LEN`, so the zero
  padding in the last frame is never fed into GCM. Without it, every encrypted
  decode fails.
- **Never hold a whole file in memory.** Encode and decode go one frame at a
  time, with about 100-200 MiB peak RSS at any size. The `verify` skill checks
  this with a 1 GiB round trip.
- **Unverified plaintext never gets the real name.**
  - Decoded data goes to a `.pixelcrypt-*.part` file and is renamed only after the GCM tag verifies.
  - Encoding also writes a `.part` file and replaces the destination only when the video is complete.
  - Every error path deletes the `.part` file.
- **Progress and cancel:** `progress(done_bytes, total_bytes)` is called on the
  worker thread about once per frame. Raising from it aborts the job and cleans
  up. The GUI's Cancel relies on this.
- Strip directories from the stored filename on restore (`_safe_filename`).
- Raise the typed errors (`WrongPasswordError`, `PasswordRequiredError`,
  `NotPixelCryptError`). The GUI maps them to messages in `describe_error()`.
- **GUI threads:** the worker thread never touches a widget. It only puts
  messages on a queue, and the main thread polls the queue with `after()`.
  tkinter isn't thread-safe.
- **tkinter details:** use `pady`, not `py`. Drag-and-drop needs `tkinterdnd2`, and the
  app runs without it.
- Code must run on Python 3.10, the oldest version in CI.

## Claude tooling

- **Skills** in `.claude/skills/`:
  - `run-app`: launch the GUI, or screenshot it headless under Xvfb.
  - `verify`: the full tests plus the large-file memory check. Run it before committing.
  - `package`: the PyInstaller build and its smoke test.
- **Subagent:** `format-guardian` is a read-only review of a diff against the format,
  crypto and temp-file rules above. Use it after changing `engine.py`.
- **Permissions:** `.claude/settings.json` allows the test, run and build commands that the skills use,
  plus read-only git.

## Roadmap

1. ~~Core engine and round-trip tests~~ (done, PR #1)
2. ~~Large files: stream the encode and decode frame by frame~~ (done)
3. ~~Desktop GUI (`app.py`, tkinter)~~ (done)
4. ~~Packaging: a one-file PyInstaller build for each OS~~ (done). The Linux build
   is verified locally. Windows and macOS build through `build.yml` once Actions
   runs.

## Known issues

- GitHub Actions jobs weren't starting because of a billing lock on the GitHub
  account. This isn't a code problem, but until it's fixed, CI hasn't run.
  Python 3.10 and the Windows and macOS builds are untested.
