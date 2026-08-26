"""图片文字可编辑化：算法层与产物结构的回归。

全部用合成图像，不依赖识别组件，因此在没装 OCR 的机器/CI 上照样能跑。
每个用例对应一个真实踩过的坑，坑的现场记在 docstring 里。
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from pptx_finder import imgtext

NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _blank(width=800, height=450, color=(255, 255, 255)) -> QImage:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(*color))
    return image


def _draw_text(image: QImage, text: str, x: int, y: int, px_size: int,
               color=(20, 20, 20), bold=False) -> None:
    painter = QPainter(image)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(px_size)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(*color))
    painter.drawText(x, y, text)
    painter.end()


def _fill(image: QImage, box, color) -> None:
    painter = QPainter(image)
    painter.fillRect(box[0], box[1], box[2] - box[0], box[3] - box[1], QColor(*color))
    painter.end()


def _gradient(width=800, height=450, left=(20, 60, 110), right=(200, 60, 45)) -> QImage:
    image = QImage(width, height, QImage.Format_RGB32)
    px = imgtext._Pixels(image)
    for x in range(width):
        t = x / max(1, width - 1)
        c = tuple(int(left[i] + (right[i] - left[i]) * t) for i in range(3))
        for y in range(height):
            px.set(x, y, c)
    return image


# ---------- 墨迹测量与取色 ----------
def test_measure_run_finds_tight_ink_box_and_colours(qtbot):
    image = _blank()
    _draw_text(image, "测试文本ABC", 100, 120, 30)
    px = imgtext._Pixels(image)
    # 检测框刻意给得比文字宽松，模拟 OCR 的不定留白
    tight, ink, background, stroke, coverage = imgtext.measure_run(px, (80, 80, 420, 150))

    assert tight is not None
    assert tight[0] >= 90 and tight[1] >= 90          # 收紧到了实际墨迹
    assert tight[2] - tight[0] < 340                  # 明显窄于检测框
    assert background == (255, 255, 255)
    assert sum(ink) < 200                             # 深色字
    assert stroke > 0
    assert 0.05 < coverage < 0.65                     # 正常文字的覆盖率区间


def test_measure_run_reports_high_coverage_for_solid_block(qtbot):
    """墨/底判反的现场：检测框越过色块边界时，色块本身会被当成「墨」。

    真实案例是红圆圈里的白数字——框略微出圈，环外采样拿到圈外的白，于是
    整个红圆成了墨，被当作文字擦掉。覆盖率是识破它的判据。
    """
    image = _blank()
    _fill(image, (100, 100, 200, 160), (200, 30, 30))
    px = imgtext._Pixels(image)
    _tight, _ink, _bg, _stroke, coverage = imgtext.measure_run(px, (95, 95, 205, 165))
    assert coverage > imgtext.MAX_INK_COVERAGE


def test_analyse_skips_solid_block_instead_of_painting_it_out(qtbot):
    image = _blank()
    _fill(image, (100, 100, 200, 160), (200, 30, 30))
    rows = [{"text": "2", "box": (95, 95, 205, 165), "score": 0.99}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert result.runs == []
    assert result.skipped_shape == ["2"]


def test_measure_run_returns_none_when_ink_matches_background(qtbot):
    image = _blank(color=(250, 250, 250))
    px = imgtext._Pixels(image)
    tight, _ink, _bg, _stroke, _cov = imgtext.measure_run(px, (10, 10, 200, 60))
    assert tight is None


# ---------- 字号 ----------
def test_fit_point_size_round_trips_within_tolerance(qtbot):
    """按墨迹高度反解字号：反解出来的字号排出来应当还原原来的墨高。"""
    pt_per_px = (13.3333 * 72) / 1600
    for ink_h in (14, 22, 36, 60):
        size = imgtext.fit_point_size("测试文本", ink_h, 10 ** 6, pt_per_px)
        font = QFont(imgtext.DEFAULT_FONT)
        font.setPointSizeF(size)
        from PySide6.QtGui import QFontMetricsF
        got_px = QFontMetricsF(font).tightBoundingRect("测试文本").height()
        want_px = ink_h * pt_per_px * imgtext.PX_PER_PT
        assert abs(got_px - want_px) <= max(1.0, want_px * 0.05)


def test_fit_point_size_skips_width_clamp_for_short_text(qtbot):
    """单字符不做宽度校正。

    现场：'3' 的墨迹框 15x25，宽度校正把 14.6pt 砍成 8.8pt（-40%），
    圆圈里的数字于是又小又歪。宽度校正是防「OCR 多认了字导致溢出」的，
    对单字形毫无信息量。
    """
    pt_per_px = (13.3333 * 72) / 1600
    narrow = imgtext.fit_point_size("3", 25, 1, pt_per_px)      # 宽度目标给到极窄
    free = imgtext.fit_point_size("3", 25, 10 ** 6, pt_per_px)
    assert narrow == pytest.approx(free)
    # 多字符仍然会被收窄
    clamped = imgtext.fit_point_size("多字符文本", 25, 1, pt_per_px)
    unclamped = imgtext.fit_point_size("多字符文本", 25, 10 ** 6, pt_per_px)
    assert clamped < unclamped


def test_analyse_skips_text_below_min_point_size(qtbot):
    """低于阈值的文字留在背景图里，不做成文本框（图表刻度就是这么被挡掉的）。"""
    image = _blank()
    _draw_text(image, "刻度标签", 100, 130, 20)
    rows = [{"text": "刻度标签", "box": (95, 100, 220, 140), "score": 0.99}]
    kept = imgtext.analyse(image, rows, slide_width_in=13.3333, min_point_size=1.0)
    assert [r.text for r in kept.runs] == ["刻度标签"]

    dropped = imgtext.analyse(image, rows, slide_width_in=13.3333,
                              min_point_size=kept.runs[0].point_size + 5)
    assert dropped.runs == []
    assert dropped.skipped_small == ["刻度标签"]


def test_analyse_drops_low_confidence_rows(qtbot):
    image = _blank()
    _draw_text(image, "文本", 100, 120, 30)
    rows = [{"text": "文本", "box": (90, 90, 200, 130), "score": 0.1}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert result.runs == []
    assert result.skipped_score == ["文本"]


# ---------- 字重 ----------
def test_assign_bold_uses_all_runs_as_baseline_not_only_large(qtbot):
    """基线必须取全体，不能只取大字。

    只在大字里取中位数时，大字本来就更可能是粗体，基线被自己抬高，
    结果标题反而够不着阈值——实测 demo 图上标题与卡头全部漏判。
    """
    runs = [imgtext.TextRun(text=f"t{i}", box=(0, 0, 10, 10), ink=(0, 0, 0),
                            background=(255, 255, 255), point_size=20.0)
            for i in range(6)]
    # 前两条是粗体（笔画显著更宽），后四条是常规
    ratios = [0.170, 0.160, 0.095, 0.092, 0.090, 0.094]
    imgtext.assign_bold(runs, ratios)
    assert [r.bold for r in runs] == [True, True, False, False, False, False]


def test_assign_bold_marks_nothing_when_page_is_uniform(qtbot):
    """整页同一字重时不该判出粗体——自校准的意义就在这里。"""
    runs = [imgtext.TextRun(text=f"t{i}", box=(0, 0, 10, 10), ink=(0, 0, 0),
                            background=(255, 255, 255), point_size=20.0)
            for i in range(6)]
    imgtext.assign_bold(runs, [0.150] * 6)
    assert not any(r.bold for r in runs)


def test_assign_bold_ignores_small_text(qtbot):
    """小字的笔画只有 1~2 像素，比值被量化误差淹没；381 条真实字重对照里，
    不设字号下限时误判 26 条，设了下限误判 0 条。"""
    runs = [imgtext.TextRun(text=f"t{i}", box=(0, 0, 10, 10), ink=(0, 0, 0),
                            background=(255, 255, 255),
                            point_size=imgtext.BOLD_MIN_PT - 1)
            for i in range(6)]
    imgtext.assign_bold(runs, [0.30, 0.30, 0.09, 0.09, 0.09, 0.09])
    assert not any(r.bold for r in runs)


# ---------- 背景补洞 ----------
def _run_at(box, ink=(20, 20, 20), background=(255, 255, 255)):
    return imgtext.TextRun(text="x", box=box, ink=ink, background=background,
                           point_size=12.0)


def test_paint_out_removes_text_on_flat_background(qtbot):
    image = _blank()
    _draw_text(image, "会被抹掉的文字", 100, 120, 28)
    px = imgtext._Pixels(image)
    tight, ink, background, _s, _c = imgtext.measure_run(px, (80, 80, 500, 140))
    cleaned = imgtext.paint_out(image, [_run_at(tight, ink, background)])
    out = imgtext._Pixels(cleaned)
    for y in range(tight[1], tight[3]):
        for x in range(tight[0], tight[2], 3):
            assert imgtext._sq_dist(out.get(x, y), (255, 255, 255)) < 400


def test_paint_out_preserves_horizontal_gradient(qtbot):
    """渐变底是这套方案的地基。

    早期「整块填众数色」在渐变上会留下肉眼可见的矩形补丁——补完之后
    框内颜色必须仍然沿用该列原本的渐变值。
    """
    image = _gradient()
    before = imgtext._Pixels(image)
    box = (100, 200, 700, 250)
    expected = [before.get(x, 225) for x in range(box[0], box[2], 25)]
    cleaned = imgtext.paint_out(image, [_run_at(box, (255, 255, 255), (110, 60, 78))])
    after = imgtext._Pixels(cleaned)
    for k, x in enumerate(range(box[0], box[2], 25)):
        assert imgtext._sq_dist(after.get(x, 225), expected[k]) < 900


def test_paint_out_does_not_smear_neighbouring_line_ink(qtbot):
    """行距很小时，上下沿采样带会正好压在相邻一行的文字上。

    现场：右栏两行间距只有 3 像素，补洞把邻行的红色一路插值涂满整个框，
    表现为一列列的红色竖条纹。采样必须同时满足「平滑」和「远离字色」。
    """
    image = _blank()
    _draw_text(image, "上面一行文字", 100, 120, 26, color=(214, 0, 0))
    _draw_text(image, "下面一行文字", 100, 152, 26, color=(214, 0, 0))
    px = imgtext._Pixels(image)
    top, ink, background, _s, _c = imgtext.measure_run(px, (95, 95, 400, 128))
    cleaned = imgtext.paint_out(image, [_run_at(top, ink, background)])
    out = imgtext._Pixels(cleaned)
    reds = sum(1 for y in range(top[1], top[3]) for x in range(top[0], top[2])
               if imgtext._sq_dist(out.get(x, y), (214, 0, 0)) < 3000)
    assert reds == 0, "补洞把相邻行的墨色涂进了框里"


# ---------- 产物结构 ----------
def _make_conversion(tmp_path, rows=None):
    image = _blank(1600, 900)
    _draw_text(image, "标题文字", 100, 120, 40)
    _draw_text(image, "正文内容 ABC 123", 100, 240, 24)
    rows = rows or [
        {"text": "标题文字", "box": (90, 80, 400, 135), "score": 0.99},
        {"text": "正文内容 ABC 123", "box": (90, 205, 520, 255), "score": 0.97},
    ]
    src = tmp_path / "src.png"
    image.save(str(src))
    dest = tmp_path / "out.pptx"
    return image, rows, src, dest


def test_convert_writes_a_readable_single_slide_pptx(qtbot, tmp_path):
    _image, rows, src, dest = _make_conversion(tmp_path)
    result = imgtext.convert(str(src), str(dest), rows)
    assert len(result.runs) == 2
    assert dest.is_file()

    with zipfile.ZipFile(dest) as z:
        assert z.testzip() is None
        names = set(z.namelist())
        assert "ppt/slides/slide1.xml" in names
        assert "ppt/media/imgtext1.png" in names
        assert 'Extension="png"' in z.read("[Content_Types].xml").decode("utf-8")
        rels = z.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8")
        assert "imgtext1.png" in rels
        root = ET.fromstring(z.read("ppt/slides/slide1.xml"))
        assert len(root.findall(f".//{NS_P}pic")) == 1
        texts = [t.text for t in root.iter(f"{NS_A}t")]
        assert texts == ["标题文字", "正文内容 ABC 123"]


def test_convert_sets_slide_size_from_image_aspect(qtbot, tmp_path):
    """输入不是 16:9 时，幻灯片尺寸必须跟着图片走，否则背景图会被拉变形。"""
    image = _blank(1000, 1000)
    _draw_text(image, "方图", 100, 120, 40)
    src = tmp_path / "square.png"
    image.save(str(src))
    dest = tmp_path / "square.pptx"
    imgtext.convert(str(src), str(dest),
                    [{"text": "方图", "box": (90, 80, 260, 135), "score": 0.99}])
    with zipfile.ZipFile(dest) as z:
        presentation = z.read("ppt/presentation.xml").decode("utf-8")
    import re
    match = re.search(r'<p:sldSz cx="(\d+)" cy="(\d+)"', presentation)
    assert match
    cx, cy = int(match.group(1)), int(match.group(2))
    assert cx == cy


def test_convert_escapes_xml_special_characters(qtbot, tmp_path):
    image = _blank(1600, 900)
    _draw_text(image, "A&B<C>", 100, 120, 40)
    src = tmp_path / "esc.png"
    image.save(str(src))
    dest = tmp_path / "esc.pptx"
    imgtext.convert(str(src), str(dest),
                    [{"text": 'A&B<C>"D"', "box": (90, 80, 400, 135), "score": 0.99}])
    with zipfile.ZipFile(dest) as z:
        root = ET.fromstring(z.read("ppt/slides/slide1.xml"))
    assert [t.text for t in root.iter(f"{NS_A}t")] == ['A&B<C>"D"']


def test_convert_leaves_source_image_untouched(qtbot, tmp_path):
    """补洞只能作用于产物里的背景图，绝不能改用户的原图。"""
    _image, rows, src, dest = _make_conversion(tmp_path)
    before = src.read_bytes()
    imgtext.convert(str(src), str(dest), rows)
    assert src.read_bytes() == before
