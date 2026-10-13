"""v2.0.1 组件按需下载：组件接口、下载中心（多源/进度/取消/失败原因）、设置页「组件」。"""
import json
import threading

import pytest
from PySide6.QtCore import Qt

from pptx_finder import components, imgtext_ocr
from pptx_finder.screenshots import formula_component
from pptx_finder.ui import component_center as cc


@pytest.fixture
def center(qapp):
    cc.reset_for_tests()
    c = cc.component_center()
    yield c
    for dlg in list(c._dialogs.values()):
        try:
            dlg.close()
        except RuntimeError:
            pass
    cc.reset_for_tests()


def _manifest(version="1.1.0", size=1000):
    return {"version": version, "archive": {"name": "component.zip", "size": size},
            "_bases": [("github-release", "https://gh"), ("self-hosted", "https://ecs")],
            "_checks": [{"name": "GitHub", "ok": True, "version": version},
                        {"name": "备用服务器", "ok": True, "version": version}]}


def _hidden(dlg):
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    return dlg


# ---------- 组件接口 ----------
def test_stale_center_destroyed_later_does_not_drop_current(qtbot, qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    cc.reset_for_tests()
    old = cc.component_center()
    cc.reset_for_tests()                      # old 只是 deleteLater，还没真正销毁
    current = cc.component_center()
    assert current is not old
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qtbot.wait(10)
    assert cc.component_center() is current
    cc.reset_for_tests()


def test_component_catalog_and_hint_sizes_match_published_archives():
    assert [c.key for c in components.ALL] == ["ocr", "formula"]
    # 发布的组件压缩包：文字识别 85,544,805 字节、公式 457,567,111 字节（按 1024² 取整）
    assert components.OCR.approx_mb == round(85544805 / 1048576)
    assert components.FORMULA.approx_mb == round(457567111 / 1048576)
    from pptx_finder.ui import imgtext_window
    assert imgtext_window.COMPONENT_HINT_MB == components.OCR.approx_mb
    with pytest.raises(ValueError):
        components.info("nope")


def test_formula_installed_version_reads_active_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(formula_component, "component_dir", lambda: tmp_path)
    assert formula_component.installed_version() == ""
    (tmp_path / "active.json").write_text(json.dumps({"runtime": "runtime-a", "version": "1.0.0"}), "utf-8")
    assert formula_component.installed_version() == "1.0.0"
    assert components.installed_version("formula") == "1.0.0"


def test_uninstall_moves_whole_folder_then_deletes(tmp_path, monkeypatch):
    root = tmp_path / "ocr"
    (root / "models").mkdir(parents=True)
    (root / "pptdoctor-ocr.exe").write_bytes(b"x" * 300)
    (root / "models" / "m.onnx").write_bytes(b"y" * 700)
    monkeypatch.setattr(imgtext_ocr, "component_dir", lambda: root)
    assert components.disk_bytes("ocr") == 1000
    assert components.uninstall("ocr") == 1000
    assert not root.exists() and not list(tmp_path.glob(".ocr-removing-*"))


def test_uninstall_of_component_in_use_leaves_it_intact(tmp_path, monkeypatch):
    root = tmp_path / "formula"
    root.mkdir()
    (root / "active.json").write_text("{}", "utf-8")
    monkeypatch.setattr(formula_component, "component_dir", lambda: root)

    def locked(src, dst):
        raise PermissionError(5, "拒绝访问")
    monkeypatch.setattr(components.os, "replace", locked)
    with pytest.raises(components.ComponentBusy, match="正在使用"):
        components.uninstall("formula")
    assert (root / "active.json").is_file()


def test_manifest_failure_carries_per_source_reasons(monkeypatch):
    def boom(*a, **k):
        raise OSError("连接超时")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(imgtext_ocr.OcrUnavailable) as info:
        imgtext_ocr.fetch_manifest_from_sources([("github-release", "https://gh"),
                                                 ("self-hosted", "https://ecs")], 1)
    checks = info.value.checks
    assert [c["ok"] for c in checks] == [False, False] and len(checks) == 2


# ---------- 下载中心 ----------
def test_center_downloads_with_progress_sources_and_calls_back(qtbot, center, monkeypatch):
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "fetch_manifest", lambda key: _manifest())
    gate = threading.Event()

    def fake_install(key, manifest, *, status=None, cancel=None):
        status({"attempts": [{"name": "GitHub", "status": "failed", "reason": "收到的是网页而不是更新文件，可能被公司代理拦截"},
                             {"name": "备用服务器", "status": "downloading"}]})
        status({"source": "备用服务器", "received": 400, "total": 1000, "speed": 200, "eta": 3})
        gate.wait(5)
        status({"source": "备用服务器", "received": 1000, "total": 1000, "speed": 200, "eta": 0})
        status({"attempts": [{"name": "GitHub", "status": "failed", "reason": "收到的是网页而不是更新文件，可能被公司代理拦截"},
                             {"name": "备用服务器", "status": "ok", "bytes": 1000, "ms": 2500}]})
        return "1.1.0"
    monkeypatch.setattr(components, "install", fake_install)
    ready = []
    dlg = _hidden(center.request("ocr", on_ready=lambda: ready.append(1)))
    qtbot.waitUntil(lambda: (center.snapshot("ocr")["status"] or {}).get("received") == 400)
    assert dlg.phase_rendered == "downloading"
    assert "备用服务器" in dlg.dl_head.text()
    assert "剩余约" in dlg.meta_right.text() and "/s" in dlg.meta_left.text()
    assert dlg.btn_cancel.isVisible() and dlg.btn_close.text() == "后台下载"
    assert any("拦截" in text for _n, _s, text in dlg.sources.rows)
    gate.set()
    qtbot.waitUntil(lambda: center.snapshot("ocr")["phase"] == "done")
    assert ready == [1]
    assert "已就绪" in dlg.msg.text() and "备用服务器" in dlg.msg.text()
    assert dlg.btn_primary.text() == "完成"
    # 第二次请求同一个已装好的组件：直接回调，不再开下载窗口
    monkeypatch.setattr(components, "is_installed", lambda key: True)
    assert center.request("ocr", on_ready=lambda: ready.append(2)) is None
    assert ready == [1, 2]


