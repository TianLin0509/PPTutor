"""v2.0 更新中心界面：弹窗各状态、同一版本只弹一次、未读红点、已读时机。

设置 PPTDOCTOR_UPDATE_SHOTS=<目录> 时顺手把各状态截图存下来（隐藏桌面上跑，供人工看）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QPushButton, QWidget

from pptx_finder import config, updater
from pptx_finder.ui import update_ui
from pptx_finder.ui.update_dialog import UpdateDialog, attempt_rows, check_rows
from pptx_finder.ui.update_ui import UpdateController

SHOTS = os.environ.get("PPTDOCTOR_UPDATE_SHOTS", "")


def _shot(dialog, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    dialog.show()
    dialog.layout().activate()
    dialog.adjustSize()
    dialog.grab().save(str(Path(SHOTS) / f"{name}.png"))


def _info(version="2.0.0", n=2, size=7_340_032):
    info = updater.UpdateInfo(
        version=version, notes="修复窗口偶发拖不动\n- 全新白色界面\n- 更新支持多源自动切换",
        changed=[(f"f{i}", "a" * 64, size // n) for i in range(n)], deleted=[])
    gh = updater.UpdateSource("github-release", "m", "b")
    bk = updater.UpdateSource("self-hosted", "m2", "b2")
    info.sources = [gh, bk]
    info.source = gh
    return info


def _report(info, current="1.6.12", ok=(True, True)):
    checks = [updater.SourceCheck("github-release", "GitHub", ok[0], "2.0.0" if ok[0] else "",
                                  "" if ok[0] else "连接超时（服务器长时间没有响应）"),
              updater.SourceCheck("self-hosted", "备用服务器", ok[1], "2.0.0" if ok[1] else "",
                                  "" if ok[1] else "收到的是网页（3.5 KB）而不是版本清单，可能被公司代理拦截")]
    return updater.CheckReport(best=info, checks=checks, current=current)


@pytest.fixture
def fresh_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _ctl(qtbot, parent=None):
    chip = QPushButton()
    qtbot.addWidget(chip)
    return UpdateController(chip, "http://127.0.0.1:9", lambda: None, parent), chip


def test_row_builders():
    rows = attempt_rows([{"name": "GitHub", "status": "failed", "reason": "连接超时"},
                         {"name": "备用服务器", "status": "ok", "bytes": 7_340_032, "ms": 3100}],
                        ["GitHub", "备用服务器"], False)
    assert rows[0] == ("GitHub", "failed", "失败：连接超时")
    assert rows[1][1] == "ok" and "7.0 MB" in rows[1][2] and "3.1 秒" in rows[1][2]
    queued = attempt_rows([{"name": "GitHub", "status": "downloading"}], ["GitHub", "备用服务器"], True)
    assert queued[1] == ("备用服务器", "idle", "排队中（前一个源失败才会用）")
    assert check_rows([{"name": "GitHub", "ok": False, "reason": "x"}])[0][2] == "连不上：x"


def test_dialog_renders_every_phase(qtbot, fresh_settings):
    parent = QWidget()
    qtbot.addWidget(parent)
    ctl, chip = _ctl(qtbot, parent)
    info = _info()
    dlg = UpdateDialog(ctl, parent)
    qtbot.addWidget(dlg)

    # 已是最新
    ctl._report = _report(None, ok=(True, False))
    ctl._state, ctl._check_status = "idle", "ok-no-update"
    dlg.refresh()
    assert dlg.phase_rendered == "none"
    assert "已是最新版本 v1.6.12" in dlg.msg.text()
    assert [r[0] for r in dlg.sources.rows] == ["GitHub", "备用服务器"]
    assert "代理拦截" in dlg.sources.rows[1][2]
    assert dlg.btn_primary.text() == "重新检查"
    _shot(dlg, "01-已是最新")

    # 查询中
    ctl._state = "checking"
    dlg.refresh()
    assert "正在向各个更新源查询" in dlg.msg.text()
    _shot(dlg, "02-查询中")

    # 有新版本
    ctl._report = _report(info)
    ctl._info, ctl._state = info, "available"
    dlg.refresh()
    assert dlg.new_ver.text() == "v2.0.0" and dlg.cur_ver.text() == "v1.6.12"
    assert "增量更新 · 7.0 MB" in dlg.plan.text()
    assert "全新白色界面" in dlg.notes.text()
    assert dlg.btn_primary.text() == "下载更新" and dlg.btn_close.text() == "稍后"
    assert "按顺序尝试" in dlg.src_head.text()
    _shot(dlg, "03-有新版本")

    # 下载中：GitHub 失败，正在用备用服务器
    ctl._state = "downloading"
    ctl._attempts = [{"name": "GitHub", "status": "failed",
                      "reason": "收到 3.5 KB 的网页而不是更新文件，可能被公司代理拦截"},
                     {"name": "备用服务器", "status": "downloading"}]
    ctl._status = {"source": "备用服务器", "received": 3_145_728, "total": 7_340_032,
                   "speed": 2_202_009.6, "eta": 2}
    dlg.refresh()
    assert dlg.dl_head.text() == "正在从「备用服务器」下载"
    assert 400 <= dlg.progress.value() <= 450
    assert "3.0 MB / 7.0 MB · 2.1 MB/s" == dlg.meta_left.text()
    assert dlg.meta_right.text() == "剩余约 2 秒"
    assert dlg.btn_cancel.isVisibleTo(dlg)
    assert dlg.sources.rows[0][1] == "failed"
    _shot(dlg, "04-下载中-已切换备用源")

    # 已就绪
    ctl._state = "downloading"
    ctl._attempts[1] = {"name": "备用服务器", "status": "ok", "bytes": 7_340_032, "ms": 3400}
    ctl._on_done()
    dlg.refresh()
    assert "已从「备用服务器」下载并校验通过" in dlg.msg.text()
    assert dlg.btn_primary.text() == "立即重启"
    assert chip.text() == "✅ 已就绪 · 重启生效" and not chip.isHidden()
    _shot(dlg, "05-已就绪")

    # 下载失败
    ctl._state = "downloading"
    ctl._attempts = [{"name": "GitHub", "status": "failed", "reason": "连接超时（服务器长时间没有响应）"},
                     {"name": "备用服务器", "status": "failed",
                      "reason": "收到 3.5 KB 的网页而不是更新文件，可能被公司代理拦截"}]
    ctl._on_failed("AllSourcesFailed: 下载更新失败：GitHub：连接超时；备用服务器：收到 3.5 KB 的网页")
    dlg.refresh()
    assert dlg.phase_rendered == "error"
    assert dlg.msg.text().startswith("下载更新失败：GitHub")
    assert dlg.btn_manual.isVisibleTo(dlg) and dlg.btn_primary.text() == "重试"
    assert "被代理拦截" in chip.text()
    _shot(dlg, "06-下载失败")


def test_new_version_prompts_once_then_stays_unread_until_seen(qtbot, fresh_settings, monkeypatch):
    opened = []
    monkeypatch.setattr(UpdateController, "open_dialog",
                        lambda self, mark_seen=True: opened.append(mark_seen) or (
                            mark_seen and self._mark_seen()))
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    ctl, chip = _ctl(qtbot, parent)
    unread = []
    ctl.unread_changed.connect(lambda u, v: unread.append((u, v)))

    ctl._state = "checking"
    ctl._on_found(_info("2.0.0"))
    assert opened == [False]                       # 自动弹了一次，不算已读
    assert chip.isHidden()                         # 「有新版本」不占标题栏
    assert ctl.is_unread() and unread[-1] == (True, "2.0.0")
    assert config.load_ui_settings()["update_prompted_version"] == "2.0.0"

    # 重启后同一版本：不再弹，但仍未读（设置入口挂红点）
    ctl2, _ = _ctl(qtbot, parent)
    ctl2._state = "checking"
    ctl2._on_found(_info("2.0.0"))
    assert opened == [False]
    assert ctl2.is_unread()

    # 从入口点开 = 已读
    ctl2.open_dialog()
    assert not ctl2.is_unread()
    assert config.load_ui_settings()["update_seen_version"] == "2.0.0"

    # 更新的版本：再弹一次、再挂红点
    ctl3, _ = _ctl(qtbot, parent)
    ctl3._state = "checking"
    ctl3._on_found(_info("2.0.1"))
    assert opened == [False, True, False]
    assert ctl3.is_unread()


def test_clicking_download_marks_seen(qtbot, fresh_settings, monkeypatch):
    starts = []
    ctl, _chip = _ctl(qtbot)
    monkeypatch.setattr(ctl, "_start_download", lambda: starts.append(1))
    ctl._info, ctl._state = _info("2.0.0"), "available"
    assert ctl.is_unread()
    ctl.start_download()
    assert starts == [1] and not ctl.is_unread()


def test_prompt_waits_until_window_first_shows(qtbot, fresh_settings, monkeypatch):
    """开机自启藏在托盘时查到新版本：不凭空弹窗，等主窗口第一次露面再弹。"""
    opened = []
    monkeypatch.setattr(UpdateController, "open_dialog",
                        lambda self, mark_seen=True: opened.append(mark_seen))
    parent = QWidget()
    qtbot.addWidget(parent)
    ctl, _chip = _ctl(qtbot, parent)
    ctl._state = "checking"
    ctl._on_found(_info("2.0.0"))
    assert opened == []
    parent.show()
    qtbot.waitUntil(lambda: opened == [False], timeout=3000)


def test_manual_check_does_not_also_auto_prompt(qtbot, fresh_settings, monkeypatch):
    opened = []
    monkeypatch.setattr(UpdateController, "open_dialog",
                        lambda self, mark_seen=True: opened.append(mark_seen))
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    ctl, _chip = _ctl(qtbot, parent)
    ctl._manual_check = True
    ctl._state = "checking"
    ctl._on_found(_info("2.0.0"))
    assert opened == []


def test_real_dialog_opens_and_closes(qtbot, fresh_settings):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    ctl, _chip = _ctl(qtbot, parent)
    ctl._report = _report(_info())
    ctl._info, ctl._state = _info(), "available"
    ctl.open_dialog()
    assert ctl._dialog is not None and ctl._dialog.isVisible()
    assert not ctl.is_unread()
    ctl._dialog.close()
    qtbot.waitUntil(lambda: ctl._dialog is None, timeout=2000)


def test_announce_last_result_toasts_once(qtbot, fresh_settings, monkeypatch, tmp_path):
    import json
    path = tmp_path / "res.json"
    monkeypatch.setattr(updater, "update_result_path", lambda: path)
    monkeypatch.setattr(update_ui, "_current_version", lambda: "2.0.0")
    path.write_text(json.dumps({"version": "2.0.0", "ok": True}), encoding="utf-8")
    ctl, _chip = _ctl(qtbot)
    toasts = []
    assert ctl.announce_last_result(toasts.append) == "✅ 已更新到 v2.0.0"
    assert ctl.announce_last_result(toasts.append) == ""
    assert toasts == ["✅ 已更新到 v2.0.0"]
