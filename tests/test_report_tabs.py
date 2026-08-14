"""胶片报告增强版应按主题分 Tab，而不是继续堆一条长页面。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QLabel, QPushButton

from pptx_finder import db, stats
from pptx_finder.ui import report_overlay as ro
from pptx_finder.ui import theme
from pptx_finder.versioning import store


def test_report_overlay_has_seven_keyboard_tabs_and_renders_each_group(qtbot, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    report = stats.build_report(conn)
    overlay = ro.ReportOverlay(report, theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()

    assert overlay._tab_bar.count() == 7
    assert [overlay._tab_bar.tabText(i) for i in range(7)] == [
        "总览",
        "名人堂",
        "创作节奏",
        "版本时光机",
        "内容人格",
        "片库版图",
        "生涯履历",
    ]
    assert overlay.copy_btn.toolTip().startswith("复制当前 Tab")

    expected_titles = {
        0: "成就徽章",
        1: "我的 PPT 之最",
        2: "真实保存时钟",
        3: "真正的「最能改奖」",
        4: "我的 PPT 口头禅",
        5: "我的胶片版图",
        6: "生涯履历",
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


def _put_2024_deck(conn) -> None:
    fid = db.upsert_file(
        conn,
        path=r"C:\work\2024 星云复盘.pptx",
        name="2024 星云复盘.pptx",
        ext=".pptx",
        size=2048,
        mtime=datetime(2024, 5, 1, 10).timestamp(),
        content_hash="h1",
        page_count=12,
        status="ok",
        error="",
        indexed_at=0,
    )
    db.replace_pages(conn, fid, [(1, "nebula 观测记录", "t")])
    conn.commit()


def test_chronicle_tab_renders_year_cards_with_keywords_and_locate_buttons(qtbot, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put_2024_deck(conn)
    overlay = ro.ReportOverlay(stats.build_report(conn), theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()

    overlay._tab_bar.setCurrentIndex(6)
    qtbot.wait(1)

    texts = [label.text() for label in overlay._content.findChildren(QLabel)]
    assert any("📅 2024 年" in text for text in texts)
    assert any("12</b> 页" in text for text in texts)  # 统计行是富文本 <b>12</b> 页
    assert any("nebula" in text for text in texts)
    locates = [b for b in overlay._content.findChildren(QPushButton) if b.text() == "定位"]
    assert locates
    assert any("2024 星云复盘.pptx" in (b.toolTip() or "") for b in locates)


def test_chronicle_tab_notes_scope_when_report_is_not_all_history(qtbot, tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put_2024_deck(conn)
    overlay = ro.ReportOverlay(stats.build_report(conn, year=2024), theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()

    overlay._tab_bar.setCurrentIndex(6)
    qtbot.wait(1)

    texts = [label.text() for label in overlay._content.findChildren(QLabel)]
    assert any("📅 2024 年" in text for text in texts)  # 范围内的年份仍逐年展示
    assert any("全部" in text for text in texts)       # 顶部提示完整生涯在「全部」


def test_chronicle_tab_shows_dash_when_version_db_not_connected(qtbot, tmp_path):
    """版本库未连接时年卡留版显示「—」（未知），不冒充 0；连接后显示真实次数。"""
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    _put_2024_deck(conn)

    # 未连接：—（版本库未连接），不是「0 次」
    overlay = ro.ReportOverlay(stats.build_report(conn), theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()
    overlay._tab_bar.setCurrentIndex(6)
    qtbot.wait(1)
    texts = [label.text() for label in overlay._content.findChildren(QLabel)]
    assert any("📅 2024 年" in text for text in texts)
    assert any("时光机留版：<b>—</b>" in text and "未连接" in text for text in texts)
    assert not any("时光机留版：<b>0</b> 次" in text for text in texts)
    overlay.close()

    # 连接版本库：显示真实留版次数
    vpath = tmp_path / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    store.upsert_doc(
        vault, "doc-1", r"C:\work\2024 星云复盘.pptx", datetime(2024, 5, 1, 10).timestamp()
    )
    store.add_version(
        vault, "v1", "doc-1", datetime(2024, 5, 2, 21).timestamp(), "s1", 12, 2048, "hash-v1"
    )
    vault.commit()
    vault.close()
    overlay2 = ro.ReportOverlay(
        stats.build_report(conn, version_db_path=vpath), theme.tok("cloud"), conn=conn
    )
    qtbot.addWidget(overlay2)
    overlay2.show()
    overlay2._tab_bar.setCurrentIndex(6)
    qtbot.wait(1)
    texts2 = [label.text() for label in overlay2._content.findChildren(QLabel)]
    assert any("时光机留版：<b>1</b> 次" in text for text in texts2)
