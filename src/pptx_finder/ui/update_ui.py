"""增量自动更新的 Qt 封装：后台检查线程 + 下载线程 + 标题栏 chip 状态机。

与纯逻辑层 updater.py 解耦——这里只管线程与 UI 状态流转，不含网络/文件细节。
chip 单按钮循环：发现新版 → 下载中 NN% → 就绪·重启 →（失败则）重试。全程非模态、不打断搜索。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .. import updater


def _staging_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pptutor_update"


class _CheckThread(QThread):
    found = Signal(object)  # UpdateInfo

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self._url = base_url

    def run(self) -> None:
        try:
            info = updater.check_for_update(self._url)
            if info is not None:
                self.found.emit(info)
        except Exception:  # noqa: BLE001 网络/解析失败一律静默，不打扰用户
            pass


class _DownloadThread(QThread):
    progress = Signal(int)   # 0-100
    done = Signal()
    failed = Signal(str)

    def __init__(self, base_url: str, info, staging: Path, parent=None):
        super().__init__(parent)
        self._url = base_url
        self._info = info
        self._staging = staging

    def run(self) -> None:
        try:
            shutil.rmtree(self._staging, ignore_errors=True)
            updater.download_delta(
                self._url, self._info, self._staging,
                progress=lambda d, t: self.progress.emit(int(d * 100 / t) if t else 100))
            self.done.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class UpdateController(QObject):
    """驱动标题栏更新 chip 的全生命周期。

    chip: 一个 QPushButton（main_window 放在玻璃标题栏）。
    quit_fn: 触发「真正退出」（绕过最小化到托盘）的回调，apply 时调用让 helper 接管。
    """

    def __init__(self, chip, base_url: str, quit_fn, parent=None):
        super().__init__(parent)
        self._chip = chip
        self._url = base_url
        self._quit = quit_fn
        self._state = "idle"
        self._info = None
        self._staging = _staging_dir()
        self._check: _CheckThread | None = None
        self._dl: _DownloadThread | None = None
        chip.hide()
        chip.clicked.connect(self._on_click)

    # ---------- 检查 ----------
    def start_check(self) -> None:
        if self._state != "idle":
            return
        self._check = _CheckThread(self._url, self)
        self._check.found.connect(self._on_found)
        self._check.start()

    def _on_found(self, info) -> None:
        self._info = info
        self._state = "available"
        mb = info.total_bytes / 1024 / 1024
        self._chip.setEnabled(True)
        self._chip.setText(f"🔵 新版 v{info.version} · 更新")
        tip = (info.notes + "\n" if info.notes else "") + f"约 {mb:.1f} MB · 点击下载并重启更新"
        self._chip.setToolTip(tip.strip())
        self._chip.show()

    # ---------- 点击：状态机 ----------
    def _on_click(self) -> None:
        if self._state in ("available", "error"):
            self._start_download()
        elif self._state == "ready":
            self._apply()

    def _start_download(self) -> None:
        self._state = "downloading"
        self._chip.setEnabled(False)
        self._chip.setText("下载中 0%")
        self._dl = _DownloadThread(self._url, self._info, self._staging, self)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(self._on_done)
        self._dl.failed.connect(self._on_failed)
        self._dl.start()

    def _on_progress(self, pct: int) -> None:
        self._chip.setText(f"下载中 {pct}%")

    def _on_done(self) -> None:
        self._state = "ready"
        self._chip.setEnabled(True)
        self._chip.setText("✅ 就绪 · 重启更新")
        self._chip.setToolTip("新版已下载完成，点击重启应用完成更新（你的索引和版本库不受影响）")

    def _on_failed(self, msg: str) -> None:
        self._state = "error"
        self._chip.setEnabled(True)
        self._chip.setText("⚠ 更新失败 · 重试")
        self._chip.setToolTip(f"下载失败：{msg}\n点击重试")

    def _apply(self) -> None:
        try:
            updater.apply_update(
                self._staging, updater.install_dir(), self._info,
                relaunch=Path(sys.executable).name)
        except Exception as e:  # noqa: BLE001
            self._on_failed(str(e))
            return
        self._quit()  # 退出主程序，helper 接管替换 + 重启
