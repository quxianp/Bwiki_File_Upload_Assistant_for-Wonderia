#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run:
    pip install PySide6 mwclient
    python bfua.py
"""

import os
import sys
import threading

import requests  # mwclient 的硬依赖

from PySide6.QtCore import Qt, Signal, Slot, QObject, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QWidget, QLabel, QLineEdit, QPushButton,
    QHBoxLayout, QVBoxLayout, QFileDialog, QGraphicsDropShadowEffect, QProgressBar,
)

try:
    from mwclient import Site as MwSite
    from mwclient.errors import MaximumRetriesExceeded
except ImportError:  # pragma: no cover - checked again at runtime
    MwSite = None
    MaximumRetriesExceeded = None

# Configuration

WIKI_HOST = "wiki.biligame.com/wonderia"   # 默认提交的 wiki：Wonderia
WIKI_PATH = "/"
USER_AGENT = "BFUA/1.0 (Bwiki File Upload Assistant; contact: wiki editor)"

ACCENT        = "#0a84ff"
ACCENT_HOVER  = "#248dff"
ACCENT_ACTIVE = "#0060df"
TEXT_MAIN     = "#1d1d1f"
TEXT_SUB      = "#6e6e73"
BG_APP        = "#f5f5f7"
BG_CARD       = "#ffffff"
BORDER        = "#d1d1d6"
OK_GREEN      = "#1e9e4a"
ERR_RED       = "#d70015"

__version__ = "1.0.0"


# BWiki/mwclient

def make_site(sessdata: str) -> "MwSite":
    """Build a logged-in mwclient Site for the Wonderia wiki.

    注意：mwclient 默认 max_retries=25 / retry_timeout=30，服务器一旦返回 5xx
    （例如 Wonderia 偶发的 567/500），重试等待累计可达数十分钟甚至更久，界面会
    长时间“毫无反应”。这里把重试次数与超时收紧，让失败尽快反馈给用户。
    """
    if MwSite is None:
        raise RuntimeError("缺少依赖 mwclient，请先安装：pip install mwclient")
    site = MwSite(
        WIKI_HOST, path=WIKI_PATH, scheme="https",
        max_retries=2,          # 5xx/网络错误最多重试 2 次
        retry_timeout=4,        # 每次重试前的等待秒数
        connection_options={"timeout": 20},  # 单次 HTTP 请求超时 20 秒
    )
    site.connection.headers["User-Agent"] = USER_AGENT
    if sessdata:
        # 真正的 Cookie 名是 SESSDATA
        site.login(cookies={"SESSDATA": sessdata})
    return site


def _friendly_error(exc: Exception) -> str:
    """把底层异常转换成面向用户的友好提示（用于对话框反馈）。"""
    if MaximumRetriesExceeded is not None and isinstance(exc, MaximumRetriesExceeded):
        return "服务器多次返回错误（可能暂时繁忙或网络不稳定），请稍后重试。"
    if isinstance(exc, requests.exceptions.Timeout):
        return "请求超时：网络不稳定或服务器无响应，请稍后重试。"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "无法连接到服务器，请检查网络后重试。"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        return f"服务器返回错误（HTTP {code}），服务器可能暂时繁忙，请稍后重试。"
    if isinstance(exc, OSError):
        return f"本地文件系统错误：{exc}"
    msg = str(exc)
    return msg if msg else type(exc).__name__


def validate_sessdata(sessdata: str):
    """验证 SESSDATA 是否可用，返回 (用户名, 用户组列表)；不可用则抛异常。"""
    site = make_site(sessdata)
    data = site.api("query", meta="userinfo", uiprop="groups")
    info = data["query"]["userinfo"]
    name = info.get("name", "")
    groups = info.get("groups", []) or []
    # API 对匿名用户会返回 "anon": ""（键存在但值为空串），因此要判断键是否出现
    if "anon" in info or not name:
        raise RuntimeError("该 SESSDATA 无效（返回的是匿名身份），请检查 Cookie 是否复制正确或已过期。")
    return name, [str(g) for g in groups]


def _is_moderation_queue(exc: Exception) -> bool:
    """判断异常是否属于「已提交审核队列」而非真正失败。

    BWiki 的新文件（尤其是图片）会先进入审核队列，Moderation 扩展通过
    error code `moderation-image-queued` 等返回提示。mwclient 会把它当成
    APIError 抛出，但实际上文件已成功提交，等待审核通过后即对其他用户可见。
    """
    code = getattr(exc, "code", None)
    return isinstance(code, str) and code.startswith("moderation-")


def _parse_upload_response(response, filename: str) -> dict:
    """解析 site.upload() 的返回值，返回 {"status", "filename", "detail"}。

    mwclient 的 site.upload() 只有在请求本身失败（5xx 重试耗尽、网络错误、
    API error 块）时才抛异常；而 MediaWiki 的 upload API 在“没有真正保存文件”
    的很多场景下仍会返回 200 + result != "Success"，例如：
      - 文件内容与已存在的文件重复（duplicate，服务器不会再次保存）
      - 文件类型与扩展名不符（filetype-mismatch）
      - 文件名被改写（badfilename）
      - 审核相关（某些版本的 Moderation 以 Warning 而非 error 返回）
      - 空响应 / 代理异常导致的空 body
    之前代码直接忽略返回值，导致这些场景一律显示“上传成功”，但文件实际并没有
    出现在 wiki 上（管理员反馈的正是这个现象）。这里必须检查 result。
    """
    upload = (response or {}).get("upload") or {}
    result = str(upload.get("result", "") or "")

    # 审核队列：无论以异常（moderation-image-queued）还是以 Warning 返回，
    # 都算“已提交审核”，而不是“上传失败”。
    warnings = upload.get("warnings") or {}
    if _is_moderation_queue_str(result) or "moderation" in warnings:
        detail = str(warnings.get("moderation", "")) or result
        return {"status": "queued", "filename": filename, "detail": detail}

    if result == "Success":
        return {"status": "ok", "filename": filename, "detail": ""}

    # 其余情况：如实反馈服务器给出的 result / warnings / errors。
    parts = [f"result={result!r}"] if result else ["服务器未返回上传结果（可能未真正保存）"]
    for key, value in warnings.items():
        if isinstance(value, (list, tuple)):
            names = [str(x.get("title", "")) if isinstance(x, dict) else str(x)
                     for x in value]
            parts.append(f"{key}: {'、'.join(n for n in names if n)}")
        else:
            parts.append(f"{key}: {value}")
    for err in upload.get("errors") or []:
        if isinstance(err, dict):
            parts.append(f"{err.get('code', '')}: {err.get('info', '')}")
        else:
            parts.append(str(err))
    detail = "；".join(p for p in parts if p)
    return {"status": "error", "filename": filename, "detail": detail}


def _is_moderation_queue_str(text: str) -> bool:
    text = (text or "").lower()
    return "moderation" in text or "queued" in text


def upload_single(sessdata: str, file_path: str, description: str, comment: str) -> dict:
    """上传单个文件。

    返回结果字典：{"status": "ok"|"queued"|"error", "filename", "detail"}。
    status 为 "queued" 表示文件已提交审核队列，等待审核通过后可见；
    "error" 表示服务器未确认保存成功，detail 为具体原因。
    """
    site = make_site(sessdata)
    filename = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            response = site.upload(f, filename=filename,
                                   description=description or "",
                                   comment=comment or "")
    except Exception as exc:  # noqa: BLE001
        if _is_moderation_queue(exc):
            detail = getattr(exc, "info", "") or str(exc)
            return {"status": "queued", "filename": filename, "detail": detail}
        raise
    return _parse_upload_response(response, filename)


def upload_batch(sessdata: str, folder_path: str, description: str, comment: str,
                 on_progress=None):
    """批量上传文件夹内所有文件，返回 (成功数, 已提交审核数, 失败数, 总数)。

    on_progress(i, total, name, status, detail) 会在关键节点被调用：
    status 为 "init"（开始连接/扫描）、"uploading"（正在上传该文件）、
    "ok"（成功）、"queued"（已提交审核）或 "error"（失败）。
    """
    if on_progress:
        on_progress(0, 0, "", "init", "")
    site = make_site(sessdata)
    try:
        files = sorted(
            name for name in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, name))
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法读取文件夹：{_friendly_error(exc)}")
    if not files:
        raise RuntimeError("该文件夹中没有可上传的文件。")
    ok = queued = failed = 0
    for i, name in enumerate(files, 1):
        if on_progress:
            on_progress(i, len(files), name, "uploading", "")
        try:
            with open(os.path.join(folder_path, name), "rb") as f:
                response = site.upload(f, filename=name,
                                       description=description or "",
                                       comment=comment or "")
            res = _parse_upload_response(response, name)
            if res["status"] == "ok":
                ok += 1
                if on_progress:
                    on_progress(i, len(files), name, "ok", "")
            elif res["status"] == "queued":
                queued += 1
                if on_progress:
                    on_progress(i, len(files), name, "queued", res["detail"])
            else:
                failed += 1
                if on_progress:
                    on_progress(i, len(files), name, "error", res["detail"])
        except Exception as exc:  # noqa: BLE001 — 单个文件失败不中断整批
            if _is_moderation_queue(exc):
                queued += 1
                if on_progress:
                    detail = getattr(exc, "info", "") or str(exc)
                    on_progress(i, len(files), name, "queued", detail)
            else:
                failed += 1
                if on_progress:
                    on_progress(i, len(files), name, "error", _friendly_error(exc))
    return ok, queued, failed, len(files)


# 样式（macOS 风格的浅色现代主题）

def build_stylesheet() -> str:
    return f"""
    * {{
        font-family: "SF Pro Text", "SF Pro Display", -apple-system, "Segoe UI",
                     "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        font-size: 13px;
        color: {TEXT_MAIN};
    }}

    QWidget#AppRoot {{
        background-color: {BG_APP};
        border-radius: 14px;
    }}

    QWidget#TitleBar {{
        background-color: rgba(0, 0, 0, 0.02);
        border-top-left-radius: 14px;
        border-top-right-radius: 14px;
    }}

    QLabel#TitleLabel {{
        color: {TEXT_SUB};
        font-size: 12px;
    }}

    QLabel#WelcomeLabel {{
        font-size: 22px;
        font-weight: 700;
        color: {TEXT_MAIN};
    }}

    QLabel#FieldLabel {{
        color: {TEXT_MAIN};
        font-weight: 500;
    }}

    QLineEdit {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 8px 10px;
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QLineEdit:focus {{
        border: 1px solid {ACCENT};
    }}

    QPushButton {{
        background-color: {BG_CARD};
        color: {TEXT_MAIN};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 8px 16px;
    }}
    QPushButton:hover {{ background-color: #f0f0f2; border-color: #c0c0c5; }}
    QPushButton:pressed {{ background-color: #e4e4e7; }}
    QPushButton:disabled {{
        color: #a5a5ab;
        background-color: #f2f2f4;
        border-color: #e2e2e5;
    }}

    QPushButton#PrimaryButton {{
        background-color: {ACCENT};
        color: #ffffff;
        border: none;
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover {{ background-color: {ACCENT_HOVER}; }}
    QPushButton#PrimaryButton:pressed {{ background-color: {ACCENT_ACTIVE}; }}
    QPushButton#PrimaryButton:disabled {{
        background-color: #9cc9ff;
        color: rgba(255, 255, 255, 0.9);
    }}

    QPushButton#UploadButton {{
        min-height: 42px;
        font-size: 14px;
        font-weight: 600;
    }}

    QProgressBar {{
        background-color: #e8e8ec;
        border: none;
        border-radius: 4px;
        max-height: 8px;
    }}
    QProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: 4px;
    }}

    QLabel#FeedbackLabel {{ color: {TEXT_SUB}; }}
    QLabel#FeedbackLabel[ok="true"] {{ color: {OK_GREEN}; }}
    QLabel#FeedbackLabel[err="true"] {{ color: {ERR_RED}; }}
    """


# 后台任务 Worker

class TaskWorker(QObject):
    """把一个普通 Python 函数放进后台线程执行，结果通过 done 信号回传。

    注意：这里刻意不使用 QThread / moveToThread。在较新的 Python 版本上
    （例如 Python 3.14 + PySide6 6.11），QThread 在线程内执行 Python 槽函数
    会触发原生崩溃（Windows 错误码 0xC0000409），导致后台任务永远无法完成。
    改用标准库 threading.Thread 后，Qt 仍支持从任意线程发射信号，跨平台稳定。
    """

    done = Signal(object)  # (bool ok, object result_or_error)

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self):
        try:
            result = self._fn(*self._args)
            self.done.emit((True, result))
        except Exception as exc:  # noqa: BLE001
            self.done.emit((False, _friendly_error(exc)))


def spawn_worker(parent, fn, callback, *args) -> threading.Thread:
    """在独立 Python 线程中执行 fn，完成后通过 done 信号回到主线程。

    返回的线程对象由调用方持有引用（self._threads），避免被 GC。
    """
    worker = TaskWorker(fn, *args)
    worker.done.connect(callback)
    thread = threading.Thread(target=worker.run, name="bfua-worker", daemon=True)
    thread.start()
    return thread


# macOS 风格交通灯按钮

class TrafficLight(QPushButton):
    """macOS 交通灯：红(关闭) / 黄(最小化) / 绿(最大化)。"""

    def __init__(self, color: str, symbol: str, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._symbol = symbol  # 'x' | '-' | '+'
        self.setFixedSize(14, 14)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._hovered = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = self._color
        if self._hovered and self.isDown():
            color = color.darker(110)
        p.setPen(QPen(color, 1))
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(1, 1, self.width() - 2, self.height() - 2))
        if self._hovered:
            p.setPen(QPen(QColor("#ffffff"), 1.2))
            c = QPointF(self.width() / 2, self.height() / 2)
            if self._symbol == "x":
                p.drawLine(c.x() - 2.2, c.y() - 2.2, c.x() + 2.2, c.y() + 2.2)
                p.drawLine(c.x() - 2.2, c.y() + 2.2, c.x() + 2.2, c.y() - 2.2)
            elif self._symbol == "-":
                p.drawLine(c.x() - 2.2, c.y(), c.x() + 2.2, c.y())
            elif self._symbol == "+":
                p.drawLine(c.x() - 2.2, c.y(), c.x() + 2.2, c.y())
                p.drawLine(c.x(), c.y() - 2.2, c.x(), c.y() + 2.2)
        p.end()


class TitleBar(QWidget):
    """可拖拽的标题栏，左侧为 macOS 交通灯。"""

    def __init__(self, title: str, dialog_mode: bool = False):
        super().__init__()
        self.setObjectName("TitleBar")
        self._drag_offset = None
        self._can_toggle_max = not dialog_mode

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(8)

        self.close_btn = TrafficLight("#ff5f57", "x")
        self.close_btn.clicked.connect(lambda: self.window().close())
        lay.addWidget(self.close_btn)

        if dialog_mode:
            self.min_btn = self.max_btn = None
        else:
            self.min_btn = TrafficLight("#febc2e", "-")
            self.min_btn.clicked.connect(lambda: self.window().showMinimized())
            self.max_btn = TrafficLight("#28c840", "+")
            self.max_btn.clicked.connect(self._toggle_maximize)
            lay.addWidget(self.min_btn)
            lay.addWidget(self.max_btn)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title_label, 1)

        n_lights = 3 if not dialog_mode else 1
        left_width = 16 + 14 * n_lights + 8 * (n_lights - 1)
        spacer = QWidget()
        spacer.setFixedWidth(left_width)
        spacer.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(spacer)

    def _toggle_maximize(self):
        win = self.window()
        if win.isMaximized():
            win.showNormal()
            self._notify_max_state(False)
        else:
            win.showMaximized()
            self._notify_max_state(True)

    def _notify_max_state(self, maximized: bool):
        fn = getattr(self.window(), "_on_maximized_state_changed", None)
        if callable(fn):
            fn(maximized)

    # ---- 拖拽移动窗口 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event):
        if self._can_toggle_max and event.button() == Qt.LeftButton:
            self._toggle_maximize()


# 无边框窗口基类

def _apply_surface(window, dialog_mode: bool, title: str):
    """初始化窗口：无边框 + 圆角 + 阴影 + 标题栏 + body 区域。"""
    window.setAttribute(Qt.WA_TranslucentBackground)

    container = QWidget(window)
    container_lay = QVBoxLayout(container)
    container_lay.setContentsMargins(36, 36, 36, 36)
    container_lay.setSpacing(0)

    root = QWidget(container)
    root.setObjectName("AppRoot")
    root.setAttribute(Qt.WA_StyledBackground, True)
    container_lay.addWidget(root)

    shadow = QGraphicsDropShadowEffect(root)
    shadow.setBlurRadius(48)
    shadow.setOffset(0, 8)
    shadow.setColor(QColor(0, 0, 0, 110))
    root.setGraphicsEffect(shadow)

    root_lay = QVBoxLayout(root)
    root_lay.setContentsMargins(0, 0, 0, 0)
    root_lay.setSpacing(0)
    titlebar = TitleBar(title, dialog_mode)
    root_lay.addWidget(titlebar)

    body = QWidget(root)
    body.setLayout(QVBoxLayout())
    root_lay.addWidget(body, 1)

    window._surface_container = container
    window._surface_root = root
    window._surface_shadow = shadow
    window._surface_titlebar = titlebar
    window._surface_body = body
    window._surface_margin = 36

    if isinstance(window, QMainWindow):
        window.setCentralWidget(container)
    else:
        outer = QVBoxLayout(window)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    return body


class FramelessWindow(QMainWindow):
    """无边框主窗口（带红黄绿交通灯）。"""

    def __init__(self, title: str):
        super().__init__()
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        _apply_surface(self, dialog_mode=False, title=title)
        self.setStyleSheet(build_stylesheet())

    def _on_maximized_state_changed(self, maximized: bool):
        margin = 0 if maximized else self._surface_margin
        lay = self._surface_container.layout()
        lay.setContentsMargins(margin, margin, margin, margin)
        self._surface_shadow.setEnabled(not maximized)

    def body_layout(self):
        return self._surface_body.layout()


class FramelessDialog(QDialog):
    """无边框模态对话框（只有红色关闭灯）。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        _apply_surface(self, dialog_mode=True, title=title)
        self.setStyleSheet(build_stylesheet())

    def body_layout(self):
        return self._surface_body.layout()

    def showEvent(self, event):
        # 在 super().showEvent() 之前完成居中定位，避免先按默认位置映射再跳转造成的闪烁
        parent = self.parentWidget()
        if parent is not None:
            self.move(parent.frameGeometry().center() - self.rect().center())
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


