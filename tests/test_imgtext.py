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


# ---------- 行归段 ----------
def _run(text, box, size=12.0, bold=False):
    return imgtext.TextRun(text=text, box=box, ink=(0, 0, 0),
                           background=(255, 255, 255), point_size=size, bold=bold)


def test_group_blocks_merges_stacked_lines_into_one_box(qtbot):
    """同一段的多行必须合成一个文本框——OCR 按视觉行给结果，不合段的话
    「彩色块里的多行居中文字」会散成几行各自定位、各自定字号的碎片。"""
    runs = [_run("Support in making", (100, 100, 300, 120)),
            _run("data FAIR", (145, 138, 255, 158))]
    blocks = imgtext.group_blocks(runs, 0.6)
    assert len(blocks) == 1
    assert [line.text for line in blocks[0].lines] == ["Support in making", "data FAIR"]
    assert blocks[0].box == (100, 100, 300, 158)


def test_group_blocks_infers_centre_alignment(qtbot):
    runs = [_run("Comply with", (200, 100, 300, 120)),
            _run("university, funder &", (160, 138, 340, 158)),
            _run("journal", (230, 176, 270, 196))]
    assert imgtext.group_blocks(runs, 0.6)[0].align == "ctr"


def test_group_blocks_infers_left_alignment(qtbot):
    runs = [_run("first line here", (100, 100, 300, 120)),
            _run("second", (100, 138, 190, 158))]
    assert imgtext.group_blocks(runs, 0.6)[0].align == "l"


def test_group_blocks_keeps_side_by_side_columns_apart(qtbot):
    """并排两栏不能因为 y 接近就被并成一段。"""
    runs = [_run("left column", (100, 100, 300, 120)),
            _run("right column", (700, 100, 900, 120))]
    assert len(imgtext.group_blocks(runs, 0.6)) == 2


def test_group_blocks_splits_on_large_vertical_gap(qtbot):
    runs = [_run("heading", (100, 100, 300, 120)),
            _run("body far below", (100, 400, 300, 420))]
    assert len(imgtext.group_blocks(runs, 0.6)) == 2


def test_group_blocks_splits_title_from_body(qtbot):
    """字号差一大截就不是一段，哪怕挨着。否则正文会被标题的字号带飞。"""
    runs = [_run("TITLE", (100, 100, 400, 150), size=32.0),
            _run("body text", (100, 156, 300, 168), size=9.0)]
    assert len(imgtext.group_blocks(runs, 0.6)) == 2


def test_group_blocks_gives_one_size_to_the_whole_block(qtbot):
    """同段共用一个字号：各行墨高本就有差（有无降部字母），逐行定字号会忽大忽小。"""
    runs = [_run("acemn", (100, 100, 300, 114)),
            _run("Anygq", (100, 138, 300, 160))]
    block = imgtext.group_blocks(runs, 0.6)[0]
    assert block.point_size > 0
    assert all(line is not None for line in block.lines)


def test_convert_emits_one_shape_per_block_with_explicit_breaks(qtbot, tmp_path):
    """多行段落用显式 <a:br/> 换行，不靠自动折行——原稿的断行位置一个字都不能变。"""
    image = _blank(600, 300)
    # 基线相差 32px = 1.23 倍字号，就是真实幻灯片上的常见行距
    _draw_text(image, "Support in making", 20, 40, 26)
    _draw_text(image, "data FAIR", 20, 72, 26)
    src = tmp_path / "block.png"
    image.save(str(src))
    rows = [{"text": "Support in making", "box": [16, 16, 300, 44], "score": 0.99},
            {"text": "data FAIR", "box": [16, 48, 180, 76], "score": 0.99}]
    dest = tmp_path / "block.pptx"
    result = imgtext.convert(str(src), str(dest), rows)
    assert len(result.runs) == 2 and len(result.blocks) == 1
    with zipfile.ZipFile(dest) as z:
        xml = z.read("ppt/slides/slide1.xml").decode("utf-8")
    assert xml.count("<p:sp>") == 1
    assert xml.count("<a:br/>") == 1
    assert "Support in making" in xml and "data FAIR" in xml


def test_group_blocks_keeps_light_and_dark_text_apart(qtbot):
    """字色差一大截就不是一段。表格的蓝底白字表头紧挨着白底黑字的首行数据，
    几何上完全符合同段，合错了整段会按白色排字，白底上的黑字直接消失。"""
    white = imgtext.TextRun(text="Independence Ratio", box=(700, 100, 900, 124),
                            ink=(255, 255, 255), background=(0, 76, 185), point_size=12.0)
    black = imgtext.TextRun(text="Instructive", box=(720, 140, 880, 164),
                            ink=(0, 0, 0), background=(255, 255, 255), point_size=11.0)
    assert len(imgtext.group_blocks([white, black], 0.6)) == 2


