---
name: package
description: Build and smoke-test PixelCrypt as a standalone desktop executable with PyInstaller (pixelcrypt.spec), locally or through the GitHub Actions build workflow for Windows, macOS and Linux. Use this whenever someone wants a binary, an .exe or .app, a release, a "one-file build", a way to share the app with people who don't have Python, or a check that the packaged app still starts after a change to app.py, engine.py or the dependencies.
---

# Package PixelCrypt

`pixelcrypt.spec` builds the GUI (`app.py`) into one executable that includes Python, PyAV's FFmpeg, pycryptodome, Tcl/Tk and tkdnd. PyInstaller can't cross-compile, so each OS builds its own:

| OS | Output | How to build |
|---|---|---|
| Linux | `dist/PixelCrypt` (one file) | locally, or CI |
| Windows | `dist/PixelCrypt.exe` (one file) | CI, or a Windows machine |
| macOS | `dist/PixelCrypt.app` (bundle) | CI, or a Mac |

macOS gets an `.app` bundle because PyInstaller deprecates one-file windowed builds there.

## Build locally

```bash
.venv/bin/pip install -r requirements-build.txt
.venv/bin/pyinstaller --clean --noconfirm pixelcrypt.spec     # about a minute
```

The building Python must have tkinter (`sudo apt install python3-tk` on Ubuntu), because PyInstaller bundles the Tcl/Tk it finds. `build/` and `dist/` are gitignored, so never commit them.

## Smoke test (always, right after building)

```bash
./dist/PixelCrypt --self-test        # macOS: ./dist/PixelCrypt.app/Contents/MacOS/PixelCrypt --self-test
```

Expect exit code 0 and `PixelCrypt self-test passed (PyAV x.y, Tcl 8.6.z)`. The self-test round-trips a file through FFV1 and AES-GCM and starts a Tcl interpreter. Those are the pieces most likely to go missing from a bundle, and they fail only at runtime, so a successful build alone proves little.

Then look at the window. A crash on start shows up as "exited early":

```bash
xvfb-run -a -s "-screen 0 1280x800x24" .venv/bin/python .claude/skills/run-app/scripts/screenshot.py --binary dist/PixelCrypt
```

Read `build/screenshots/launched.png` to confirm the Encode tab rendered.

## Build on CI (Windows, macOS, Linux)

`.github/workflows/build.yml` runs on a manual dispatch or on a pushed `v*` tag. Each OS builds, runs `--self-test` (on Windows through `Start-Process -Wait`, because a windowed .exe doesn't block the shell), and uploads its result as an artifact.

```bash
gh workflow run build.yml --ref <branch>      # or: git tag v1.0.0 && git push origin v1.0.0
gh run list --workflow build.yml              # if jobs sit queued, see "Known issues" in CLAUDE.md
gh run download <run-id> --dir build/artifacts
```

Pushing a tag or triggering CI is visible to others, so confirm with the user first.

## When something's missing from the bundle

| Symptom | Likely fix |
|---|---|
| `No module named 'av.…'` | `collect_submodules("av")` in the spec should cover it. Check that PyAV and pyinstaller-hooks-contrib are up to date. |
| `Can't find a usable init.tcl` | The building Python has no Tk data. Install `python3-tk`/`tk8.6`, or build with a python.org or `actions/setup-python` Python. |
| Drag-and-drop missing in the build | tkinterdnd2 ≥ 0.7 has its own PyInstaller hook (`pyinstaller40` entry point). Check that it's installed in the build environment. The app still runs without drag-and-drop. |
| macOS: "app is damaged / can't be opened" | The app is unsigned. Right-click and choose Open, or sign and notarize it for distribution. |
| Windows SmartScreen warning | The app is unsigned. Choose "More info", then "Run anyway", or sign it. |

Expect the binary to be about 60-100 MB. Most of that is FFmpeg.
