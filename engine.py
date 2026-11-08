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

Streaming: both directions work one frame at a time, so memory use depends on
the frame size, not the file size. Every length above follows from the file
size, and the GCM tag comes last, so the encoder never has to look back. The
layout is the same one the in-memory engine wrote, so FORMAT_VERSION is still 1.

Temporary files: encoding writes ``.pixelcrypt-*.part`` next to the output
video and renames it only once the video is complete. Decoding writes the
restored data to a ``.part`` file in the output folder and renames it to the
stored filename only after the GCM tag has been verified, so unverified
plaintext never appears under the real name. On any error, or when the job is
cancelled, the ``.part`` file is deleted.

A wrong password is caught within the first 64 KiB or so of the payload, which
is inside the first frame at the default size: once decrypted, FN_LEN and
DATA_LEN must add up to the payload length from the header. Nothing is written
before that check passes. The GCM tag at the end remains the real integrity
check.

Progress: ``encode_file_to_video`` and ``decode_video_to_file`` accept
``progress(done_bytes, total_bytes)``, called from the thread doing the work
about once per frame. If the callback raises, the job stops, its temporary
file is deleted and the exception propagates. This is how a GUI cancels a job.
"""

from __future__ import annotations

import io
import os
import secrets
import struct
from collections.abc import Callable, Iterable, Iterator
from typing import BinaryIO

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

READ_CHUNK = 1 << 20  # the input file is read 1 MiB at a time
TEMP_PREFIX = ".pixelcrypt-"
TEMP_SUFFIX = ".part"

_FIXED_HEADER = struct.Struct(">8sBBQ")  # magic, version, flags, body_len
_CRYPTO_HEADER = struct.Struct(f">{SALT_SIZE}s{NONCE_SIZE}sI")  # salt, nonce, iterations
_FN_LEN = struct.Struct(">H")
_DATA_LEN = struct.Struct(">Q")
_MAX_FILENAME = 0xFFFF

ProgressCallback = Callable[[int, int], None]
"""``progress(done_bytes, total_bytes)``; raise from it to abort the job."""


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
# Building the byte stream
# ---------------------------------------------------------------------------

def _encode_name(filename: str) -> bytes:
    name = filename.encode("utf-8")
    if len(name) > _MAX_FILENAME:
        raise ValueError("File name is too long.")
    return name


def _stream_size(name_len: int, data_len: int, encrypted: bool) -> int:
    """Length of the unpadded stream for a file, without building it."""
    payload_len = _FN_LEN.size + name_len + _DATA_LEN.size + data_len
    if encrypted:
        return _FIXED_HEADER.size + _CRYPTO_HEADER.size + payload_len + TAG_SIZE
    return _FIXED_HEADER.size + payload_len


def _read_exactly(src: BinaryIO, size: int) -> Iterator[bytes]:
    """Yield exactly ``size`` bytes from ``src`` in chunks of up to READ_CHUNK."""
    remaining = size
    while remaining:
        chunk = src.read(min(READ_CHUNK, remaining))
        if not chunk:
            raise OSError("The input file got shorter while it was being read.")
        remaining -= len(chunk)
        yield chunk
    if src.read(1):
        raise OSError("The input file got longer while it was being read.")


def _iter_stream(
    name: bytes, size: int, src: BinaryIO, password: str | None
) -> Iterator[bytes]:
    """Yield the unpadded PixelCrypt stream for ``size`` bytes read from ``src``."""
    prefix = _FN_LEN.pack(len(name)) + name + _DATA_LEN.pack(size)
    payload_len = len(prefix) + size

    if not password:
        yield _FIXED_HEADER.pack(MAGIC, FORMAT_VERSION, 0, payload_len)
        yield prefix
        yield from _read_exactly(src, size)
        return

    salt = get_random_bytes(SALT_SIZE)
    nonce = get_random_bytes(NONCE_SIZE)
    header = _FIXED_HEADER.pack(MAGIC, FORMAT_VERSION, FLAG_ENCRYPTED, payload_len + TAG_SIZE)
    header += _CRYPTO_HEADER.pack(salt, nonce, KDF_ITERATIONS)

    cipher = AES.new(derive_key(password, salt), AES.MODE_GCM, nonce=nonce)
    cipher.update(header)
    yield header
    yield cipher.encrypt(prefix)
    for chunk in _read_exactly(src, size):
        yield cipher.encrypt(chunk)
    yield cipher.digest()


def build_stream(filename: str, data: bytes, password: str | None = None) -> bytes:
    """Return the unpadded PixelCrypt byte stream for a file held in memory."""
    name = _encode_name(filename)
    return b"".join(_iter_stream(name, len(data), io.BytesIO(data), password))


# ---------------------------------------------------------------------------
# Parsing the byte stream
# ---------------------------------------------------------------------------

class _ChunkReader:
    """Read exact byte counts from an iterator of byte chunks of any size."""

    def __init__(self, chunks: Iterable[bytes]):
        self._chunks = iter(chunks)
        self._buf = memoryview(b"")

    def iter_exact(self, n: int, truncated_message: str) -> Iterator[memoryview]:
        """Yield views that together hold exactly the next ``n`` bytes."""
        while n:
            if not self._buf:
                for chunk in self._chunks:
                    if len(chunk):
                        self._buf = memoryview(chunk)
                        break
                else:
                    raise NotPixelCryptError(truncated_message)
            piece = self._buf[:n]
            self._buf = self._buf[len(piece) :]
            n -= len(piece)
            yield piece

    def read_exact(self, n: int, truncated_message: str) -> bytes:
        return b"".join(self.iter_exact(n, truncated_message))


def _decode_stream(
    chunks: Iterable[bytes],
    password: str | None,
    open_sink: Callable[[], BinaryIO],
    progress: ProgressCallback | None = None,
) -> str:
    """Parse a (possibly padded) PixelCrypt stream arriving in chunks.

    DATA is written to the file object returned by ``open_sink()``, which is
    only called once the header and the payload lengths have been checked.
    Returns the stored filename. When encrypted, the GCM tag has been verified
    before this returns; if it doesn't verify, the sink holds unverified bytes
    and the caller must throw them away.
    """
    reader = _ChunkReader(chunks)
    not_pixelcrypt = "This video does not contain PixelCrypt data."
    header = reader.read_exact(_FIXED_HEADER.size, not_pixelcrypt)
    magic, version, flags, body_len = _FIXED_HEADER.unpack(header)
    if magic != MAGIC:
        raise NotPixelCryptError(not_pixelcrypt)
    if version != FORMAT_VERSION:
        raise NotPixelCryptError(f"Unsupported PixelCrypt format version {version}.")

    encrypted = bool(flags & FLAG_ENCRYPTED)
    cipher = None
    if encrypted:
        if not password:
            raise PasswordRequiredError(
                "This video is encrypted. Please provide the decryption password."
            )
        header += reader.read_exact(
            _CRYPTO_HEADER.size, "The video is truncated: encryption header is incomplete."
        )
        salt, nonce, iterations = _CRYPTO_HEADER.unpack_from(header, _FIXED_HEADER.size)
        if body_len < TAG_SIZE:
            raise NotPixelCryptError("The video is truncated: encrypted data is incomplete.")
        if iterations < 1:
            raise NotPixelCryptError("The encryption header inside the video is damaged.")
        payload_len = body_len - TAG_SIZE
        cipher = AES.new(derive_key(password, salt, iterations), AES.MODE_GCM, nonce=nonce)
        cipher.update(header)
        bad_payload: PixelCryptError = WrongPasswordError(
            "Incorrect password, or the video has been modified."
        )
    else:
        payload_len = body_len
        bad_payload = NotPixelCryptError("The file header inside the video is damaged.")

    def plain(data: bytes | memoryview) -> bytes | memoryview:
        return data

    transform = cipher.decrypt if cipher is not None else plain
    truncated = "The video is truncated: file data is incomplete."

    # FN_LEN, FILENAME and DATA_LEN must add up to the payload length exactly.
    # With a wrong password they decrypt to noise, so this fails after at most
    # 2 + 65535 + 8 bytes, before anything is written.
    if payload_len < _FN_LEN.size + _DATA_LEN.size:
        raise bad_payload
    (fn_len,) = _FN_LEN.unpack(transform(reader.read_exact(_FN_LEN.size, truncated)))
    if _FN_LEN.size + fn_len + _DATA_LEN.size > payload_len:
        raise bad_payload
    raw_name = bytes(transform(reader.read_exact(fn_len, truncated)))
    (data_len,) = _DATA_LEN.unpack(transform(reader.read_exact(_DATA_LEN.size, truncated)))
    if _FN_LEN.size + fn_len + _DATA_LEN.size + data_len != payload_len:
        raise bad_payload

    total = len(header) + body_len
    done = total - data_len - (TAG_SIZE if encrypted else 0)
    reported = -1

    def report(value: int) -> None:
        nonlocal reported
        if progress is not None and value != reported:
            reported = value
            progress(value, total)

    sink = open_sink()
    report(done)
    for piece in reader.iter_exact(data_len, truncated):
        sink.write(transform(piece))
        done += len(piece)
        report(done)

    if cipher is not None:
        tag = reader.read_exact(TAG_SIZE, "The video is truncated: encrypted data is incomplete.")
        try:
            cipher.verify(tag)
        except ValueError as exc:
            raise WrongPasswordError(
                "Incorrect password, or the video has been modified."
            ) from exc

    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotPixelCryptError("The file header inside the video is damaged.") from exc
    report(total)
    return name


def parse_stream(stream: bytes, password: str | None = None) -> tuple[str, bytes]:
    """Parse a (possibly padded) PixelCrypt byte stream into (filename, data)."""
    sink = io.BytesIO()
    name = _decode_stream([stream], password, lambda: sink)
    return name, sink.getvalue()


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


class _FramePacker:
    """Collect stream bytes into frame-sized buffers.

    The same buffer is yielded for every frame, so each one must be used
    before the generator is advanced.
    """

    def __init__(self, frame_bytes: int):
        self._buf = np.empty(frame_bytes, dtype=np.uint8)
        self._fill = 0

    def feed(self, data: bytes) -> Iterator[np.ndarray]:
        """Add bytes and yield the buffer every time it is full."""
        if not data:
            return
        view = np.frombuffer(data, dtype=np.uint8)
        size = len(self._buf)
        while len(view):
            take = min(size - self._fill, len(view))
            self._buf[self._fill : self._fill + take] = view[:take]
            self._fill += take
            view = view[take:]
            if self._fill == size:
                self._fill = 0
                yield self._buf

    def finish(self) -> Iterator[np.ndarray]:
        """Zero-pad and yield the last, partly filled frame, if there is one."""
        if self._fill:
            self._buf[self._fill :] = 0
            self._fill = 0
            yield self._buf


# ---------------------------------------------------------------------------
# Temporary files
# ---------------------------------------------------------------------------

def _create_temp_file(directory: str) -> tuple[str, BinaryIO]:
    """Create a new, empty ``.part`` file in ``directory`` and open it for writing."""
    while True:
        path = os.path.join(directory, f"{TEMP_PREFIX}{secrets.token_hex(8)}{TEMP_SUFFIX}")
        try:
            return path, open(path, "xb")
        except FileExistsError:
            continue


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _free_path(path: str) -> str:
    """Return ``path``, or ``name (1).ext``, ``name (2).ext``... if it exists."""
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 1
    while os.path.lexists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


def _safe_filename(name: str) -> str:
    """Strip any directory parts so a crafted video can't write outside output_dir."""
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return "restored_file"
    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _write_video(
    path: str,
    chunks: Iterable[bytes],
    width: int,
    height: int,
    fps: int,
    total: int,
    progress: ProgressCallback | None,
) -> int:
    """Pack a byte stream into FFV1 frames in a new .mkv at ``path``."""
    frame_bytes = width * height * BYTES_PER_PIXEL
    packer = _FramePacker(frame_bytes)
    frames = 0
    container = av.open(path, mode="w", format="matroska")
    try:
        stream = container.add_stream(CODEC, rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = PIX_FMT

        for chunk in chunks:
            for buf in packer.feed(chunk):
                for packet in stream.encode(_bytes_to_frame(buf, width, height)):
                    container.mux(packet)
                frames += 1
                done = frames * frame_bytes
                if progress is not None and done < total:
                    progress(done, total)
        for buf in packer.finish():
            for packet in stream.encode(_bytes_to_frame(buf, width, height)):
                container.mux(packet)
            frames += 1
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return frames


def encode_file_to_video(
    input_path: str,
    output_video_path: str,
    password: str | None = None,
    width: int = 1080,
    height: int = 1080,
    fps: int = 30,
    progress: ProgressCallback | None = None,
) -> int:
    """Encrypt a file and store its bytes as a lossless FFV1 .mkv video.

    The file is read and written one frame at a time. ``output_video_path`` is
    only created (or replaced) once the video is complete. ``progress``, if
    given, is called as ``progress(done_bytes, total_bytes)``.

    Returns the number of frames written.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Width and height must be positive.")
    if fps <= 0:
        raise ValueError("FPS must be positive.")

    with open(input_path, "rb") as src:
        size = os.fstat(src.fileno()).st_size
        name = _encode_name(os.path.basename(input_path))
        total = _stream_size(len(name), size, encrypted=bool(password))

        out_dir = os.path.dirname(os.path.abspath(output_video_path))
        tmp_path, tmp_file = _create_temp_file(out_dir)
        tmp_file.close()
        try:
            frames = _write_video(
                tmp_path, _iter_stream(name, size, src, password),
                width, height, fps, total, progress,
            )
            if progress is not None:
                progress(total, total)
            os.replace(tmp_path, output_video_path)
        except BaseException:
            _remove_quietly(tmp_path)
            raise
    return frames


def _iter_video_bytes(container: av.container.InputContainer) -> Iterator[bytes]:
    """Yield the data bytes of each frame of the first video stream, in order."""
    frames = container.decode(video=0)
    while True:
        try:
            frame = next(frames)
        except StopIteration:
            return
        except av.FFmpegError as exc:
            raise NotPixelCryptError(f"The video is damaged: {exc}") from exc
        yield _frame_to_bytes(frame)


def decode_video_to_file(
    video_path: str,
    output_dir: str,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    overwrite: bool = True,
) -> str:
    """Read a PixelCrypt video and restore the original file into output_dir.

    The video is read one frame at a time. The restored file only appears
    under its real name once it is complete and, if encrypted, verified. With
    ``overwrite=False`` an existing file is kept and the restored one gets a
    free name such as ``report (1).pdf``. ``progress``, if given, is called as
    ``progress(done_bytes, total_bytes)``.

    Returns the path of the restored file.
    """
    try:
        container = av.open(video_path, mode="r")
    except av.FFmpegError as exc:
        raise NotPixelCryptError(f"Could not open the video: {exc}") from exc

    tmp_path: str | None = None
    sink: BinaryIO | None = None

    def open_sink() -> BinaryIO:
        nonlocal tmp_path, sink
        os.makedirs(output_dir, exist_ok=True)
        tmp_path, sink = _create_temp_file(output_dir)
        return sink

    try:
        try:
            if not container.streams.video:
                raise NotPixelCryptError("This file has no video stream.")
            name = _decode_stream(_iter_video_bytes(container), password, open_sink, progress)
        finally:
            container.close()

        assert sink is not None and tmp_path is not None
        sink.close()
        out_path = os.path.join(output_dir, _safe_filename(name))
        if not overwrite:
            out_path = _free_path(out_path)
        os.replace(tmp_path, out_path)
        tmp_path = None
        return out_path
    finally:
        if sink is not None:
            sink.close()
        if tmp_path is not None:
            _remove_quietly(tmp_path)