# 主窗口

class MainWindow(FramelessWindow):
    def __init__(self):
        super().__init__("BFUA — Bwiki File Upload Assistant")
        self._threads = []
        self.sessdata = ""
        self.validated = False

        self.setFixedSize(620, 368)

        body = self.body_layout()
        body.setContentsMargins(28, 18, 28, 26)
        body.setSpacing(14)

        # ---- 第 1 行：欢迎文字 ----
        self.welcome = QLabel("Welcome to BFUA!")
        self.welcome.setObjectName("WelcomeLabel")
        self.welcome.setAlignment(Qt.AlignCenter)
        body.addWidget(self.welcome)

        # ---- 第 2 行：设置你的 SSESDATA / 输入框 / Confirm ----
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.sess_label = QLabel("设置你的 SSESDATA: ")
        self.sess_label.setObjectName("FieldLabel")
        self.sess_input = QLineEdit()
        self.sess_input.setPlaceholderText("粘贴你的 SESSDATA Cookie 值")
        self.sess_input.setEchoMode(QLineEdit.Password)
        self.sess_input.textChanged.connect(self._on_sess_changed)
        self.sess_input.returnPressed.connect(self._on_confirm)
        self.confirm_btn = QPushButton("确认")
        self.confirm_btn.setObjectName("PrimaryButton")
        self.confirm_btn.setCursor(Qt.PointingHandCursor)
        self.confirm_btn.clicked.connect(self._on_confirm)
        row2.addWidget(self.sess_label)
        row2.addWidget(self.sess_input, 1)
        row2.addWidget(self.confirm_btn)
        body.addLayout(row2)

        # ---- 第 3 行：文本反馈 ----
        self.feedback = QLabel("尚未验证 SESSDATA。")
        self.feedback.setObjectName("FeedbackLabel")
        self.feedback.setWordWrap(True)
        self.feedback.setMinimumHeight(20)
        body.addWidget(self.feedback)

        # ---- 第 4 行：两个上传按钮 ----
        row4 = QHBoxLayout()
        row4.setSpacing(12)
        self.single_btn = QPushButton("Only one file...")
        self.single_btn.setObjectName("UploadButton")
        self.single_btn.setCursor(Qt.PointingHandCursor)
        self.single_btn.clicked.connect(lambda: self._open_upload("single"))
        self.many_btn = QPushButton("So many files!!!")
        self.many_btn.setObjectName("UploadButton")
        self.many_btn.setCursor(Qt.PointingHandCursor)
        self.many_btn.clicked.connect(lambda: self._open_upload("batch"))
        row4.addWidget(self.single_btn, 1)
        row4.addWidget(self.many_btn, 1)
        body.addLayout(row4)

        self._set_upload_enabled(False)

    # ---- 反馈区 ----
    def _set_feedback(self, text: str, error=None):
        self.feedback.setText(text)
        self.feedback.setProperty("ok", error is False)
        self.feedback.setProperty("err", error is True)
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)

    def _set_upload_enabled(self, enabled: bool):
        self.single_btn.setEnabled(enabled)
        self.many_btn.setEnabled(enabled)

    # ---- SESSDATA 验证 ----
    @Slot()
    def _on_sess_changed(self, text: str):
        if self.validated and text != self.sessdata:
            self.validated = False
            self._set_upload_enabled(False)
            self._set_feedback("SESSDATA 已修改，请重新点击 Confirm 验证。")

    @Slot()
    def _on_confirm(self):
        sess = self.sess_input.text().strip()
        if not sess:
            self._set_feedback("请先填入你的 SESSDATA。", error=True)
            return
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.setText("验证中…")
        self._set_feedback("正在验证 SESSDATA…")
        self._threads.append(
            spawn_worker(self, validate_sessdata, self._on_validate_done, sess)
        )

    @Slot(object)
    def _on_validate_done(self, payload):
        self.confirm_btn.setEnabled(True)
        self.confirm_btn.setText("Confirm")
        ok, result = payload
        if ok:
            self.sessdata = self.sess_input.text().strip()
            self.validated = True
            self._set_upload_enabled(True)
            name, groups = result
            groups_text = "、".join(groups) if groups else "（无）"
            self._set_feedback(f"恭喜，验证通过！  用户ID：{name}   用户组：{groups_text}")
        else:
            self.validated = False
            self._set_upload_enabled(False)
            self._set_feedback(f"不恭喜，验证失败！  {result}", error=True)

    # ---- 打开上传窗口 ----
    def _open_upload(self, mode: str):
        if not self.validated or not self.sessdata:
            self._set_feedback("请先完成 SESSDATA 验证。", error=True)
            return
        dlg = UploadDialog(mode, self.sessdata, self)
        # 在 exec() 之前先把对话框定位到主窗口中心：
        # 否则对话框会先在系统默认位置闪出、随后才跳转到居中，看起来就是“闪过一个小窗口”。
        dlg.move(self.frameGeometry().center() - dlg.rect().center())
        dlg.exec()


