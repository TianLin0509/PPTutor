"""软件更新弹窗（v2.0）：照 AI Hub 一键更新的状态与文案。

显示：当前版本 → 新版本、增量还是整包与大小、更新说明；下载时显示正在用哪个源、
进度、速度、剩余时间，以及每个源的结果（成功 / 失败 + 人话原因）；下载好后「立即重启」。
数据与动作都在 UpdateController，这里只负责按 snapshot() 画出来。

SourceList 是独立小部件，识别组件下载失败时也用它列出各源原因。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from ..netfetch import fmt_bytes, fmt_eta


def _tokens(parent) -> dict:
    tok = getattr(parent, "_tok", None)
    if isinstance(tok, dict) and tok.get("acc"):
        return tok
    from . import theme
    try:
        from ..config import get_theme
        return theme.tok(get_theme())
    except Exception:  # noqa: BLE001
        return theme.tok("atelier")


def _solid(color: str, bg: str) -> str:
    from .theme import _solid_qcolor
    try:
        return _solid_qcolor(color, bg).name()
    except Exception:  # noqa: BLE001
        return color


def _palette(tok: dict) -> dict:
    """弹窗永远浅底深字：深色主题下也用浅色卡片，保证文字清楚（田哥偏好浅底）。"""
    acc = tok.get("acc", "#0A6CFF")
    if tok.get("is_light", True):
        win = tok.get("win", "#F5F5F7")
        return {
            "card": "#FFFFFF", "sunken": _solid(tok.get("field", "#F0F0F2"), win),
            "ink1": tok.get("ink1", "#1D1D1F"), "ink2": tok.get("ink2", "#494B50"),
            "ink3": tok.get("ink3", "#6E6E73"), "bd": _solid(tok.get("bd", "#E3E3E7"), win),
            "acc": acc,
        }
    return {"card": "#FFFFFF", "sunken": "#F2F3F5", "ink1": "#1D1D1F", "ink2": "#494B50",
            "ink3": "#6E6E73", "bd": "#E3E3E7", "acc": acc}


def _qss(p: dict) -> str:
    return f"""