def test_center_cancel_leaves_retry_and_drops_callbacks(qtbot, center, monkeypatch):
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "fetch_manifest", lambda key: _manifest())
    started = threading.Event()

    def slow_install(key, manifest, *, status=None, cancel=None):
        status({"source": "GitHub", "received": 10, "total": 1000})
        started.set()
        cancel.wait(5)
        raise InterruptedError("下载已取消")
    monkeypatch.setattr(components, "install", slow_install)
    ready = []
    dlg = _hidden(center.request("formula", on_ready=lambda: ready.append(1)))
    assert started.wait(5)
    dlg.btn_cancel.click()
    qtbot.waitUntil(lambda: center.snapshot("formula")["phase"] == "cancelled")
    assert ready == [] and dlg.btn_primary.text() == "重新下载"
    assert "没有留下半套组件" in dlg.msg.text()


def test_center_failure_lists_each_source_and_offers_full_package(qtbot, center, monkeypatch):
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "fetch_manifest", lambda key: _manifest())

    def fail(key, manifest, *, status=None, cancel=None):
        status({"attempts": [{"name": "GitHub", "status": "failed", "reason": "连接超时"},
                             {"name": "备用服务器", "status": "failed", "reason": "HTTP 404"}]})
        raise imgtext_ocr.OcrUnavailable("识别组件下载失败：GitHub：连接超时；备用服务器：HTTP 404")
    monkeypatch.setattr(components, "install", fail)
    ready = []
    dlg = _hidden(center.request("ocr", on_ready=lambda: ready.append(1)))
    qtbot.waitUntil(lambda: center.snapshot("ocr")["phase"] == "error")
    texts = [text for _n, _s, text in dlg.sources.rows]
    assert any("连接超时" in t for t in texts) and any("404" in t for t in texts)
    assert dlg.btn_manual.isVisible() and "完整版" in dlg.btn_manual.text()
    assert dlg.btn_primary.text() == "重试" and ready == []


