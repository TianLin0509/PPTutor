"""stats 单元测试：趣味统计的纯计算层（肝度/改版/规模/称号）。

设计：核心是纯函数（输入 FileStat 列表，输出统计 dataclass），
不依赖真实 pptx，时间相关用确定性的本地时间戳构造。
"""
from __future__ import annotations

from datetime import datetime

from pptx_finder import db, stats


def _ts(y: int, mo: int, d: int, h: int, mi: int = 0) -> float:
    """本地时区的 Unix 时间戳（与 stats 内 fromtimestamp 一致）。"""
    return datetime(y, mo, d, h, mi).timestamp()


def _fs(*, name="deck.pptx", mtime=0.0, size=1000, page_count=10,
        status="ok", group_id=None, char_count=100) -> "stats.FileStat":
    return stats.FileStat(name=name, mtime=mtime, size=size, page_count=page_count,
                          status=status, group_id=group_id, char_count=char_count)


# ---------- ① 肝度 night_owl ----------

def test_night_owl_counts_late_night_files():
    # 深夜定义 [22:00, 06:00)
    files = [
        _fs(mtime=_ts(2026, 6, 1, 2)),    # 凌晨2点 → 深夜
        _fs(mtime=_ts(2026, 6, 1, 23)),   # 23点   → 深夜
        _fs(mtime=_ts(2026, 6, 1, 15)),   # 下午3点 → 否
        _fs(mtime=_ts(2026, 6, 1, 10)),   # 上午10点 → 否
    ]
    r = stats.night_owl(files)
    assert r.night_count == 2
    assert r.night_ratio == 0.5


def test_night_owl_counts_weekend_files():
    files = [
        _fs(mtime=_ts(2026, 6, 6, 15)),   # 周六
        _fs(mtime=_ts(2026, 6, 7, 15)),   # 周日
        _fs(mtime=_ts(2026, 6, 1, 15)),   # 周一
    ]
    r = stats.night_owl(files)
    assert r.weekend_count == 2


def test_night_owl_latest_is_deepest_predawn():
    """最晚一次 = 深夜序里最靠后的（凌晨越深越狠）。"""
    files = [
        _fs(name="a.pptx", mtime=_ts(2026, 6, 1, 23)),   # 23点
        _fs(name="b.pptx", mtime=_ts(2026, 6, 2, 4)),    # 凌晨4点 → 最狠
        _fs(name="c.pptx", mtime=_ts(2026, 6, 1, 22)),   # 22点
    ]
    r = stats.night_owl(files)
    assert r.latest_name == "b.pptx"
    assert r.latest_hour == 4


def test_night_owl_empty_when_no_night_files():
    files = [_fs(mtime=_ts(2026, 6, 1, 14)), _fs(mtime=_ts(2026, 6, 1, 9))]
    r = stats.night_owl(files)
    assert r.night_count == 0
    assert r.latest_name is None


# ---------- ① 肝度 heatmap（7×24）----------

def test_heatmap_buckets_by_weekday_and_hour():
    files = [
        _fs(mtime=_ts(2026, 6, 1, 2)),    # 周一(0) 凌晨2点
        _fs(mtime=_ts(2026, 6, 1, 2)),    # 周一(0) 凌晨2点 → 同格 +1
        _fs(mtime=_ts(2026, 6, 7, 14)),   # 周日(6) 14点
    ]
    m = stats.heatmap(files)
    assert len(m) == 7
    assert all(len(row) == 24 for row in m)
    assert m[0][2] == 2    # 周一凌晨2点 2 份
    assert m[6][14] == 1   # 周日14点 1 份
    assert m[3][9] == 0    # 没碰过的格子


# ---------- ③ 改版名场面 version_drama ----------

def test_version_drama_most_revised_group():
    """最能改奖 = 成员最多的版本组；代表名取组内最新一版。"""
    files = [
        _fs(name="述职v1.pptx", mtime=_ts(2026, 6, 1, 10), group_id=7),
        _fs(name="述职v2.pptx", mtime=_ts(2026, 6, 2, 10), group_id=7),
        _fs(name="述职终版.pptx", mtime=_ts(2026, 6, 3, 10), group_id=7),
        _fs(name="周报.pptx", mtime=_ts(2026, 6, 1, 10), group_id=9),
    ]
    r = stats.version_drama(files)
    assert r.top_group_versions == 3
    assert r.top_group_name == "述职终版.pptx"


def test_version_drama_final_curse_count():
    files = [
        _fs(name="方案最终版.pptx"),
        _fs(name="方案final.pptx"),
        _fs(name="方案v2.pptx"),
        _fs(name="正常命名.pptx"),
    ]
    r = stats.version_drama(files)
    assert r.final_curse_count == 3
    assert r.final_curse_ratio == 0.75


def test_version_drama_zombie_is_oldest():
    files = [
        _fs(name="新.pptx", mtime=_ts(2026, 6, 10, 10)),
        _fs(name="老古董.pptx", mtime=_ts(2020, 1, 1, 10)),
    ]
    r = stats.version_drama(files)
    assert r.zombie_name == "老古董.pptx"


