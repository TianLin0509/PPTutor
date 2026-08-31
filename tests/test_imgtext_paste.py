# -*- coding: utf-8 -*-
"""Ctrl+V 粘贴剪贴板图片。

截图工具（Win+Shift+S）产出的图只存在于剪贴板，没有文件路径。而识别管线吃的是
路径，所以粘贴必须「先落盘再走原有流程」——这一步如果落在缓存目录以外，临时截图
就会被索引扫进搜索结果；如果不清理，截图会在缓存里无限堆积。两件事都在这里钉住。
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QUrl                      # noqa: E402
from PySide6.QtGui import QImage, QKeySequence                  # noqa: E402

from pptx_finder import config                                  # noqa: E402
from pptx_finder.ui import imgtext_window                       # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(imgtext_window.imgtext_ocr, "is_installed", lambda: True)
    w = imgtext_window.ImgTextWindow({})
    qtbot.addWidget(w)
    return w


def _image(w=40, h=30, colour=0xFF3366AA):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(colour)
    return img


def test_paste_bitmap_lands_in_the_cache_dir_and_becomes_the_source(win, qtbot):
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setImage(_image(64, 48))
    assert win.paste_from_clipboard() is True

    src = win._source
    assert os.path.isfile(src)
    assert src.lower().endswith(".png")
    # 必须在缓存目录下：索引扫描排除的正是这里，否则临时截图会污染搜索结果
    assert os.path.normcase(str(config.cache_dir())) in os.path.normcase(src)
    assert win._source_is_pasted is True
    assert "剪贴板图片" in win._status.text()
    assert "64" in win._status.text() and "48" in win._status.text()


def test_paste_prefers_a_copied_image_file_over_bitmap_data(win, tmp_path, qtbot):
    """Explorer 里 Ctrl+C 一个 png：直接用原文件，不必再复制一份。"""
    from PySide6.QtGui import QGuiApplication

    real = tmp_path / "效果图.png"
    _image(20, 20).save(str(real), "PNG")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(real))])
    mime.setImageData(_image(99, 99))
    QGuiApplication.clipboard().setMimeData(mime)

    assert win.paste_from_clipboard() is True
    assert os.path.normcase(win._source) == os.path.normcase(str(real))
    assert win._source_is_pasted is False


def test_paste_with_nothing_usable_says_so_instead_of_going_quiet(win, qtbot):
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setText("这不是图片")
    assert win.paste_from_clipboard() is False
    assert not win._source
    assert "剪贴板里没有图片" in win._status.text()


def test_paste_is_refused_while_converting(win, qtbot):
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setImage(_image())
    win._busy = True
    assert win.paste_from_clipboard() is False
    assert not win._source


def test_paste_dir_keeps_only_the_recent_shots(win, tmp_path, qtbot):
    from PySide6.QtGui import QGuiApplication

    root = imgtext_window.paste_dir()
    for i in range(20):
        (root / f"old-{i:02d}.png").write_bytes(b"x")
    QGuiApplication.clipboard().setImage(_image())
    assert win.paste_from_clipboard() is True

    left = list(root.glob("*.png"))
    assert len(left) == imgtext_window.PASTE_KEEP, [p.name for p in left]
    # 刚粘的那张必须活着
    assert os.path.normcase(win._source) in {os.path.normcase(str(p)) for p in left}


def test_pasted_image_does_not_default_the_save_dialog_into_the_cache(win, qtbot):
    """粘贴图的「原目录」是缓存目录，拿它当另存默认位置等于把成果藏起来。"""
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setImage(_image())
    assert win.paste_from_clipboard() is True
    target = win._default_save_target()
    assert target.endswith("_可编辑.pptx")
    assert os.path.normcase(str(config.cache_dir())) not in os.path.normcase(target)


def test_picked_file_still_saves_next_to_the_original(win, tmp_path, qtbot):
    real = tmp_path / "季度 汇报.png"
    _image().save(str(real), "PNG")
    win.set_source(str(real))
    assert win._default_save_target() == str(tmp_path / "季度 汇报_可编辑.pptx")


def test_the_window_binds_the_standard_paste_shortcut(win):
    keys = {sc.key().toString() for sc in win.findChildren(type(win._paste_sc))}
    assert QKeySequence(QKeySequence.StandardKey.Paste).toString() in keys


def test_pressing_ctrl_v_actually_pastes(win, qtbot):
    """绑上了不等于按得动——真按一次键，别只断言连线存在。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setImage(_image(72, 36))
    win.show()
    qtbot.waitExposed(win)
    # QShortcut 是 WindowShortcut 上下文：窗口不是活动窗口时 QShortcutMap 直接
    # 不匹配。真实用户按 Ctrl+V 时窗口必然是活动的，所以这里要先激活再按。
    win.activateWindow()
    win.raise_()
    qtbot.wait(400)          # waitActive 返回得比 OS 真正给焦点早，这里要实等
    if not win.isActiveWindow():
        pytest.skip("窗口无法激活（无头/远程桌面），快捷键上下文不成立")
    qtbot.keyClick(win, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: bool(win._source), timeout=3000)
    assert win._source_is_pasted is True
    assert "72" in win._status.text()


def test_paste_button_is_visible_so_the_feature_is_discoverable(win):
    assert win._paste_btn.isEnabled()
    assert "粘贴" in win._paste_btn.text()
