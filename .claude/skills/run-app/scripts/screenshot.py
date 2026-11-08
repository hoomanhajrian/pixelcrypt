"""Screenshot the PixelCrypt window on an X display (normally a virtual one).

Run it under Xvfb so nothing shows up on the user's screen:

    xvfb-run -a -s "-screen 0 1280x800x24" .venv/bin/python .claude/skills/run-app/scripts/screenshot.py

This drives app.py in-process and writes these files to build/screenshots/
(gitignored), or to the folder given with --out:

    encode.png    Encode tab filled in (passwords masked)
    decode.png    Decode tab filled in
    progress.png  a real encode job captured part-way, before it's cancelled

To screenshot the packaged app instead, pass --binary. It starts that
executable (with no arguments), waits, and grabs the whole screen into
launched.png:

    xvfb-run -a -s "-screen 0 1280x800x24" .venv/bin/python .claude/skills/run-app/scripts/screenshot.py --binary dist/PixelCrypt

Needs Pillow (in requirements-dev.txt) and a Python with tkinter.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
PASSWORD = "correct horse battery staple"


def grab(path: str, bbox: tuple[int, int, int, int] | None = None) -> None:
    from PIL import ImageGrab

    ImageGrab.grab(bbox=bbox, xdisplay=os.environ["DISPLAY"]).save(path)
    print(path)


def pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def grab_window(root, path: str, settle: float = 0.3) -> None:
    pump(root, settle)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    grab(path, (x, y, x + root.winfo_width(), y + root.winfo_height()))


def shoot_app(out_dir: str) -> None:
    sys.path.insert(0, REPO)
    import app

    root, dnd = app.create_root()
    gui = app.PixelCryptApp(root, dnd=dnd)
    # Dialogs are modal and would block this script, so print them instead.
    gui.show_error = lambda title, message: print(f"[error dialog] {title}: {message}")
    gui.show_info = lambda title, message: print(f"[info dialog] {title}: {message}")
    gui.ask_yes_no = lambda title, message: True
    root.geometry("+0+0")

    with tempfile.TemporaryDirectory(prefix="pixelcrypt-screenshot-") as tmp:
        sample = os.path.join(tmp, "quarterly-report.pdf")
        with open(sample, "wb") as f:
            for _ in range(48):
                f.write(os.urandom(1 << 20))

        gui.set_encode_input(sample)
        gui.encode_password.set(PASSWORD)
        gui.encode_confirm.set(PASSWORD)
        gui.notebook.select(gui.encode_tab)
        grab_window(root, os.path.join(out_dir, "encode.png"))

        gui.set_decode_input(sample + ".mkv")
        gui.decode_password.set(PASSWORD)
        gui.notebook.select(gui.decode_tab)
        grab_window(root, os.path.join(out_dir, "decode.png"))

        gui.notebook.select(gui.encode_tab)
        gui.start_encode()
        deadline = time.monotonic() + 120
        while gui.busy and gui.progress["value"] < 30 and time.monotonic() < deadline:
            pump(root, 0.02)
        # Grab at once: a 48 MiB job only takes a second or two.
        grab_window(root, os.path.join(out_dir, "progress.png"), settle=0.0)
        gui.cancel()
        while gui.busy:
            pump(root, 0.05)
    root.destroy()


def shoot_binary(out_dir: str, binary: str, wait: float) -> int:
    proc = subprocess.Popen([os.path.abspath(binary)])
    try:
        time.sleep(wait)
        if proc.poll() is not None:
            print(f"{binary} exited early with code {proc.returncode}", file=sys.stderr)
            return 1
        grab(os.path.join(out_dir, "launched.png"))
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot the PixelCrypt window.")
    parser.add_argument(
        "--out", default=os.path.join(REPO, "build", "screenshots"),
        help="folder for the PNG files (default: build/screenshots/)",
    )
    parser.add_argument(
        "--binary", metavar="PATH",
        help="start this executable (no arguments) instead of app.py and grab the whole screen",
    )
    parser.add_argument(
        "--wait", type=float, default=5.0,
        help="seconds to wait before grabbing with --binary (default: 5)",
    )
    args = parser.parse_args()

    if not os.environ.get("DISPLAY"):
        print("No X display. Run this under xvfb-run (see --help).", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    if args.binary:
        return shoot_binary(args.out, args.binary, args.wait)
    shoot_app(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