def test_block_ink_is_the_median_not_the_first_line(qtbot):
    runs = [_run("a", (100, 100, 200, 120)), _run("b", (100, 138, 200, 158)),
            _run("c", (100, 176, 200, 196))]
    runs[0].ink = (40, 0, 0)
    runs[1].ink = (0, 0, 0)
    runs[2].ink = (0, 0, 0)
    assert imgtext.group_blocks(runs, 0.6)[0].ink == (0, 0, 0)


# ---------- 抹字：跨色边与花底 ----------
def test_drop_sample_across_edge_keeps_the_one_matching_the_local_background(qtbot):
    """色块常在文字下方几像素处就结束，向下采样会拿到色块外面的颜色。"""
    blue, white = (0, 76, 185), (255, 255, 255)
    top, bottom = imgtext._drop_sample_across_edge(blue, white, blue)
    assert top == bottom == blue


def test_drop_sample_across_edge_leaves_a_real_gradient_alone(qtbot):
    """两端都离众数色不远 -> 是真的纵向渐变，插值仍然是最优解，不许瞎改。"""
    a, b = (200, 200, 200), (215, 215, 215)
    assert imgtext._drop_sample_across_edge(a, b, (208, 208, 208)) == (a, b)


def test_background_jitter_is_zero_on_a_flat_background(qtbot):
    image = _blank(400, 200)
    _draw_text(image, "hello world", 40, 100, 28)
    px = imgtext._Pixels(image)
    run = imgtext.TextRun(text="hello world", box=(38, 78, 240, 104), ink=(20, 20, 20),
                          background=(255, 255, 255), point_size=14.0)
    assert imgtext.background_jitter(px, run) < 1.0


