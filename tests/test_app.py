"""Tests for the tkinter app.

Tests that need a window are skipped when there's no display. On Linux, run
them headless with ``xvfb-run -a python -m pytest``.
"""

import os
import sys
import time

import pytest

tk = pytest.importorskip("tkinter")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import engine  # noqa: E402

PASSWORD = "correct horse battery staple"


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def gui(root):
    """The app with its dialogs replaced by a recorder. ask_yes_no answers yes."""
    a = app.PixelCryptApp(root, dnd=False)
    a.dialogs = []
    a.answer = True

    def ask(title, message):
        a.dialogs.append(("ask", title, message))
        return a.answer

    a.show_error = lambda title, message: a.dialogs.append(("error", title, message))
    a.show_info = lambda title, message: a.dialogs.append(("info", title, message))
    a.ask_yes_no = ask
    return a


def wait_until_idle(gui, timeout=120):
    deadline = time.monotonic() + timeout
    while gui.busy:
        if time.monotonic() > deadline:
            raise AssertionError("the job did not finish")
        gui.root.update()
        time.sleep(0.01)
    gui.root.update()


def _write(path, data):
    path.write_bytes(data)
    return str(path)


def test_round_trip_through_the_gui(tmp_path, gui):
    data = os.urandom(150_000)
    src = _write(tmp_path / "secret.pdf", data)

    gui.set_encode_input(src)
    assert gui.encode_output.get() == src + ".mkv"
    gui.encode_password.set(PASSWORD)
    gui.encode_confirm.set(PASSWORD)
    assert gui.start_encode()
    assert gui.busy
    assert str(gui.encode_button.cget("state")) == "disabled"
    wait_until_idle(gui)
    assert gui.dialogs[-1][:2] == ("info", "Video saved")
    assert os.path.isfile(src + ".mkv")
    assert gui.progress["value"] == 100

    gui.set_decode_input(src + ".mkv")
    assert gui.decode_output.get() == str(tmp_path)
    gui.decode_output.set(str(tmp_path / "restored"))
    gui.decode_password.set(PASSWORD)
    assert gui.start_decode()
    wait_until_idle(gui)
    assert gui.dialogs[-1][:2] == ("info", "File restored")
    assert (tmp_path / "restored" / "secret.pdf").read_bytes() == data
    assert str(gui.encode_button.cget("state")) == "normal"


def test_decode_never_replaces_an_existing_file(tmp_path, gui):
    src = _write(tmp_path / "notes.txt", b"from the video")
    video = str(tmp_path / "notes.mkv")
    engine.encode_file_to_video(src, video, width=64, height=64)
    (tmp_path / "notes.txt").write_bytes(b"newer local copy")

    gui.set_decode_input(video)  # restores into the same folder
    assert gui.start_decode()
    wait_until_idle(gui)
    assert (tmp_path / "notes.txt").read_bytes() == b"newer local copy"
    assert (tmp_path / "notes (1).txt").read_bytes() == b"from the video"


def test_wrong_password_reports_error_and_writes_nothing(tmp_path, gui):
    src = _write(tmp_path / "s.bin", b"top secret")
    video = str(tmp_path / "s.mkv")
    engine.encode_file_to_video(src, video, password=PASSWORD, width=64, height=64)

    gui.set_decode_input(video)
    gui.decode_output.set(str(tmp_path / "restored"))
    gui.decode_password.set("not the password")
    assert gui.start_decode()
    wait_until_idle(gui)
    assert gui.dialogs[-1][:2] == ("error", "Wrong password")
    assert not (tmp_path / "restored").exists()
    assert "failed" in gui.status.get()


def test_missing_password_asks_for_one(tmp_path, gui):
    src = _write(tmp_path / "s.bin", b"top secret")
    video = str(tmp_path / "s.mkv")
    engine.encode_file_to_video(src, video, password=PASSWORD, width=64, height=64)

    gui.set_decode_input(video)
    assert gui.start_decode()
    wait_until_idle(gui)
    assert gui.dialogs[-1][:2] == ("error", "Password needed")


def test_mismatched_passwords_start_no_job(tmp_path, gui):
    gui.set_encode_input(_write(tmp_path / "a.txt", b"x"))
    gui.encode_password.set("one")
    gui.encode_confirm.set("two")
    assert not gui.start_encode()
    assert not gui.busy
    assert gui.dialogs == [("error", "Passwords don't match", "Type the same password in both fields.")]
    assert not os.path.exists(str(tmp_path / "a.txt.mkv"))


