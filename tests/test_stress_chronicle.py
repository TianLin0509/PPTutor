# -*- coding: utf-8 -*-
"""生涯履历（yearly_chronicle / 生涯履历 Tab）对抗性压测。

- 20 年 × 每年 200 文件（3 份 600 页大部头 + 60 份 0 页文件 + 137 份常规，
  其中 1 份 page_count>0 但 pages_raw 缺行的“幽灵”文件）+ mtime 边界值
  （0 / 负值 / 1980 下限 / 未来 / 跨年瞬间），灌 pages_raw 真实量级文本；
  实测 build_report 端到端耗时与内存峰值，并对账分桶、关键词、留版数。
- UI 侧 offscreen 渲染 30 张年卡，断言不崩、耗时合理、富文本转义正确。

运行：QT_QPA_PLATFORM=offscreen uv run pytest tests/test_stress_chronicle.py -s
"""
from __future__ import annotations

import os
import re
import time
import tracemalloc
from datetime import datetime

import pytest

# 注意：不要在模块级 setdefault("QT_QPA_PLATFORM", ...)——收集期 import 会把
# 全进程 QPA 逼成 offscreen，连累同进程其他 UI 测试（test_theme_decouple 曾因此红）。
# 需要 offscreen 时命令行显式给：QT_QPA_PLATFORM=offscreen uv run pytest ...

from pptx_finder import db, report_insights, stats
from pptx_finder.versioning import store

CORE_YEARS = tuple(range(2007, 2027))  # 20 年
BIG_PER_YEAR = 3
BIG_PAGES = 600
ZERO_PER_YEAR = 60
NORMAL_PER_YEAR = 137  # 200 = 3 大部头 + 60 零页 + 137 常规（含 1 份幽灵）
FILES_PER_YEAR = BIG_PER_YEAR + ZERO_PER_YEAR + NORMAL_PER_YEAR
MIN_VALID = datetime(1980, 1, 1).timestamp()
FUTURE_TS = time.time() + 10 * 365.25 * 86400  # 约 10 年后（< year 3000，远离 CRT 上限）
FUTURE_YEAR = datetime.fromtimestamp(FUTURE_TS).year


