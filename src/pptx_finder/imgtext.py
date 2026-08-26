"""图片文字可编辑化：一张图 -> 一页 PPTX，图上的文字变成真正的文本框。

只做一件事：把图片里的文字变成可编辑文本。图形、图表、配色、版式一律原样留在
背景图里，不做识别、不做重建——这是刻意的范围约束，不是能力缺失。

管线（全程离线、确定性、无 LLM）：
    OCR 给出行框与文字
      -> 墨迹测量：在行框内扫出真正的紧致外接矩形（OCR 的框留白不定，同版同号字
         能给出 27~39 像素的框高，直接用会让字号忽大忽小）
      -> 取色：环外众数=背景，框内离背景最远的一撮中位数=字色
      -> 可行性守卫：墨/底判反（MAX_INK_COVERAGE）与花底（MAX_BACKGROUND_JITTER）
         一律**原样保留**，不排字也不补洞
      -> 背景补洞：按列取上下沿颜色做垂直插值（纯色/横向渐变/纵向渐变都还原得掉；
         早期用「整块填众数色」，渐变底上补丁一眼可见）
      -> 字号反解：用真实字体度量二分，让排出来的墨迹高度对上量到的墨迹高度
      -> 字重：同版笔画宽度相对比较，只判大字（零误判优先，见 assign_bold）
      -> 行归段：垂直相邻、同列、同色、字号相近的行并成一段，共用字号、反推对齐
         （见 group_blocks——不归段的话彩色块里的多行居中文字会散成碎片）
      -> 写 PPTX：往空白模板里注入背景图 + 每段一个文本框，行距给绝对值、
         首行落点按 PowerPoint 实测标定反解（见 _shape_xml）

**跳过永远是安全的**：被跳过的文字原样留在背景图里，不会丢失，只是不可编辑。
这条是全模块的取舍基准——宁可少排一处，不可把画面弄花。

已知限制（都不是本模块能修的，属于 OCR 识别层）：
  · 长而低对比的标题偶尔被 OCR 截断，只擦中段会留下半截原文（34 张实测 1 例）
  · 个别行的词间空格丢失、变音符号丢失
  · 行内粗体/多色混排会被拉平成整行一种样式

依赖上刻意只用 PySide6(QImage/QFontMetricsF) 与标准库 zipfile —— 两者本来就在，
不为这个功能引入 Pillow / python-pptx / opencv。OCR 在独立 sidecar 进程里（见
imgtext_ocr.py），主程序不碰 onnxruntime。
"""
from __future__ import annotations

import logging
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage

from .config import resource_path

log = logging.getLogger(__name__)

EMU_PER_INCH = 914400
POINTS_PER_INCH = 72.0
PX_PER_PT = 96.0 / 72.0          # QFontMetricsF 用设备像素，OCR 框换算出来是磅
DEFAULT_FONT = "Microsoft YaHei UI"
TEMPLATE_ASSET = ("assets", "blank_16x9.pptx")

#: 低于这个磅值的文字留在背景图里，不做成文本框——图表坐标轴刻度常在 4~6pt，
#: 变成几十个浮在图表上的微型文本框只会碍事。
#: 别往上调：24 张真实幻灯片实测，密集排版稿的**正文**就落在 7~9pt
#: （1600px 渲染 = 120 DPI），阈值设 9.0 会一次砍掉 41/58 处真实正文。
#: 被跳过的文字原样留在背景图里，不会丢失，只是不可编辑。
DEFAULT_MIN_PT = 6.0
#: OCR 置信度低于此值直接丢弃。刻意设得低：本地模型在清晰稿上普遍 >0.93，而
#: 少数正文（公式里的单个字母、页码）会掉到 0.72~0.85，宁可多出一个可以一键删掉的
#: 文本框，也不要把真实内容判没。
DEFAULT_MIN_SCORE = 0.50
#: 墨迹占紧致框的比例超过这个值 -> 被当成「墨」的其实是一整块实心色（圆圈底、色块），
#: 也就是墨与底判反了。此时**既不排字也不补洞**，原样保留。
#: 判反怎么发生的：检测框略微越过色块边界时，环外采样拿到的是色块外面的白，于是
#: 色块本身成了「离底色最远的一撮」= 墨。红圆圈里的白数字正是这样被整块擦掉的。
#: 710 处真实 run 实测：中位覆盖率 0.32、95% 分位 0.55，而所有判反的样本都 >0.67
#: （'2' 0.81、图标 'O' 0.80、'中心云DC' 0.82、'亮点' 1.00）。
#: 跳过永远是安全的——像素原样保留，代价只是这一处不可编辑。
MAX_INK_COVERAGE = 0.65
#: 背景横向抖动超过这个值 -> 底是照片/纹理，抹不干净，整处**原样保留**。
#: 见 background_jitter 的实测分布。跳过的代价只是这一处不可编辑；不跳过的代价
#: 是照片上被刷出一片白方块和彩色竖条——后者是不可接受的画面破坏。
MAX_BACKGROUND_JITTER = 4.0
#: 墨迹比自己还高、且不止一个字 -> OCR 把竖着排的几项并成了一「行」。
#: 实测：一页里竖排的序号 1)2)3)4) 被 RapidOCR 返回成单个 36x111 的竖框、文字
#: "234"，照排就成了横着的一串。883 处「两字及以上」的墨迹里宽高比低于 1.0 的
#: 只有 2 处（0.32 与 0.87），下一档就跳到 1.30，阈值取在这个空档上。
#: 跳过即原样保留——竖排内容留在背景图里，看起来和原稿一模一样。
MIN_ASPECT_RATIO = 1.0