# 上传窗口（单个文件 / 批量文件共用）

class UploadDialog(FramelessDialog):
    progress_signal = Signal(int, int, str, str, str)  # (i, total, name, status, detail)

    def __init__(self, mode: str, sessdata: str, parent=None):
        is_batch = mode == "batch"
        super().__init__("批量上传文件" if is_batch else "上传单个文件", parent)
        self.mode = mode
        self.sessdata = sessdata
        self._threads = []
        self._busy = False

        self.resize(600, 400 if is_batch else 340)

        body = self.body_layout()
        body.setContentsMargins(28, 16, 28, 24)
        body.setSpacing(12)

        # 路径行
        row_path = QHBoxLayout()
        row_path.setSpacing(10)
        self.path_label = QLabel("文件夹路径：" if is_batch else "文件路径：")
        self.path_label.setObjectName("FieldLabel")
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(
            "选择包含要上传文件的文件夹" if is_batch else "选择要上传的文件"
        )
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.setCursor(Qt.PointingHandCursor)
        self.browse_btn.clicked.connect(self._browse)
        row_path.addWidget(self.path_label)
        row_path.addWidget(self.path_input, 1)
        row_path.addWidget(self.browse_btn)
        body.addLayout(row_path)

        # description 行（可选）
        row_desc = QHBoxLayout()
        row_desc.setSpacing(10)
        self.desc_label = QLabel("description（可选）：")
        self.desc_label.setObjectName("FieldLabel")
        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("文件页要添加的内容，可留空")
        row_desc.addWidget(self.desc_label)
        row_desc.addWidget(self.desc_input, 1)
        body.addLayout(row_desc)

        # comment 行（可选）
        row_cmt = QHBoxLayout()
        row_cmt.setSpacing(10)
        self.cmt_label = QLabel("comment（可选）：")
        self.cmt_label.setObjectName("FieldLabel")
        self.cmt_input = QLineEdit()
        self.cmt_input.setPlaceholderText("编辑摘要，可留空")
        row_cmt.addWidget(self.cmt_label)
        row_cmt.addWidget(self.cmt_input, 1)
        body.addLayout(row_cmt)

        # 进度条（仅批量模式显示）
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setVisible(is_batch)
        self.progress_bar.setRange(0, 100)
        body.addWidget(self.progress_bar)

        # 状态反馈
        self.status = QLabel("")
        self.status.setObjectName("FeedbackLabel")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(20)
        body.addWidget(self.status)

        # 上传按钮
        self.upload_btn = QPushButton("开始上传")
        self.upload_btn.setObjectName("PrimaryButton")
        self.upload_btn.setCursor(Qt.PointingHandCursor)
        self.upload_btn.clicked.connect(self._start)
        body.addWidget(self.upload_btn)

        for field in (self.path_input, self.desc_input, self.cmt_input):
            field.returnPressed.connect(self._start)

        self.progress_signal.connect(self._on_progress)

    def _set_status(self, text: str, error=None):
        self.status.setText(text)
        self.status.setProperty("ok", error is False)
        self.status.setProperty("err", error is True)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.upload_btn.setEnabled(not busy)
        self.path_input.setEnabled(not busy)
        self.desc_input.setEnabled(not busy)
        self.cmt_input.setEnabled(not busy)
        self.browse_btn.setEnabled(not busy)
        self.upload_btn.setText("上传中…" if busy else "开始上传")

    def _browse(self):
        if self.mode == "single":
            path, _ = QFileDialog.getOpenFileName(self, "选择要上传的文件")
        else:
            path = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if path:
            self.path_input.setText(path)

    # ---- 开始上传 ----
    @Slot()
    def _start(self):
        if self._busy:
            return
        path = self.path_input.text().strip()
        if not path:
            self._set_status("请先填写文件/文件夹路径。", error=True)
            return
        if not os.path.exists(path):
            self._set_status("路径不存在，请检查后重试。", error=True)
            return
        if self.mode == "single" and not os.path.isfile(path):
            self._set_status("该路径不是文件。", error=True)
            return
        if self.mode == "batch" and not os.path.isdir(path):
            self._set_status("该路径不是文件夹。", error=True)
            return

        desc = self.desc_input.text().strip()
        cmt = self.cmt_input.text().strip()
        self._set_busy(True)

        if self.mode == "single":
            self._set_status("正在上传…")
            self._threads.append(
                spawn_worker(self, upload_single, self._on_single_done,
                             self.sessdata, path, desc, cmt)
            )
        else:
            self._set_status("正在批量上传…")
            self._threads.append(
                spawn_worker(self, upload_batch, self._on_batch_done,
                             self.sessdata, path, desc, cmt,
                             lambda i, t, n, s, d: self.progress_signal.emit(i, t, n, s, d))
            )

    @Slot(int, int, str, str, str)
    def _on_progress(self, i, total, name, status, detail):
        if status == "init":
            self._set_status("正在连接 wiki 并扫描文件夹…")
            return
        if total:
            self.progress_bar.setValue(int(i / total * 100))
        if status == "uploading":
            self._set_status(f"[{i}/{total}] 正在上传：{name}")
        elif status == "ok":
            self._set_status(f"[{i}/{total}] 已上传：{name}")
        elif status == "queued":
            self._set_status(f"[{i}/{total}] 已提交审核：{name}")
        else:
            self._set_status(f"[{i}/{total}] 失败：{name}（{detail}）", error=True)

    @Slot(object)
    def _on_single_done(self, payload):
        self._set_busy(False)
        ok, result = payload
        if ok:
            status = result.get("status")
            if status == "queued":
                self._set_status(
                    f"已提交审核：{result['filename']}\n"
                    f"文件已进入审核队列，审核通过后即可对其他用户可见。",
                    error=False)
            elif status == "ok":
                self._set_status(f"上传成功：{result['filename']}", error=False)
            else:
                self._set_status(
                    f"上传未成功：{result.get('filename', '')}\n{result.get('detail', '')}",
                    error=True)
        else:
            self._set_status(f"上传失败：{result}", error=True)

    @Slot(object)
    def _on_batch_done(self, payload):
        self._set_busy(False)
        ok, result = payload
        if ok:
            done, queued, failed, total = result
            msg = (f"批量上传完成：成功 {done} 个，已提交审核 {queued} 个，"
                   f"失败 {failed} 个，共 {total} 个。")
            self._set_status(msg, error=(failed > 0))
        else:
            self._set_status(f"批量上传失败：{result}", error=True)

    def closeEvent(self, event):
        if self._busy:
            self._set_status("正在上传中，请等待完成后再关闭窗口。", error=True)
            event.ignore()
        else:
            super().closeEvent(event)


# 入口

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BFUA")
    app.setApplicationDisplayName("BFUA — Bwiki File Upload Assistant")
    app.setOrganizationName("BFUA")
    app.setStyle("Fusion")

    if MwSite is None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            None, "缺少依赖",
            "未安装 mwclient，请先运行：\npip install mwclient",
        )

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