def _page_text(kw: str, page_no: int, size: int) -> str:
    base = f"第{page_no}页 {kw} 星云 数据 增长 战略 复盘 客户 价值 交付 里程碑 路线图 "
    return (base * (size // len(base) + 1))[:size]


def _fill_big_library(conn) -> dict:
    """灌 20 年 × 200 文件 + 6 份边界文件，返回逐年期望指标。"""
    expected: dict[int, dict] = {}
    pages_rows: list[tuple[int, int, str]] = []

    def put(name, mtime, texts: list[str], size, *, ghost=False):
        year = datetime.fromtimestamp(mtime).year if mtime >= MIN_VALID else None
        fid = db.upsert_file(
            conn, path=rf"C:\arc\{name}", name=name, ext=".pptx", size=size,
            mtime=mtime, content_hash="h-" + name, page_count=len(texts),
            status="ok", error="", indexed_at=mtime + 1,
        )
        if not ghost:
            pages_rows.extend((fid, p, t) for p, t in enumerate(texts, 1))
        if year is not None:
            e = expected.setdefault(year, {"deck": 0, "pages": 0, "chars": 0, "size": 0})
            e["deck"] += 1
            e["pages"] += len(texts)
            e["chars"] += 0 if ghost else sum(len(t) for t in texts)
            e["size"] += size
        return fid

    for y in CORE_YEARS:
        kw = f"kw{y}alpha"
        # 3 份 600 页大部头，年内 mtime 最新（12 月底），每页 ~300 字
        for k in range(BIG_PER_YEAR):
            put(
                f"{y}/大部头{y}_{k}.pptx",
                datetime(y, 12, 28 + k, 22, k).timestamp(),
                [_page_text(kw, p, 300) for p in range(1, BIG_PAGES + 1)],
                5_000_000 + k,
            )
        # 60 份 0 页文件：计入 deck_count，不进关键词抽样
        for i in range(ZERO_PER_YEAR):
            put(
                f"{y}/零页{y}_{i:02d}.pptx",
                datetime(y, 1 + i % 6, 1 + i % 28, 10).timestamp(),
                [],
                1000 + i,
            )
        # 137 份常规文件；i==0 为幽灵：page_count=10 但 pages_raw 无行
        for i in range(NORMAL_PER_YEAR):
            pages = 5 + (i % 26)
            put(
                f"{y}/常规{y}_{i:03d}.pptx",
                datetime(y, 1 + i % 11, 1 + i % 28, 9 + i % 8).timestamp(),
                [_page_text(kw, p, 80) for p in range(1, pages + 1)],
                1000 + (i * 37 % 9000),
                ghost=(i == 0),
            )

    # ---- mtime 边界值 ----
    # 跨年瞬间：A 落在旧年、B 落在新年（A 是 2024 最新文件，会被抽到样；
    # 抽样新语义下预算会被后续常规文件装满，A 用 10 页高密度特征词保住 top5 可见性）
    put("crosstail2024.pptx", datetime(2024, 12, 31, 23, 59, 59).timestamp(),
        ["crosstail2024 " * 100] * 10, 500)
    put("crosshead2025.pptx", datetime(2025, 1, 1, 0, 0, 1).timestamp(),
        ["crosshead2025 kw2025alpha 跨年头"] * 7, 700)
    # 1980 下限（== _MIN_VALID_MTIME，恰好收录）
    put("远古1980.pptx", MIN_VALID, ["ancient1980 远古 胶片"] * 3, 300)
    # 未来时间（约 10 年后）
    put("未来.pptx", FUTURE_TS, ["futuredeck 未来 胶片"] * 4, 400)
    # 0 与负值：必须被生涯履历排除（但仍在报告总份数里）
    put("无时间.pptx", 0.0, ["zerotime 无时间"] * 2, 200)
    put("负时间.pptx", -5000.0, ["negtime 负时间"] * 2, 200)

    conn.executemany(
        "INSERT INTO pages_raw(file_id, page_no, raw_text) VALUES (?,?,?)", pages_rows
    )
    conn.commit()
    return expected


def _fill_versions(vault) -> dict[int, int]:
    """每年 1+(y%3) 次健康留版；另插 3 条必须被排除的记录。"""
    expected: dict[int, int] = {}
    for y in CORE_YEARS:
        n = 1 + (y % 3)
        expected[y] = n
        store.upsert_doc(vault, f"doc-{y}", rf"C:\arc\{y}\甲{y}.pptx",
                         datetime(y, 1, 2, 9).timestamp())
        for k in range(n):
            ts = datetime(y, 3 + k, 10, 20).timestamp()
            store.add_version(vault, f"v-{y}-{k}", f"doc-{y}", ts, f"s{y}{k}",
                              10, 100, f"hash-{y}-{k}")
    # 排除项①：health='corrupt'（2024 年，不应计入）
    store.upsert_doc(vault, "doc-bad", r"C:\arc\bad.pptx", datetime(2024, 5, 1).timestamp())
    store.add_version(vault, "v-bad", "doc-bad", datetime(2024, 5, 2, 9).timestamp(),
                      "sbad", 10, 100, "hash-bad", health="corrupt")
    # 排除项②：ts 低于 _MIN_VALID_MTIME
    store.add_version(vault, "v-old", "doc-bad", datetime(1975, 6, 1).timestamp(),
                      "sold", 10, 100, "hash-old")
    return expected


@pytest.fixture(scope="module")
def big_library(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("chronicle_stress")
    conn = db.connect(tmp / "index.db")
    db.init_db(conn)
    t0 = time.perf_counter()
    meta = _fill_big_library(conn)
    print(f"\n[stress] 灌库 4006 文件 / {sum(m['pages'] for m in meta.values())} 页文本行: "
          f"{time.perf_counter() - t0:.2f}s")
    vpath = tmp / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    saves = _fill_versions(vault)
    vault.commit()
    vault.close()
    yield conn, vpath, meta, saves
    conn.close()


def test_chronicle_buckets_metrics_keywords_and_saves_reconcile(big_library):
    conn, vpath, meta, saves = big_library
    report = stats.build_report(conn, version_db_path=vpath)
    chronicle = report.chronicle

    # 分桶数：20 个核心年 + 1980 + 未来年；无 1969/1970（0/负 mtime 被排除）
    years = [c.year for c in chronicle]
    assert years == sorted(years)
    assert len(chronicle) == 22
    assert min(years) == 1980 and max(years) == FUTURE_YEAR
    assert 1969 not in years and 1970 not in years
    # 报告总份数含无时间文件；生涯履历总份数不含
    assert report.deck_count == 20 * FILES_PER_YEAR + 6
    assert sum(c.deck_count for c in chronicle) == 20 * FILES_PER_YEAR + 4

    by_year = {c.year: c for c in chronicle}
    for y in CORE_YEARS:
        c = by_year[y]
        e = meta[y]
        assert c.deck_count == e["deck"]
        assert c.page_count == e["pages"]      # 0 页与幽灵文件按 files 表口径计入
        assert c.char_count == e["chars"]      # 幽灵文件缺 pages_raw 行 → 0 字
        assert c.total_size == e["size"]
        labels = [k.label for k in c.top_keywords]
        assert f"kw{y}alpha" in labels, f"{y} 年关键词缺失：{labels}"
        # top3 代表文件：页数最多的 600 页大部头在最前
        assert len(c.top_files) == 3
        assert c.top_files[0].value == BIG_PAGES
        assert c.top_files[0].path and c.top_files[0].detail.endswith(" 字")
        # 留版数逐年对账
        assert c.version_saves == saves[y], f"{y}: {c.version_saves} != {saves[y]}"

    # 跨年瞬间两侧归属
    assert by_year[2024].deck_count == FILES_PER_YEAR + 1
    assert by_year[2025].deck_count == FILES_PER_YEAR + 1
    assert "crosstail2024" in [k.label for k in by_year[2024].top_keywords]
    # 两条病态留版（corrupt / 1975 年前）不得计入任何年份
    assert sum(c.version_saves for c in chronicle) == sum(saves.values())
    # 1980 / 未来年：有文件、无留版
    assert by_year[1980].deck_count == 1 and by_year[1980].version_saves == 0
    assert "ancient1980" in [k.label for k in by_year[1980].top_keywords]
    assert by_year[FUTURE_YEAR].deck_count == 1 and by_year[FUTURE_YEAR].version_saves == 0


def test_build_report_end_to_end_time_and_memory(big_library):
    conn, vpath, _meta, _saves = big_library
    report_insights._terms.cache_clear()  # 冷缓存，测真实耗时
    t0 = time.perf_counter()
    report = stats.build_report(conn, version_db_path=vpath)
    dur = time.perf_counter() - t0
    tracemalloc.start()
    stats.build_report(conn, version_db_path=vpath)  # 第二轮专测内存
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n[stress] build_report 冷缓存端到端: {dur:.2f}s, "
          f"tracemalloc 峰值 {peak / 1e6:.1f} MB, chronicle {len(report.chronicle)} 年")
    assert len(report.chronicle) == 22
    assert dur < 10.0, f"build_report 耗时 {dur:.1f}s 超出预算"
    assert peak < 400 * 1024 * 1024, f"内存峰值 {peak / 1e6:.0f}MB 超出预算"


def test_chronicle_excludes_out_of_range_version_ts(tmp_path):
    """版本库混入超 SQLite 日期上限的 ts（如 2^62）时，生涯履历按 NULL 年份排除、不崩。

    注意：整条 build_report 在此输入下仍会崩——崩点是既有 version_insights
    （report_insights.py:653 的 datetime.fromtimestamp），不属于本次改动；
    本用例只钉住新代码 yearly_chronicle 自身对该输入稳健。
    """
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)
    db.upsert_file(
        conn, path=r"C:\s\甲.pptx", name="甲.pptx", ext=".pptx", size=100,
        mtime=datetime(2024, 5, 1, 10).timestamp(), content_hash="h0",
        page_count=1, status="ok", error="", indexed_at=0,
    )
    conn.commit()
    vpath = tmp_path / "versions.db"
    vault = store.connect(vpath)
    store.init_db(vault)
    store.upsert_doc(vault, "doc-a", r"C:\s\甲.pptx", datetime(2024, 5, 1).timestamp())
    store.add_version(vault, "v-ok", "doc-a", datetime(2024, 5, 2, 9).timestamp(),
                      "s0", 1, 100, "hash-ok")
    store.add_version(vault, "v-huge", "doc-a", float(2**62), "s1", 1, 100, "hash-huge")
    vault.commit()
    vault.close()

    chronicle = report_insights.yearly_chronicle(
        conn, stats.fetch_file_stats(conn), version_db_path=vpath
    )
    assert [c.year for c in chronicle] == [2024]
    assert chronicle[0].version_saves == 1  # 2^62 那条被 NULL 年份排除


def test_chronicle_sampling_budget_skips_oversize_and_keeps_filling(tmp_path):
    """小样例钉住抽样语义：首份超限保底；放不下的文件跳过（不 break）、继续用
    后续文件装满预算；0 页不占名额、pages_raw 缺行的幽灵文件不进关键词、
    整年 0 页时关键词为空。"""
    conn = db.connect(tmp_path / "index.db")
    db.init_db(conn)

    def put(name, mtime, texts, *, ghost=False):
        fid = db.upsert_file(
            conn, path=rf"C:\s\{name}", name=name, ext=".pptx", size=100,
            mtime=mtime, content_hash="h-" + name, page_count=len(texts),
            status="ok", error="", indexed_at=mtime + 1,
        )
        if not ghost:
            db.replace_pages(conn, fid, [(p, t, "t") for p, t in enumerate(texts, 1)])

    # 2020：最新 600 页（超 400 预算，首份保底整取）→ 余量 -200 → 后面全部跳过
    put("a 大部头.pptx", datetime(2020, 12, 1, 10).timestamp(), ["kwaaa 甲"] * 600)
    put("b 幽灵.pptx", datetime(2020, 8, 1, 10).timestamp(), ["kwghost 乙"] * 10, ghost=True)
    put("c 中等.pptx", datetime(2020, 6, 1, 10).timestamp(), ["kwbbb 丙"] * 300)
    put("d 零页.pptx", datetime(2020, 1, 1, 10).timestamp(), [])
    # 2021：整年只有 0 页文件 → 有年卡但无关键词
    put("e 零页1.pptx", datetime(2021, 3, 1, 10).timestamp(), [])
    put("f 零页2.pptx", datetime(2021, 4, 1, 10).timestamp(), [])
    # 2022：最新 10 页小文件（余 390）→ 次新 600 页大部头放不下被整个跳过（不 break）
    # → 300 页中等文件继续装满（余 90）→ 200 页的又放不下被跳过
    put("g 最新小.pptx", datetime(2022, 12, 1, 10).timestamp(), ["kwrecent 庚"] * 10)
    put("h 次新大部头.pptx", datetime(2022, 8, 1, 10).timestamp(), ["kwbig 辛"] * 600)
    put("i 中等.pptx", datetime(2022, 6, 1, 10).timestamp(), ["kwmid 壬"] * 300)
    put("j 次老.pptx", datetime(2022, 3, 1, 10).timestamp(), ["kwold 癸"] * 200)
    conn.commit()

    by_year = {c.year: c for c in report_insights.yearly_chronicle(conn, stats.fetch_file_stats(conn))}
    c2020 = by_year[2020]
    labels = [k.label for k in c2020.top_keywords]
    assert "kwaaa" in labels          # 首份保底被抽到
    assert "kwbbb" not in labels      # 保底整取后余量为负，后续全跳过
    assert "kwghost" not in labels    # 幽灵文件无 pages_raw 行
    assert (c2020.deck_count, c2020.page_count) == (4, 600 + 10 + 300 + 0)
    c2021 = by_year[2021]
    assert c2021.deck_count == 2 and c2021.top_keywords == ()
    c2022 = by_year[2022]
    labels22 = [k.label for k in c2022.top_keywords]
    assert "kwrecent" in labels22     # 最新小文件优先抽到
    assert "kwmid" in labels22        # 大部头被跳过后，中等文件继续装满预算
    assert "kwbig" not in labels22    # 600 页 > 余量 390，整个跳过
    assert "kwold" not in labels22    # 200 页 > 余量 90，跳过


def test_chronicle_tab_renders_30_year_cards_offscreen(qtbot, tmp_path):
    """UI 压测：30 张年卡 offscreen 渲染 + 特殊字符转义 + 全图抓取。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    from pptx_finder.ui import report_overlay as ro
    from pptx_finder.ui import theme

    conn = db.connect(tmp_path / "ui.db")
    db.init_db(conn)
    for y in range(1997, 2027):  # 30 年
        for i in range(5):
            name = f"{y}-片{i}.pptx"
            pages = 3 + i
            if y == 2026 and i == 4:
                name = 'A&B <draft> "引号".pptx'
                pages = 50
            fid = db.upsert_file(
                conn, path=rf"C:\ui\{y}\{name}", name=name, ext=".pptx",
                size=1000 + i, mtime=datetime(y, 6, 15, 10).timestamp(),
                content_hash="h-" + name, page_count=pages, status="ok",
                error="", indexed_at=0,
            )
            db.replace_pages(conn, fid, [(p, f"kw{y}ui 界面 第{p}页", "t") for p in range(1, pages + 1)])
    conn.commit()

    overlay = ro.ReportOverlay(stats.build_report(conn), theme.tok("cloud"), conn=conn)
    qtbot.addWidget(overlay)
    overlay.show()

    t0 = time.perf_counter()
    overlay._tab_bar.setCurrentIndex(6)  # 生涯履历
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    dur = time.perf_counter() - t0
    print(f"\n[stress] 30 年卡渲染: {dur:.2f}s")

    labels = [lab.text() for lab in overlay._content.findChildren(QLabel)]
    year_titles = [t for t in labels if re.fullmatch(r"📅 \d{4} 年", t or "")]
    assert len(year_titles) == 30
    assert year_titles[0] == "📅 2026 年"  # 最新一年排最前
    locates = [b for b in overlay._content.findChildren(QPushButton) if b.text() == "定位"]
    assert len(locates) == 90  # 30 年 × 3 份代表文件
    # 特殊字符：正文富文本必须转义，tooltip 原样保留真实路径
    assert any("A&amp;B" in t and "&lt;draft&gt;" in t for t in labels)
    tips = [b.toolTip() for b in locates]
    assert any(t and t.endswith('A&B <draft> "引号".pptx') for t in tips)
    assert dur < 10.0, f"30 张年卡渲染 {dur:.1f}s 超出预算"

    # 「复制当前 Tab」的全图抓取路径：30 张年卡应拼出一张高图
    pm = overlay._grab_full_report_pixmap()
    print(f"[stress] 全图抓取: {pm.width()}x{pm.height()}")
    assert pm.width() > 0 and pm.height() > 3000