def test_analyse_skips_text_sitting_on_a_photo(qtbot):
    """底是照片时抹不干净，只能整块填单色——那是在破坏画面，比不抹更糟。

    这里刻意造「虚化照片」而不是随机噪点：虚化的照片**局部是平滑的**，早先那版
    「能不能找到平滑的一段」的判据在实拍图上一路给满分，一个都拦不住。真正能分开
    的是**横向**是否连续，所以这里让颜色逐列缓慢起伏。
    """
    import math
    image = _blank(600, 300)
    px = imgtext._Pixels(image)
    for x in range(600):
        base = (128 + int(60 * math.sin(x / 7.0)), 120, 110 + int(50 * math.cos(x / 5.0)))
        for y in range(300):
            px.set(x, y, base)
    _draw_text(image, "photo caption", 60, 150, 28, color=(0, 0, 0))
    # 检测框留足余量：环外取色的采样带要落在干净背景上，压到降部就会把字色当成底色
    rows = [{"text": "photo caption", "box": [50, 118, 400, 168], "score": 0.99}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert result.runs == []
    assert result.skipped_busy == ["photo caption"]


def test_analyse_keeps_text_on_a_vertical_gradient(qtbot):
    """纵向渐变是横向连续的，垂直插值能精确还原——不许被上面那条守卫误伤。"""
    image = _blank(600, 300)
    px = imgtext._Pixels(image)
    for y in range(300):
        c = (40 + y // 3, 60 + y // 4, 120)
        for x in range(600):
            px.set(x, y, c)
    _draw_text(image, "gradient caption", 60, 150, 28, color=(255, 255, 255))
    rows = [{"text": "gradient caption", "box": [50, 118, 420, 168], "score": 0.99}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert [r.text for r in result.runs] == ["gradient caption"]
    assert result.skipped_busy == []


# ---------- 落点 ----------
def test_shape_uses_absolute_line_spacing_matching_the_measured_pitch(qtbot):
    """行距给绝对值（spcPts），不给相对单倍行距的百分比——单倍行距是字号的多少倍
    由字体度量决定，猜错了多行段落会整体错位二十多像素。"""
    runs = [_run("first line", (100, 100, 400, 124)),
            _run("second line", (100, 140, 400, 164))]
    block = imgtext.group_blocks(runs, 0.6)[0]
    emu_per_px = imgtext.EMU_PER_INCH * 13.3333 / 1600
    xml = imgtext._shape_xml(0, block, emu_per_px)
    assert "spcPct" not in xml
    pitch_pt = block.line_pitch * (13.3333 * 72) / 1600
    assert f'<a:spcPts val="{int(round(pitch_pt * 100))}"/>' in xml


def test_shape_left_edge_follows_the_alignment(qtbot):
    """左对齐对左边、右对齐对右边——早先一律 left = x0 - pad，每段都左偏一个 pad。"""
    import re
    emu_per_px = imgtext.EMU_PER_INCH * 13.3333 / 1600

    def geometry(block):
        xml = imgtext._shape_xml(0, block, emu_per_px)
        off = int(re.search(r'<a:off x="(\d+)"', xml).group(1))
        cx = int(re.search(r'<a:ext cx="(\d+)"', xml).group(1))
        return off / emu_per_px, cx / emu_per_px

    left, _ = geometry(imgtext.group_blocks([_run("only line", (300, 100, 700, 124))], 0.6)[0])
    assert abs(left - 300) < 1.0

    block = imgtext.group_blocks(
        [_run("aaaa", (500, 100, 700, 124)), _run("bb", (620, 140, 700, 164))], 0.6)[0]
    assert block.align == "r"
    left, width = geometry(block)
    assert abs((left + width) - 700) < 1.0          # 右边缘回到原位

    block = imgtext.group_blocks(
        [_run("wide line", (400, 100, 800, 124)), _run("mid", (550, 140, 650, 164))], 0.6)[0]
    assert block.align == "ctr"
    left, width = geometry(block)
    assert abs((left + width / 2) - 600) < 1.0      # 中线回到原位


def test_block_bold_needs_a_majority_not_a_single_line(qtbot):
    """单行的字重误判不能放大成整段。

    实测：一页 12 行的常规正文里只有一行短句被 assign_bold 误标，用 any() 就把
    整段 12 行全排成了粗体——「零误判」是字重这块唯一不让步的指标。
    """
    lines = [_run(f"line {i}", (100, 100 + i * 38, 500, 124 + i * 38)) for i in range(6)]
    lines[2].bold = True
    assert imgtext.group_blocks(lines, 0.6)[0].bold is False
    for line in lines[:4]:
        line.bold = True
    assert imgtext.group_blocks(lines, 0.6)[0].bold is True


def test_single_line_block_keeps_its_own_weight(qtbot):
    """单行段落的行为与合段之前完全一致——那是 381 条对照验证过的路径。"""
    line = _run("Title", (100, 100, 400, 140), bold=True)
    assert imgtext.group_blocks([line], 0.6)[0].bold is True


def test_analyse_skips_a_box_taller_than_it_is_wide(qtbot):
    """OCR 会把竖着排的几项并成一「行」——序号 1)2)3)4) 实测被返回成单个
    36x111 的竖框、文字 "234"，照排就成了横着的一串。整处原样保留。"""
    image = _blank(300, 400)
    for i, ch in enumerate("234"):
        _draw_text(image, ch, 40, 120 + i * 60, 34)
    rows = [{"text": "234", "box": [36, 86, 74, 200], "score": 0.98}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert result.runs == []
    assert result.skipped_shape == ["234"]


def test_analyse_keeps_a_normal_two_character_run(qtbot):
    """两字的横排短串不许被上面那条误伤。"""
    image = _blank(300, 200)
    _draw_text(image, "AB", 40, 120, 34)
    rows = [{"text": "AB", "box": [36, 86, 110, 130], "score": 0.98}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert [r.text for r in result.runs] == ["AB"]


def test_median_filter_drops_isolated_bad_samples(qtbot):
    """向外找干净背景时会撞上相邻一行的抗锯齿边缘，那种半灰像素照样「平滑」
    且离字色够远，怎么调门槛都拦不住——但它是孤立的单列，中值滤波正好对症。"""
    flat = [(243, 243, 242)] * 9
    flat[4] = (226, 226, 225)
    assert imgtext._median_filter(flat, 9) == [(243, 243, 242)] * 9


def test_median_filter_preserves_a_smooth_gradient(qtbot):
    """真渐变沿 x 平滑，中值滤波近乎恒等——不许把渐变抹平。"""
    ramp = [(100 + i, 100 + i, 100 + i) for i in range(40)]
    out = imgtext._median_filter(ramp, 9)
    assert out[20] == ramp[20]
    assert max(abs(a[0] - b[0]) for a, b in zip(out, ramp)) <= 4


def test_paint_out_leaves_a_flat_background_perfectly_flat(qtbot):
    """两行紧挨着时，上一行的字会污染下一行的采样，画出一根根细竖线。"""
    image = _blank(600, 300, color=(243, 243, 242))
    _draw_text(image, "first line of text", 40, 120, 26, color=(89, 89, 89))
    _draw_text(image, "second line of text", 40, 158, 26, color=(89, 89, 89))
    rows = [{"text": "first line of text", "box": [36, 96, 320, 128], "score": 0.99},
            {"text": "second line of text", "box": [36, 134, 340, 166], "score": 0.99}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert len(result.runs) == 2
    cleaned = imgtext.paint_out(image, result.runs)
    px = imgtext._Pixels(cleaned)
    run = result.runs[1]
    y = (run.box[1] + run.box[3]) // 2
    colours = {px.get(x, y) for x in range(run.box[0], run.box[2])}
    assert colours == {(243, 243, 242)}


# ---------- 公式：整处保留原样 ----------
def test_has_math_spots_greek_operators_and_scripts(qtbot):
    assert imgtext.has_math("σ²I")
    assert imgtext.has_math("max ∑ Ui(Ri)")
    assert imgtext.has_math("x̂ = y")          # 组合帽子，独立码点
    assert not imgtext.has_math("About the Course")
    assert not imgtext.has_math("存储压缩比3.2×")     # × 是「倍」，不是数学符号
    assert not imgtext.has_math("·要点1")            # 中文项目符号


def test_find_formula_runs_spreads_along_the_same_line(qtbot):
    """公式常被 OCR 切成好几段，往往只有一段带得动数学字符。"""
    left = _run("h=RMSGSH", (890, 566, 1057, 606))
    right = _run("SR S+σ²I", (1077, 569, 1313, 601))
    below = _run("y:Follower 实时稀疏 SRS", (827, 615, 1052, 640))
    marked = imgtext.find_formula_runs([left, right, below])
    assert marked == {0, 1}          # 同一行的两段都算，下面一行不算


def test_find_formula_runs_does_not_cross_a_wide_gap(qtbot):
    seed = _run("σ²", (100, 100, 140, 140))
    far = _run("正常文字", (600, 100, 900, 140))
    assert imgtext.find_formula_runs([seed, far]) == {0}


def test_analyse_leaves_formula_pixels_untouched(qtbot):
    """公式排不出来是结构性的（上下标是二维排版），所以整处不排字也不补洞。"""
    image = _blank(600, 300)
    _draw_text(image, "σ² + x", 60, 150, 30)
    rows = [{"text": "σ² + x", "box": [50, 118, 260, 168], "score": 0.95}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333)
    assert result.runs == []
    assert result.skipped_formula == ["σ² + x"]
    # 跳过 = 不补洞：paint_out 拿到空列表，像素一个都不动
    cleaned = imgtext.paint_out(image, result.runs)
    a, b = imgtext._Pixels(image), imgtext._Pixels(cleaned)
    assert all(a.get(x, y) == b.get(x, y)
               for y in range(118, 168, 3) for x in range(50, 260, 3))


def test_analyse_keeps_formulas_when_the_switch_is_off(qtbot):
    image = _blank(600, 300)
    _draw_text(image, "σ² + x", 60, 150, 30)
    rows = [{"text": "σ² + x", "box": [50, 118, 260, 168], "score": 0.95}]
    result = imgtext.analyse(image, rows, slide_width_in=13.3333, skip_formula=False)
    assert [r.text for r in result.runs] == ["σ² + x"]
    assert result.skipped_formula == []


# ---------- 抹除框沿边扩张 ----------
def test_grow_erase_box_covers_ink_clipped_outside_the_box(qtbot):
    """OCR 的框会切掉标点的尾巴——中文全角逗号「，」的尾巴常拖在框外，
    框外那一截我们从来不看，画面上就留下一小撮毛刺。"""
    image = _blank(400, 200)
    _fill(image, (100, 60, 300, 100), (200, 0, 0))      # 框内的「字」
    _fill(image, (150, 100, 160, 112), (200, 0, 0))     # 拖到框外的尾巴
    px = imgtext._Pixels(image)
    grown = imgtext.grow_erase_box(px, (100, 60, 300, 100), (200, 0, 0), (255, 255, 255))
    assert grown[3] >= 112                               # 下边扩到把尾巴包进去
    assert grown[1] == 60 and grown[0] == 100            # 其余三边不动


def test_grow_erase_box_stops_at_a_neighbouring_line(qtbot):
    """相邻一行的墨量会占满整个框宽，那是撞上邻行，不能吞。"""
    image = _blank(400, 200)
    _fill(image, (100, 60, 300, 90), (0, 0, 0))
    _fill(image, (100, 96, 300, 126), (0, 0, 0))        # 紧挨着的下一行
    px = imgtext._Pixels(image)
    grown = imgtext.grow_erase_box(px, (100, 60, 300, 90), (0, 0, 0), (255, 255, 255))
    assert grown[3] < 96


def test_grow_erase_box_refuses_to_grow_inside_dense_graphics(qtbot):
    """扩到上限还没干净 = 身处一片密集图形里，扩下去会啃掉图形，那一侧就不扩。"""
    image = _blank(400, 200, color=(0, 0, 0))           # 整张全是「墨」
    px = imgtext._Pixels(image)
    box = (100, 60, 300, 100)
    assert imgtext.grow_erase_box(px, box, (0, 0, 0), (255, 255, 255)) == box


def test_group_blocks_needs_the_lines_to_share_an_edge(qtbot):
    """一段文字总是沿某条边码齐；三条边都对不上就不是一段。

    实测这条挡的是：图表的顶部居中标题被并进右侧图例——那一对在另外四个判据上
    全部刚好擦边通过，合完整段判成右对齐，标题被拉到右边还缩了字号。
    """
    title = _run("典型用户切换期BLER波动（示例）", (351, 26, 1242, 83), size=34.9)
    legend = _run("频繁切换(现网初始化)", (1116, 148, 1457, 186), size=22.9)
    assert len(imgtext.group_blocks([title, legend], 0.6)) == 2


def test_group_blocks_tolerates_an_indented_continuation_line(qtbot):
    """项目符号的续行会缩进一个行高左右，那仍是同一段，不许被上面那条误伤。"""
    first = _run("·The findings highlight the importance", (100, 100, 700, 130))
    cont = _run("exploitation rate in forward-looking", (136, 140, 700, 170))
    assert len(imgtext.group_blocks([first, cont], 0.6)) == 1


# ---------- 相邻行墨迹框重叠 ----------
def test_resolve_vertical_overlaps_splits_at_the_midline(qtbot):
    """行距紧的版式上，OCR 的框会连带框住上下行的笔画，量出的字高被撑大两三成，
    字号跟着放大，排出来行行叠在一起。重叠区对半分。"""
    a = _run("上面这一行", (100, 100, 500, 146))
    b = _run("下面这一行", (100, 139, 500, 185))
    n = imgtext.resolve_vertical_overlaps([a, b], 0.6)
    assert n == 1
    assert a.box[3] == b.box[1]          # 切在中线上，不再重叠
    assert a.box[1] == 100 and b.box[3] == 185


def test_resolve_vertical_overlaps_leaves_side_by_side_columns_alone(qtbot):
    a = _run("左栏", (100, 100, 300, 146))
    b = _run("右栏", (700, 139, 900, 185))
    assert imgtext.resolve_vertical_overlaps([a, b], 0.6) == 0
    assert a.box == (100, 100, 300, 146)


def test_resolve_vertical_overlaps_refuses_a_deep_overlap(qtbot):
    """重叠过半说明不是「吃到邻行」，乱切会切掉真正的笔画。"""
    a = _run("甲", (100, 100, 500, 150))
    b = _run("乙", (100, 105, 500, 155))
    assert imgtext.resolve_vertical_overlaps([a, b], 0.6) == 0


def test_resolve_vertical_overlaps_recomputes_the_point_size(qtbot):
    a = _run("上面这一行", (100, 100, 500, 146))
    b = _run("下面这一行", (100, 139, 500, 185))
    before = a.point_size
    imgtext.resolve_vertical_overlaps([a, b], 0.6)
    assert a.point_size != before        # 字高变了，字号必须跟着重算


# ---------- 同一块像素的重复检测 ----------
def test_drop_nested_duplicates_removes_a_substring_box(qtbot):
    whole = _run("推理token", (100, 100, 300, 131))
    part = _run("推理", (100, 108, 160, 120))
    kept = imgtext.drop_nested_duplicates([whole, part])
    assert [r.text for r in kept] == ["推理token"]


def test_drop_nested_duplicates_keeps_the_higher_confidence_one(qtbot):
    a = _run("Score1st", (100, 100, 300, 140)); a.score = 0.86
    b = _run("测填score1note", (105, 105, 305, 145)); b.score = 0.97
    kept = imgtext.drop_nested_duplicates([a, b])
    assert [r.text for r in kept] == ["测填score1note"]


def test_drop_nested_duplicates_keeps_merely_touching_boxes(qtbot):
    """相邻两行只轻微相交，是正常版式，不能当成重复丢掉。"""
    a = _run("上面这一行", (100, 100, 500, 146))
    b = _run("下面这一行", (100, 142, 500, 188))
    assert len(imgtext.drop_nested_duplicates([a, b])) == 2
