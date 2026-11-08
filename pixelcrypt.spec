# PyInstaller spec for the PixelCrypt desktop app.
#
#   pip install -r requirements-build.txt
#   pyinstaller --clean --noconfirm pixelcrypt.spec
#
# Output: dist/PixelCrypt (Linux), dist/PixelCrypt.exe (Windows), or
# dist/PixelCrypt.app (macOS: one-file windowed builds are deprecated there,
# so it's an app bundle). A build only runs on the OS and CPU it was built on.
# Smoke-test it with:  <binary> --self-test
#
# PyAV's wheels bundle FFmpeg, and tkinterdnd2 ships its own PyInstaller hook,
# which bundles only the tkdnd library for this platform.

import sys

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["app.py"],
    hiddenimports=collect_submodules("av"),
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="PixelCrypt",
        console=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="PixelCrypt", upx=False)
    app = BUNDLE(
        coll,
        name="PixelCrypt.app",
        bundle_identifier="io.github.hoomanhajrian.pixelcrypt",
        info_plist={"NSHighResolutionCapable": True},
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="PixelCrypt",
        console=False,
        upx=False,
        runtime_tmpdir=None,
    )
