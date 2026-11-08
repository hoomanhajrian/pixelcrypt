"""PixelCrypt core engine.

Encrypts a file with AES-256-GCM, lays the resulting bytes out as RGB pixels,
and stores them as frames of a lossless FFV1 video in a Matroska (.mkv)
container. Decoding reverses every step and restores the original file byte
for byte.

Stream layout (the bytes spread across the frames, in order)::

    MAGIC      8B   b"PXCRYPT1"
    VERSION    1B   format version (currently 1)
    FLAGS      1B   bit 0 set = encrypted
    BODY_LEN   8B   big-endian length of BODY
    if encrypted:
      SALT       16B  PBKDF2 salt
      NONCE      12B  AES-GCM nonce
      ITERATIONS 4B   big-endian PBKDF2 iteration count
    BODY       BODY_LEN bytes
    zero padding up to the end of the last frame

BODY is the payload in plain form, or ``ciphertext || tag`` when encrypted.
Everything before BODY is passed to AES-GCM as associated data, so tampering
with the header is detected as well.

PAYLOAD::

    FN_LEN     2B   big-endian length of FILENAME
    FILENAME   UTF-8 base name of the original file
    DATA_LEN   8B   big-endian length of DATA
    DATA       the original file bytes

Pixel format: FFV1 does not accept 8-bit packed ``rgb24`` (or 8-bit planar
``gbrp``), so frames are written as ``bgr0``. Each pixel carries three data
bytes in its B, G and R channels; the fourth byte is always zero. FFV1 codes
this losslessly, and no colour-space conversion ever touches the data.
"""

from __future__ import annotations

import os
import struct

import av
import numpy as np
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes

MAGIC = b"PXCRYPT1"
FORMAT_VERSION = 1
FLAG_ENCRYPTED = 0x01

SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32  # AES-256
KDF_ITERATIONS = 600_000  # PBKDF2-HMAC-SHA256, per current OWASP guidance

BYTES_PER_PIXEL = 3
PIX_FMT = "bgr0"
CODEC = "ffv1"

_FIXED_HEADER = struct.Struct(">8sBBQ")  # magic, version, flags, body_len
_CRYPTO_HEADER = struct.Struct(f">{SALT_SIZE}s{NONCE_SIZE}sI")  # salt, nonce, iterations


class PixelCryptError(ValueError):
    """Base class for errors raised while decoding a PixelCrypt video."""


class NotPixelCryptError(PixelCryptError):
    """The video does not contain PixelCrypt data, or it is damaged."""


class PasswordRequiredError(PixelCryptError):
    """The video is encrypted and no password was given."""


class WrongPasswordError(PixelCryptError):
    """The password is wrong, or the encrypted data was modified."""


