"""PixelCrypt desktop app (tkinter).

Two tabs: Encode hides a file inside a lossless video, optionally encrypted
with a password, and Decode restores it.

Threading: encoding and decoding run on a worker thread. The worker never
touches a widget; it only puts messages on a queue. The main thread polls the
queue with ``after()``, and it alone updates widgets and opens dialogs,
because tkinter isn't thread-safe. Cancel works through the engine's progress
callback: once the cancel flag is set, the callback raises ``Cancelled``, and
the engine deletes its temporary file.

Usage::

    python app.py              # open the window
    python app.py --self-test  # round-trip a temp file, no window; exit code 0 = OK
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, ttk
from typing import Any

import engine

try:  # Drag-and-drop is optional: plain tkinter can't do it.
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

APP_NAME = "PixelCrypt"
POLL_MS = 50
MASK = "•"
HINT_COLOUR = "#5f6368"
ACCENT_COLOUR = "#2f6fdb"
VIDEO_TYPES = [("PixelCrypt video", "*.mkv"), ("All files", "*")]


class Cancelled(Exception):
    """Raised from the progress callback to stop a running job."""


def format_size(num_bytes: int) -> str:
    """Return a short human-readable size, such as ``420 MB`` or ``1.2 GB``."""
    if num_bytes < 1000:
        return "1 byte" if num_bytes == 1 else f"{num_bytes} bytes"
    value, unit = float(num_bytes), "bytes"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000:
            break
    return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"


def describe_error(exc: BaseException) -> tuple[str, str]:
    """Return the (title, message) to show for an error from a job."""
    if isinstance(exc, engine.WrongPasswordError):
        return (
            "Wrong password",
            "The password is incorrect, or the video was changed after it was made.",
        )
    if isinstance(exc, engine.PasswordRequiredError):
        return ("Password needed", "This video is encrypted. Enter its password and try again.")
    if isinstance(exc, engine.NotPixelCryptError):
        return ("Not a PixelCrypt video", str(exc))
    if isinstance(exc, OSError):
        message = exc.strerror or str(exc)
        if exc.filename:
            message += f"\n\n{exc.filename}"
        return ("Couldn't read or write a file", message)
    return ("Something went wrong", f"{type(exc).__name__}: {exc}")


def create_root() -> tuple[tk.Tk, bool]:
    """Create the main window, with drag-and-drop if tkdnd can be loaded."""
    root = tk.Tk()
    if TkinterDnD is None:
        return root, False
    try:
        TkinterDnD.require(root)
    except (RuntimeError, tk.TclError):
        return root, False
    return root, True


class PixelCryptApp(ttk.Frame):
    """The main window's contents: Encode and Decode tabs, progress and Cancel."""

    def __init__(self, root: tk.Tk, dnd: bool = False):
        super().__init__(root, padding=12)
        self.root = root
        self.dnd = dnd

        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._poll_id: str | None = None
        self._job = ""
        self._on_success: Callable[[Any], None] = lambda result: None
        self._quit_when_idle = False
        self._suggested_output = ""
        self._suggested_folder = ""
        self._confirmed_output = ""

        self.encode_input = tk.StringVar(master=root)
        self.encode_output = tk.StringVar(master=root)
        self.encode_password = tk.StringVar(master=root)
        self.encode_confirm = tk.StringVar(master=root)
        self.show_encode_password = tk.BooleanVar(master=root, value=False)
        self.decode_input = tk.StringVar(master=root)
        self.decode_output = tk.StringVar(master=root)
        self.decode_password = tk.StringVar(master=root)
        self.show_decode_password = tk.BooleanVar(master=root, value=False)
        self.status = tk.StringVar(
            master=root, value="Choose a file to encode, or a video to decode."
        )

        style = ttk.Style(root)
        if root.tk.call("tk", "windowingsystem") == "x11" and "clam" in style.theme_names():
            style.theme_use("clam")
            # clam's default bar is nearly the colour of its trough.
            style.configure(
                "Horizontal.TProgressbar",
                background=ACCENT_COLOUR,
                lightcolor=ACCENT_COLOUR,
                darkcolor=ACCENT_COLOUR,
            )

        root.title(APP_NAME)
        root.minsize(620, 0)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.grid(row=0, column=0, sticky="nsew")
        self._build()

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.encode_tab = ttk.Frame(self.notebook, padding=12)
        self.decode_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.encode_tab, text="Encode")
        self.notebook.add(self.decode_tab, text="Decode")
        self._build_encode_tab(self.encode_tab)
        self._build_decode_tab(self.decode_tab)

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        ttk.Label(self, textvariable=self.status).grid(row=2, column=0, sticky="w")
        self.cancel_button = ttk.Button(
            self, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.grid(row=2, column=1, sticky="e", padx=(8, 0))

    def _build_encode_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        self.encode_input_entry = self._path_row(
            tab, 0, "File to hide:", self.encode_input, "Browse…", self.browse_encode_input
        )
        self.encode_output_entry = self._path_row(
            tab, 1, "Save video as:", self.encode_output, "Save as…", self.browse_encode_output
        )
        self.encode_password_entry = self._password_row(tab, 2, "Password:", self.encode_password)
        self.encode_confirm_entry = self._password_row(
            tab, 3, "Confirm password:", self.encode_confirm
        )
        self.encode_show_check = ttk.Checkbutton(
            tab,
            text="Show password",
            variable=self.show_encode_password,
            command=lambda: self._apply_mask(
                self.show_encode_password, self.encode_password_entry, self.encode_confirm_entry
            ),
        )
        self.encode_show_check.grid(row=4, column=1, sticky="w", pady=(2, 8))
        self._hint(tab, 5, "Keep the password safe. Without it, the file can't be restored.")

        self.encode_button = ttk.Button(tab, text="Encode", command=self.start_encode)
        self.encode_button.grid(row=6, column=2, sticky="e", pady=(12, 0))
        if self.dnd:
            self._hint(tab, 6, "Tip: drop a file anywhere on this tab.", pady=(12, 0))
            self._accept_drops(tab, self.set_encode_input)
            self._accept_drops(self.encode_input_entry, self.set_encode_input)
            self._accept_drops(self.encode_output_entry, self.encode_output.set)
        for entry in (
            self.encode_input_entry,
            self.encode_output_entry,
            self.encode_password_entry,
            self.encode_confirm_entry,
        ):
            entry.bind("<Return>", lambda event: self.start_encode())

    def _build_decode_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        self.decode_input_entry = self._path_row(
            tab, 0, "PixelCrypt video:", self.decode_input, "Browse…", self.browse_decode_input
        )
        self.decode_output_entry = self._path_row(
            tab, 1, "Restore into:", self.decode_output, "Choose…", self.browse_decode_output
        )
        self.decode_password_entry = self._password_row(tab, 2, "Password:", self.decode_password)
        self.decode_show_check = ttk.Checkbutton(
            tab,
            text="Show password",
            variable=self.show_decode_password,
            command=lambda: self._apply_mask(self.show_decode_password, self.decode_password_entry),
        )
        self.decode_show_check.grid(row=3, column=1, sticky="w", pady=(2, 8))
        self._hint(
            tab, 4,
            "Leave the password empty if the video was saved without one. "
            "Existing files are never replaced.",
        )

        self.decode_button = ttk.Button(tab, text="Decode", command=self.start_decode)
        self.decode_button.grid(row=5, column=2, sticky="e", pady=(12, 0))
        if self.dnd:
            self._hint(tab, 5, "Tip: drop a video anywhere on this tab.", pady=(12, 0))
            self._accept_drops(tab, self.set_decode_input)
            self._accept_drops(self.decode_input_entry, self.set_decode_input)
            self._accept_drops(self.decode_output_entry, self.decode_output.set)
        for entry in (self.decode_input_entry, self.decode_output_entry, self.decode_password_entry):
            entry.bind("<Return>", lambda event: self.start_decode())

    def _path_row(
        self,
        tab: ttk.Frame,
        row: int,
        label: str,
        var: tk.StringVar,
        button_text: str,
        command: Callable[[], None],
    ) -> ttk.Entry:
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(tab, textvariable=var, width=50)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(tab, text=button_text, command=command).grid(
            row=row, column=2, sticky="ew", padx=(8, 0), pady=4
        )
        return entry

    def _password_row(self, tab: ttk.Frame, row: int, label: str, var: tk.StringVar) -> ttk.Entry:
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(tab, textvariable=var, show=MASK, width=50)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        return entry

    def _hint(self, tab: ttk.Frame, row: int, text: str, pady: Any = 0) -> None:
        ttk.Label(tab, text=text, foreground=HINT_COLOUR, wraplength=440).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=pady
        )

    @staticmethod
    def _apply_mask(show: tk.BooleanVar, *entries: ttk.Entry) -> None:
        for entry in entries:
            entry.configure(show="" if show.get() else MASK)

    def _accept_drops(self, widget: tk.Misc, setter: Callable[[str], None]) -> None:
        widget.drop_target_register(DND_FILES)

        def on_drop(event: Any) -> Any:
            paths = self.root.tk.splitlist(event.data)
            if paths:
                setter(paths[0])
            return event.action

        widget.dnd_bind("<<Drop>>", on_drop)

    # -- choosing files -----------------------------------------------------

    def set_encode_input(self, path: str) -> None:
        """Set the file to encode and, unless the user typed one, suggest an output."""
        self.encode_input.set(path)
        if self.encode_output.get() in ("", self._suggested_output):
            self._suggested_output = path + ".mkv"
            self.encode_output.set(self._suggested_output)

    def set_decode_input(self, path: str) -> None:
        """Set the video to decode and, unless the user chose one, suggest its folder."""
        self.decode_input.set(path)
        if self.decode_output.get() in ("", self._suggested_folder):
            self._suggested_folder = os.path.dirname(os.path.abspath(path))
            self.decode_output.set(self._suggested_folder)

    def browse_encode_input(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Choose a file to encode")
        if path:
            self.set_encode_input(path)

    def browse_encode_output(self) -> None:
        current = self.encode_output.get().strip()
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save the video as",
            defaultextension=".mkv",
            filetypes=VIDEO_TYPES,
            initialdir=os.path.dirname(current) or None,
            initialfile=os.path.basename(current) or None,
        )
        if path:
            self.encode_output.set(path)
            # The save dialog has already asked about replacing an existing file.
            self._confirmed_output = os.path.abspath(path)

    def browse_decode_input(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose a PixelCrypt video", filetypes=VIDEO_TYPES
        )
        if path:
            self.set_decode_input(path)

    def browse_decode_output(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="Restore the file into",
            initialdir=self.decode_output.get().strip() or None,
            mustexist=False,
        )
        if path:
            self.decode_output.set(path)

    # -- starting jobs (main thread) ------------------------------------------

    def start_encode(self) -> bool:
        """Validate the Encode tab and start encoding. Returns True if a job started."""
        if self.busy:
            return False
        src = self.encode_input.get().strip()
        out = self.encode_output.get().strip()
        password = self.encode_password.get()

        if not src:
            self.show_error("No file chosen", "Choose the file you want to encode.")
            return False
        if not os.path.isfile(src):
            self.show_error("File not found", f"There's no file at:\n{src}")
            return False
        if not out:
            self.show_error("No video name", "Choose where to save the video.")
            return False
        out = os.path.abspath(out)
        if out == os.path.abspath(src):
            self.show_error("Choose another name", "The video can't replace the file you're encoding.")
            return False
        if not os.path.isdir(os.path.dirname(out)):
            self.show_error("Folder not found", f"There's no folder at:\n{os.path.dirname(out)}")
            return False
        if os.path.isdir(out):
            self.show_error("Choose a file name", f"{out} is a folder. Enter a file name for the video.")
            return False
        if password != self.encode_confirm.get():
            self.show_error("Passwords don't match", "Type the same password in both fields.")
            return False
        if not password and not self.ask_yes_no(
            "Save without a password?",
            "Without a password the file is stored unencrypted, and anyone who has "
            "the video can restore it. Continue?",
        ):
            return False
        if (
            os.path.exists(out)
            and out != self._confirmed_output
            and not self.ask_yes_no("Replace the video?", f"{os.path.basename(out)} already exists. Replace it?")
        ):
            return False

        size = os.path.getsize(src)

        def on_success(frames: int) -> None:
            self.show_info(
                "Video saved",
                f"Saved {format_size(size)} as {frames} frame{'s' if frames != 1 else ''} in:\n{out}",
            )

        self._start_job(
            "Encoding",
            engine.encode_file_to_video,
            {"input_path": src, "output_video_path": out, "password": password or None},
            on_success,
        )
        return True

    def start_decode(self) -> bool:
        """Validate the Decode tab and start decoding. Returns True if a job started."""
        if self.busy:
            return False
        video = self.decode_input.get().strip()
        if not video:
            self.show_error("No video chosen", "Choose the PixelCrypt video to decode.")
            return False
        if not os.path.isfile(video):
            self.show_error("Video not found", f"There's no file at:\n{video}")
            return False
        out_dir = self.decode_output.get().strip() or os.path.dirname(os.path.abspath(video))
        if os.path.exists(out_dir) and not os.path.isdir(out_dir):
            self.show_error("Not a folder", f"{out_dir} is a file. Choose a folder to restore into.")
            return False
        password = self.decode_password.get()

        def on_success(path: str) -> None:
            self.show_info(
                "File restored",
                f"Restored {os.path.basename(path)} ({format_size(os.path.getsize(path))}) to:\n"
                f"{os.path.dirname(path)}",
            )

        self._start_job(
            "Decoding",
            engine.decode_video_to_file,
            {
                "video_path": video,
                "output_dir": out_dir,
                "password": password or None,
                "overwrite": False,
            },
            on_success,
        )
        return True

    # -- running jobs -------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _start_job(
        self,
        name: str,
        func: Callable[..., Any],
        kwargs: dict[str, Any],
        on_success: Callable[[Any], None],
    ) -> None:
        self._cancel.clear()
        self._job = name
        self._on_success = on_success
        self._set_busy(True)
        self.progress["value"] = 0
        self.status.set(f"{name}…")
        self._worker = threading.Thread(
            target=self._work, args=(func, kwargs), name="pixelcrypt-worker", daemon=True
        )
        self._worker.start()
        self._poll_id = self.after(POLL_MS, self._poll)

    def _work(self, func: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        """Worker thread. Must not touch any widget: it only talks through the queue."""
        try:
            result = func(progress=self._report_progress, **kwargs)
        except Cancelled:
            self._queue.put(("cancelled", None))
        except BaseException as exc:  # shown to the user by the main thread
            self._queue.put(("error", exc))
        else:
            self._queue.put(("done", result))

    def _report_progress(self, done: int, total: int) -> None:
        """Progress callback, called on the worker thread."""
        if self._cancel.is_set():
            raise Cancelled
        self._queue.put(("progress", (done, total)))

    def _poll(self) -> None:
        """Main thread: apply queued progress, and finish the job when it's over."""
        self._poll_id = None
        latest = None
        finished = None
        while True:
            try:
                kind, value = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest = value
            else:
                finished = (kind, value)
        if latest is not None and not self._cancel.is_set():
            self._show_progress(*latest)
        if finished is None:
            self._poll_id = self.after(POLL_MS, self._poll)
        else:
            self._finish_job(*finished)

    def _show_progress(self, done: int, total: int) -> None:
        percent = 100 * done / total if total else 100
        self.progress["value"] = percent
        self.status.set(
            f"{self._job}… {percent:.0f}% ({format_size(done)} of {format_size(total)})"
        )

    def _finish_job(self, kind: str, value: Any) -> None:
        if self._worker is not None:
            self._worker.join()
        self._worker = None
        self._set_busy(False)
        if self._quit_when_idle:
            self.root.destroy()
            return

        if kind == "done":
            self.progress["value"] = 100
            self.status.set(f"{self._job} finished.")
            self._on_success(value)
        elif kind == "cancelled":
            self.progress["value"] = 0
            self.status.set("Cancelled. Nothing was saved.")
        else:
            self.progress["value"] = 0
            title, message = describe_error(value)
            self.status.set(f"{self._job} failed: {title.lower()}.")
            self.show_error(title, message)
            if isinstance(value, (engine.PasswordRequiredError, engine.WrongPasswordError)):
                self.notebook.select(self.decode_tab)
                self.decode_password_entry.focus_set()
                self.decode_password_entry.select_range(0, "end")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.encode_button.configure(state=state)
        self.decode_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def cancel(self) -> None:
        """Ask the running job to stop; it stops at the next frame and cleans up."""
        if self.busy:
            self._cancel.set()
            self.status.set("Cancelling…")

    def on_close(self) -> None:
        if not self.busy:
            self.root.destroy()
        elif self.ask_yes_no(
            "Stop and quit?", "A job is still running. Stop it and quit? Nothing will be saved."
        ):
            # Quit once the worker has cleaned up its temporary file.
            self._quit_when_idle = True
            self.cancel()

    def destroy(self) -> None:
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        super().destroy()

    # -- dialogs (tests replace these) --------------------------------------

    def show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self.root)

    def show_info(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self.root)

    def ask_yes_no(self, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title, message, parent=self.root))


