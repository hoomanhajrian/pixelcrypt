import hashlib
import os
import sys
import tracemalloc

import av
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
from engine import (  # noqa: E402
    NotPixelCryptError,
    PasswordRequiredError,
    WrongPasswordError,
    build_stream,
    decode_video_to_file,
    encode_file_to_video,
    parse_stream,
)

# Small frames keep the tests fast while still exercising multi-frame videos.
W, H = 64, 48
FRAME_BYTES = W * H * 3
PASSWORD = "correct horse battery staple"


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _round_trip(tmp_path, data, name="input.bin", password=None, width=W, height=H):
    src = _write(tmp_path / name, data)
    video = str(tmp_path / "out.mkv")
    frames = encode_file_to_video(src, video, password=password, width=width, height=height)
    out = decode_video_to_file(video, str(tmp_path / "restored"), password=password)
    return frames, out, video


def _random_bytes(n, seed=0):
    return np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()


CASES = {
    "empty": b"",
    "one_byte": b"\x00",
    "tiny_text": b"hello, pixelcrypt!\n",
    "all_byte_values": bytes(range(256)) * 4,
    "multi_frame": _random_bytes(FRAME_BYTES * 3 + 1234, seed=1),
    "exact_frame_boundary": _random_bytes(FRAME_BYTES * 2 - 100, seed=2),
    "all_zeros_multi_frame": b"\x00" * (FRAME_BYTES * 2 + 7),
    "all_ff_multi_frame": b"\xff" * (FRAME_BYTES * 2 + 7),
}


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
@pytest.mark.parametrize("case", list(CASES))
def test_round_trip_is_byte_exact(tmp_path, case, password):
    data = CASES[case]
    frames, out, _ = _round_trip(tmp_path, data, password=password)
    assert os.path.basename(out) == "input.bin"
    assert _read(out) == data
    assert frames >= 1


def test_multi_frame_file_spans_multiple_frames(tmp_path):
    data = CASES["multi_frame"]
    frames, out, video = _round_trip(tmp_path, data, password=PASSWORD)
    assert frames >= 4
    with av.open(video) as c:
        assert sum(1 for _ in c.decode(video=0)) == frames
    assert _read(out) == data


def test_default_1080p_frame_size(tmp_path):
    data = _random_bytes(4 * 1024 * 1024, seed=3)  # 4 MiB -> 2 frames at 1080x1080
    src = _write(tmp_path / "big.bin", data)
    video = str(tmp_path / "big.mkv")
    frames = encode_file_to_video(src, video, password=PASSWORD)
    assert frames == 2
    out = decode_video_to_file(video, str(tmp_path / "restored"), password=PASSWORD)
    assert _read(out) == data


def test_odd_frame_dimensions(tmp_path):
    data = _random_bytes(5000, seed=4)
    _, out, _ = _round_trip(tmp_path, data, password=PASSWORD, width=17, height=11)
    assert _read(out) == data


def test_video_is_ffv1_matroska(tmp_path):
    _, _, video = _round_trip(tmp_path, b"abc")
    with av.open(video) as c:
        assert "matroska" in c.format.name
        assert c.streams.video[0].codec_context.name == "ffv1"


def test_unicode_filename_is_preserved(tmp_path):
    data = b"unicode name"
    _, out, _ = _round_trip(tmp_path, data, name="résumé 📄.txt", password=PASSWORD)
    assert os.path.basename(out) == "résumé 📄.txt"
    assert _read(out) == data


def test_wrong_password_fails(tmp_path):
    src = _write(tmp_path / "secret.txt", b"top secret")
    video = str(tmp_path / "out.mkv")
    encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    with pytest.raises(WrongPasswordError):
        decode_video_to_file(video, str(tmp_path / "restored"), password="wrong")
    assert not (tmp_path / "restored" / "secret.txt").exists()


def test_missing_password_fails(tmp_path):
    src = _write(tmp_path / "secret.txt", b"top secret")
    video = str(tmp_path / "out.mkv")
    encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    with pytest.raises(PasswordRequiredError):
        decode_video_to_file(video, str(tmp_path / "restored"))


