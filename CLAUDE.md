# PixelCrypt

PixelCrypt is a desktop tool that encrypts any file with AES-256-GCM, packs the
encrypted bytes into RGB pixels, and saves them as frames of a lossless FFV1
video in an `.mkv` container. Decoding restores the original file byte for byte.

## Layout

- `engine.py`: the core engine. `encode_file_to_video()` and
  `decode_video_to_file()` are the public API. The byte layout is documented at
  the top of the file.
- `tests/test_engine.py`: pytest round-trip tests.
- `.github/workflows/tests.yml`: CI on Ubuntu, macOS and Windows with Python
  3.10 and 3.13.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

The pip package is `av` (PyAV), not `pyav`. It bundles its own FFmpeg
libraries, so the engine needs no system FFmpeg.

## Rules

- **Byte-for-byte round trips are the core guarantee.** Every change must keep
  `pytest` green, and new behaviour needs a round-trip test.
- **The on-disk format is versioned.** If you change the stream layout, bump
  `FORMAT_VERSION` and keep decoding older versions.
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
- Strip directories from the stored filename on restore (`_safe_filename`).
- Raise the typed errors (`WrongPasswordError`, `PasswordRequiredError`,
  `NotPixelCryptError`). The GUI will use them to show clear messages.

## Roadmap

1. ~~Core engine and round-trip tests~~ (done, PR #1)
2. **Large files:** stream the encode and decode frame by frame instead of
   holding the whole file in memory. GCM can stream with
   `cipher.encrypt()` / `cipher.digest()`, and the length fields are already in
   the header.
3. **Desktop GUI (`app.py`, tkinter):** encode and decode tabs, file pickers,
   a password field with a show/hide toggle, a progress bar, and clear error
   messages. Keep these in mind:
   - Run the work on a background thread, but open every dialog and update
     every widget on the main thread (use `after()`). Tkinter isn't thread-safe.
   - Use `pady`, not `py`. The original guide had this typo.
   - Drag-and-drop needs `tkinterdnd2`, because plain tkinter can't do it.
   - Report progress through a callback parameter on the engine functions.
4. **Packaging (optional):** a one-file PyInstaller build for each OS. PyAV's
   wheels already include FFmpeg.

## Known issues

- GitHub Actions jobs weren't starting because of a billing lock on the GitHub
  account. This isn't a code problem.