def test_version_drama_no_real_groups():
    """单成员组 / 无组 → 没有最能改。"""
    files = [_fs(name="a.pptx", group_id=None), _fs(name="b.pptx", group_id=5)]
    r = stats.version_drama(files)
    assert r.top_group_name is None
    assert r.top_group_versions == 0


# ---------- ⑤ 规模仓鼠 scale ----------

def test_scale_longest_and_biggest():
    files = [
        _fs(name="小册子.pptx", page_count=5, size=1000),
        _fs(name="百页巨制.pptx", page_count=120, size=50_000),
        _fs(name="中等.pptx", page_count=30, size=8000),
    ]
    r = stats.scale(files)
    assert r.longest_name == "百页巨制.pptx"
    assert r.longest_pages == 120
    assert r.biggest_name == "百页巨制.pptx"
    assert r.biggest_bytes == 50_000


def test_scale_totals():
    files = [
        _fs(char_count=300_000, size=1000),
        _fs(char_count=430_000, size=2000),
    ]
    r = stats.scale(files)
    assert r.total_chars == 730_000
    assert r.total_bytes == 3000
    assert r.deck_count == 2


# ---------- ⑥ 人格称号 persona ----------

def _night(ratio=0.0, w=0.0):
    return stats.NightOwlStat(night_count=0, night_ratio=ratio, weekend_count=0,
                              weekend_ratio=w, latest_name=None, latest_hour=None)


def _drama(curse=0.0, versions=0):
    return stats.VersionDramaStat(top_group_name=None, top_group_versions=versions,
                                  final_curse_count=0, final_curse_ratio=curse,
                                  zombie_name=None, zombie_mtime=0.0)


def _scale(chars=1000, decks=5):
    return stats.ScaleStat(longest_name="a", longest_pages=10, biggest_name="a",
                           biggest_bytes=100, total_chars=chars, total_bytes=100,
                           deck_count=decks)


def test_persona_night_owl_is_primary_title():
    p = stats.persona(_night(ratio=0.5), _drama(), _scale())
    assert p.title == "深夜画师"


def test_persona_collects_other_hits_as_badges():
    p = stats.persona(_night(ratio=0.5), _drama(curse=0.5), _scale())
    assert p.title == "深夜画师"
    assert "终版收割机" in p.badges


def test_persona_default_when_nothing_stands_out():
    # avg 字数 = 1000/5 = 200，不触发极简（<200）；其它都不命中
    p = stats.persona(_night(), _drama(), _scale(chars=1000, decks=5))
    assert p.title and p.title not in p.badges
    assert p.badges == []


# ---------- db 访问层 + 报告组装 ----------

def test_fetch_file_stats_joins_chars_and_group(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    fid = db.upsert_file(conn, path="/x/述职.pptx", name="述职.pptx", ext=".pptx",
                         size=2048, mtime=_ts(2026, 6, 1, 2), content_hash="h",
                         page_count=12, status="ok", error="", indexed_at=0)
    db.replace_pages(conn, fid, [(1, "你好世界", "t"), (2, "昇腾算力", "t")])
    conn.execute("INSERT INTO minhash(file_id, sig, page_hashes, group_id) VALUES(?,?,?,?)",
                 (fid, b"", "[]", 3))
    conn.commit()
    files = stats.fetch_file_stats(conn)
    assert len(files) == 1
    f = files[0]
    assert f.name == "述职.pptx"
    assert f.page_count == 12
    assert f.group_id == 3
    assert f.char_count == 8  # len(你好世界)+len(昇腾算力)，LENGTH 按字符计


def test_fetch_file_stats_null_group_and_no_pages(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    db.upsert_file(conn, path="/y.pptx", name="y.pptx", ext=".pptx", size=10,
                   mtime=_ts(2026, 6, 1, 2), content_hash="h", page_count=0,
                   status="encrypted", error="", indexed_at=0)
    conn.commit()
    f = stats.fetch_file_stats(conn)[0]
    assert f.group_id is None
    assert f.char_count == 0


def _put(conn, name, mtime, **kw):
    return db.upsert_file(conn, path="/" + name, name=name, ext=".pptx",
                          size=kw.get("size", 1000), mtime=mtime, content_hash="h",
                          page_count=kw.get("page_count", 5), status="ok",
                          error="", indexed_at=0)


def test_build_report_filters_by_year(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    _put(conn, "a.pptx", _ts(2026, 6, 1, 2))
    _put(conn, "b.pptx", _ts(2020, 1, 1, 2))
    conn.commit()
    rep = stats.build_report(conn, year=2026)
    assert rep.deck_count == 1
    assert rep.scope_year == 2026


def test_build_report_all_history_assembles_everything(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    db.init_db(conn)
    _put(conn, "a.pptx", _ts(2026, 6, 1, 2))
    _put(conn, "b.pptx", _ts(2020, 1, 1, 2))
    conn.commit()
    rep = stats.build_report(conn, year=None)
    assert rep.deck_count == 2
    assert rep.scope_year is None
    assert rep.persona.title
    assert len(rep.heatmap) == 7