def test_password_on_plain_video_is_ignored(tmp_path):
    src = _write(tmp_path / "plain.txt", b"not secret")
    video = str(tmp_path / "out.mkv")
    encode_file_to_video(src, video, width=W, height=H)
    out = decode_video_to_file(video, str(tmp_path / "restored"), password="anything")
    assert _read(out) == b"not secret"


def test_encrypted_video_does_not_leak_plaintext(tmp_path):
    secret = b"VERY-SECRET-MARKER-1234567890" * 10
    src = _write(tmp_path / "s.bin", secret)
    video = str(tmp_path / "out.mkv")
    encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    with av.open(video) as c:
        raw = b"".join(engine._frame_to_bytes(f) for f in c.decode(video=0))
    assert b"VERY-SECRET-MARKER" not in raw
    assert b"s.bin" not in raw


def test_same_input_encrypts_differently_each_time():
    a = build_stream("f", b"same data", PASSWORD)
    b = build_stream("f", b"same data", PASSWORD)
    assert a != b
    assert parse_stream(a, PASSWORD) == parse_stream(b, PASSWORD) == ("f", b"same data")


def test_tampered_ciphertext_is_rejected():
    stream = bytearray(build_stream("f", b"payload", PASSWORD))
    stream[-20] ^= 0x01
    with pytest.raises(WrongPasswordError):
        parse_stream(bytes(stream), PASSWORD)


def test_tampered_header_is_rejected():
    stream = bytearray(build_stream("f", b"payload", PASSWORD))
    stream[20] ^= 0x01  # inside the salt, which is authenticated data
    with pytest.raises(WrongPasswordError):
        parse_stream(bytes(stream), PASSWORD)


def test_padding_after_body_is_ignored():
    stream = build_stream("f", b"payload", PASSWORD) + b"\x00" * 500
    assert parse_stream(stream, PASSWORD) == ("f", b"payload")


def test_non_pixelcrypt_video_is_rejected(tmp_path):
    video = str(tmp_path / "random.mkv")
    with av.open(video, "w") as c:
        s = c.add_stream("ffv1", rate=30)
        s.width, s.height, s.pix_fmt = W, H, "gray"
        frame = av.VideoFrame.from_ndarray(np.full((H, W), 7, dtype=np.uint8), format="gray")
        for p in s.encode(frame):
            c.mux(p)
        for p in s.encode():
            c.mux(p)
    with pytest.raises(NotPixelCryptError):
        decode_video_to_file(video, str(tmp_path / "restored"))


def test_non_video_file_is_rejected(tmp_path):
    bogus = _write(tmp_path / "notavideo.mkv", b"this is not a video")
    with pytest.raises(NotPixelCryptError):
        decode_video_to_file(bogus, str(tmp_path / "restored"))


def test_filename_cannot_escape_output_dir(tmp_path):
    for evil in ("../../evil.txt", "..\\..\\evil.txt", "/etc/evil.txt"):
        stream = build_stream(evil, b"x")
        name, _ = parse_stream(stream)
        assert engine._safe_filename(name) == "evil.txt"
    assert engine._safe_filename("..") == "restored_file"


@pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (-1, 10)])
def test_invalid_dimensions_rejected(tmp_path, w, h):
    src = _write(tmp_path / "f", b"x")
    with pytest.raises(ValueError):
        encode_file_to_video(src, str(tmp_path / "o.mkv"), width=w, height=h)


# ---------------------------------------------------------------------------
# Format stability: videos written by the original in-memory engine (v1)
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GOLDEN_DATA = bytes(range(256)) * 6  # 1536 bytes, 3 frames at 16x16
GOLDEN_PASSWORD = "pixelcrypt-golden"
GOLDEN_TEXT = b"PixelCrypt golden stream\n"
# build_stream("golden.txt", GOLDEN_TEXT[, GOLDEN_PASSWORD]) from the original
# engine, with get_random_bytes(n) returning bytes(range(n)).
GOLDEN_PLAIN_HEX = (
    "50584352595054310100000000000000002d000a676f6c64656e2e747874000000000000"
    "0019506978656c437279707420676f6c64656e2073747265616d0a"
)
GOLDEN_ENCRYPTED_HEX = (
    "50584352595054310101000000000000003d000102030405060708090a0b0c0d0e0f0001"
    "02030405060708090a0b000927c0a0fc00db9cb03820f38827a3fa294855c2fed894473d"
    "ab25e01d079fa7a5d219562191d4d37edc1d3b4c89365eab89b369e1b26a3ceb8ab48c9f"
    "a48605"
)