QFrame#auCard {{ background: {p['card']}; border: 1px solid {p['bd']}; border-radius: 14px; }}
QFrame#auCard QLabel {{ background: transparent; color: {p['ink1']}; }}
QLabel#auTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#auSub {{ font-size: 12px; color: {p['ink3']}; }}
QPushButton#auClose {{ border: none; background: transparent; color: {p['ink3']}; font-size: 17px; padding: 2px 8px; border-radius: 6px; }}
QPushButton#auClose:hover {{ background: rgba(0,0,0,0.06); color: {p['ink1']}; }}
QFrame#auVersions {{ background: {p['sunken']}; border: none; border-radius: 10px; }}
QLabel#auVerSmall {{ font-size: 11px; color: {p['ink3']}; }}
QLabel#auVerBig {{ font-size: 17px; font-weight: 700; }}
QLabel#auArrow {{ font-size: 18px; color: {p['acc']}; }}
QLabel#auPlan {{ font-size: 12px; color: {p['ink3']}; }}
QLabel#auSection {{ font-size: 12px; font-weight: 700; color: {p['ink3']}; }}
QLabel#auNotes {{ font-size: 13px; color: {p['ink2']}; }}
QLabel#auMsg {{ border-radius: 10px; padding: 9px 12px; font-size: 12.5px; }}
QLabel#auMsg[kind="ok"] {{ background: #ECF7F0; color: #24704A; }}
QLabel#auMsg[kind="bad"] {{ background: #FBEFEC; color: #9B3A24; }}
QLabel#auMsg[kind="info"] {{ background: {p['sunken']}; color: {p['ink2']}; }}
QProgressBar#auProgress {{ background: {p['sunken']}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }}
QProgressBar#auProgress::chunk {{ background: {p['acc']}; border-radius: 4px; }}
QLabel#auMeta {{ font-size: 12px; color: {p['ink3']}; }}
QFrame#auSources {{ background: transparent; border: 1px solid {p['bd']}; border-radius: 10px; }}
QLabel#auSrcName {{ font-size: 12px; font-weight: 700; }}
QLabel#auSrcText {{ font-size: 12px; color: {p['ink3']}; }}
QLabel#auSrcText[st="ok"] {{ color: #2F8A57; }}
QLabel#auSrcText[st="failed"] {{ color: #B4472F; }}
QLabel#auSrcText[st="downloading"] {{ color: {p['acc']}; }}
QFrame#auSrcSep {{ background: {p['bd']}; border: none; max-height: 1px; min-height: 1px; }}
QPushButton#auBtn {{ background: {p['card']}; color: {p['ink1']}; border: 1px solid {p['bd']}; border-radius: 8px; padding: 7px 16px; font-size: 13px; }}
QPushButton#auBtn:hover {{ background: {p['sunken']}; }}
QPushButton#auPrimary {{ background: {p['acc']}; color: #FFFFFF; border: 1px solid {p['acc']}; border-radius: 8px; padding: 7px 18px; font-size: 13px; font-weight: 700; }}
QPushButton#auPrimary:hover {{ background: {p['acc']}; border-color: {p['acc']}; }}
QPushButton:disabled {{ color: {p['ink3']}; }}
"""


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


class SourceList(QFrame):
    """各源结果列表：「GitHub　下载完成并校验通过 · 7.2 MB · 3.1 秒」「备用服务器　失败：…」。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("auSources")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 2, 0, 2)
        self._lay.setSpacing(0)
        self.rows: list[tuple[str, str, str]] = []

    def set_rows(self, rows: list[tuple[str, str, str]]) -> None:
        """rows: [(源名, 状态 ok/failed/downloading/idle, 说明文字)]"""
        self.rows = list(rows)
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # 立刻摘掉：只 deleteLater 的话，旧行在本轮事件循环里还画在原处，和新行叠在一起
                w.hide()
                w.setParent(None)
                w.deleteLater()
        for i, (name, st, text) in enumerate(self.rows):
            if i:
                sep = QFrame()
                sep.setObjectName("auSrcSep")
                self._lay.addWidget(sep)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 7, 12, 7)
            rl.setSpacing(10)
            n = QLabel(name)
            n.setObjectName("auSrcName")
            n.setFixedWidth(78)
            t = QLabel(text)
            t.setObjectName("auSrcText")
            t.setProperty("st", st)
            t.setWordWrap(True)
            t.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rl.addWidget(n, 0, Qt.AlignTop)
            rl.addWidget(t, 1)
            self._lay.addWidget(row)
        self.setVisible(bool(self.rows))


def attempt_rows(attempts: list, source_names: list, downloading: bool) -> list:
    by_name = {a.get("name"): a for a in attempts or []}
    names = list(source_names or []) or [a.get("name", "") for a in attempts or []]
    for a in attempts or []:
        if a.get("name") not in names:
            names.append(a.get("name"))
    rows = []
    for name in names:
        a = by_name.get(name)
        if a is None:
            rows.append((name, "idle", "排队中（前一个源失败才会用）" if downloading else "未使用"))
            continue
        st = a.get("status", "")
        if st == "ok":
            text = f"下载完成并校验通过 · {fmt_bytes(a.get('bytes', 0))} · {a.get('ms', 0) / 1000:.1f} 秒"
        elif st == "downloading":
            text = "正在下载…"
        elif st == "cancelled":
            text = "已取消"
        else:
            text = f"失败：{a.get('reason') or '原因未知'}"
        rows.append((name, st, text))
    return rows


def check_rows(checks: list) -> list:
    return [(c.get("name", ""), "ok" if c.get("ok") else "failed",
             f"可用，最新 v{c.get('version')}" if c.get("ok") else f"连不上：{c.get('reason') or '原因未知'}")
            for c in checks or []]


class UpdateDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self._ctl = controller
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("软件更新 · PPT Doctor")
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
        app_icon = parent.windowIcon() if parent is not None else QIcon()
        if not app_icon.isNull():
            icon.setPixmap(app_icon.pixmap(34, 34))
        icon.setFixedSize(34, 34)
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("软件更新")
        self.title.setObjectName("auTitle")
        self.sub = QLabel(self._subtitle())
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
        self._head_widgets = (icon, self.title, self.sub)

        self.versions = QFrame()
        self.versions.setObjectName("auVersions")
        vl = QHBoxLayout(self.versions)
        vl.setContentsMargins(14, 10, 14, 10)
        vl.setSpacing(12)
        self.cur_box, self.cur_ver = self._ver_block("当前版本")
        self.arrow = QLabel("→")
        self.arrow.setObjectName("auArrow")
        self.new_box, self.new_ver = self._ver_block("新版本")
        self.plan = QLabel("")
        self.plan.setObjectName("auPlan")
        self.plan.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.plan.setWordWrap(True)
        vl.addWidget(self.cur_box)
        vl.addWidget(self.arrow)
        vl.addWidget(self.new_box)
        vl.addWidget(self.plan, 1)
        lay.addWidget(self.versions)

        self.msg = QLabel("")
        self.msg.setObjectName("auMsg")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.msg)

        self.notes_head = QLabel("本次更新")
        self.notes_head.setObjectName("auSection")
        self.notes = QLabel("")
        self.notes.setObjectName("auNotes")
        self.notes.setWordWrap(True)
        lay.addWidget(self.notes_head)
        lay.addWidget(self.notes)

        self.dl_head = QLabel("")
        self.dl_head.setObjectName("auSection")
        lay.addWidget(self.dl_head)
        self.progress = QProgressBar()
        self.progress.setObjectName("auProgress")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        lay.addWidget(self.progress)
        meta = QHBoxLayout()
        self.meta_left = QLabel("")
        self.meta_left.setObjectName("auMeta")
        self.meta_right = QLabel("")
        self.meta_right.setObjectName("auMeta")
        meta.addWidget(self.meta_left, 1)
        meta.addWidget(self.meta_right, 0, Qt.AlignRight)
        self.meta_row = QWidget()
        self.meta_row.setLayout(meta)
        meta.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.meta_row)

        self.src_head = QLabel("")
        self.src_head.setObjectName("auSection")
        lay.addWidget(self.src_head)
        self.sources = SourceList()
        lay.addWidget(self.sources)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.btn_manual = self._btn("打开发布页手动下载", self._open_release)
        self.btn_close = self._btn("稍后", self.close)
        self.btn_cancel = self._btn("取消下载", self._ctl.cancel_download)
        self.btn_primary = self._btn("下载更新", self._on_primary, primary=True)
        for b in (self.btn_manual, self.btn_close, self.btn_cancel, self.btn_primary):
            foot.addWidget(b)
        lay.addSpacing(4)
        lay.addLayout(foot)

        # 宽度固定、高度跟内容走：各状态内容多少差很多，不能沿用上一个状态的高度
        card.setFixedWidth(544)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        self._ctl.state_changed.connect(self.refresh)
        self.refresh()

    # ---------- 小部件 ----------
    def _ver_block(self, label: str):
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

    @staticmethod
    def _subtitle() -> str:
        try:
            import sys
            if getattr(sys, "frozen", False):
                from ..editions import LABELS, current_edition
                return f"PPT Doctor · {LABELS[current_edition()]}"
        except Exception:  # noqa: BLE001
            pass
        return "PPT Doctor"

    def _set_msg(self, text: str, kind: str) -> None:
        self.msg.setText(text)
        self.msg.setProperty("kind", kind)
        _repolish(self.msg)
        self.msg.setVisible(bool(text))

    # ---------- 动作 ----------
    def _on_primary(self) -> None:
        phase = self._ctl.phase()
        if phase in ("available",):
            self._ctl.start_download()
        elif phase == "ready":
            self._ctl.apply()
        elif phase == "error" and self._ctl._info is not None:
            self._ctl.start_download()
        else:
            self._ctl.check_now()

    def _open_release(self) -> None:
        QDesktopServices.openUrl(QUrl(self._ctl.snapshot().get("release_page", "")))

    # ---------- 渲染 ----------
    def refresh(self) -> None:
        try:
            s = self._ctl.snapshot()
        except RuntimeError:
            return
        phase = s["phase"]
        self.phase_rendered = phase
        latest = s["latest"]
        self.cur_ver.setText(f"v{s['current']}" if s["current"] else "—")
        has_new = bool(latest) and phase in ("available", "downloading", "ready", "applying", "error")
        for w in (self.arrow, self.new_box, self.plan):
            w.setVisible(has_new)
        self.new_ver.setText(f"v{latest}" if latest else "")
        if has_new:
            kind = "增量更新" if s["kind"] != "package" else "完整安装包"
            files = f"只下载变化的 {s['files']} 个文件" if s["kind"] != "package" and s["files"] else ""
            self.plan.setText(f"<b>{kind} · {fmt_bytes(s['bytes'])}</b><br>{files}")

        notes_lines = [ln.strip(" -•\t") for ln in str(s["notes"] or "").splitlines() if ln.strip()]
        show_notes = phase == "available" and bool(notes_lines)
        self.notes_head.setVisible(show_notes)
        self.notes.setVisible(show_notes)
        self.notes.setText("\n".join("· " + ln for ln in notes_lines[:12]))

        downloading = phase == "downloading"
        st = s["status"] or {}
        for w in (self.dl_head, self.progress, self.meta_row):
            w.setVisible(downloading)
        if downloading:
            current = st.get("source") or next(
                (a.get("name") for a in s["attempts"] if a.get("status") == "downloading"), "…")
            self.dl_head.setText(f"正在从「{current}」下载")
            total = st.get("total") or s["bytes"] or 0
            received = st.get("received", 0)
            self.progress.setValue(int(min(1.0, received / total) * 1000) if total else 0)
            speed = f" · {fmt_bytes(st['speed'])}/s" if st.get("speed") else ""
            self.meta_left.setText(f"{fmt_bytes(received)} / {fmt_bytes(total)}{speed}")
            self.meta_right.setText(fmt_eta(st.get("eta")).replace("剩约", "剩余约"))

        rows: list = []
        src_title = ""
        if phase == "none":
            self._set_msg(f"已是最新版本 v{s['current']}。", "ok")
            rows, src_title = check_rows(s["checks"]), "更新源"
        elif phase == "checking":
            self._set_msg("正在向各个更新源查询最新版本…", "info")
        elif phase == "available":
            self._set_msg("", "info")
            if s["attempts"]:
                rows = attempt_rows(s["attempts"], s["source_names"], False)
            else:
                rows = check_rows(s["checks"])
            src_title = "更新源（按顺序尝试，失败自动换下一个）"
        elif downloading:
            self._set_msg("", "info")
            rows = attempt_rows(s["attempts"], s["source_names"], True)
        elif phase == "ready":
            self._set_msg(f"新版本已从「{s['ready_source'] or '更新源'}」下载并校验通过。"
                          "点「立即重启」后约 5 秒完成替换并自动重新打开；替换失败会自动恢复旧版本。",
                          "ok")
            rows = attempt_rows(s["attempts"], s["source_names"], False)
        elif phase == "applying":
            self._set_msg("正在关闭并替换程序文件，完成后会自动重新打开…", "info")
        elif phase == "error":
            self._set_msg(s["error"] or "更新失败", "bad")
            if s["attempts"]:
                rows, src_title = attempt_rows(s["attempts"], s["source_names"], False), "各下载源的结果"
            else:
                rows, src_title = check_rows(s["checks"]), "更新源"
        else:
            self._set_msg("点「检查更新」查询最新版本。", "info")
        self.src_head.setText(src_title)
        self.src_head.setVisible(bool(src_title) and bool(rows))
        self.sources.set_rows(rows)

        primary = {"available": "下载更新", "ready": "立即重启", "none": "重新检查",
                   "idle": "检查更新", "error": "重试"}.get(phase, "")
        self.btn_primary.setText(primary)
        self.btn_primary.setVisible(bool(primary))
        self.btn_cancel.setVisible(downloading)
        self.btn_manual.setVisible(phase == "error")
        self.btn_close.setText({"none": "好的", "ready": "稍后", "available": "稍后"}.get(phase, "关闭"))
        self.btn_close.setVisible(phase != "applying")
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
