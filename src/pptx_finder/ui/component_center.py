"""组件下载中心（v2.0.1）：照「软件更新」弹窗的样子下载文字识别 / 公式识别组件。

- 整个程序只有一个 ComponentCenter（component_center() 取），下载在后台线程跑，
  关掉窗口也继续；设置页、转字窗口、截图识字/公式窗口看到的是同一份状态。
- 窗口显示：要下载多少、正在用哪个源、进度 / 速度 / 剩余时间、各源结果（失败写人话原因，
  被公司代理拦截会写明），可取消、可重试；全部失败时给「打开发布页」改装完整版的出路。
- 下载完成后回调发起方（例如截图识字窗口自动开始识别）。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .. import components
from ..netfetch import fmt_bytes, fmt_eta
from .update_dialog import SourceList, _palette, _qss, _repolish, _tokens, attempt_rows, check_rows

log = logging.getLogger(__name__)

RELEASE_PAGE = "https://github.com/TianLin0509/PPTutor/releases/latest"

#: idle 未开始 / checking 查询各源 / downloading 下载中 / installing 校验并解压 /
#: done 已就绪 / cancelled 已取消 / error 失败
PHASES = ("idle", "checking", "downloading", "installing", "done", "cancelled", "error")


def _blank_state() -> dict:
    return {"phase": "idle", "status": {}, "attempts": [], "checks": [], "error": "",
            "version": "", "bytes": 0, "source_names": [], "ready_source": "", "local": False}


class ComponentCenter(QObject):
    state_changed = Signal(str)        # key
    installed = Signal(str)            # key：下载并安装成功
    _update = Signal(str, object)        # 工作线程 → 主线程

    def __init__(self, parent=None):
        super().__init__(parent)
        self._states = {c.key: _blank_state() for c in components.ALL}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._callbacks: dict[str, list] = {c.key: [] for c in components.ALL}
        self._dialogs: dict[str, ComponentDialog] = {}
        self._update.connect(self._apply)

    # ---------- 状态 ----------
    def snapshot(self, key: str) -> dict:
        components.info(key)
        return dict(self._states[key])

    def busy(self, key: str) -> bool:
        return self._states[key]["phase"] in ("checking", "downloading", "installing")

    def _apply(self, key: str, change: dict) -> None:
        state = self._states[key]
        callbacks = []
        if change.get("phase") == "done":
            callbacks, self._callbacks[key] = self._callbacks[key], []
        elif change.get("phase") in ("error", "cancelled"):
            self._callbacks[key] = []
        state.update(change)
        self.state_changed.emit(key)
        if change.get("phase") == "done":
            self.installed.emit(key)
            for cb in callbacks:
                try:
                    cb()
                except Exception:  # noqa: BLE001 发起方窗口可能已关，不能拖垮下载中心
                    log.exception("组件就绪回调失败")

    # ---------- 动作 ----------
    def start(self, key: str) -> None:
        """查询各源 → 下载 → 校验解压。已在进行中则什么也不做。"""
        components.info(key)
        if self.busy(key):
            return
        local = components.bundled(key) and key == "formula"
        cancel = threading.Event()
        self._cancel[key] = cancel
        fresh = _blank_state()
        fresh.update(phase="installing" if local else "checking", local=local,
                     source_names=["安装包内置"] if local else components.source_names(key))
        self._states[key] = fresh
        self.state_changed.emit(key)
        thread = threading.Thread(target=self._work, args=(key, cancel, local),
                                  name=f"component-{key}", daemon=True)
        self._threads[key] = thread
        thread.start()

    def _work(self, key: str, cancel: threading.Event, local: bool) -> None:
        emit = self._update.emit
        manifest = None
        if not local:
            try:
                manifest = components.fetch_manifest(key)
            except Exception as exc:  # noqa: BLE001 原因带回界面
                checks = getattr(exc, "checks", None) or []
                emit(key, {"phase": "error", "error": _reason(exc), "checks": checks})
                return
            if cancel.is_set():
                emit(key, {"phase": "cancelled"})
                return
            archive = manifest.get("archive") or {}
            emit(key, {"phase": "downloading", "checks": manifest.get("_checks") or [],
                       "version": str(manifest.get("version") or ""),
                       "bytes": int(archive.get("size") or 0),
                       "source_names": [components_name(label) for label, _b in manifest.get("_bases") or []]
                       or self._states[key]["source_names"]})

        total_holder = {"installing": local}
        last_attempts: list = []    # 工作线程自己记一份：主线程那份要等排队的信号处理完才更新

        def on_status(st: dict) -> None:
            if "attempts" in st:
                last_attempts[:] = list(st["attempts"])
                emit(key, {"attempts": list(st["attempts"])})
                return
            change: dict = {"status": dict(st)}
            total = st.get("total") or 0
            if not local and total and st.get("received", 0) >= total and not total_holder["installing"]:
                total_holder["installing"] = True
                change["phase"] = "installing"
            emit(key, change)

        try:
            version = components.install(key, manifest, status=on_status, cancel=cancel)
        except InterruptedError:
            emit(key, {"phase": "cancelled"})
            return
        except Exception as exc:  # noqa: BLE001
            log.info("%s安装失败：%s", components.info(key).name, exc)
            emit(key, {"phase": "error", "error": _reason(exc)})
            return
        if cancel.is_set():
            emit(key, {"phase": "cancelled"})
            return
        ok = next((a.get("name", "") for a in last_attempts if a.get("status") == "ok"), "")
        emit(key, {"phase": "done", "version": version or "",
                   "ready_source": "安装包内置" if local else ok})

    def cancel(self, key: str) -> None:
        event = self._cancel.get(key)
        if event is not None:
            event.set()

    def request(self, key: str, parent=None, on_ready=None, *, autostart: bool = True):
        """功能入口用：打开下载窗口并开始下载；装好后调用 on_ready()。"""
        components.info(key)
        if on_ready is not None:
            self._callbacks[key].append(on_ready)
        if autostart and not self.busy(key) and not components.is_installed(key):
            self.start(key)
        elif components.is_installed(key) and not self.busy(key):
            callbacks, self._callbacks[key] = self._callbacks[key], []
            for cb in callbacks:
                cb()
            return None
        return self.open_dialog(key, parent)

    def open_dialog(self, key: str, parent=None):
        dlg = self._dialogs.get(key)
        try:
            if dlg is not None and dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return dlg
        except RuntimeError:
            pass
        dlg = ComponentDialog(self, key, parent)
        self._dialogs[key] = dlg
        dlg.destroyed.connect(lambda *_a, k=key: self._dialogs.pop(k, None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def wait(self, key: str, timeout: float | None = None) -> None:
        """测试与退出用：等后台线程结束。"""
        thread = self._threads.get(key)
        if thread is not None:
            thread.join(timeout)


def components_name(label: str) -> str:
    from ..netfetch import source_name
    return source_name(label)


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


_center: ComponentCenter | None = None


def component_center() -> ComponentCenter:
    """整个程序共用一个下载中心（挂在 QApplication 上，窗口关了也继续下载）。"""
    global _center
    if _center is None:
        center = ComponentCenter(QApplication.instance())
        center.destroyed.connect(lambda *_a, c=center: _forget(c))
        _center = center
    return _center


def _forget(center) -> None:
    """只忘掉被销毁的那一个：旧实例延迟销毁时不能把已换上的新实例也清掉。"""
    global _center
    if _center is center:
        _center = None


def reset_for_tests() -> None:
    global _center
    if _center is not None:
        for key in list(_center._threads):
            _center.cancel(key)
            _center.wait(key, 5)
        try:
            _center.deleteLater()
        except RuntimeError:
            pass
    _center = None


class ComponentDialog(QDialog):
    def __init__(self, center: ComponentCenter, key: str, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self._center = center
        self.key = key
        self.info = components.info(key)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"{self.info.name} · PPT Doctor")
        self.setModal(False)
        self._drag_from: QPoint | None = None
        pal = _palette(_tokens(parent))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        card = QFrame()
        card.setObjectName("auCard")
        card.setStyleSheet(_qss(pal))
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(42)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(30, 24, 60, 60))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        self.card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        from ..config import resource_path
        logo = QPixmap(str(resource_path("assets", "logo.png")))
        if not logo.isNull():
            logo = logo.scaled(102, 102, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo.setDevicePixelRatio(3.0)
            icon.setPixmap(logo)
        icon.setFixedSize(34, 34)
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(f"下载{self.info.name}")
        self.title.setObjectName("auTitle")
        self.sub = QLabel(f"用于{self.info.purpose} · 装好后在本机离线识别，图片不上传")
        self.sub.setObjectName("auSub")
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        head.addLayout(titles, 1)
        close = QPushButton("×")
        close.setObjectName("auClose")
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("关闭（下载会在后台继续）")
        close.clicked.connect(self.close)
        head.addWidget(close, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.summary = QFrame()
        self.summary.setObjectName("auVersions")
        sl = QHBoxLayout(self.summary)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(18)
        self.size_box, self.size_val = self._kv("下载大小")
        self.ver_box, self.ver_val = self._kv("组件版本")
        self.where = QLabel("")
        self.where.setObjectName("auPlan")
        self.where.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.where.setWordWrap(True)
        sl.addWidget(self.size_box)
        sl.addWidget(self.ver_box)
        sl.addWidget(self.where, 1)
        lay.addWidget(self.summary)

        self.msg = QLabel("")
        self.msg.setObjectName("auMsg")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.msg)

        self.dl_head = QLabel("")
        self.dl_head.setObjectName("auSection")
        lay.addWidget(self.dl_head)
        self.progress = QProgressBar()
        self.progress.setObjectName("auProgress")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        lay.addWidget(self.progress)
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        self.meta_left = QLabel("")
        self.meta_left.setObjectName("auMeta")
        self.meta_right = QLabel("")
        self.meta_right.setObjectName("auMeta")
        meta.addWidget(self.meta_left, 1)
        meta.addWidget(self.meta_right, 0, Qt.AlignRight)
        self.meta_row = QWidget()
        self.meta_row.setLayout(meta)
        lay.addWidget(self.meta_row)

        self.src_head = QLabel("")
        self.src_head.setObjectName("auSection")
        lay.addWidget(self.src_head)
        self.sources = SourceList()
        lay.addWidget(self.sources)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.btn_manual = self._btn("打开发布页（改装完整版）", self._open_release)
        self.btn_close = self._btn("关闭", self.close)
        self.btn_cancel = self._btn("取消下载", lambda: self._center.cancel(self.key))
        self.btn_primary = self._btn("下载", self._on_primary, primary=True)
        for b in (self.btn_manual, self.btn_close, self.btn_cancel, self.btn_primary):
            foot.addWidget(b)
        lay.addSpacing(4)
        lay.addLayout(foot)

        card.setFixedWidth(544)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        center.state_changed.connect(self._on_state)
        self.refresh()

    # ---------- 小部件 ----------
    def _kv(self, label: str):
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        small = QLabel(label)
        small.setObjectName("auVerSmall")
        big = QLabel("")
        big.setObjectName("auVerBig")
        bl.addWidget(small)
        bl.addWidget(big)
        return box, big

    def _btn(self, text, slot, primary=False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("auPrimary" if primary else "auBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _set_msg(self, text: str, kind: str) -> None:
        self.msg.setText(text)
        self.msg.setProperty("kind", kind)
        _repolish(self.msg)
        self.msg.setVisible(bool(text))

    # ---------- 动作 ----------
    def _on_primary(self) -> None:
        phase = self._center.snapshot(self.key)["phase"]
        if phase == "done":
            self.close()
            return
        self._center.start(self.key)

    def _open_release(self) -> None:
        QDesktopServices.openUrl(QUrl(RELEASE_PAGE))

    def _on_state(self, key: str) -> None:
        if key == self.key:
            self.refresh()

    # ---------- 渲染 ----------
    def refresh(self) -> None:
        try:
            s = self._center.snapshot(self.key)
        except RuntimeError:
            return
        phase = s["phase"]
        installed = components.is_installed(self.key)
        if phase == "idle" and installed:
            phase = "installed"
        self.phase_rendered = phase
        size = s["bytes"] or (s["status"] or {}).get("total") or 0
        self.size_val.setText(fmt_bytes(size) if size else f"约 {self.info.approx_mb} MB")
        version = s["version"] or components.installed_version(self.key)
        self.ver_val.setText(f"v{version}" if version else "—")
        self.where.setText("安装包已内置，本地解压，不联网" if s["local"]
                           else "GitHub 优先，不通自动换备用服务器")

        st = s["status"] or {}
        moving = phase in ("downloading", "installing")
        for w in (self.dl_head, self.progress, self.meta_row):
            w.setVisible(moving)
        if moving:
            total = st.get("total") or size or 0
            received = st.get("received", 0)
            self.progress.setValue(int(min(1.0, received / total) * 1000) if total else 0)
            if phase == "installing" and not s["local"]:
                self.dl_head.setText("下载完成，正在校验并解压…")
                self.progress.setRange(0, 0)   # 解压没有细进度，用来回滚动的忙碌条
                self.meta_left.setText(fmt_bytes(total) if total else "")
                self.meta_right.setText("")
            else:
                self.progress.setRange(0, 1000)
                current = st.get("source") or next(
                    (a.get("name") for a in s["attempts"] if a.get("status") == "downloading"), "…")
                self.dl_head.setText("正在从安装包内置副本解压" if s["local"] else f"正在从「{current}」下载")
                speed = f" · {fmt_bytes(st['speed'])}/s" if st.get("speed") else ""
                self.meta_left.setText(f"{fmt_bytes(received)} / {fmt_bytes(total)}{speed}")
                self.meta_right.setText(fmt_eta(st.get("eta")).replace("剩约", "剩余约"))

        rows: list = []
        src_title = ""
        if phase == "installed":
            self._set_msg(f"{self.info.name}已安装，可以直接使用。", "ok")
        elif phase == "idle":
            self._set_msg("下载一次即可，之后完全离线。点「下载」开始。", "info")
        elif phase == "checking":
            self._set_msg("正在向各个下载源查询组件…", "info")
        elif moving:
            self._set_msg("", "info")
            if not s["local"]:
                rows = attempt_rows(s["attempts"], s["source_names"], True)
                src_title = "下载源（按顺序尝试，失败自动换下一个）"
        elif phase == "done":
            src = s["ready_source"]
            self._set_msg(f"{self.info.name}已就绪" + (f"（来自「{src}」，已校验）" if src else "")
                          + "。之后完全离线使用。", "ok")
            if s["attempts"]:
                rows, src_title = attempt_rows(s["attempts"], s["source_names"], False), "各下载源的结果"
        elif phase == "cancelled":
            self._set_msg("已取消下载，没有留下半套组件。随时可以重新下载。", "info")
        elif phase == "error":
            self._set_msg((s["error"] or "下载失败") + "\n公司网络连不上时，可以改装「完整版」安装包（已内置全部组件）。", "bad")
            if s["attempts"]:
                rows, src_title = attempt_rows(s["attempts"], s["source_names"], False), "各下载源的结果"
            elif s["checks"]:
                rows, src_title = check_rows(s["checks"]), "下载源"
        self.src_head.setText(src_title)
        self.src_head.setVisible(bool(src_title) and bool(rows))
        self.sources.set_rows(rows)

        primary = {"idle": "下载", "cancelled": "重新下载", "error": "重试", "done": "完成"}.get(phase, "")
        self.btn_primary.setText(primary)
        self.btn_primary.setVisible(bool(primary))
        self.btn_cancel.setVisible(phase in ("checking", "downloading") or (phase == "installing" and s["local"]))
        self.btn_manual.setVisible(phase == "error")
        self.btn_close.setText("后台下载" if phase in ("checking", "downloading", "installing") else "关闭")
        self.btn_close.setVisible(phase != "done")
        self.card.layout().activate()
        self.layout().activate()
        self.adjustSize()

    # ---------- 拖动（无边框卡片，按住标题区拖） ----------
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton and e.position().y() < 70:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag_from is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_from)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag_from = None
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)