@pytest.mark.parametrize(
    "video,name,password",
    [
        ("v1_plain.mkv", "golden-plain.bin", None),
        ("v1_encrypted.mkv", "golden-secret.bin", GOLDEN_PASSWORD),
    ],
    ids=["plain", "encrypted"],
)
def test_v1_fixture_still_decodes(tmp_path, video, name, password):
    out = decode_video_to_file(os.path.join(FIXTURES, video), str(tmp_path), password=password)
    assert os.path.basename(out) == name
    assert _read(out) == GOLDEN_DATA


def test_stream_layout_matches_v1_golden(monkeypatch):
    monkeypatch.setattr(engine, "get_random_bytes", lambda n: bytes(range(n)))
    assert build_stream("golden.txt", GOLDEN_TEXT).hex() == GOLDEN_PLAIN_HEX
    assert build_stream("golden.txt", GOLDEN_TEXT, GOLDEN_PASSWORD).hex() == GOLDEN_ENCRYPTED_HEX


def test_video_frames_hold_stream_then_zero_padding(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "get_random_bytes", lambda n: bytes(range(n)))
    data = _random_bytes(FRAME_BYTES * 2 + 77, seed=5)
    src = _write(tmp_path / "f.bin", data)
    video = str(tmp_path / "f.mkv")
    frames = encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    with av.open(video) as c:
        raw = b"".join(engine._frame_to_bytes(f) for f in c.decode(video=0))
    expected = build_stream("f.bin", data, PASSWORD)
    assert len(raw) == frames * FRAME_BYTES
    assert raw[: len(expected)] == expected
    assert raw[len(expected) :] == bytes(len(raw) - len(expected))


# ---------------------------------------------------------------------------
# Streaming: progress, aborting, temporary files, memory
# ---------------------------------------------------------------------------

class _Stop(Exception):
    pass


def _stop_after(n):
    calls = []

    def progress(done, total):
        calls.append(done)
        if len(calls) >= n:
            raise _Stop

    return progress


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_progress_is_monotonic_and_completes(tmp_path, password):
    data = CASES["multi_frame"]
    src = _write(tmp_path / "p.bin", data)
    video = str(tmp_path / "p.mkv")
    enc, dec = [], []
    frames = encode_file_to_video(
        src, video, password=password, width=W, height=H,
        progress=lambda d, t: enc.append((d, t)),
    )
    out = decode_video_to_file(
        video, str(tmp_path / "restored"), password=password,
        progress=lambda d, t: dec.append((d, t)),
    )
    assert _read(out) == data
    assert len(enc) >= frames - 1
    for calls in (enc, dec):
        done = [d for d, _ in calls]
        assert len({t for _, t in calls}) == 1
        assert done == sorted(done)
        assert done[-1] == calls[-1][1]