@dataclass
class TextRun:
    """一处将被排成文本框的文字。坐标单位是源图像素。"""

    text: str
    box: tuple[int, int, int, int]      # 墨迹紧致外接矩形 (x0, y0, x1, y1)
    ink: tuple[int, int, int]
    background: tuple[int, int, int]
    point_size: float
    bold: bool = False
    score: float = 1.0

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> int:
        return self.box[3] - self.box[1]


@dataclass
class Conversion:
    """一次转换的结果与自检数据。"""

    runs: list[TextRun] = field(default_factory=list)
    blocks: list = field(default_factory=list)
    skipped_blank: list[str] = field(default_factory=list)
    skipped_shape: list[str] = field(default_factory=list)
    skipped_busy: list[str] = field(default_factory=list)
    skipped_small: list[str] = field(default_factory=list)
    skipped_score: list[str] = field(default_factory=list)
    image_size: tuple[int, int] = (0, 0)

    def summary(self) -> str:
        boxes = len(self.blocks) if self.blocks else len(self.runs)
        return (
            f"排出文本框 {boxes} 个 / 文字行 {len(self.runs)} 处；"
            f"跳过：无可读墨迹 {len(self.skipped_blank)}、"
            f"疑似图形或竖排 {len(self.skipped_shape)}、"
            f"底为图像 {len(self.skipped_busy)}、"
            f"字号过小 {len(self.skipped_small)}、"
            f"识别存疑 {len(self.skipped_score)}"
        )


