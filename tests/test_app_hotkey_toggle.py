"""Alt+F 唤起去抖：同一次按压的重复 WM_HOTKEY（含长按自动重复）合并为一次切换。

背景：v1.2.2 前，一次按压触发多次 _toggle_window（show→hide 连续翻转），
表现为窗口「一闪而过」。修复 = 0.4s 去抖 + _force_foreground 稳定前台。
"""
from __future__ import annotations

import pptx_finder.app as app


class _FakeWin:
    def __init__(self) -> None:
        self.visible = False
        self.minimized = False
        self.active = False
        self.hidden_count = 0
        self.fg_calls = 0

    def isVisible(self) -> bool:
        return self.visible

    def isMinimized(self) -> bool:
        return self.minimized

    def isActiveWindow(self) -> bool:
        return self.active

    def hide(self) -> None:
        self.visible = False
        self.active = False
        self.hidden_count += 1

    def showNormal(self) -> None:
        self.visible = True

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        self.active = True

    def winId(self) -> int:
        return 0

    def focus_search(self) -> None:
        self.fg_calls += 1


def _fg(win: _FakeWin) -> None:
    """_force_foreground 替身：模拟成功置前。"""
    win.visible = True
    win.active = True


def test_toggle_debounce_collapses_repeated_fires(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(app, "_force_foreground", _fg)
    w = _FakeWin()
    app._toggle_window(w)  # 首次按压：显示
    app._toggle_window(w)  # 同次按压的重复投递：忽略
    app._toggle_window(w)  # 长按自动重复：忽略
    assert w.fg_calls == 1
    assert w.hidden_count == 0
    assert w.visible and w.active
    clock["t"] += 0.5
    app._toggle_window(w)  # 间隔足够 → 新一次按压：隐藏
    assert w.hidden_count == 1
    assert w.fg_calls == 1
    clock["t"] += 0.5
    app._toggle_window(w)  # 再次唤起
    assert w.fg_calls == 2
    assert w.visible


def test_show_window_uses_force_foreground(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_force_foreground", lambda w: calls.append(w))
    w = _FakeWin()
    app._show_window(w)
    assert calls == [w]