def self_test() -> int:
    """Round-trip a temporary file through the engine without opening a window.

    Used to smoke-test packaged builds. Returns a process exit code.
    """
    import av

    def say(message: str) -> None:
        if sys.stdout is not None:  # windowed builds have no console
            print(message, flush=True)

    try:
        tcl_version = tk.Tcl().eval("info patchlevel")
        data = os.urandom(200_000) + b"PixelCrypt self-test"
        with tempfile.TemporaryDirectory(prefix="pixelcrypt-self-test-") as tmp:
            src = os.path.join(tmp, "self-test.bin")
            with open(src, "wb") as f:
                f.write(data)
            for password in ("self-test password", None):
                video = os.path.join(tmp, "self-test.mkv")
                engine.encode_file_to_video(src, video, password=password, width=128, height=128)
                restored = engine.decode_video_to_file(
                    video, os.path.join(tmp, "restored"), password=password
                )
                with open(restored, "rb") as f:
                    if f.read() != data:
                        raise AssertionError("the restored file differs from the original")
                if password:
                    try:
                        engine.decode_video_to_file(video, os.path.join(tmp, "x"), password="wrong")
                    except engine.WrongPasswordError:
                        pass
                    else:
                        raise AssertionError("a wrong password was accepted")
    except Exception as exc:
        say(f"PixelCrypt self-test FAILED: {type(exc).__name__}: {exc}")
        return 1
    say(f"PixelCrypt self-test passed (PyAV {av.__version__}, Tcl {tcl_version}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_NAME, description="Hide a file inside a lossless video, optionally encrypted."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="round-trip a temporary file through the engine, without a window, and exit",
    )
    args, _unknown = parser.parse_known_args(argv)  # ignore extras such as macOS -psn_*
    if args.self_test:
        return self_test()

    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    root, dnd = create_root()
    PixelCryptApp(root, dnd=dnd)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
