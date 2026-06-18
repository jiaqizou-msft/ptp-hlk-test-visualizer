"""Build a standalone Windows executable + release zip for PTP Metrics.

Usage:
    python build_exe.py            # build dist/PTPMetrics.exe and release zip
    python build_exe.py --clean    # remove build artifacts first

It will install PyInstaller if missing. The resulting exe is a single windowed
binary that needs no Python on the target machine.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "PTPMetrics"
ENTRY = os.path.join(HERE, "ptp_metrics_app.py")
VERSION = "0.1.0"


def _run(cmd, **kw):
    print(">", " ".join(cmd))
    subprocess.check_call(cmd, **kw)


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        print("Installing PyInstaller…")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def clean():
    for d in ("build", "dist", f"{APP_NAME}.spec"):
        p = os.path.join(HERE, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    print("Cleaned build artifacts.")


def build():
    ensure_pyinstaller()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onedir",                   # folder build: DLLs sit next to the exe.
        # --onefile extracts python3xx.dll to %TEMP%\_MEI* and LoadLibrary's it
        # from there, which Windows Defender Application Control (WDAC) / Smart
        # App Control blocks on managed machines (error 0xc0e90002). --onedir
        # avoids the temp extraction entirely and runs under those policies.
        "--windowed",                 # no console window (GUI app)
        "--collect-submodules", "ptp_metrics",
        # matplotlib + numpy hooks are bundled automatically by PyInstaller,
        # but be explicit about the TkAgg backend dependency:
        "--hidden-import", "matplotlib.backends.backend_tkagg",
        "--hidden-import", "tkinter",
        ENTRY,
    ]
    _run(cmd, cwd=HERE)
    exe = os.path.join(HERE, "dist", APP_NAME, f"{APP_NAME}.exe")
    if not os.path.exists(exe):
        raise SystemExit("Build failed: exe not produced.")
    size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(os.path.join(HERE, "dist", APP_NAME))
        for f in fs
    )
    print(f"\nBuilt: {exe}  (folder total {size/1e6:.1f} MB)")
    return exe


def make_release(exe: str):
    rel_dir = os.path.join(HERE, "release")
    os.makedirs(rel_dir, exist_ok=True)
    zip_name = os.path.join(rel_dir, f"{APP_NAME}-v{VERSION}-win-x64.zip")
    app_dir = os.path.join(HERE, "dist", APP_NAME)   # the onedir folder
    sample = os.path.join(HERE, "demo_dashboard.png")
    note = (
        f"{APP_NAME} v{VERSION}\n"
        f"Built {datetime.now():%Y-%m-%d %H:%M}\n\n"
        "Run by double-clicking PTPMetrics\\PTPMetrics.exe (Windows 10/11, x64).\n"
        "Keep the whole PTPMetrics folder together — the exe needs the files\n"
        "next to it. No install or Python required.\n\n"
        "Click 'Start Live' and touch the touchpad. Use Record + Save CSV to\n"
        "store a session; Save Report writes a PNG dashboard + JSON metrics.\n"
        "Open CSV/Folder analyzes a ptrecorder recording offline.\n\n"
        "NOTE: This is a folder build (not single-file) so it runs under\n"
        "Windows Defender Application Control / Smart App Control policies.\n"
    )
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as z:
        # whole onedir folder, under a top-level PTPMetrics/ prefix
        for dp, _, fs in os.walk(app_dir):
            for f in fs:
                full = os.path.join(dp, f)
                arc = os.path.join(APP_NAME, os.path.relpath(full, app_dir))
                z.write(full, arc)
        for src, arc in (("README.md", "README.md"),
                         ("requirements.txt", "requirements.txt")):
            p = os.path.join(HERE, src)
            if os.path.exists(p):
                z.write(p, arc)
        if os.path.exists(sample):
            z.write(sample, "sample_dashboard.png")
        z.writestr("RELEASE_NOTES.txt", note)
    print(f"Release package: {zip_name}  ({os.path.getsize(zip_name)/1e6:.1f} MB)")
    return zip_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="clean before building")
    ap.add_argument("--no-zip", action="store_true", help="skip release zip")
    args = ap.parse_args()
    if args.clean:
        clean()
    exe = build()
    if not args.no_zip:
        make_release(exe)
    print("\nDone.")


if __name__ == "__main__":
    main()
