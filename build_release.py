#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross-platform release build script for BFUA.

PyInstaller cannot cross-compile: you must build on the target OS.
This script detects the host OS and produces the correct artifact:

    Windows -> Releases/<version>/BFUA.exe   (single-file, no console)
    macOS   -> Releases/<version>/BFUA.app   (.app bundle, windowed) + BFUA binary
    Linux   -> Releases/<version>/BFUA       (single-file, no console)

Usage:
    python build_release.py                  # builds into Releases/v1.0.0
    python build_release.py --version v1.1.0 --zip
    python build_release.py --help

Requires on the build machine:
    pip install -r requirements.txt pyinstaller
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT = os.path.join(HERE, "bfua.py")


def default_version() -> str:
    return os.environ.get("BFUA_VERSION", "v1.0.0")


def platform_opts() -> tuple:
    """Return (pyinstaller_extra_opts, artifact_name) for the host OS."""
    system = platform.system()
    if system == "Windows":
        return ["--onefile", "--noconsole", "--name", "BFUA"], "BFUA.exe"
    if system == "Darwin":
        return [
            "--onefile",
            "--windowed",
            "--name", "BFUA",
            "--osx-bundle-identifier", "com.bfua.uploader",
        ], "BFUA.app"
    if system == "Linux":
        return ["--onefile", "--noconsole", "--name", "BFUA"], "BFUA"
    sys.exit(f"Unsupported platform: {system}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build BFUA for the host OS.")
    ap.add_argument("--version", default=default_version(),
                    help="Output subfolder under Releases/ (default: v1.0.0)")
    ap.add_argument("--zip", action="store_true",
                    help="Also archive the release folder into a .zip")
    ap.add_argument("--clean", action="store_true",
                    help="Pass --clean to PyInstaller")
    args = ap.parse_args()

    if not os.path.isfile(APP_SCRIPT):
        sys.exit(f"Not found: {APP_SCRIPT} (run this from the project root)")

    version = args.version.lstrip("v") if args.version.startswith("v") else args.version
    dist_dir = os.path.join(HERE, "Releases", f"v{version}")
    work_dir = os.path.join(HERE, "build", "pyinstaller")
    os.makedirs(dist_dir, exist_ok=True)

    opts, artifact_name = platform_opts()

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if args.clean:
        cmd.append("--clean")
    cmd += ["--distpath", dist_dir, "--workpath", work_dir,
            "--specpath", work_dir] + opts + [APP_SCRIPT]

    print(">>> Running:", " ".join(cmd))
    subprocess.check_call(cmd)

    artifact = os.path.join(dist_dir, artifact_name)
    if not os.path.exists(artifact):
        # On macOS PyInstaller may only produce the .app bundle plus the
        # onefile binary; accept the folder bundle as the artifact.
        print(f"!!! Expected artifact not found at {artifact}")
        print("!!! Contents of dist:", os.listdir(dist_dir))
        return 1

    print(f"\n>>> OK: {artifact}")

    if args.zip:
        zip_base = os.path.join(dist_dir, f"BFUA-{platform.system().lower()}")
        shutil.make_archive(zip_base, "zip", dist_dir)
        print(f">>> OK: {zip_base}.zip")

    return 0


if __name__ == "__main__":
    sys.exit(main())