def test_empty_password_needs_confirmation(tmp_path, gui):
    src = _write(tmp_path / "a.txt", b"not secret")
    gui.set_encode_input(src)

    gui.answer = False
    assert not gui.start_encode()
    assert gui.dialogs[-1][:2] == ("ask", "Save without a password?")

    gui.answer = True
    assert gui.start_encode()
    wait_until_idle(gui)
    out = engine.decode_video_to_file(src + ".mkv", str(tmp_path / "restored"))
    assert open(out, "rb").read() == b"not secret"


def test_existing_video_needs_confirmation(tmp_path, gui):
    src = _write(tmp_path / "a.txt", b"data")
    (tmp_path / "a.txt.mkv").write_bytes(b"older video")
    gui.set_encode_input(src)
    gui.encode_password.set(PASSWORD)
    gui.encode_confirm.set(PASSWORD)

    gui.answer = False
    assert not gui.start_encode()
    assert gui.dialogs[-1][:2] == ("ask", "Replace the video?")
    assert (tmp_path / "a.txt.mkv").read_bytes() == b"older video"


def test_missing_input_is_reported(tmp_path, gui):
    assert not gui.start_encode()
    assert gui.dialogs[-1][:2] == ("error", "No file chosen")
    gui.set_decode_input(str(tmp_path / "nope.mkv"))
    assert not gui.start_decode()
    assert gui.dialogs[-1][:2] == ("error", "Video not found")


def test_cancel_stops_the_job_and_saves_nothing(tmp_path, gui):
    src = _write(tmp_path / "big.bin", os.urandom(8 * 1024 * 1024))
    gui.set_encode_input(src)
    gui.encode_password.set(PASSWORD)
    gui.encode_confirm.set(PASSWORD)
    assert gui.start_encode()
    gui.cancel()
    wait_until_idle(gui)
    assert gui.status.get() == "Cancelled. Nothing was saved."
    assert sorted(os.listdir(tmp_path)) == ["big.bin"]
    assert not any(kind == "error" for kind, *_ in gui.dialogs)


def test_show_password_toggles_the_mask(gui):
    entries = (gui.encode_password_entry, gui.encode_confirm_entry)
    assert all(e.cget("show") == app.MASK for e in entries)
    gui.encode_show_check.invoke()
    assert all(e.cget("show") == "" for e in entries)
    gui.encode_show_check.invoke()
    assert all(e.cget("show") == app.MASK for e in entries)

    assert gui.decode_password_entry.cget("show") == app.MASK
    gui.decode_show_check.invoke()
    assert gui.decode_password_entry.cget("show") == ""


def test_typed_output_is_not_overwritten_by_suggestions(gui):
    gui.encode_output.set("/somewhere/chosen.mkv")
    gui.set_encode_input("/tmp/file.txt")
    assert gui.encode_output.get() == "/somewhere/chosen.mkv"


# -- no display needed ------------------------------------------------------

@pytest.mark.parametrize(
    "exc,title",
    [
        (engine.WrongPasswordError("x"), "Wrong password"),
        (engine.PasswordRequiredError("x"), "Password needed"),
        (engine.NotPixelCryptError("The video is truncated."), "Not a PixelCrypt video"),
        (PermissionError(13, "Permission denied", "/root/x"), "Couldn't read or write a file"),
        (RuntimeError("boom"), "Something went wrong"),
    ],
)
def test_describe_error(exc, title):
    got_title, message = app.describe_error(exc)
    assert got_title == title
    assert message


def test_describe_error_messages():
    assert app.describe_error(PermissionError(13, "Permission denied", "/root/x"))[1] == (
        "Permission denied\n\n/root/x"
    )
    assert app.describe_error(engine.NotPixelCryptError("The video is truncated."))[1] == (
        "The video is truncated."
    )


@pytest.mark.parametrize(
    "size,text",
    [(0, "0 bytes"), (1, "1 byte"), (999, "999 bytes"), (1000, "1.0 KB"),
     (420_000_000, "420 MB"), (1_234_000_000, "1.2 GB"), (5 * 10**15, "5000 TB")],
)
def test_format_size(size, text):
    assert app.format_size(size) == text


def test_self_test_passes():
    assert app.self_test() == 0
