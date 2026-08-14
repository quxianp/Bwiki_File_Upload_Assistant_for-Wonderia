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
    pip install -r requirements.txt "pyinstaller>=6.10"
"""

import argparse
import os
import platform
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT = os.path.join(HERE, "bfua.py")

# 版本号白名单：仅允许字母数字及 . _ + -，防止 --version ../../evil 之类路径穿越
VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


def zip_artifact(zip_path: str, artifact: str) -> None:
    """把单个构建产物打成 zip（只打包产物本身，避免把 Releases 目录里的其他文件带进去）。

    artifact 可以是文件（BFUA.exe / BFUA），也可以是目录（macOS 的 BFUA.app）。
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(artifact):
            for root, _dirs, files in os.walk(artifact):
                for name in files:
                    full = os.path.join(root, name)
                    # 统一用正斜杠，避免 Windows 上 zip 内出现反斜杠路径
                    arc = os.path.relpath(full, os.path.dirname(artifact)).replace(os.sep, "/")
                    zf.write(full, arc)
        else:
            zf.write(artifact, os.path.basename(artifact))


def default_version() -> str:
    return os.environ.get("BFUA_VERSION", "v1.0.0")


# 我们只用 QtCore / QtGui / QtWidgets；显式排除其余 PySide6 模块，
# 避免它们连同各自的 Qt 库一起被打包（PySide6 附带的大量模块是体积大头）。
PYINSTALLER_EXCLUDES = [
    # QML / Quick 全家桶
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQuick3D", "PySide6.QtQuickLayouts",
    # 用不到的 Qt 模块
    "PySide6.QtNetwork", "PySide6.QtSvg", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtXml", "PySide6.QtDBus", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtDesigner", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtWebEngine", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtTextToSpeech", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtHttpServer", "PySide6.QtLanguageServer",
    # Qt3D 全家桶
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
]


def platform_opts() -> tuple:
    """Return (pyinstaller_extra_opts, artifact_name) for the host OS."""
    system = platform.system()
    excludes = [flag for m in PYINSTALLER_EXCLUDES for flag in ("--exclude-module", m)]

    if system == "Windows":
        return ["--onefile", "--noconsole", "--name", "BFUA"] + excludes, "BFUA.exe"

    if system == "Darwin":
        # macOS 用 --onedir 生成 .app 包：
        #  - --onefile 在 macOS 上需要先组装单文件 bootloader 再做 .app 包装，步骤慢，
        #    与 PySide6 的大量 Qt 框架一起在 CI 上容易长时间卡住（甚至半小时无输出）。
        #  - --onedir 直接生成 .app 包，构建快、首次启动也快，是 PySide6 macOS 的标准做法。
        #  - --codesign-identity=- 强制 ad-hoc 签名，避免 runner 上签名身份/钥匙串问题。
        #  - --target-architecture 只保留指定架构，可大幅瘦身（PySide6 的 dylib 是
        #    universal2 双架构，默认两者都打进 .app）。可用 BFUA_ARCH=arm64|x86_64|universal 控制。
        arch = os.environ.get("BFUA_ARCH", "universal").lower()
        opts = [
            "--windowed",
            "--onedir",
            "--name", "BFUA",
            "--osx-bundle-identifier", "com.bfua.uploader",
            "--codesign-identity", "-",
        ] + excludes
        if arch in ("arm64", "x86_64"):
            opts += ["--target-architecture", arch]
        elif arch != "universal":
            sys.exit(f"Unsupported BFUA_ARCH: {arch} (use arm64 / x86_64 / universal)")
        return opts, "BFUA.app"

    if system == "Linux":
        # --strip 剔除 .so 的调试符号，Linux 包通常能减小 15-30%。
        return ["--onefile", "--noconsole", "--strip", "--name", "BFUA"] + excludes, "BFUA"

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

    # 校验版本号，防止 --version ../../evil 之类路径穿越
    if not VERSION_RE.match(args.version):
        sys.exit(f"非法版本号：{args.version!r}（仅允许字母、数字、. _ + -）")

    # 提前检查 PyInstaller 是否安装，给出友好提示
    # （Python 3.13/3.14 需要较新的 PyInstaller 才支持，故提示固定最低版本）
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("未安装 PyInstaller，请先运行：pip install \"pyinstaller>=6.10\"")

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

    # 签名 / 安全提示：ad-hoc 与无签名的产物会被系统拦截，提醒发布者准备说明
    if platform.system() == "Darwin":
        print(">>> 提示：macOS 产物使用 ad-hoc 签名，用户首次运行需在"
              "「系统设置 > 隐私与安全性」中允许打开，或用正式开发者证书签名。")
    elif platform.system() == "Windows":
        print(">>> 提示：Windows 产物未签名，SmartScreen 可能提示未知发布者；"
              "分发时请在 README 中说明，或使用正式代码签名证书。")

    if args.zip:
        zip_base = os.path.join(dist_dir, f"BFUA-{platform.system().lower()}")
        zip_artifact(f"{zip_base}.zip", artifact)
        print(f">>> OK: {zip_base}.zip")

    return 0


if __name__ == "__main__":
    sys.exit(main())
