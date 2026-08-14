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
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT = os.path.join(HERE, "bfua.py")


def zip_artifact(zip_path: str, artifact: str) -> None:
    """把单个构建产物打成 zip（只打包产物本身，避免把 Releases 目录里的其他文件带进去）。

    artifact 可以是文件（BFUA.exe / BFUA），也可以是目录（macOS 的 BFUA.app）。
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(artifact):
            for root, _dirs, files in os.walk(artifact):
                for name in files:
                    full = os.path.join(root, name)
                    arc = os.path.relpath(full, os.path.dirname(artifact))
                    zf.write(full, arc)
        else:
            zf.write(artifact, os.path.basename(artifact))


def default_version() -> str:
    return os.environ.get("BFUA_VERSION", "v1.0.0")


def platform_opts() -> tuple:
    """Return (pyinstaller_extra_opts, artifact_name) for the host OS."""
    system = platform.system()
    if system == "Windows":
        return ["--onefile", "--noconsole", "--name", "BFUA"], "BFUA.exe"
    if system == "Darwin":
        # macOS 用 --onedir 生成 .app 包：
        #  - --onefile 在 macOS 上需要先组装单文件 bootloader 再做 .app 包装，步骤慢，
        #    与 PySide6 的大量 Qt 框架一起在 CI 上容易长时间卡住（甚至半小时无输出）。
        #  - --onedir 直接生成 .app 包，构建快、首次启动也快，是 PySide6 macOS 的标准做法。
        #  - --codesign-identity=- 强制 ad-hoc 签名，避免 runner 上签名身份/钥匙串问题。
        return [
            "--windowed",
            "--onedir",
            "--name", "BFUA",
            "--osx-bundle-identifier", "com.bfua.uploader",
            "--codesign-identity", "-",
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
        zip_artifact(f"{zip_base}.zip", artifact)
        print(f">>> OK: {zip_base}.zip")

    return 0


if __name__ == "__main__":
    sys.exit(main())