def test_bundled_formula_installs_locally_without_network(qtbot, center, monkeypatch):
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    monkeypatch.setattr(components, "bundled", lambda key: key == "formula")

    def no_network(key):
        raise AssertionError("内置组件不应联网查询")
    monkeypatch.setattr(components, "fetch_manifest", no_network)
    seen = []

    def local(key, manifest, *, status=None, cancel=None):
        seen.append(manifest)
        status({"source": "安装包内置", "received": 5, "total": 10})
        return "1.0.0"
    monkeypatch.setattr(components, "install", local)
    dlg = _hidden(center.request("formula"))
    qtbot.waitUntil(lambda: center.snapshot("formula")["phase"] == "done")
    assert seen == [None] and "安装包内置" in dlg.msg.text()


# ---------- 设置页「组件」 ----------
def test_settings_components_tab_shows_status_and_opens_download(qtbot, center, monkeypatch, tmp_path):
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager

    monkeypatch.setattr(components, "is_installed", lambda key: key == "formula")
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "installed_version", lambda key: "1.0.0" if key == "formula" else "")
    monkeypatch.setattr(components, "disk_bytes", lambda key: 1258291200 if key == "formula" else 0)
    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    dlg.show_tab("组件")
    assert dlg.tabs.tabText(dlg.tabs.currentIndex()) == "组件"
    rows = dlg._component_rows
    assert "未安装" in rows["ocr"]["status"].text() and "82 MB" in rows["ocr"]["status"].text()
    assert rows["ocr"]["download"].text() == "下载" and not rows["ocr"]["remove"].isVisibleTo(dlg)
    qtbot.waitUntil(lambda: "占用" in rows["formula"]["status"].text())
    assert "已安装 v1.0.0" in rows["formula"]["status"].text() and "1.2 GB" in rows["formula"]["status"].text() and dlg._components_measured
    assert rows["formula"]["remove"].isVisibleTo(dlg)

    calls = []
    monkeypatch.setattr(center, "request", lambda key, parent=None, **kw: calls.append(key))
    rows["ocr"]["download"].click()
    assert calls == ["ocr"]


def test_settings_uninstall_confirms_then_frees_space(qtbot, center, monkeypatch):
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager

    state = {"installed": True}
    monkeypatch.setattr(components, "is_installed", lambda key: key == "formula" and state["installed"])
    monkeypatch.setattr(components, "bundled", lambda key: False)
    monkeypatch.setattr(components, "disk_bytes", lambda key: 2048 if state["installed"] and key == "formula" else 0)

    def fake_uninstall(key):
        state["installed"] = False
        return 2048
    monkeypatch.setattr(components, "uninstall", fake_uninstall)
    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    answers = iter([False, True])
    monkeypatch.setattr(dlg, "_confirm_component_uninstall", lambda key: next(answers))
    row = dlg._component_rows["formula"]
    row["remove"].click()                     # 第一次点「取消」：什么也不删
    assert state["installed"]
    row["remove"].click()
    qtbot.waitUntil(lambda: "已卸载" in row["status"].text())
    assert "未安装" in row["status"].text() and not row["remove"].isVisibleTo(dlg)


def test_settings_bundled_ocr_has_no_uninstall(qtbot, center, monkeypatch):
    from pptx_finder.ui.settings_dialog import SettingsDialog
    from pptx_finder.versioning.manager import VersionManager

    monkeypatch.setattr(components, "is_installed", lambda key: key == "ocr")
    monkeypatch.setattr(components, "bundled", lambda key: True)
    dlg = SettingsDialog(VersionManager())
    qtbot.addWidget(dlg)
    assert "随安装包内置" in dlg._component_rows["ocr"]["status"].text()
    assert not dlg._component_rows["ocr"]["remove"].isVisibleTo(dlg)
    assert "第一次使用时自动解压" in dlg._component_rows["formula"]["status"].text()


def test_imgtext_window_download_button_uses_center(qtbot, center, monkeypatch):
    from pptx_finder.ui import imgtext_window
    from pptx_finder.ui.theme import tok

    monkeypatch.setattr(imgtext_ocr, "is_installed", lambda: False)
    win = imgtext_window.ImgTextWindow(tok("atelier"))
    qtbot.addWidget(win)
    calls = []
    monkeypatch.setattr(center, "request", lambda key, parent=None, on_ready=None, **kw: calls.append((key, on_ready)))
    win._component_btn.click()
    assert calls and calls[0][0] == "ocr"
    assert "82 MB" in win._component_btn.text()