def test_aborted_encode_leaves_no_files(tmp_path):
    src = _write(tmp_path / "in.bin", CASES["multi_frame"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(_Stop):
        encode_file_to_video(
            src, str(out_dir / "v.mkv"), password=PASSWORD, width=W, height=H,
            progress=_stop_after(2),
        )
    assert list(out_dir.iterdir()) == []


def test_aborted_encode_keeps_existing_output(tmp_path):
    src = _write(tmp_path / "in.bin", CASES["multi_frame"])
    video = tmp_path / "v.mkv"
    video.write_bytes(b"previous video")
    with pytest.raises(_Stop):
        encode_file_to_video(src, str(video), width=W, height=H, progress=_stop_after(1))
    assert video.read_bytes() == b"previous video"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.bin", "v.mkv"]


def test_aborted_decode_leaves_no_files(tmp_path):
    src = _write(tmp_path / "in.bin", CASES["multi_frame"])
    video = str(tmp_path / "v.mkv")
    encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    restored = tmp_path / "restored"
    with pytest.raises(_Stop):
        decode_video_to_file(video, str(restored), password=PASSWORD, progress=_stop_after(2))
    assert list(restored.iterdir()) == []


def test_wrong_password_creates_no_files(tmp_path):
    src = _write(tmp_path / "secret.bin", CASES["multi_frame"])
    video = str(tmp_path / "v.mkv")
    encode_file_to_video(src, video, password=PASSWORD, width=W, height=H)
    restored = tmp_path / "restored"
    with pytest.raises(WrongPasswordError):
        decode_video_to_file(video, str(restored), password="wrong")
    assert not restored.exists()


@pytest.mark.parametrize("wrong", ["wrong", "Wrong!", "correct horse"])
def test_wrong_password_fails_within_the_payload_prefix(wrong):
    # With a wrong password FN_LEN decrypts to noise (up to 65535), so the
    # decoder may read that many "filename" bytes, but no more: DATA_LEN then
    # fails the length check. That is within the first frame at the default size.
    stream = build_stream("f.bin", _random_bytes(200_000, seed=6), PASSWORD)
    limit = 18 + 32 + 2 + 0xFFFF + 8  # headers + the largest possible prefix

    def chunks():
        yield stream[:limit]
        raise AssertionError("the decoder read past the payload prefix")

    def open_sink():
        raise AssertionError("the output was opened before the password was checked")

    with pytest.raises(WrongPasswordError):
        engine._decode_stream(chunks(), wrong, open_sink)


def _copy_frames(src_video, dst_video, keep):
    """Write the first ``keep`` frames of a PixelCrypt video to a new video."""
    with av.open(src_video) as c:
        stream = c.streams.video[0]
        width, height = stream.width, stream.height
        chunks = [engine._frame_to_bytes(f) for f in c.decode(video=0)][:keep]
    with av.open(dst_video, "w", format="matroska") as out:
        s = out.add_stream(engine.CODEC, rate=30)
        s.width, s.height, s.pix_fmt = width, height, engine.PIX_FMT
        for chunk in chunks:
            frame = engine._bytes_to_frame(np.frombuffer(chunk, dtype=np.uint8), width, height)
            for p in s.encode(frame):
                out.mux(p)
        for p in s.encode():
            out.mux(p)


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_truncated_video_is_rejected_and_leaves_no_files(tmp_path, password):
    src = _write(tmp_path / "in.bin", CASES["multi_frame"])
    video = str(tmp_path / "v.mkv")
    frames = encode_file_to_video(src, video, password=password, width=W, height=H)
    truncated = str(tmp_path / "cut.mkv")
    _copy_frames(video, truncated, keep=frames - 1)
    restored = tmp_path / "restored"
    with pytest.raises(NotPixelCryptError):
        decode_video_to_file(truncated, str(restored), password=password)
    assert list(restored.iterdir()) == []


def test_decode_without_overwrite_picks_a_free_name(tmp_path):
    src = _write(tmp_path / "doc.txt", b"new contents")
    video = str(tmp_path / "doc.mkv")
    encode_file_to_video(src, video, width=W, height=H)
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "doc.txt").write_bytes(b"old contents")
    (restored / "doc (1).txt").write_bytes(b"older contents")

    out = decode_video_to_file(video, str(restored), overwrite=False)
    assert os.path.basename(out) == "doc (2).txt"
    assert _read(out) == b"new contents"
    assert (restored / "doc.txt").read_bytes() == b"old contents"

    out = decode_video_to_file(video, str(restored))  # default: overwrite
    assert out == str(restored / "doc.txt")
    assert _read(out) == b"new contents"


def test_memory_does_not_grow_with_file_size(tmp_path):
    size = 48 * 1024 * 1024  # about 15 frames at 1080x1080
    src = tmp_path / "big.bin"
    rng = np.random.default_rng(7)
    with open(src, "wb") as f:
        for _ in range(size // (1 << 20)):
            f.write(rng.integers(0, 256, 1 << 20, dtype=np.uint8).tobytes())
    video = str(tmp_path / "big.mkv")

    tracemalloc.start()
    try:
        encode_file_to_video(str(src), video, password=PASSWORD)
        _, encode_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        out = decode_video_to_file(video, str(tmp_path / "restored"), password=PASSWORD)
        _, decode_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert _sha256(out) == _sha256(src)
    # Holding the whole file would need at least `size`; one frame is ~3.3 MiB.
    assert encode_peak < size // 2, f"encode peaked at {encode_peak / 2**20:.1f} MiB"
    assert decode_peak < size // 2, f"decode peaked at {decode_peak / 2**20:.1f} MiB"
