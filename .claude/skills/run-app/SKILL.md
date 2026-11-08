---
name: run-app
description: Launch, drive and screenshot the PixelCrypt desktop app (app.py, tkinter), including headless on a virtual display with Xvfb. Use this whenever you need to run PixelCrypt, open its window for the user, see what the Encode or Decode tab looks like, check a change to app.py in the real app, reproduce a GUI bug, or smoke-test the app with --self-test, even if the user just says "run it" or "show me the app".
---

# Run the PixelCrypt app

`app.py` is a tkinter GUI on top of `engine.py`. It has two tabs (Encode and Decode), a progress bar and a Cancel button. The engine runs on a worker thread, and the main thread polls a queue with `after()`.

## 1. Set up once

tkinter can't be installed with pip. It comes with the system Python.

```bash
# Debian/Ubuntu: tkinter, venv support, and Xvfb for headless runs
sudo apt install python3-tk python3-venv xvfb
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -c "import tkinter, tkinterdnd2, av; print('ok')"
```

If `python3 -m venv` says ensurepip is missing and you can't install `python3-venv`, use `python3 -m venv --without-pip .venv && python3 -m pip --python .venv/bin/python install pip`, then install the requirements. You can't install apt packages yourself without sudo, so ask the user.

## 2. Pick the right way to run it

| Goal | Command |
|---|---|
| Check the engine and Tcl work, with no window | `.venv/bin/python app.py --self-test` (exit 0 and prints "self-test passed") |
| Open the window for the user to use | `.venv/bin/python app.py` in the background. It blocks until the window is closed. |
| See the GUI yourself | the screenshot script below, under Xvfb |
| Check behaviour automatically | drive it from code (section 4) or run `tests/test_app.py` |

When you open the window for the user, it appears on their display, not yours. Tell them it's open and what to try. Don't claim it looks right unless you've taken a screenshot.

## 3. Take screenshots headless

```bash
xvfb-run -a -s "-screen 0 1280x800x24" .venv/bin/python .claude/skills/run-app/scripts/screenshot.py
```

This writes `encode.png`, `decode.png` and `progress.png` (a real 48 MiB encode captured part-way, then cancelled) to `build/screenshots/`, which is gitignored. Use `--out DIR` to put them elsewhere. Open them with the Read tool and actually look at them: check layout, clipped text, button states, and that the progress text makes sense.

To screenshot the packaged app instead, add `--binary`. It starts that executable with no arguments and grabs the whole screen into `launched.png`:

```bash
xvfb-run -a -s "-screen 0 1280x800x24" .venv/bin/python .claude/skills/run-app/scripts/screenshot.py --binary dist/PixelCrypt
```

Keep these two commands exactly as written. `.claude/settings.json` allows these exact forms, so they run without a permission prompt.

There's no window manager under Xvfb, so the window has no title bar. That's expected.

## 4. Drive the app from code

Use the same pattern as `tests/test_app.py`:

```python
root = tk.Tk(); root.withdraw()
gui = app.PixelCryptApp(root, dnd=False)
gui.show_error = gui.show_info = lambda title, message: print(title, message)
gui.ask_yes_no = lambda title, message: True
gui.set_encode_input("/path/file"); gui.encode_password.set("pw"); gui.encode_confirm.set("pw")
gui.start_encode()                     # returns False if validation failed
while gui.busy: root.update(); time.sleep(0.01)
```

There are two reasons for this pattern:

- **Dialogs are replaced** because message boxes are modal: they'd block the script forever.
- **The loop keeps calling `root.update()`** because the worker's results only reach the UI through `after()` callbacks, which run only while the event loop is processing events.

## 5. Keep the GUI rules intact

When you change `app.py`, check these:

- **Threads:** the worker thread must never touch a widget or open a dialog, because tkinter isn't thread-safe. It only puts messages on the queue. Everything else happens on the main thread.
- **Cancel:** Cancel works because the progress callback raises `Cancelled`. The engine then deletes its temporary file, so don't catch that exception inside the engine call.
- **Padding:** use `pady`/`padx`, not `py`.
- **Drag-and-drop:** it only works when `tkinterdnd2` loads, through `create_root()`. Under Xvfb there's nothing to drag from, so test drops by hand on a real desktop.
- **After a GUI change:** run the `verify` skill.
