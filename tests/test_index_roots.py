"""自定义索引根 + 网络路径（UNC）。

覆盖：config 持久化/清洗/校验三态、MainWindow._resolve_index_roots 优先级与合并语义、
设置对话框「索引范围」列表编辑。各测试用 monkeypatch 隔离 PPTX_FINDER_DATA_DIR，
并清掉 PPTX_FINDER_ROOTS 防真实环境污染。
"""
from __future__ import annotations

import json
import os

import pytest

from pptx_finder import config
import pptx_finder.scanner as scanner_mod
import pptx_finder.ui.settings_dialog as settings_dialog_mod
from pptx_finder.ui.main_window import MainWindow
from pptx_finder.ui.settings_dialog import SettingsDialog
from pptx_finder.versioning.manager import VersionManager


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.delenv("PPTX_FINDER_ROOTS", raising=False)


@pytest.fixture
def mgr():
    m = VersionManager()
    yield m
    m.stop()


# ---------- config：持久化与清洗 ----------

def test_index_roots_roundtrip_and_clean(tmp_path):
    assert config.get_index_roots() == ()
    config.set_index_roots([
        "  " + str(tmp_path / "a") + "\\  ",   # 空白 + 尾分隔符清洗
        str(tmp_path / "A"),                    # normcase 后与上一条同 key → 去重
        "",
        "\\\\server\\share\\dir\\",             # UNC 尾分隔符剥掉
        "\\\\server\\share\\dir",               # 与上一条重复
    ])
    roots = config.get_index_roots()
    assert roots == (str(tmp_path / "a"), "\\\\server\\share\\dir")
    data = json.loads((config.data_dir() / "ui.json").read_text("utf-8"))
    assert data["index_roots"] == list(roots)
    config.set_index_roots([])
    assert config.get_index_roots() == ()


def test_index_roots_clean_keeps_drive_root_separator():
    # "C:\" 剥尾分隔符会变 "C:"（盘当前目录），清洗时必须还原
    config.set_index_roots(["C:\\", "c:\\"])
    assert config.get_index_roots() == ("C:\\",)


# ---------- config：validate_index_root 三态 ----------

def test_validate_index_root_rejects_malformed():
    for bad in ("", "   ", "C:", "not-a-path", "relative\\dir",
                "\\\\onlyserver", "\\\\?\\UNC\\srv\\shr"):
        ok, reachable, msg = config.validate_index_root(bad)
        assert (ok, reachable) == (False, False), bad
        assert msg, bad


def test_validate_index_root_reachable_local(tmp_path):
    assert config.validate_index_root(str(tmp_path)) == (True, True, "")


def test_validate_index_root_unreachable_is_warning_not_reject(tmp_path):
    ok, reachable, msg = config.validate_index_root(str(tmp_path / "no-such-dir"))
    assert (ok, reachable) == (True, False)   # 第三态：警告但允许保存
    assert "不可达" in msg


def test_validate_index_root_unc_form(monkeypatch):
    # 真实 UNC 探测可能卡数秒，这里 stub 掉 isdir 只验证三态分流
    monkeypatch.setattr(os.path, "isdir", lambda _p: True)
    ok, reachable, _msg = config.validate_index_root("\\\\server\\share\\dir")
    assert (ok, reachable) == (True, True)
    monkeypatch.setattr(os.path, "isdir", lambda _p: False)
    ok, reachable, msg = config.validate_index_root("\\\\server\\share\\dir")
    assert (ok, reachable) == (True, False)
    assert "不可达" in msg


def test_validate_index_root_nul_char_does_not_raise():
    # 含 \x00 的输入会让 os.path.isdir 抛 ValueError（非 OSError）；
    # 必须按不可达兜底，否则设置对话框永远停在「正在检测…」
    ok, reachable, msg = config.validate_index_root("D:\\bad\x00dir")
    assert (ok, reachable) == (True, False)
    assert "不可达" in msg


# ---------- MainWindow._resolve_index_roots：优先级与合并 ----------

def _fixed_drives() -> list[str]:
    return ["C:\\", "D:\\"]


def test_resolve_ctor_param_wins(monkeypatch):
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    config.set_index_roots(["D:\\data"])
    monkeypatch.setenv("PPTX_FINDER_ROOTS", "E:\\env")
    assert MainWindow._resolve_index_roots(["F:\\explicit"]) == ["F:\\explicit"]


def test_resolve_env_only_keeps_legacy_replace_semantics(monkeypatch):
    # 无自定义根时保留旧行为：env 单独存在仍是完整索引范围（脚本靠它限定）
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    monkeypatch.setenv("PPTX_FINDER_ROOTS", os.pathsep.join(["E:\\env", "F:\\env2"]))
    assert MainWindow._resolve_index_roots(None) == ["E:\\env", "F:\\env2"]


