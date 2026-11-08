import os
import sys

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