def derive_key(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    """Derive a 256-bit key from a password with PBKDF2-HMAC-SHA256."""
    return PBKDF2(password, salt, dkLen=KEY_SIZE, count=iterations, hmac_hash_module=SHA256)


# ---------------------------------------------------------------------------
# Byte stream building and parsing
# ---------------------------------------------------------------------------

def _build_payload(filename: str, data: bytes) -> bytes:
    name = filename.encode("utf-8")
    if len(name) > 0xFFFF:
        raise ValueError("File name is too long.")
    return struct.pack(">H", len(name)) + name + struct.pack(">Q", len(data)) + data


def _parse_payload(payload: bytes) -> tuple[str, bytes]:
    try:
        (fn_len,) = struct.unpack_from(">H", payload, 0)
        cursor = 2
        name = payload[cursor : cursor + fn_len].decode("utf-8")
        cursor += fn_len
        (data_len,) = struct.unpack_from(">Q", payload, cursor)
        cursor += 8
    except (struct.error, UnicodeDecodeError) as exc:
        raise NotPixelCryptError("The file header inside the video is damaged.") from exc
    data = payload[cursor : cursor + data_len]
    if len(data) != data_len:
        raise NotPixelCryptError("The video is truncated: file data is incomplete.")
    return name, data


def build_stream(filename: str, data: bytes, password: str | None = None) -> bytes:
    """Return the unpadded PixelCrypt byte stream for a file."""
    payload = _build_payload(filename, data)

    if not password:
        header = _FIXED_HEADER.pack(MAGIC, FORMAT_VERSION, 0, len(payload))
        return header + payload

    salt = get_random_bytes(SALT_SIZE)
    nonce = get_random_bytes(NONCE_SIZE)
    body_len = len(payload) + TAG_SIZE
    header = _FIXED_HEADER.pack(MAGIC, FORMAT_VERSION, FLAG_ENCRYPTED, body_len)
    header += _CRYPTO_HEADER.pack(salt, nonce, KDF_ITERATIONS)

    cipher = AES.new(derive_key(password, salt), AES.MODE_GCM, nonce=nonce)
    cipher.update(header)
    ciphertext, tag = cipher.encrypt_and_digest(payload)
    return header + ciphertext + tag


def parse_stream(stream: bytes, password: str | None = None) -> tuple[str, bytes]:
    """Parse a (possibly padded) PixelCrypt byte stream into (filename, data)."""
    if len(stream) < _FIXED_HEADER.size:
        raise NotPixelCryptError("This video does not contain PixelCrypt data.")
    magic, version, flags, body_len = _FIXED_HEADER.unpack_from(stream, 0)
    if magic != MAGIC:
        raise NotPixelCryptError("This video does not contain PixelCrypt data.")
    if version != FORMAT_VERSION:
        raise NotPixelCryptError(f"Unsupported PixelCrypt format version {version}.")

    cursor = _FIXED_HEADER.size
    encrypted = bool(flags & FLAG_ENCRYPTED)

    if not encrypted:
        body = stream[cursor : cursor + body_len]
        if len(body) != body_len:
            raise NotPixelCryptError("The video is truncated: data is incomplete.")
        return _parse_payload(body)

    if not password:
        raise PasswordRequiredError(
            "This video is encrypted. Please provide the decryption password."
        )
    if len(stream) < cursor + _CRYPTO_HEADER.size:
        raise NotPixelCryptError("The video is truncated: encryption header is incomplete.")
    salt, nonce, iterations = _CRYPTO_HEADER.unpack_from(stream, cursor)
    cursor += _CRYPTO_HEADER.size
    header = stream[:cursor]

    body = stream[cursor : cursor + body_len]
    if len(body) != body_len or body_len < TAG_SIZE:
        raise NotPixelCryptError("The video is truncated: encrypted data is incomplete.")
    ciphertext, tag = body[:-TAG_SIZE], body[-TAG_SIZE:]

    cipher = AES.new(derive_key(password, salt, iterations), AES.MODE_GCM, nonce=nonce)
    cipher.update(header)
    try:
        payload = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as exc:
        raise WrongPasswordError(
            "Incorrect password, or the video has been modified."
        ) from exc
    return _parse_payload(payload)


# ---------------------------------------------------------------------------
# Pixel <-> frame conversion
# ---------------------------------------------------------------------------

def _bytes_to_frame(chunk: np.ndarray, width: int, height: int) -> av.VideoFrame:
    """Pack width*height*3 data bytes into one bgr0 video frame."""
    rgb = chunk.reshape(height, width, BYTES_PER_PIXEL)
    frame = av.VideoFrame(width, height, PIX_FMT)
    plane = frame.planes[0]
    buf = np.zeros((height, plane.line_size), dtype=np.uint8)
    pixels = buf[:, : width * 4].reshape(height, width, 4)
    pixels[..., 0] = rgb[..., 2]  # B
    pixels[..., 1] = rgb[..., 1]  # G
    pixels[..., 2] = rgb[..., 0]  # R
    plane.update(buf)
    return frame


def _frame_to_bytes(frame: av.VideoFrame) -> bytes:
    """Unpack one bgr0 video frame back into width*height*3 data bytes."""
    if frame.format.name != PIX_FMT:
        frame = frame.reformat(format=PIX_FMT)
    plane = frame.planes[0]
    width, height = frame.width, frame.height
    buf = np.frombuffer(plane, dtype=np.uint8).reshape(height, plane.line_size)
    pixels = buf[:, : width * 4].reshape(height, width, 4)
    return pixels[..., [2, 1, 0]].tobytes()  # R, G, B


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encode_file_to_video(
    input_path: str,
    output_video_path: str,
    password: str | None = None,
    width: int = 1080,
    height: int = 1080,
    fps: int = 30,
) -> int:
    """Encrypt a file and store its bytes as a lossless FFV1 .mkv video.

    Returns the number of frames written.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Width and height must be positive.")
    if fps <= 0:
        raise ValueError("FPS must be positive.")

    with open(input_path, "rb") as f:
        raw_data = f.read()

    stream_bytes = build_stream(os.path.basename(input_path), raw_data, password)

    bytes_per_frame = width * height * BYTES_PER_PIXEL
    padding = (-len(stream_bytes)) % bytes_per_frame
    byte_array = np.frombuffer(stream_bytes + b"\x00" * padding, dtype=np.uint8)
    num_frames = len(byte_array) // bytes_per_frame

    container = av.open(output_video_path, mode="w", format="matroska")
    try:
        stream = container.add_stream(CODEC, rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = PIX_FMT

        for i in range(num_frames):
            chunk = byte_array[i * bytes_per_frame : (i + 1) * bytes_per_frame]
            for packet in stream.encode(_bytes_to_frame(chunk, width, height)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()

    return num_frames


def _safe_filename(name: str) -> str:
    """Strip any directory parts so a crafted video can't write outside output_dir."""
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return "restored_file"
    return base


def decode_video_to_file(
    video_path: str, output_dir: str, password: str | None = None
) -> str:
    """Read a PixelCrypt video and restore the original file into output_dir.

    Returns the path of the restored file.
    """
    try:
        container = av.open(video_path, mode="r")
    except av.FFmpegError as exc:
        raise NotPixelCryptError(f"Could not open the video: {exc}") from exc

    try:
        if not container.streams.video:
            raise NotPixelCryptError("This file has no video stream.")
        chunks = [_frame_to_bytes(frame) for frame in container.decode(video=0)]
    finally:
        container.close()

    filename, data = parse_stream(b"".join(chunks), password)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, _safe_filename(filename))
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path