def test_resolve_env_roots_are_cleaned(monkeypatch):
    # env 根与自定义根同口径清洗：尾分隔符/大小写重复在合并前就被剥掉，
    # 否则同一棵树会成为独立根被重复扫描
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    monkeypatch.setenv(
        "PPTX_FINDER_ROOTS",
        os.pathsep.join(["E:\\env\\", "e:\\ENV", "F:\\keep"]),
    )
    assert MainWindow._resolve_index_roots(None) == ["E:\\env", "F:\\keep"]


def test_resolve_custom_merges_fixed_drives_and_env(monkeypatch):
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    config.set_index_roots(["\\\\server\\share\\dir", "D:\\data"])
    # env 作为追加项合并去重（"d:\DATA" 与自定义根 normcase 同 key 被去重）
    monkeypatch.setenv("PPTX_FINDER_ROOTS", os.pathsep.join(["E:\\env", "d:\\DATA"]))
    assert MainWindow._resolve_index_roots(None) == [
        "\\\\server\\share\\dir", "D:\\data", "C:\\", "D:\\", "E:\\env",
    ]


def test_resolve_custom_root_dedups_fixed_drive(monkeypatch):
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    config.set_index_roots(["D:\\"])
    assert MainWindow._resolve_index_roots(None) == ["D:\\", "C:\\"]


def test_resolve_default_falls_back_to_fixed_drives(monkeypatch):
    monkeypatch.setattr(scanner_mod, "fixed_drives", _fixed_drives)
    assert MainWindow._resolve_index_roots(None) == ["C:\\", "D:\\"]


# ---------- 设置对话框：索引范围列表编辑 ----------

def test_settings_lists_saved_roots(qtbot, mgr):
    config.set_index_roots(["D:\\data", "\\\\server\\share\\dir"])
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    assert dlg.index_roots_list.count() == 2
    assert dlg.index_roots_list.item(0).text() == "D:\\data"
    assert dlg.index_roots_list.item(1).text() == "\\\\server\\share\\dir"


def test_settings_add_and_remove_network_root(qtbot, mgr):
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.index_root_edit.setText("\\\\server\\share\\dir")
    dlg._add_index_root_network()
    assert dlg.index_roots_list.count() == 1
    assert dlg.index_root_edit.text() == ""
    dlg.index_root_edit.setText("")  # 空输入不添加
    dlg._add_index_root_network()
    assert dlg.index_roots_list.count() == 1

    dlg.index_roots_list.setCurrentRow(0)
    dlg._remove_index_root()
    assert dlg.index_roots_list.count() == 0

    dlg._apply_index_roots()  # 空列表保存 = 回到仅固定盘
    qtbot.waitUntil(lambda: "已保存" in dlg._index_roots_result.text(), timeout=5000)
    assert config.get_index_roots() == ()


def test_settings_add_local_root_via_picker(qtbot, mgr, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings_dialog_mod.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg._add_index_root_local()
    assert dlg.index_roots_list.count() == 1
    assert dlg.index_roots_list.item(0).text() == str(tmp_path)


def test_settings_apply_saves_and_marks_unreachable(qtbot, mgr, tmp_path):
    missing = tmp_path / "no-such-dir"
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.index_root_edit.setText(str(tmp_path))
    dlg._add_index_root_network()
    dlg.index_root_edit.setText(str(missing) + "\\")  # 尾分隔符在保存清洗时剥掉
    dlg._add_index_root_network()

    dlg._apply_index_roots()
    qtbot.waitUntil(lambda: "已保存" in dlg._index_roots_result.text(), timeout=5000)

    assert config.get_index_roots() == (str(tmp_path), str(missing))
    assert dlg.index_roots_list.count() == 2
    assert dlg.index_roots_list.item(0).text() == str(tmp_path)  # 可达：无警示
    warn_text = dlg.index_roots_list.item(1).text()
    assert str(missing) in warn_text and "不可达" in warn_text

    dlg2 = SettingsDialog(mgr)  # 重开回显：警示文字不进持久化
    qtbot.addWidget(dlg2)
    assert dlg2.index_roots_list.item(1).text() == str(missing)


def test_settings_apply_rejects_malformed(qtbot, mgr):
    dlg = SettingsDialog(mgr)
    qtbot.addWidget(dlg)
    dlg.index_root_edit.setText("not-a-path")
    dlg._add_index_root_network()
    dlg._apply_index_roots()
    qtbot.waitUntil(lambda: "未保存" in dlg._index_roots_result.text(), timeout=5000)
    assert config.get_index_roots() == ()
