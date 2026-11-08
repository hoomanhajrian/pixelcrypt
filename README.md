# PixelCrypt

PixelCrypt hides any file inside a video. It encrypts the file with AES-256-GCM,
lays the encrypted bytes out as RGB pixels, and saves them as frames of a
lossless FFV1 video in an `.mkv` file. Decoding reverses every step and
restores the original file byte for byte.

- **Strong encryption:** AES-256-GCM with a PBKDF2-HMAC-SHA256 key (600,000
  iterations). Any change to the video is detected.
- **Any size:** files are processed one frame at a time, so a 1 GB file needs
  about the same memory as a 1 MB one (around 100-200 MB).
- **Desktop app:** Encode and Decode tabs, a progress bar, Cancel, and drag-and-drop.
- **Safe by default:**
  - A wrong password is reported in under a second.
  - A failed or cancelled job leaves no half-written files.
  - Decoding never replaces an existing file.

## Install

Requires Python 3.10+ with tkinter. The `av` package (PyAV) ships its own
FFmpeg, so you don't need to install FFmpeg.

```bash
# Debian/Ubuntu only: tkinter (and Xvfb, for running the GUI tests headless) come from apt
sudo apt install python3-tk python3-venv xvfb

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The python.org installers for Windows and macOS already include tkinter.

## Use the app

```bash
.venv/bin/python app.py
```

- **Encode:**
  1. Choose a file. The video name is suggested for you (`report.pdf` → `report.pdf.mkv`).
  2. Type a password twice and click **Encode**.
  3. Keep the password safe, because the file can't be restored without it. Leaving the password empty stores the file unencrypted, and the app asks you to confirm.
- **Decode:** choose a `.mkv` made by PixelCrypt, choose a folder (the video's own folder by default), enter the password and click **Decode**. If a file with the same name already exists, the restored one is saved as `name (1).ext`.
- **Drag and drop:** drop a file onto either tab. This needs `tkinterdnd2`, which is in `requirements.txt`.

## Use it from Python

```python
from engine import encode_file_to_video, decode_video_to_file

def show(done, total):
    print(f"{100 * done / total:.0f}%")

frames = encode_file_to_video("report.pdf", "report.mkv", password="s3cret", progress=show)
restored = decode_video_to_file("report.mkv", "restored/", password="s3cret", overwrite=False)
```

- **Errors:** decoding raises `WrongPasswordError`, `PasswordRequiredError` or `NotPixelCryptError`, all subclasses of `ValueError`.
- **Progress:** `progress(done_bytes, total_bytes)` is called about once per frame, from the calling thread. Raise any exception from it to cancel. The job stops and its temporary file is deleted.

## Build a standalone app

```bash
.venv/bin/pip install -r requirements-build.txt
.venv/bin/pyinstaller --clean --noconfirm pixelcrypt.spec
./dist/PixelCrypt --self-test     # should print "PixelCrypt self-test passed"
```

This produces `dist/PixelCrypt` on Linux, `dist/PixelCrypt.exe` on Windows and
`dist/PixelCrypt.app` on macOS. Each OS has to build its own. The `build`
GitHub Actions workflow builds all three when you run it manually or push a
`v*` tag.

## How it works

- **Key derivation:** PBKDF2-HMAC-SHA256 with 600,000 iterations and a random
  16-byte salt. The iteration count is stored in the video, so it can be raised
  later without breaking old files.
- **Encryption:** AES-256-GCM with a random 12-byte nonce. The whole cleartext
  header is authenticated too, so any modification is detected.
- **Pixels:** each pixel holds 3 data bytes. FFV1 can't take 8-bit packed RGB
  (`rgb24`), so frames are written as `bgr0`, with the 4th byte always zero.
  No colour conversion touches the data, so it stays lossless.
- **Video:** FFV1 in a Matroska container. The last frame is zero-padded, and the
  header records the exact data length, so the padding is dropped on decode.
- **Streaming:** every length in the header follows from the file size, and the
  GCM tag comes last, so both directions work one frame at a time.
  - The restored file goes to a temporary `.part` file and gets its real name only once the tag has been verified.
  - A wrong password is caught at the first frame, because the decrypted lengths don't add up.

The exact byte layout is documented at the top of `engine.py`.

> Don't re-encode, compress or upload these videos to platforms that transcode
> them (YouTube etc.). Any lossy step destroys the data.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
xvfb-run -a .venv/bin/python -m pytest -q   # Linux (GUI tests on a virtual display)
.venv/bin/python -m pytest -q               # macOS / Windows
```

- **Format pins:** `tests/fixtures/` holds videos made by the first version of the engine. They pin the file format: if they stop decoding, the change broke compatibility.
- **Claude Code:** `CLAUDE.md` has the project rules. `.claude/` has the skills for running, verifying and packaging the app, and a reviewer subagent for format and crypto changes.