# ---------- 像素读写：直接吃 QImage 的缓冲区，不做 per-pixel 的 Qt 调用 ----------
class _Pixels:
    """Format_RGB32 缓冲区的轻量随机访问。内存里是 BGRA，小端。"""

    __slots__ = ("view", "stride", "width", "height")

    def __init__(self, image: QImage):
        self.view = image.bits()
        self.stride = image.bytesPerLine()
        self.width = image.width()
        self.height = image.height()

    def get(self, x: int, y: int) -> tuple[int, int, int]:
        i = y * self.stride + x * 4
        v = self.view
        return v[i + 2], v[i + 1], v[i]

    def set(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        i = y * self.stride + x * 4
        v = self.view
        v[i + 2], v[i + 1], v[i] = rgb[0], rgb[1], rgb[2]


def load_rgb(path: str | Path) -> QImage:
    image = QImage(str(path))
    if image.isNull():
        raise ValueError(f"无法读取图片：{path}")
    return image.convertToFormat(QImage.Format_RGB32)


def _sq_dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _median_rgb(samples: list[tuple[int, int, int]],
                fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not samples:
        return fallback
    mid = len(samples) // 2
    return tuple(sorted(s[i] for s in samples)[mid] for i in range(3))


# ---------- 一、量墨迹 / 取色 / 判纯净 ----------
def _ring_background(px: _Pixels, box, pad: int = 4,
                     band: int = 4) -> tuple[int, int, int]:
    """框外一圈的众数色。用众数而不是均值：底色通常是大片同色，均值会被文字边缘拖偏。"""
    x0, y0, x1, y1 = box
    counts: dict[tuple[int, int, int], int] = {}
    ys = list(range(max(0, y0 - pad - band), max(0, y0 - pad)))
    ys += list(range(min(px.height, y1 + pad), min(px.height, y1 + pad + band)))
    xs = list(range(max(0, x0 - pad - band), max(0, x0 - pad)))
    xs += list(range(min(px.width, x1 + pad), min(px.width, x1 + pad + band)))
    step = max(1, (x1 - x0) // 160)
    for y in ys:
        for x in range(max(0, x0 - pad), min(px.width, x1 + pad), step):
            c = px.get(x, y)
            counts[c] = counts.get(c, 0) + 1
    for x in xs:
        for y in range(max(0, y0 - pad), min(px.height, y1 + pad)):
            c = px.get(x, y)
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return (255, 255, 255)
    return max(counts.items(), key=lambda kv: kv[1])[0]


def measure_run(px: _Pixels, det_box):
    """在检测框内量出墨迹矩形、字色、底色与笔画宽度。

    返回 (tight_box, ink, background, stroke, coverage)；无可用墨迹时 tight_box 为 None。
    coverage = 墨迹像素占紧致框的比例，用来识破「墨/底判反」——见 MAX_INK_COVERAGE。

    为什么必须重新量墨迹而不直接用 OCR 的检测框：检测框的留白不固定，同一版里
    同样大小的字能给出 27~39 像素的框高，直接拿它定字号会忽大忽小。
    """
    x0 = max(0, int(det_box[0]))
    y0 = max(0, int(det_box[1]))
    x1 = min(px.width, int(math.ceil(det_box[2])))
    y1 = min(px.height, int(math.ceil(det_box[3])))
    if x1 <= x0 or y1 <= y0:
        return None, (0, 0, 0), (255, 255, 255), 0.0, 0.0

    background = _ring_background(px, (x0, y0, x1, y1))
    inside = [px.get(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    if not inside:
        return None, (0, 0, 0), background, 0.0, 0.0

    # 字色 = 「离底色足够远」的那批像素的中位数。阈值由距离分布自己决定，不能像
    # 早先那样固定取「最远的 1/8」——那等于假设墨迹至少占检测框 12.5%。真实 OCR
    # 的框较紧（覆盖率 25~35%）所以一直没暴露，但框一松（或文字稀疏）时，那 1/8
    # 里大半是底色，中位数就退化成底色，整处被误判成「没有文字」。
    ordered = sorted(inside, key=lambda c: -_sq_dist(c, background))
    far = _sq_dist(ordered[len(ordered) // 100], background)   # 99 分位距离，抗孤立噪点
    core = [c for c in ordered if _sq_dist(c, background) >= far * 0.5]
    ink = _median_rgb(core or ordered[:1], (0, 0, 0))
    span = _sq_dist(ink, background)
    if span < 900:                      # 字色与底色几乎同色：这块没有可读文字
        return None, ink, background, 0.0, 0.0

    ink_threshold = span * 0.30
    cols: list[int] = []
    rows: list[int] = []
    stroke_runs: list[int] = []
    for y in range(y0, y1):
        run = 0
        for x in range(x0, x1):
            if _sq_dist(px.get(x, y), background) >= ink_threshold:
                cols.append(x)
                rows.append(y)
                run += 1
            else:
                if run:
                    stroke_runs.append(run)
                run = 0
        if run:
            stroke_runs.append(run)

    if not cols:
        return None, ink, background, 0.0, 0.0
    tight = (min(cols), min(rows), max(cols) + 1, max(rows) + 1)
    area = (tight[2] - tight[0]) * (tight[3] - tight[1])
    coverage = len(cols) / area if area else 0.0
    stroke_runs.sort()
    stroke = float(stroke_runs[len(stroke_runs) // 2]) if stroke_runs else 0.0
    return tight, ink, background, stroke, coverage


# ---------- 二、字号与字重 ----------
def fit_point_size(text: str, ink_h_px: float, ink_w_px: float,
                   pt_per_px: float, *, bold: bool = False,
                   font_name: str = DEFAULT_FONT) -> float:
    """二分搜索字号，让渲染出的墨迹高度对上量到的墨迹高度；超宽再按宽度收一次。

    ⚠ 要调这里的常量，务必在**真实平台**上量，不能用 QT_QPA_PLATFORM=offscreen：
    离屏平台不加载系统字体（`QFontInfo(QFont("Microsoft YaHei UI")).family()` 是
    空串），一切度量都来自一个回退字体。同一张图在离屏下算出的正文是 8.5~9.7pt，
    真实平台上是 11.6pt——差两成半，据此定出来的阈值全是错的。
    """
    font = QFont(font_name)
    font.setBold(bold)

    def height_at(size: float) -> float:
        font.setPointSizeF(size)
        return QFontMetricsF(font).tightBoundingRect(text).height()

    lo, hi = 4.0, 300.0
    target_h = ink_h_px * pt_per_px * PX_PER_PT
    for _ in range(48):
        mid = (lo + hi) / 2
        if height_at(mid) < target_h:
            lo = mid
        else:
            hi = mid
    # 二分收敛到的是「墨高 ≥ 目标的最小字号」，也就是上界。字体度量在小字号下按
    # 整设备像素量化，上下两侧能差一整像素——一律取上界就等于系统性地偏大。
    # 8pt 上实测偏了 1.4px（12%）。两端各量一次，取更接近目标的那个。
    size = lo if abs(height_at(lo) - target_h) < abs(height_at(hi) - target_h) else hi
    font.setPointSizeF(size)
    # 宽度校正必须用 tightBoundingRect().width()：ink_w_px 量的是**墨迹**宽度，
    # 而 horizontalAdvance 含左右边距。与早先 pt/px 那处是同一类单位错配。
    #
    # 只对「多字符」生效：单字符的宽度完全由字形决定，不带额外信息，硬套只会
    # 误伤——实测数字 '3' 被砍掉 40% 字号，圆圈里的数字因此又小又歪。
    #
    # 容差 1.25 的依据：34 张真实幻灯片 591 处文字实测，按墨高对齐后，微软雅黑排
    # 出来的宽度比原稿的中位数是 0.93（西文 0.93 / 中日韩 0.94），75% 分位 0.98，
    # 只有 7% 超过 1.25。也就是说 1.25 以内是正常的字体度量差异，不该动；超过
    # 1.25 的基本是 OCR 把两栏并成一行之类的粗错，这才是这条校正要拦的东西。
    # （顺带：同样这份实测里 Segoe UI / Calibri / Arial 都不比微软雅黑更贴，
    #   所以不按语种换字体——那个复杂度买不到保真度。）
    if len(text) >= 3:
        rendered_w = QFontMetricsF(font).tightBoundingRect(text).width()
        target_w = ink_w_px * pt_per_px * PX_PER_PT
        if rendered_w > target_w * 1.25 and rendered_w > 0:
            size *= (target_w * 1.25) / rendered_w
    return max(4.0, size)


#: 字重判定只作用于这个磅值以上的文字。小字的笔画只有 1~2 像素，宽度比值被量化
#: 误差淹没：381 条真实字重对照里，不设下限时误判 26 条，设了下限误判 0 条。
BOLD_MIN_PT = 12.0
#: 笔画宽度比中位数高出这个倍数才判粗体。同版对照（自校准），不是跟字体绑死的魔数：
#: 整页都是粗体时中位数跟着抬高，不会全判成粗。K=1.28 在 381 条对照上
#: 准确率 100%、召回率 30%（大字口径）——**零误判**是这里唯一不能让步的指标，
#: 因为「该细的被加粗」看起来是坏掉，而「该粗的没加粗」只是不够醒目。
BOLD_STROKE_FACTOR = 1.28
#: 少于这么多行时没有可靠的中位数可比，一律不判。
BOLD_MIN_POPULATION = 4


def assign_bold(runs: list[TextRun], stroke_ratios: list[float]) -> None:
    """就地给 runs 标字重：同版笔画宽度显著高于中位数的大字判为粗体。

    基线（中位数）取**全部**文本，判定只落在大字上。别把基线也限制在大字里——
    大字本来就更可能是粗体，基线会被自己抬高，结果标题反而够不着阈值。

    走过的弯路记在这里，免得再来一次：
      1) 墨迹占空比与「按 regular/bold 渲染出的占空比」比对 —— 两次都失败，
         占空比同时被字形结构和字号污染，区分度不够。
      2) 笔画游程与渲染参照比对 —— 在 offscreen 平台上 QFont.setBold 根本不生效
         （粗体与常规渲染出的暗像素数完全相同），参照物本身就是错的。
    最后落到「同版相对比较」：不需要任何渲染参照，也不依赖平台字体引擎。
    """
    baseline = sorted(r for r in stroke_ratios if r > 0)
    if len(baseline) < BOLD_MIN_POPULATION:
        return
    median = baseline[len(baseline) // 2]
    if median <= 0:
        return
    for i, run in enumerate(runs):
        if run.point_size < BOLD_MIN_PT or stroke_ratios[i] <= 0:
            continue
        if stroke_ratios[i] > median * BOLD_STROKE_FACTOR:
            run.bold = True


# ---------- 三、背景补洞 ----------
#: 判定「这几行像素是平滑背景而不是文字」的通道极差上限。
#: 背景（纯色或渐变）在竖直方向几个像素内变化极小；文字笔画会造成上百的跳变。
_SMOOTH_TOLERANCE = 40
#: 向外找干净背景的最大距离（像素）。找不到就退回环外众数色。
_SMOOTH_MAX_SEARCH = 48


def _smooth_sample(px: _Pixels, x: int, start_y: int, step: int,
                   band: int, max_search: int, ink, reject_dist: float):
    """从 start_y 沿 step 方向向外找一段干净背景，返回其中位色；找不到返回 None。

    两条判据缺一不可：
      平滑     —— 通道极差 <= _SMOOTH_TOLERANCE。背景（纯色或渐变）竖直方向变化极小。
      远离字色 —— 只「平滑」不够：一根**竖直笔画**沿列方向同样是恒定色，会被误判
                 成干净背景，于是把墨色一路涂下去（实测表现为一列列彩色竖条纹）。

    早先还犯过更粗的错：盲取固定偏移的一条带。行距小于「留白+带宽」时那条带正好
    压在相邻一行的文字上，同样会把邻行的墨色涂满整个框。
    """
    buf: list[tuple[int, int, int]] = []
    y = start_y
    for _ in range(max_search):
        if not (0 <= y < px.height):
            break
        buf.append(px.get(x, y))
        if len(buf) > band:
            buf.pop(0)
        if len(buf) == band:
            spread = max(max(s[i] for s in buf) - min(s[i] for s in buf)
                         for i in range(3))
            if spread <= _SMOOTH_TOLERANCE:
                candidate = _median_rgb(buf, buf[0])
                if _sq_dist(candidate, ink) > reject_dist:
                    return candidate
                buf.clear()          # 撞在笔画上，跳过继续往外找
        y += step
    return None


#: 采样色与本处底色的平方距离超过这个值，就当它越过了一条硬色边。
#: 约等于每通道 41，纵向渐变在一行文字的高度上根本走不了这么远。
EDGE_DIST = 5000


def _drop_sample_across_edge(c_top, c_bottom, run_bg):
    """上下两个采样里若有一个明显越过了色块边界，就丢掉它，两端都用另一个。

    为什么需要：表头色块常常在文字下方几像素处就结束。向下采样于是拿到了色块
    **外面**的颜色，插值就在本该纯色的地方刷出一道渐变，视觉上是一条横贯的色带
    （深底白字的表格最明显）。本处的环外底色是可靠的先验，谁离它远谁就是越界的。

    只在「一个明显远、另一个明显近」时才动手；两个都远说明底本来就花（照片、
    强渐变），那时插值仍是最好的选择，不要瞎改。
    """
    d_top = _sq_dist(c_top, run_bg)
    d_bottom = _sq_dist(c_bottom, run_bg)
    far, near = max(d_top, d_bottom), min(d_top, d_bottom)
    if far > EDGE_DIST and near * 4 < far:
        keep = c_top if d_top < d_bottom else c_bottom
        return keep, keep
    return c_top, c_bottom


def background_jitter(px: _Pixels, run, *, pad: int = 2, band: int = 3,
                      guard: int = 2, stride: int = 3) -> float:
    """这处文字的底「有多花」——相邻列背景采样色差的中位数。

    抹字靠的是「按列在上下两侧各找一段干净背景再垂直插值」。这个操作能不能成立，
    取决于背景在**横向**是否连续：纯色底、横向渐变、纵向渐变都连续，相邻列的采样
    几乎一模一样；照片、书页特写、密集图表则每一列都不同，插值出来是一堆彩色竖条
    和白方块——那是在破坏画面，比不抹更糟。

    别用「能不能找到平滑的一段」来判：**虚化的照片本身就是局部平滑的**，那个判据
    在实拍图上一路给满分，一个都拦不住（实测 f2 的书本特写 28 处全是 1.000）。

    28 张真实幻灯片 685 处文字实测：95% 的中位列差是 **0.0**（垂直插值精确复现），
    99% 在 1.7 以下；超过 4 的只有 2 处，都是 logo 里压在红橙纹理上的微型白字。
    """
    box, ink, run_bg = run.box, run.ink, run.background
    x0 = max(0, int(box[0]) - pad)
    y0 = max(0, int(box[1]) - pad)
    x1 = min(px.width, int(math.ceil(box[2])) + pad)
    y1 = min(px.height, int(math.ceil(box[3])) + pad)
    if x1 - x0 < 8 or y1 <= y0:
        return 0.0
    reject = _sq_dist(ink, run_bg) * 0.30
    reach = min(_SMOOTH_MAX_SEARCH, max(12, (y1 - y0) * 2))
    seq = []
    for x in range(x0, x1, stride):
        top = _smooth_sample(px, x, y0 - guard, -1, band, reach, ink, reject) or run_bg
        bottom = _smooth_sample(px, x, y1 + guard, 1, band, reach, ink, reject) or run_bg
        seq.append((top, bottom))
    if len(seq) < 3:
        return 0.0
    diffs = [math.sqrt(_sq_dist(seq[i][k], seq[i - 1][k]))
             for i in range(1, len(seq)) for k in (0, 1)]
    diffs.sort()
    return diffs[len(diffs) // 2]


def paint_out(image: QImage, runs, *, pad: int = 2, band: int = 3,
              guard: int = 2) -> QImage:
    """把每处文字抹平：按列在上下两侧各找一段干净背景，再垂直插值填充。

    文字行是横向的、背景在垂直方向变化慢，所以「框上方的背景色」和「框下方的
    背景色」之间做插值，就能同时还原纯色底、横向渐变和纵向渐变。
    早先整块填众数色的做法在渐变底上会留下肉眼可见的矩形补丁。

    runs 需要带上每处自己的 ink / background —— 采样时要靠 ink 把笔画剔除掉。
    返回新图，不改原图。
    """
    out = image.copy()          # 深拷贝；QImage 是写时复制，直接改会连累原图
    px = _Pixels(out)
    src = _Pixels(image)
    for run in runs:
        box, ink, run_bg = run.box, run.ink, run.background
        x0 = max(0, int(box[0]) - pad)
        y0 = max(0, int(box[1]) - pad)
        x1 = min(px.width, int(math.ceil(box[2])) + pad)
        y1 = min(px.height, int(math.ceil(box[3])) + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        height = y1 - y0
        reject = _sq_dist(ink, run_bg) * 0.30
        fallback = run_bg
        reach = min(_SMOOTH_MAX_SEARCH, max(12, height * 2))
        for x in range(x0, x1):
            c_top = _smooth_sample(src, x, y0 - guard, -1, band, reach, ink, reject)
            c_bottom = _smooth_sample(src, x, y1 + guard, 1, band, reach, ink, reject)
            if c_top is None and c_bottom is None:
                c_top = c_bottom = fallback
            elif c_top is None:
                c_top = c_bottom
            elif c_bottom is None:
                c_bottom = c_top
            else:
                c_top, c_bottom = _drop_sample_across_edge(c_top, c_bottom, run_bg)
            for y in range(y0, y1):
                t = (y - y0) / height if height else 0.0
                px.set(x, y, (
                    int(round(c_top[0] + (c_bottom[0] - c_top[0]) * t)),
                    int(round(c_top[1] + (c_bottom[1] - c_top[1]) * t)),
                    int(round(c_top[2] + (c_bottom[2] - c_top[2]) * t)),
                ))
    return out


# ---------- 四、分析：把 OCR 行变成 TextRun ----------
def analyse(image: QImage, ocr_rows, *, slide_width_in: float,
            min_point_size: float = DEFAULT_MIN_PT,
            min_score: float = DEFAULT_MIN_SCORE,
            font_name: str = DEFAULT_FONT,
            detect_weight: bool = True) -> Conversion:
    """ocr_rows: [{"text": str, "box": (x0,y0,x1,y1), "score": float}, ...]"""
    px = _Pixels(image)
    pt_per_px = (slide_width_in * POINTS_PER_INCH) / max(1, image.width())
    result = Conversion(image_size=(image.width(), image.height()))
    stroke_ratios: list[float] = []

    for row in ocr_rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        score = float(row.get("score", 1.0))
        if score < min_score:
            result.skipped_score.append(text)
            continue
        tight, ink, background, stroke, coverage = measure_run(px, row["box"])
        if tight is None:
            result.skipped_blank.append(text)
            continue
        if coverage > MAX_INK_COVERAGE:
            result.skipped_shape.append(text)
            continue
        ink_h = tight[3] - tight[1]
        ink_w = tight[2] - tight[0]
        if len(text) >= 2 and ink_w < ink_h * MIN_ASPECT_RATIO:
            result.skipped_shape.append(text)      # 竖排/误并，见 MIN_ASPECT_RATIO
            continue
        size = fit_point_size(text, ink_h, ink_w, pt_per_px, font_name=font_name)
        if size < min_point_size:
            result.skipped_small.append(text)
            continue
        candidate = TextRun(text=text, box=tight, ink=ink, background=background,
                            point_size=round(size, 1), score=score)
        if background_jitter(px, candidate) > MAX_BACKGROUND_JITTER:
            result.skipped_busy.append(text)
            continue
        result.runs.append(TextRun(text=text, box=tight, ink=ink,
                                   background=background, point_size=round(size, 1),
                                   score=score))
        stroke_ratios.append(stroke / ink_h if ink_h else 0.0)

    if detect_weight:
        assign_bold(result.runs, stroke_ratios)
    return result


# ---------- 四点五、行归段 ----------
#: 行间空白超过这个倍数的行高就判为两段。
#: 28 张真实幻灯片、143 对「同列且字号相近」的相邻行实测，gap/height 的分布在
#: 1.0~1.3 之间有明显的谷：60% 分位 0.68、70% 分位 1.00，而 75% 分位就跳到 1.32、
#: 85% 分位 1.66。谷左边是段内换行，右边是各自独立的元素。1.15 取在谷底。
BLOCK_GAP_FACTOR = 1.15
#: 同段各行的字号差不得超过这个倍数——差太多说明是标题压着正文，不是一段。
BLOCK_SIZE_RATIO = 1.6
#: 水平重叠占窄边的比例低于此值就不算同段（并排两栏不能被并成一段）。
BLOCK_OVERLAP = 0.35
#: 同段各行的字色平方距离上限。一段文字通常同色，颜色差一大截就是两段。
#: 这条不是锦上添花：表格的**蓝底白字表头**紧挨着**白底黑字**的首行数据，几何上
#: 完全符合同段（同列、间距小、字号近），只有颜色能把它们分开。合错的后果很重
#: ——整段按第一行的白色排字，于是白底上的黑字全变成白底白字，肉眼直接消失。
#: 阈值 3000 约等于每通道 32，宽到容得下取色抖动，窄到挡得住黑白/深浅对调。
BLOCK_INK_DIST = 3000


@dataclass
class TextBlock:
    """一段多行文字。每行仍保留自己的墨迹框，但共用字号与对齐。"""

    lines: list[TextRun]
    point_size: float
    align: str                       # "l" / "ctr" / "r"
    box: tuple[int, int, int, int]
    line_pitch: float                # 相邻行基线间距（源图像素）

    @property
    def bold(self) -> bool:
        """段内多数行判粗才算粗。

        用 any() 会把**单行的误判放大成整段**：实测一页 12 行的常规正文里，只有
        一行短句被 assign_bold 误标，结果整段 12 行全排成粗体——而「零误判」是
        字重这块唯一不让步的指标（见 assign_bold）。
        真正的粗体段落里每一行的笔画都粗，多数票同样成立；单行段落的行为与
        原来完全一致，也就是那份 381 条对照验证过的路径。
        """
        marked = sum(1 for line in self.lines if line.bold)
        return marked * 2 > len(self.lines)

    @property
    def ink(self) -> tuple[int, int, int]:
        """取各行字色的中位数，而不是第一行——单行的取色偶尔会被边缘像素带偏。"""
        return _median_rgb([line.ink for line in self.lines], (0, 0, 0))


def _overlap_ratio(a, b) -> float:
    left, right = max(a[0], b[0]), min(a[2], b[2])
    if right <= left:
        return 0.0
    narrow = min(a[2] - a[0], b[2] - b[0])
    return (right - left) / narrow if narrow else 0.0


def _infer_align(lines) -> str:
    """段内各行按哪条边对齐，就是原来的对齐方式。取三者中离散度最小的一个。"""
    if len(lines) < 2:
        return "l"

    def spread(values):
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)

    lefts = [line.box[0] for line in lines]
    rights = [line.box[2] for line in lines]
    centers = [(line.box[0] + line.box[2]) / 2 for line in lines]
    options = (("l", spread(lefts)), ("ctr", spread(centers)), ("r", spread(rights)))
    return min(options, key=lambda kv: kv[1])[0]


def group_blocks(runs: list[TextRun], pt_per_px: float,
                 font_name: str = DEFAULT_FONT) -> list[TextBlock]:
    """把逐行的识别结果归成段落。

    为什么必须归段：OCR 是按**视觉行**给结果的，一段居中的多行文字会被拆成
    互不相干的几行。若每行各自定字号、各自按墨迹左边缘定位，就会出现
    「同一段里字号忽大忽小、每行起点参差不齐、居中变左对齐」——彩色块里放
    多行居中文字是 PPT 里最常见的版式之一，这个缺陷非常刺眼。

    归段之后：同段共用一个字号（取各行墨高的中位数反解），对齐方式由各行的
    边缘离散度反推，整段排进一个文本框、行间用显式换行（不靠自动折行，
    这样原稿的断行位置一个字都不会变）。
    """
    ordered = sorted(runs, key=lambda r: (r.box[1], r.box[0]))
    groups: list[list[TextRun]] = []
    for run in ordered:
        placed = False
        for group in groups:
            last = group[-1]
            gap = run.box[1] - last.box[3]
            same_column = _overlap_ratio(last.box, run.box) >= BLOCK_OVERLAP
            close = -last.height * 0.5 <= gap <= max(last.height, run.height) * BLOCK_GAP_FACTOR
            similar = (max(last.point_size, run.point_size)
                       <= min(last.point_size, run.point_size) * BLOCK_SIZE_RATIO)
            same_ink = _sq_dist(last.ink, run.ink) <= BLOCK_INK_DIST
            if same_column and close and similar and same_ink:
                group.append(run)
                placed = True
                break
        if not placed:
            groups.append([run])

    blocks: list[TextBlock] = []
    for group in groups:
        group.sort(key=lambda r: r.box[1])
        heights = sorted(line.height for line in group)
        widest = max(group, key=lambda r: r.width)
        size = fit_point_size(widest.text, heights[len(heights) // 2],
                              widest.width, pt_per_px,
                              bold=any(line.bold for line in group),
                              font_name=font_name)
        box = (min(line.box[0] for line in group), min(line.box[1] for line in group),
               max(line.box[2] for line in group), max(line.box[3] for line in group))
        if len(group) > 1:
            pitches = sorted(group[i + 1].box[1] - group[i].box[1]
                             for i in range(len(group) - 1))
            pitch = float(pitches[len(pitches) // 2])
        else:
            pitch = float(group[0].height) * 1.2
        blocks.append(TextBlock(lines=group, point_size=round(size, 1),
                                align=_infer_align(group), box=box, line_pitch=pitch))
    return blocks


# ---------- 五、写 PPTX：往空白模板里注入 ----------
_XML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _esc(text: str) -> str:
    return "".join(_XML_ESCAPE.get(c, c) for c in text)


def _shape_xml(index: int, block: TextBlock, emu_per_px: float) -> str:
    x0, y0, x1, y1 = block.box
    line_h = block.lines[0].height
    color = "%02X%02X%02X" % block.ink
    size = int(round(block.point_size * 100))
    bold = "1" if block.bold else "0"
    pt_per_px = emu_per_px * POINTS_PER_INCH / EMU_PER_INCH

    # 行距给**绝对值**（spcPts）而不是相对单倍行距的百分比（spcPct）：spcPct 要求
    # 我知道「单倍行距是字号的多少倍」，而那个倍数由字体的 ascent/descent/lineGap
    # 决定、各字体不同。给绝对值就与字体度量无关，量到多少就排多少。
    # PowerPoint 标定实测：给 40px 排出来 40/41px，给 26px 排出来 27px——精确执行。
    pitch_px = max(1.0, block.line_pitch)
    spacing = int(round(pitch_px * pt_per_px * 100))
    text_h_px = pitch_px * len(block.lines)

    # 首行落点的标定模型（同一份实测，anchor=t/ctr、spAutoFit/noAutofit 结果一致）：
    #     基线   = 框顶 + 行距 - descent
    #     墨迹顶 = 基线 + tightBoundingRect(首行文字).top()   （top 是负值）
    # 反解出框顶，让首行的墨迹顶正好落在源图量到的 y0 上。
    # 不这么算的话，多行段落会整体下移二十多像素——项目符号这类留在背景图里的
    # 图形就和文字对不上了，一眼可见。
    font = QFont(DEFAULT_FONT)
    font.setBold(block.bold)
    font.setPointSizeF(block.point_size)
    metrics = QFontMetricsF(font)
    dev_to_px = 1.0 / (PX_PER_PT * pt_per_px)      # 设备像素 -> 源图像素
    above_px = -metrics.tightBoundingRect(block.lines[0].text).top() * dev_to_px
    top_px = y0 - pitch_px + metrics.descent() * dev_to_px + above_px

    # 横向：框比墨迹宽出一点余量以吸收字体度量差异，再按对齐方式把**该对齐的那条边**
    # 放回原位——左对齐时对左边，居中时对中线，右对齐时对右边。
    # （早先一律 left = x0 - pad，于是每个左对齐的段都整体左偏一个 pad。）
    pad_x = max(4.0, line_h * 0.35)
    width_px = (x1 - x0) + 2 * pad_x
    if block.align == "ctr":
        left_px = (x0 + x1) / 2 - width_px / 2
    elif block.align == "r":
        left_px = x1 - width_px
    else:
        left_px = x0
    left = int(left_px * emu_per_px)
    top = int(top_px * emu_per_px)
    width = int(width_px * emu_per_px)
    height = int(text_h_px * emu_per_px)

    runs = []
    for i, line in enumerate(block.lines):
        if i:
            runs.append("<a:br/>")
        runs.append(
            f'<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="{size}" b="{bold}" dirty="0">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'<a:latin typeface="{DEFAULT_FONT}"/><a:ea typeface="{DEFAULT_FONT}"/>'
            f'<a:cs typeface="{DEFAULT_FONT}"/></a:rPr>'
            f'<a:t>{_esc(line.text)}</a:t></a:r>')
    body = "".join(runs)
    # 多行段落必须允许折行以外的显式换行；wrap 仍关掉，断行位置完全按原稿。
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{index + 100}" name="文本 {index}"/>'
        f'<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{max(0, left)}" y="{max(0, top)}"/>'
        f'<a:ext cx="{max(1, width)}" cy="{max(1, height)}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t">'
        f'<a:spAutoFit/></a:bodyPr><a:lstStyle/><a:p>'
        f'<a:pPr algn="{block.align}"><a:lnSpc><a:spcPts val="{spacing}"/></a:lnSpc></a:pPr>'
        f'{body}</a:p></p:txBody></p:sp>'
    )


def _slide_xml(shape_xml, image_cx: int, image_cy: int) -> str:
    picture = (
        '<p:pic><p:nvPicPr><p:cNvPr id="2" name="背景图"/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="rIdImg"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{image_cx}" cy="{image_cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )
    shapes = "".join(shape_xml)
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/>"
        f"{picture}{shapes}"
        "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def write_pptx(background_png: bytes, blocks, image_size, dest,
               *, slide_width_in: float = 13.3333) -> Path:
    """把背景图与文本框注入空白模板，落地成一页 PPTX。

    刻意用「改现成模板」而不是从零手写 OpenXML：母版 / 版式 / 主题都来自真实
    PowerPoint 产物，兼容性有保证，代码量也只有三分之一。
    """
    width_px, height_px = image_size
    slide_cx = int(round(slide_width_in * EMU_PER_INCH))
    slide_cy = int(round(slide_cx * height_px / max(1, width_px)))
    emu_per_px = slide_cx / max(1, width_px)

    shapes = [_shape_xml(i, block, emu_per_px) for i, block in enumerate(blocks)]
    slide = _slide_xml(shapes, slide_cx, slide_cy).replace("rIdImg", "rId2")
    rels = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"'
        ' Target="../slideLayouts/slideLayout7.xml"/>'
        '<Relationship Id="rId2"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
        ' Target="../media/imgtext1.png"/></Relationships>'
    )

    template = resource_path(*TEMPLATE_ASSET)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template) as src, zipfile.ZipFile(
            dest, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "ppt/slides/slide1.xml":
                data = slide.encode("utf-8")
            elif item.filename == "ppt/slides/_rels/slide1.xml.rels":
                data = rels.encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                if 'Extension="png"' not in text:
                    text = text.replace(
                        "<Default Extension=", '<Default Extension="png"'
                        ' ContentType="image/png"/><Default Extension=', 1)
                data = text.encode("utf-8")
            elif item.filename == "ppt/presentation.xml":
                text = data.decode("utf-8")
                text = re.sub(r"<p:sldSz[^>]*/>",
                              f'<p:sldSz cx="{slide_cx}" cy="{slide_cy}"/>', text)
                data = text.encode("utf-8")
            out.writestr(item, data)
        out.writestr("ppt/media/imgtext1.png", background_png)
    return dest


def png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray

    store = QByteArray()
    buffer = QBuffer(store)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(store)


# ---------- 六、公开入口 ----------
def convert(image_path, dest, ocr_rows, *, slide_width_in: float = 13.3333,
            min_point_size: float = DEFAULT_MIN_PT,
            min_score: float = DEFAULT_MIN_SCORE,
            font_name: str = DEFAULT_FONT,
            detect_weight: bool = True) -> Conversion:
    """一张图 + 一份 OCR 结果 -> 一页文字可编辑的 PPTX。"""
    image = load_rgb(image_path)
    result = analyse(image, ocr_rows, slide_width_in=slide_width_in,
                     min_point_size=min_point_size, min_score=min_score,
                     font_name=font_name, detect_weight=detect_weight)
    cleaned = paint_out(image, result.runs)
    pt_per_px = (slide_width_in * POINTS_PER_INCH) / max(1, image.width())
    blocks = group_blocks(result.runs, pt_per_px, font_name=font_name)
    result.blocks = blocks
    write_pptx(png_bytes(cleaned), blocks, result.image_size, dest,
               slide_width_in=slide_width_in)
    log.info("imgtext: %s -> %s（%s）", image_path, dest, result.summary())
    return result
