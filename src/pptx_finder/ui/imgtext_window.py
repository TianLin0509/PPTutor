"""图片转可编辑文字：拖进来一张图，出一页文字可编辑的 PPTX。

刻意不做校对台：产出本身就是可编辑 PPTX，改错字在 PowerPoint 里做最自然，
再造一个编辑器只是重复造轮子。（早期设想过「自动标出可疑处让你只看那几行」，
实测行不通——OCR 的错误高度稳定，多趟比对根本抓不住，见 CHANGELOG。）
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import actions, config, imgtext, imgtext_ocr
from .bg_task import BackgroundTask

ACCEPTED = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
COMPONENT_HINT_MB = 65
# 粘贴进来的图先落盘（识别管线吃的是路径，不是内存里的 QImage）。放缓存目录下，
# 那里本来就被索引扫描排除，不会把临时图混进搜索结果。
PASTE_SUBDIR = "imgtext-paste"
PASTE_KEEP = 8          # 只留最近这么多张，别让截图在缓存里无限堆积


def paste_dir() -> Path:
    p = config.cache_dir() / PASTE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def prune_paste_dir(keep: int = PASTE_KEEP) -> int:
    """保留最近 keep 张，其余删掉。返回删除数量。"""
    try:
        shots = sorted(paste_dir().glob("*.png"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return 0
    removed = 0
    for old in shots[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class ImgTextWindow(QWidget):
    """一张图 -> 一页 PPTX。识别组件按需下载，没装时只提示、不阻塞窗口打开。"""

    def __init__(self, tok: dict, parent=None, *, source: str = ""):
        qt_parent = parent if isinstance(parent, QWidget) else None
        super().__init__(qt_parent)
        self._tok = tok or {}
        self._source = ""
        self._source_is_pasted = False
        self._result_path = ""
        self._closing = False
        self._busy = False
        self._cancel_download = False
        self._tasks: list[BackgroundTask] = []

        self.setObjectName("imgTextWin")
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("图片转可编辑文字 · PPT Doctor")
        self.setAcceptDrops(True)
        self.resize(720, 640)
        self._build()
        # 截图工具出来的图只在剪贴板里，逼用户先存一次盘再拖进来是多余的一步。
        self._paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self)
        self._paste_sc.activated.connect(self.paste_from_clipboard)
        self._refresh_component_state()
        if source:
            self.set_source(source)

    # ---------- 组装 ----------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("图片转可编辑文字")
        title.setObjectName("dashTitle")
        root.addWidget(title)
        note = QLabel("把 PPT 效果图拖进来，图里的文字会变成真正的文本框；"
                      "图形、配色、版式原样保留在背景里。全程离线。")
        note.setObjectName("emptyMeta")
        note.setWordWrap(True)
        root.addWidget(note)

        self._component_box = QFrame()
        self._component_box.setObjectName("card")
        box = QHBoxLayout(self._component_box)
        box.setContentsMargins(12, 10, 12, 10)
        self._component_label = QLabel("")
        self._component_label.setWordWrap(True)
        box.addWidget(self._component_label, 1)
        self._component_btn = QPushButton(f"下载识别组件（约 {COMPONENT_HINT_MB} MB）")
        self._component_btn.setObjectName("primary")
        self._component_btn.clicked.connect(self._install_component)
        box.addWidget(self._component_btn, 0)
        root.addWidget(self._component_box)

        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.hide()
        root.addWidget(self._progress)

        self._drop = QLabel("把图片拖到这里，或按 Ctrl+V 粘贴截图，或点「选择图片」")
        self._drop.setObjectName("previewImage")
        self._drop.setAlignment(Qt.AlignCenter)
        self._drop.setMinimumHeight(280)
        root.addWidget(self._drop, 1)

        self._status = QLabel("")
        self._status.setObjectName("emptyMeta")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        row = QHBoxLayout()
        self._pick_btn = QPushButton("选择图片…")
        self._pick_btn.clicked.connect(self._pick)
        row.addWidget(self._pick_btn)
        # 快捷键再方便也得看得见，否则和「找不到入口」是同一个问题。
        self._paste_btn = QPushButton("粘贴剪贴板")
        self._paste_btn.setToolTip("Ctrl+V：直接粘贴剪贴板里的截图")
        self._paste_btn.clicked.connect(self.paste_from_clipboard)
        row.addWidget(self._paste_btn)
        self._convert_btn = QPushButton("转换为可编辑 PPTX")
        self._convert_btn.setObjectName("primary")
        self._convert_btn.setEnabled(False)
        self._convert_btn.clicked.connect(self._convert)
        row.addWidget(self._convert_btn)
        row.addStretch(1)
        self._open_btn = QPushButton("打开结果")
        self._open_btn.setEnabled(False)
        self._open_btn.clicked.connect(self._open_result)
        row.addWidget(self._open_btn)
        self._folder_btn = QPushButton("打开所在文件夹")
        self._folder_btn.setEnabled(False)
        self._folder_btn.clicked.connect(self._open_folder)
        row.addWidget(self._folder_btn)
        root.addLayout(row)

    # ---------- 组件状态 ----------
    def _refresh_component_state(self) -> None:
        ready = imgtext_ocr.is_installed()
        self._component_box.setVisible(not ready)
        if not ready:
            self._component_label.setText(
                "还需要一次性下载识别组件（离线识别模型）。装好之后完全离线，不再联网。")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        ready = imgtext_ocr.is_installed()
        self._convert_btn.setEnabled(bool(self._source) and ready and not self._busy)
        self._pick_btn.setEnabled(not self._busy)
        self._paste_btn.setEnabled(not self._busy)

    def _install_component(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._cancel_download = False
        self._component_btn.setEnabled(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("正在下载识别组件… %p%")
        self._progress.show()

        def work():
            def on_progress(done, total):
                pct = int(done * 100 / max(1, total))
                self._progress.setValue(pct)
            return imgtext_ocr.install(progress=on_progress,
                                       cancel=lambda: self._cancel_download or self._closing)

        self._start(work, self._on_component_installed, "imgtext-install")

    def _on_component_installed(self, result: object) -> None:
        self._busy = False
        self._progress.hide()
        self._component_btn.setEnabled(True)
        if isinstance(result, str) and result:
            self._status.setText(f"识别组件已就绪（版本 {result}），之后完全离线。")
        else:
            self._status.setText("识别组件下载失败；稍后重试，或在健康诊断里查看原因。")
        self._refresh_component_state()

    # ---------- 选图 ----------
    def set_source(self, path: str, *, pasted: bool = False) -> None:
        path = os.path.abspath(path)
        if os.path.splitext(path)[1].lower() not in ACCEPTED:
            self._status.setText("只支持 PNG / JPG / BMP / WEBP 图片。")
            return
        self._source = path
        self._source_is_pasted = pasted
        self._result_path = ""
        self._open_btn.setEnabled(False)
        self._folder_btn.setEnabled(False)
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self._drop.setPixmap(pixmap.scaled(
                self._drop.width() or 640, self._drop.height() or 280,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        label = "剪贴板图片" if pasted else os.path.basename(path)
        self._status.setText(f"{label} · {pixmap.width()}×{pixmap.height()}")
        self._sync_buttons()

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.set_source(path)

    # ---------- 剪贴板 ----------
    def paste_from_clipboard(self) -> bool:
        """Ctrl+V：截图工具/浏览器复制的图直接进来，不用先存成文件。

        剪贴板里可能是三种东西，按「最像用户意图」的顺序取：先看有没有复制的
        图片文件（Explorer 里 Ctrl+C 一张 png），再看有没有位图数据（截图工具、
        网页里「复制图片」）。两者都没有就明说，别静默无反应。
        """
        if self._busy:
            self._status.setText("正在忙，稍后再粘贴。")
            return False
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self._status.setText("读不到剪贴板。")
            return False
        mime = clipboard.mimeData()

        if mime is not None and mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if local and local.lower().endswith(ACCEPTED) and os.path.exists(local):
                    self.set_source(local)
                    return True

        image = clipboard.image()
        if image is None or image.isNull():
            self._status.setText(
                "剪贴板里没有图片。先用截图工具截一张（Win+Shift+S），或复制一个图片文件。")
            return False

        saved = self._save_pasted_image(image)
        if not saved:
            self._status.setText("剪贴板图片存盘失败，换「选择图片」试试。")
            return False
        self.set_source(saved, pasted=True)
        return True

    def _save_pasted_image(self, image) -> str:
        """存成 PNG（无损）。OCR 吃的是路径，且再压一次 JPEG 只会伤识别率。"""
        try:
            root = paste_dir()
            stem = time.strftime("粘贴-%Y%m%d-%H%M%S")
            target = root / f"{stem}.png"
            n = 1
            while target.exists():          # 同一秒内连粘两次
                target = root / f"{stem}-{n}.png"
                n += 1
            if not image.save(str(target), "PNG"):
                return ""
            prune_paste_dir()
            return str(target)
        except (OSError, ValueError):
            return ""

    def _default_save_target(self) -> str:
        """粘贴来的图没有「原图所在目录」可用，别把结果丢进缓存目录。"""
        if not self._source_is_pasted:
            return os.path.splitext(self._source)[0] + "_可编辑.pptx"
        desktop = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation) or str(Path.home())
        stem = os.path.splitext(os.path.basename(self._source))[0]
        return os.path.normpath(os.path.join(desktop, f"{stem}_可编辑.pptx"))

    def dragEnterEvent(self, event):  # noqa: N802
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(u.toLocalFile().lower().endswith(ACCEPTED) for u in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local.lower().endswith(ACCEPTED):
                self.set_source(local)
                event.acceptProposedAction()
                return

    # ---------- 转换 ----------
    def _convert(self) -> None:
        if self._busy or not self._source:
            return
        default = self._default_save_target()
        dest, _ = QFileDialog.getSaveFileName(
            self, "另存为", default, "PowerPoint (*.pptx)")
        if not dest:
            return
        if not dest.lower().endswith(".pptx"):
            dest += ".pptx"
        self._busy = True
        self._sync_buttons()
        self._convert_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setFormat("正在识别与排版…")
        self._progress.show()
        self._status.setText("正在识别…（首次调用要加载模型，约几秒）")
        source, target = self._source, dest

        def work():
            rows = imgtext_ocr.recognize_one(source)
            result = imgtext.convert(source, target, rows)
            return {"dest": target, "result": result}

        self._start(work, self._on_converted, "imgtext-convert")

    def _on_converted(self, payload: object) -> None:
        self._busy = False
        self._progress.hide()
        self._sync_buttons()
        if not isinstance(payload, dict):
            self._status.setText(
                "转换失败。若提示识别组件不可用，请先下载组件；"
                "其余原因可在设置的健康诊断里查看日志。")
            return
        result = payload["result"]
        self._result_path = payload["dest"]
        self._open_btn.setEnabled(True)
        self._folder_btn.setEnabled(True)
        # 跳过的都原样留在背景图里。如实说清去向，别让人以为内容丢了。
        extra = ""
        if result.skipped_small:
            extra += f"；{len(result.skipped_small)} 处小字（图表刻度之类）保留在背景里"
        if result.skipped_shape:
            extra += f"；{len(result.skipped_shape)} 处疑似图形未改动"
        if result.skipped_busy:
            extra += f"；{len(result.skipped_busy)} 处压在照片上的文字保留原样（改了会糊掉照片）"
        if result.skipped_formula:
            extra += f"；{len(result.skipped_formula)} 处公式保留原样（上下标排不出来，动了反而更差）"
        # 一段多行会合成一个文本框，所以框数不等于行数——两个都报，免得用户
        # 数着行数以为少了。
        lines = "" if len(result.runs) == len(result.blocks) else f"（共 {len(result.runs)} 行）"
        self._status.setText(
            f"已生成 {len(result.blocks)} 个可编辑文本框{lines}{extra}。"
            f"\n{self._result_path}")

    def _open_result(self) -> None:
        if self._result_path:
            actions.open_file(self._result_path)

    def _open_folder(self) -> None:
        if self._result_path:
            actions.open_folder(self._result_path)

    # ---------- 后台任务 ----------
    def _start(self, fn, done, label: str) -> None:
        task = BackgroundTask(fn, label, self)
        self._tasks.append(task)
        task.done.connect(lambda payload: None if self._closing else done(payload))
        task.finished.connect(lambda t=task: self._tasks.remove(t) if t in self._tasks else None)
        task.start()

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        self._cancel_download = True
        super().closeEvent(event)
