# PixelCrypt

PixelCrypt encrypts any file with AES-256-GCM, lays the encrypted bytes out as
RGB pixels, and saves them as frames of a lossless FFV1 video in an `.mkv`
container. Decoding reverses every step and restores the original file byte for
byte.

This repository currently contains the core engine (`engine.py`) and its tests.
The desktop app, large-file streaming, and packaging are planned next.

## Setup

Requires Python 3.10+. The `av` package (PyAV) ships its own FFmpeg libraries,
so a separate FFmpeg install isn't needed for the engine.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Usage

```python
from engine import encode_file_to_video, decode_video_to_file

frames = encode_file_to_video("report.pdf", "report.mkv", password="s3cret")
restored = decode_video_to_file("report.mkv", "restored/", password="s3cret")
```

Without a password the data is stored unencrypted. Decoding raises
`WrongPasswordError`, `PasswordRequiredError` or `NotPixelCryptError` (all
subclasses of `ValueError`) when something is wrong.

## How it works

- **Key derivation:** PBKDF2-HMAC-SHA256 with 600,000 iterations and a random
  16-byte salt. The iteration count is stored in the video so it can be raised
  later without breaking old files.
- **Encryption:** AES-256-GCM with a random 12-byte nonce. The whole cleartext
  header is authenticated too, so any modification is detected.
- **Pixels:** each pixel holds 3 data bytes. FFV1 can't take 8-bit packed RGB
  (`rgb24`), so frames are written as `bgr0`, with the 4th byte always zero.
  No colour conversion touches the data, so it stays lossless.
- **Video:** FFV1 in a Matroska container. The last frame is zero-padded; the
  header records the exact data length, so the padding is dropped on decode.

The exact byte layout is documented at the top of `engine.py`.

> Don't re-encode, compress or upload these videos to platforms that transcode
> them (YouTube etc.). Any lossy step destroys the data.
