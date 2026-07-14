"""胶片报告增强版应按主题分 Tab，而不是继续堆一条长页面。"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from pptx_finder import db, stats
from pptx_finder.ui import report_overlay as ro
from pptx_finder.ui import theme


def test_report_overlay_has_six_keyboard_tabs_and_renders_each_group(qtbot, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    report = stats.build_report(conn)
    overlay = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()

    assert overlay._tab_bar.count() == 6
    assert [overlay._tab_bar.tabText(i) for i in range(6)] == [
        "总览",
        "名人堂",
        "创作节奏",
        "版本时光机",
        "内容人格",
        "片库版图",
    ]
    assert overlay.copy_btn.toolTip().startswith("复制当前 Tab")

    expected_titles = {
        0: "成就徽章",
        1: "我的 PPT 之最",
        2: "真实保存时钟",
        3: "真正的「最能改奖」",
        4: "我的 PPT 口头禅",
        5: "我的胶片版图",
    }
    for index, title in expected_titles.items():
        overlay._tab_bar.setCurrentIndex(index)
        qtbot.wait(1)
        texts = [label.text() for label in overlay._content.findChildren(QLabel)]
        assert any(title in text for text in texts), (index, title, texts)


def test_export_filename_includes_current_tab(qtbot, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    report = stats.build_report(conn)
    overlay = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay._tab_bar.setCurrentIndex(3)
    captured = []

    monkeypatch.setattr(
        ro.QFileDialog,
        "getSaveFileName",
        lambda *_a, **_kw: (captured.append(_a[2]) or ("", "")),
    )
    overlay._export_clicked()

    assert captured and "版本时光机" in captured[0]


def test_switching_tabs_removes_previous_widgets_immediately(qtbot, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    overlay = ro.ReportOverlay(stats.build_report(conn), theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()
    assert any("成就徽章" in label.text() for label in overlay._content.findChildren(QLabel))

    overlay._tab_bar.setCurrentIndex(1)

    texts = [label.text() for label in overlay._content.findChildren(QLabel)]
    assert any("我的 PPT 之最" in text for text in texts)
    assert not any("成就徽章" in text for text in texts)
