"""PDF 页面版面分析——先分区、再定栏、最后才允许合并（Stage 1 前置几何层）。

旧实现的分栏判断有三处过粗，分别对应用户实测到的错序：

1. **不等宽双栏误判**：多栏页面里宽度超过页宽 62% 的块一律当跨栏块。宽左栏 +
   窄右栏时，左栏每一行都被当成"跨栏分隔"，结果读成 R1 → L1 → R2 → L2。
2. **窄栏距提前合并**：``_merge_inline_segments`` 在分栏之前就把同一原始块里
   垂直重叠、水平间隔不超过两倍字号的两行拼在一起——窄栏距页面上左右两栏的
   同一行会被缝成一行。
3. **同页栏数变化**：按整页左边界聚类，上半页单栏、下半页双栏互相干扰。

本模块只做**几何**：输入一页所有文字行的 bbox（以及书写方向），输出

- 栏间空白（gutter）：两侧都有 ≥ 2 行文字支撑、且两侧文字在纵向上确实**共存**
  （同一高度范围内左右都有行）的持续空白带。单栏页面的居中标题 / 段末短行凑不出
  这种共存关系，因此不会被误认成第二栏。
- 栏（column）：相邻 gutter 之间的区间，带支撑行数。
- 纵向分区（zone）：被跨栏块群切开的页面片段，每片段**局部重算**栏结构，上单栏
  下双栏这类页面在各自片段里得到各自的栏。
- 每个块的落位（:class:`Placement`）：跨栏（横跨了已检测到的 gutter，而不是
  "居中"或"够宽"）或落在某一栏，附带**不确定性原因**：同时贴合多栏、压在 gutter
  上、栏支撑太弱、局部栏数与全页不一致、旋转文字。

不确定性原因由调用方写进 ``Block.metadata["layout_uncertain_reasons"]``，再由
阅读顺序模块折成 ``reading_order_uncertain``，让下游"只复审不确定区域"的门控能
真正拿到信号——旧实现只在区域图成环时标不确定，错误但无环的顺序从不被发现。

本模块不依赖 PyMuPDF，纯几何、可单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "LineBox",
    "Gutter",
    "Column",
    "Zone",
    "Placement",
    "PageLayout",
    "analyze_page_layout",
    "group_separators",
    "REASON_ROTATED",
    "REASON_MULTI_COLUMN_FIT",
    "REASON_OVERLAPS_GUTTER",
    "REASON_WEAK_SUPPORT",
    "REASON_COLUMN_COUNT_CHANGES",
    "REASON_OUTSIDE_COLUMNS",
    "REASON_SPANNING_SPLITS_COLUMNS",
    "REASON_GRID_LIKE",
    "REASON_FIGURE_LIKE",
    "FigureRegion",
]

# ── 不确定性原因（写进 metadata，供下游门控消费）────────────────────────
REASON_ROTATED = "rotated_text"
REASON_MULTI_COLUMN_FIT = "fits_multiple_columns"
REASON_OVERLAPS_GUTTER = "overlaps_gutter"
REASON_WEAK_SUPPORT = "weak_column_support"
REASON_COLUMN_COUNT_CHANGES = "column_count_changes"
REASON_OUTSIDE_COLUMNS = "outside_columns"
REASON_SPANNING_SPLITS_COLUMNS = "spanning_block_splits_columns"
REASON_GRID_LIKE = "grid_like_zone"
REASON_FIGURE_LIKE = "figure_like_zone"

# 网格区（未被 find_tables 识别的无框线表格）判定：连续 ≥ 4 行"整行都是短文字段"
# 的行，且这些行自己能排出 ≥ 4 列。栏数下限取 4 而不是 3，是为了不误伤 3 栏索引 /
# 参考文献表——那些同样是短行，但应当按栏读，判错的代价比漏判大。
_GRID_MIN_COLUMNS = 4
_GRID_MIN_LINES = 6
# 矢量图内标签簇：至少这么多短标签贴着有二维延展的墨迹，才算图区域。
# 少于此数的轴标题 / 单个箭头文字不够成簇；下划线、页框这类一维描边不算墨迹。
_FIGURE_MIN_LABELS = 4
_FIGURE_INK_MIN_DIM_RATIO = 3.0

# 一栏至少要有这么多行文字支撑，否则它只是一个碰巧另起左边界的孤行。
_MIN_COLUMN_SUPPORT = 2
# 判"块横跨 gutter"时允许块边缘吃进 gutter 的容差（pt），并且不超过 gutter 宽的 1/4。
_CROSS_TOL_PT = 3.0
# 一栏里至少要有一行宽度达到行高的这个倍数（≈ 8 个字符），否则只是标签簇。
_MIN_COLUMN_WIDTH_RATIO = 4.0
# 局部重算栏结构至少需要这么多行。
_MIN_ZONE_LINES_FOR_LOCAL = 4
# 跨栏块群：相邻跨栏行纵向间隔不超过行高的这个倍数时归为同一群（同一 band）。
_SEPARATOR_GAP_RATIO = 1.5


@dataclass(frozen=True)
class LineBox:
    """一行（或一个非文字块）的几何。``horizontal=False`` 的行不参与分栏。"""

    x0: float
    y0: float
    x1: float
    y1: float
    horizontal: bool = True
    is_text: bool = True

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass
class Gutter:
    x0: float
    x1: float
    support_left: int
    support_right: int
    crossing: int

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    def cross_tol(self) -> float:
        return min(_CROSS_TOL_PT, self.width / 4.0)

    def is_crossed_by(self, x0: float, x1: float) -> bool:
        tol = self.cross_tol()
        return x0 < self.x0 - tol and x1 > self.x1 + tol

    def overlap_with(self, x0: float, x1: float) -> float:
        return max(0.0, min(x1, self.x1) - max(x0, self.x0))


@dataclass
class Column:
    x0: float
    x1: float
    support: int

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    def overlap_with(self, x0: float, x1: float) -> float:
        return max(0.0, min(x1, self.x1) - max(x0, self.x0))


@dataclass
class Zone:
    """页面的一个纵向片段及其栏结构。``spanning=True`` 的片段是跨栏块群本身。"""

    y0: float
    y1: float
    columns: list[Column]
    gutters: list[Gutter]
    spanning: bool = False
    local: bool = False
    grid: bool = False
    signals: list[str] = field(default_factory=list)

    @property
    def reference(self) -> tuple[float, float]:
        """本片段的文字水平范围（跨栏块的对齐参考）。"""
        if not self.columns:
            return (0.0, 0.0)
        return (self.columns[0].x0, self.columns[-1].x1)

    def contains_y(self, yc: float) -> bool:
        return self.y0 <= yc < self.y1


@dataclass(frozen=True)
class FigureRegion:
    """矢量图占据的二维区域（墨迹 ∪ 贴着墨迹的短标签），不是纵向片段。

    图经常只占一栏、另一栏同高度仍是正文。按 y 切片段会把邻栏正文一并偷走，
    所以图区域是矩形，落位时看块是否落在矩形里。
    """

    x0: float
    y0: float
    x1: float
    y1: float

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def overlap_ratio(self, x0: float, y0: float, x1: float, y1: float) -> float:
        return _rect_overlap_ratio((x0, y0, x1, y1), (self.x0, self.y0, self.x1, self.y1))


@dataclass
class Placement:
    zone_index: int
    column_index: int | None          # None → 跨栏
    reference: tuple[float, float]    # 对齐 / 行宽的参考区间
    reasons: list[str] = field(default_factory=list)
    column_count: int = 1
    support: int = 0                  # 参考区间背后的文字行数（可靠度依据）
    grid: bool = False                # 落在网格区（按行主序读，不按栏）
    figure: bool = False              # 落在矢量图标签簇里（区域类型，不是阅读顺序）

    @property
    def spanning(self) -> bool:
        return self.column_index is None


@dataclass
class PageLayout:
    page_width: float
    page_height: float
    line_height: float
    zones: list[Zone]
    separators: list[tuple[float, float]]
    signals: list[str] = field(default_factory=list)
    figures: list[FigureRegion] = field(default_factory=list)

    # ── 查询 ────────────────────────────────────────────────────────
    def zone_for(self, y0: float, y1: float) -> tuple[int, Zone]:
        yc = (y0 + y1) / 2.0
        for index, zone in enumerate(self.zones):
            if zone.contains_y(yc):
                return index, zone
        # 超出页面范围（负坐标 / 越界 bbox）：按中心距离取最近片段。
        index = min(
            range(len(self.zones)),
            key=lambda i: abs((self.zones[i].y0 + self.zones[i].y1) / 2.0 - yc),
        )
        return index, self.zones[index]

    @property
    def max_column_count(self) -> int:
        return max((len(zone.columns) for zone in self.zones), default=1)

    def crosses_gutter(self, bbox: Sequence[float]) -> bool:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        _, zone = self.zone_for(y0, y1)
        return any(g.is_crossed_by(x0, x1) for g in zone.gutters)

    def gap_covers_gutter(self, gap_x0: float, gap_x1: float, y0: float, y1: float) -> bool:
        """一段水平空隙是否压住了某条 gutter 的主体（≥ 一半宽度）。

        行内片段合并的守卫：左右两个片段之间的空隙若正好是栏间空白，它们就是
        两栏各自的一行，不能缝在一起。
        """
        if gap_x1 <= gap_x0:
            return False
        _, zone = self.zone_for(y0, y1)
        for gutter in zone.gutters:
            if gutter.overlap_with(gap_x0, gap_x1) >= 0.5 * gutter.width:
                return True
        return False

    def _short_vs_body(self, x0: float, x1: float) -> bool:
        """短标签相对页面最宽栏（正文），不相对标签自己挤出来的窄栏。"""
        widths = [column.width for zone in self.zones for column in zone.columns]
        reference = max(widths, default=self.page_width)
        return reference > 0 and (x1 - x0) < 0.5 * reference

    def _figure_for(self, x0: float, y0: float, x1: float, y1: float) -> FigureRegion | None:
        xc = (x0 + x1) / 2.0
        yc = (y0 + y1) / 2.0
        for figure in self.figures:
            if figure.contains_point(xc, yc) or figure.overlap_ratio(x0, y0, x1, y1) >= 0.5:
                return figure
        return None

    def classify(
        self,
        bbox: Sequence[float],
        *,
        alignment: str | None = None,
        horizontal: bool = True,
    ) -> Placement:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        zone_index, zone = self.zone_for(y0, y1)
        reasons: list[str] = []
        if not horizontal:
            reasons.append(REASON_ROTATED)
        reasons.extend(zone.signals)
        column_count = max(1, len(zone.columns))

        if zone.grid:
            # 网格区里没有"栏"可言：单元格按行主序（先 y 再 x）读，整块区域如实标
            # 不确定——它本该是一张表 / 一幅图，却没被当成表 / 图抽出来。
            return Placement(
                zone_index, None, zone.reference, reasons, column_count,
                sum(column.support for column in zone.columns), grid=True,
            )

        figure = self._figure_for(x0, y0, x1, y1)
        if figure is not None and self._short_vs_body(x0, x1):
            # 图经常只占一栏：标签留在所在栏的阅读流里，不要升成跨栏分隔，
            # 否则邻栏同高度的正文会被切到另一个 band。整幅图横跨 gutter 时
            # 才按跨栏处理（图在两栏之间，上下正文本来就被它切开）。
            fig_spans = any(g.is_crossed_by(figure.x0, figure.x1) for g in zone.gutters)
            if fig_spans or not zone.columns:
                return Placement(
                    zone_index, None, zone.reference, reasons, column_count,
                    sum(column.support for column in zone.columns), figure=True,
                )
            xc = (figure.x0 + figure.x1) / 2.0
            best_index = min(
                range(len(zone.columns)),
                key=lambda i: abs((zone.columns[i].x0 + zone.columns[i].x1) / 2.0 - xc),
            )
            column = zone.columns[best_index]
            return Placement(
                zone_index, best_index, (column.x0, column.x1), reasons, column_count,
                column.support, figure=True,
            )

        spanning = any(g.is_crossed_by(x0, x1) for g in zone.gutters)
        if not spanning:
            xc = (x0 + x1) / 2.0
            for g in zone.gutters:
                if g.overlap_with(x0, x1) < 0.5 * g.width:
                    continue
                # 压住 gutter 主体、且自身中心就落在 gutter 里：骑在两栏之间的块
                # （居中跨栏标题的常见形态），不是某一栏里探出去几个点的孤行。
                if g.x0 <= xc <= g.x1 or alignment == "center":
                    spanning = True
                    break
        if spanning or not zone.columns:
            return Placement(
                zone_index, None, zone.reference, reasons, column_count,
                sum(column.support for column in zone.columns),
            )

        width = max(0.0, x1 - x0)
        overlaps = sorted(
            ((column.overlap_with(x0, x1), -index, index) for index, column in enumerate(zone.columns)),
            reverse=True,
        )
        best_overlap, _, best_index = overlaps[0]
        if best_overlap <= 0.0:
            xc = (x0 + x1) / 2.0
            best_index = min(
                range(len(zone.columns)),
                key=lambda i: abs((zone.columns[i].x0 + zone.columns[i].x1) / 2.0 - xc),
            )
            if len(zone.columns) > 1:
                reasons.append(REASON_OUTSIDE_COLUMNS)
        else:
            if len(overlaps) > 1 and width > 0 and overlaps[1][0] >= 0.25 * width:
                reasons.append(REASON_MULTI_COLUMN_FIT)
            if any(g.overlap_with(x0, x1) >= 0.5 * g.width for g in zone.gutters):
                reasons.append(REASON_OVERLAPS_GUTTER)
        column = zone.columns[best_index]
        if len(zone.columns) > 1 and column.support < _MIN_COLUMN_SUPPORT + 1:
            reasons.append(REASON_WEAK_SUPPORT)
        return Placement(
            zone_index, best_index, (column.x0, column.x1), reasons, column_count,
            column.support,
        )


# ── 分析 ────────────────────────────────────────────────────────────

def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int(round(q * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _gutter_between(
    left: list[LineBox],
    right: list[LineBox],
    all_lines: list[LineBox],
    line_height: float,
    require_body_width: bool = True,
) -> Gutter | None:
    """左右两个左边界簇之间是否存在一条真实 gutter。

    条件：① 左簇里有行没有伸进右簇的起点（这些行的最右端就是 gutter 左沿）；
    ② 两侧支撑行各 ≥ 2；③ gutter 宽度不小于 0.4 行高；④ 两侧支撑行在纵向上共存
    （同一高度范围里左右都有文字）；⑤ 共存范围内横跨 gutter 的行不超过支撑行的一半。

    ``require_body_width=False`` 用于已经确定是网格区的那一小片行：那里按定义全是
    短文字段，再要求"每侧至少有一行正文长度"就是自相矛盾。
    """
    right_x0 = _percentile([line.x0 for line in right], 0.10)
    left_support = [line for line in left if line.x1 <= right_x0 + 1.0]
    right_support = [line for line in right if line.x0 >= right_x0 - 1.0]
    if len(left_support) < _MIN_COLUMN_SUPPORT or len(right_support) < _MIN_COLUMN_SUPPORT:
        return None

    # 两侧支撑行的纵向共存范围：gutter 的左右沿只由**这个范围内**的行决定。范围外
    # 的行（栏上方的标题 / 摘要）即便右端恰好停在下一栏起点，也不能把 gutter 挤没。
    co0 = max(min(l.y0 for l in left_support), min(l.y0 for l in right_support))
    co1 = min(max(l.y1 for l in left_support), max(l.y1 for l in right_support))
    if co1 - co0 < 0.5 * line_height:
        return None
    left_in = [l for l in left_support if co0 <= l.yc <= co1]
    right_in = [l for l in right_support if co0 <= l.yc <= co1]
    if len(left_in) < _MIN_COLUMN_SUPPORT or len(right_in) < _MIN_COLUMN_SUPPORT:
        return None

    g0 = max(line.x1 for line in left_in)
    g1 = _percentile([line.x0 for line in right_in], 0.10)
    if g1 - g0 < max(4.0, 0.4 * line_height):
        return None
    # 两侧都得有至少一行像正文那么长；全是几个字的短标签（图内文字、坐标轴刻度）
    # 撑不起一栏——图区里的标签簇曾在正文栏内部造出一条假 gutter。
    min_body_like_width = _MIN_COLUMN_WIDTH_RATIO * max(line_height, 1.0)
    if require_body_width and (
        max(l.width for l in left_in) < min_body_like_width
        or max(l.width for l in right_in) < min_body_like_width
    ):
        return None

    # 行级共存：右侧的每一**行**（同高度的多个单元格只算一行）在左侧找得到同高度
    # （±0.75 行高）的伙伴才算一次共存。横跨 gutter 的行不得多于共存行——真 gutter
    # 只被少数标题 / 图注横跨，假 gutter 会被正文一行行地穿过去。按行而不按块数
    # 数，是为了不让一张 8 列表格的 200 个单元格把 25 行共存夸大成 200。
    partner_tol = 0.75 * max(line_height, 1.0)
    left_centers = sorted(l.yc for l in left_in)
    partner_rows = 0
    row_yc: float | None = None
    for yc_right in sorted(l.yc for l in right_in):
        if row_yc is not None and yc_right - row_yc <= partner_tol:
            continue
        row_yc = yc_right
        if any(abs(yc_right - yc) <= partner_tol for yc in left_centers):
            partner_rows += 1
    if partner_rows < _MIN_COLUMN_SUPPORT:
        return None

    # 横跨这条 gutter 的行：共存范围内的是强反证（左右明明各有一行，它却横着过去），
    # 两簇纵向范围内、共存范围外的是弱反证（比如栏上方的整宽摘要，或者——假 gutter
    # 的典型——被它切开的正文栏在图区下方一行行穿过去）。强 + 0.5 × 弱不得多于共存行。
    tol = min(_CROSS_TOL_PT, (g1 - g0) / 4.0)
    ext0 = min(l.y0 for l in left + right)
    ext1 = max(l.y1 for l in left + right)
    crossing_inside = 0
    crossing_outside = 0
    for line in all_lines:
        if not (ext0 <= line.yc <= ext1):
            continue
        if line.x0 < g0 - tol and line.x1 > g1 + tol:
            if co0 <= line.yc <= co1:
                crossing_inside += 1
            else:
                crossing_outside += 1
    if crossing_inside + 0.5 * crossing_outside > partner_rows:
        return None
    return Gutter(g0, g1, len(left_in), len(right_in), crossing_inside + crossing_outside)


def _detect_columns(
    lines: list[LineBox],
    page_width: float,
    line_height: float,
    require_body_width: bool = True,
) -> tuple[list[Column], list[Gutter]]:
    """左边界密度峰 → 峰的右侧缩进窗内归簇 → 逐对验证 gutter → 无效则合并簇。

    不用"按运行均值链式聚类"：表格单元格的左边界会以 40–70pt 的步距铺满整页宽，
    链式聚类的均值一路漂移，把右栏正文的左边界也吞进表格簇里。改成先找左边界的
    密度峰（一栏的正文行左边界全部重合，是压倒性的峰；缩进首行落在峰右侧一个缩进
    宽度内），峰左侧或远离任何峰的行不归簇——它们仍参与横跨计数，也照常被落位。
    """
    if not lines:
        return [], []
    bin_width = max(4.0, 0.5 * max(line_height, 1.0))
    bins: dict[int, list[LineBox]] = {}
    for line in lines:
        bins.setdefault(int(line.x0 // bin_width), []).append(line)
    peak_min = max(_MIN_COLUMN_SUPPORT, int(0.04 * len(lines) + 0.999))
    candidates = [
        (len(members), index) for index, members in bins.items()
        if len(members) >= peak_min
        and all(len(bins.get(index + d, ())) <= len(members) for d in (-2, -1, 1, 2))
    ]
    merge_tol = max(24.0, 3.0 * max(line_height, 1.0))
    peaks: list[float] = []
    for count, index in sorted(candidates, reverse=True):
        x = (index + 0.5) * bin_width
        if all(abs(x - p) > merge_tol for p in peaks):
            peaks.append(x)
    if not peaks:
        x0 = min(l.x0 for l in lines)
        x1 = max(l.x1 for l in lines)
        return [Column(x0, x1, len(lines))], []
    peaks.sort()

    clusters: list[list[LineBox]] = [[] for _ in peaks]
    left_slack = bin_width
    for line in lines:
        best = min(range(len(peaks)), key=lambda i: abs(peaks[i] - line.x0))
        offset = line.x0 - peaks[best]
        if -left_slack <= offset <= merge_tol:
            clusters[best].append(line)
    clusters = [c for c in clusters if c]

    gutters: list[Gutter] = []
    while True:
        clusters.sort(key=lambda c: _median([l.x0 for l in c]))
        gutters = []
        merged = False
        for index in range(len(clusters) - 1):
            gutter = _gutter_between(
                clusters[index], clusters[index + 1], lines, line_height, require_body_width,
            )
            if gutter is None:
                clusters[index:index + 2] = [clusters[index] + clusters[index + 1]]
                merged = True
                break
            gutters.append(gutter)
        if not merged:
            break

    columns: list[Column] = []
    for index, cluster in enumerate(clusters):
        non_crossing = [
            l for l in cluster if not any(g.is_crossed_by(l.x0, l.x1) for g in gutters)
        ] or cluster
        x0 = min(l.x0 for l in non_crossing)
        if index < len(gutters):
            x1 = gutters[index].x0
        else:
            x1 = max(l.x1 for l in non_crossing)
        if index > 0:
            x0 = max(x0, gutters[index - 1].x1)
        columns.append(Column(x0, max(x0, x1), len(non_crossing)))
    return columns, gutters


def group_separators(
    spanning: Iterable[LineBox],
    column_lines: Iterable[LineBox],
    line_height: float,
) -> list[tuple[float, float]]:
    """把跨栏块按纵向相邻归群；中间夹着栏内块的不归同一群。返回各群 (y0, y1)。"""
    ordered = sorted(spanning, key=lambda l: (l.y0, l.x0))
    if not ordered:
        return []
    column_centers = sorted(l.yc for l in column_lines)
    groups: list[list[float]] = []
    max_gap = _SEPARATOR_GAP_RATIO * max(line_height, 1.0)
    for line in ordered:
        if groups:
            prev_y0, prev_y1 = groups[-1]
            gap = line.y0 - prev_y1
            interleaved = any(prev_y1 < yc < line.y0 for yc in column_centers)
            if gap <= max_gap and not interleaved:
                groups[-1] = [min(prev_y0, line.y0), max(prev_y1, line.y1)]
                continue
        groups.append([line.y0, line.y1])
    return [(g[0], g[1]) for g in groups]


def analyze_page_layout(
    boxes: Iterable[LineBox],
    page_width: float,
    page_height: float,
    drawings: Iterable[Sequence[float]] | None = None,
) -> PageLayout:
    """分析一页：全页分栏 → 跨栏块群切纵向片段 → 片段内局部重算栏结构。

    ``drawings`` 是矢量路径的 bbox（``page.get_cdrawings()`` 的 ``rect``），本模块
    不依赖 PyMuPDF。有墨迹时才识别图内标签簇；没有墨迹的短标签只是短标签。
    """
    all_boxes = list(boxes)
    text = [
        b for b in all_boxes
        if b.is_text and b.horizontal and b.x1 > b.x0 and b.y1 > b.y0
    ]
    page_height = float(page_height) if page_height and page_height > 0 else max(
        (b.y1 for b in all_boxes), default=0.0
    ) + 1.0
    drawing_rects = _normalize_rects(drawings)
    if not text:
        column = Column(0.0, float(page_width), 0)
        return PageLayout(
            page_width, page_height, 0.0,
            [Zone(0.0, page_height, [column], [])], [],
        )

    line_height = _median([b.height for b in text])
    columns, gutters = _detect_columns(text, page_width, line_height)

    spanning = [b for b in text if any(g.is_crossed_by(b.x0, b.x1) for g in gutters)]
    column_lines = [b for b in text if b not in spanning]
    separators = group_separators(spanning, column_lines, line_height)

    # 纵向片段：[0, sep0.y0) 列区，[sep0.y0, sep0.y1) 跨栏区，…，[sepN.y1, H) 列区。
    zones: list[Zone] = []
    cursor = 0.0
    for sep_y0, sep_y1 in separators:
        sep_y0 = max(sep_y0, cursor)
        sep_y1 = max(sep_y1, sep_y0)
        if sep_y0 > cursor:
            zones.append(Zone(cursor, sep_y0, list(columns), list(gutters)))
        if sep_y1 > sep_y0:
            zones.append(Zone(sep_y0, sep_y1, list(columns), list(gutters), spanning=True))
        cursor = sep_y1
    if cursor < page_height or not zones:
        zones.append(Zone(cursor, max(page_height, cursor + 1.0), list(columns), list(gutters)))

    signals: list[str] = []
    for zone in zones:
        if zone.spanning:
            continue
        zone_lines = [b for b in column_lines if zone.contains_y(b.yc)]
        if len(zone_lines) < _MIN_ZONE_LINES_FOR_LOCAL:
            continue
        local_columns, local_gutters = _detect_columns(zone_lines, page_width, line_height)
        if len(local_columns) < 2:
            continue
        zone.columns, zone.gutters, zone.local = local_columns, local_gutters, True
        if len(local_columns) != len(columns):
            zone.signals.append(REASON_COLUMN_COUNT_CHANGES)
            if REASON_COLUMN_COUNT_CHANGES not in signals:
                signals.append(REASON_COLUMN_COUNT_CHANGES)

    # 网格区（漏检的无框线表格 / 图内标签簇）：连续多行、每行都是 ≥ 2 段填不满所在栏
    # 一半的短文字、且这些行自己能排出 ≥ 3 列。正文栏是被行填满的，排不出这种网格。
    # 网格区按行主序读、整区标不确定。它可能横跨若干个片段（表格行一部分越过正文
    # gutter 成了"跨栏行"、一部分没越过），所以在**行**上找，找到后再切片段。
    for region_y0, region_y1, grid_columns, grid_gutters in _detect_grid_regions(
        text, columns, line_height, page_width,
    ):
        zones = _carve_grid_zone(zones, region_y0, region_y1, grid_columns, grid_gutters)
        if REASON_GRID_LIKE not in signals:
            signals.append(REASON_GRID_LIKE)

    figures = _detect_figure_regions(
        text, drawing_rects, columns, line_height, page_width,
    )
    if figures and REASON_FIGURE_LIKE not in signals:
        signals.append(REASON_FIGURE_LIKE)

    return PageLayout(
        page_width, page_height, line_height, zones, separators, signals, figures,
    )


def _is_short_line(line: LineBox, columns: list[Column]) -> bool:
    """行宽不到所在栏（横跨 gutter 的行按整幅文字区）的一半。"""
    if not columns:
        return False
    if any(
        line.x0 < a.x1 and line.x1 > b.x0
        for a, b in zip(columns, columns[1:])
    ):
        reference = columns[-1].x1 - columns[0].x0
    else:
        column = max(columns, key=lambda c: c.overlap_with(line.x0, line.x1))
        reference = column.width
    return reference > 0 and line.width < 0.5 * reference


def _detect_grid_regions(
    text: list[LineBox],
    columns: list[Column],
    line_height: float,
    page_width: float,
) -> list[tuple[float, float, list[Column], list[Gutter]]]:
    if len(text) < _GRID_MIN_LINES or not columns:
        return []
    row_tol = 0.75 * max(line_height, 1.0)
    rows: list[list[LineBox]] = []
    for line in sorted(text, key=lambda l: l.yc):
        if rows and abs(line.yc - rows[-1][0].yc) <= row_tol:
            rows[-1].append(line)
        else:
            rows.append([line])

    def _row_kind(row: list[LineBox]) -> str:
        if all(_is_short_line(l, columns) for l in row):
            return "grid" if len(row) >= 2 else "neutral"
        return "text"

    regions: list[tuple[float, float, list[Column], list[Gutter]]] = []
    run: list[list[LineBox]] = []
    max_gap = 2.0 * max(line_height, 1.0)

    def _flush() -> None:
        grid_rows = [row for row in run if _row_kind(row) == "grid"]
        if len(grid_rows) < 4:
            return
        lines = [l for row in run for l in row]
        local_columns, local_gutters = _detect_columns(
            lines, page_width, line_height, require_body_width=False,
        )
        if len(local_columns) < _GRID_MIN_COLUMNS:
            return
        regions.append((
            min(l.y0 for l in lines), max(l.y1 for l in lines), local_columns, local_gutters,
        ))

    for row in rows:
        kind = _row_kind(row)
        if kind == "text":
            _flush()
            run = []
            continue
        if run and row[0].y0 - max(l.y1 for l in run[-1]) > max_gap:
            _flush()
            run = []
        run.append(row)
    _flush()
    return regions


def _carve_grid_zone(
    zones: list[Zone],
    y0: float,
    y1: float,
    columns: list[Column],
    gutters: list[Gutter],
) -> list[Zone]:
    """把 [y0, y1) 从现有片段里切出来作为网格片段，其余片段保留原栏结构。"""
    result: list[Zone] = []
    grid_zone = Zone(y0, y1, list(columns), list(gutters), grid=True, signals=[REASON_GRID_LIKE])
    inserted = False
    for zone in zones:
        if zone.y1 <= y0 or zone.y0 >= y1:
            result.append(zone)
            continue
        if zone.y0 < y0:
            result.append(Zone(zone.y0, y0, zone.columns, zone.gutters, zone.spanning,
                               zone.local, False, list(zone.signals)))
        if not inserted:
            result.append(grid_zone)
            inserted = True
        if zone.y1 > y1:
            result.append(Zone(y1, zone.y1, zone.columns, zone.gutters, zone.spanning,
                               zone.local, False, list(zone.signals)))
    if not inserted:
        result.append(grid_zone)
        result.sort(key=lambda z: z.y0)
    return result


def _normalize_rects(
    drawings: Iterable[Sequence[float]] | None,
) -> list[tuple[float, float, float, float]]:
    rects: list[tuple[float, float, float, float]] = []
    for raw in drawings or ():
        if raw is None or len(raw) < 4:
            continue
        x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    return rects


def _rect_overlap_ratio(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    x0, y0, x1, y1 = inner
    ox0, oy0, ox1, oy1 = outer
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if area <= 0.0:
        return 0.0
    ix0, iy0 = max(x0, ox0), max(y0, oy0)
    ix1, iy1 = min(x1, ox1), min(y1, oy1)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    return intersection / area


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad_x: float = 0.0,
    pad_y: float = 0.0,
) -> bool:
    return (
        a[0] < b[2] + pad_x and b[0] < a[2] + pad_x
        and a[1] < b[3] + pad_y and b[1] < a[3] + pad_y
    )


def _union_rects(
    rects: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def _cluster_rects(
    rects: Sequence[tuple[float, float, float, float]],
    pad_x: float,
    pad_y: float,
) -> list[list[tuple[float, float, float, float]]]:
    if not rects:
        return []
    parent = list(range(len(rects)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _rects_overlap(rects[i], rects[j], pad_x, pad_y):
                parent[find(j)] = find(i)

    groups: dict[int, list[tuple[float, float, float, float]]] = {}
    for index, rect in enumerate(rects):
        groups.setdefault(find(index), []).append(rect)
    return list(groups.values())


def _detect_figure_regions(
    text: list[LineBox],
    drawings: list[tuple[float, float, float, float]],
    columns: list[Column],
    line_height: float,
    page_width: float,
) -> list[FigureRegion]:
    """贴着有二维延展的矢量墨迹的短标签簇 → 图区域。

    没有墨迹就不认：作者单位、关键词、索引短行同样短，不能只靠"短"判成图。
    下划线 / 页框是一维描边，并出来的墨迹在一个方向上薄得不像图。整页边框
    （几乎等于页宽×页高）也排除——那是装饰，不是图。
    """
    if not drawings or not text or not columns:
        return []
    page_h = max((l.y1 for l in text), default=0.0)
    body_width = max((column.width for column in columns), default=0.0)
    if body_width <= 0.0:
        return []

    def _is_rule(rect: tuple[float, float, float, float]) -> bool:
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        return (
            (height < 2.5 and width > 0.4 * page_width)
            or (width < 2.5 and height > 0.4 * max(page_h, 1.0))
        )

    def _is_page_frame(rect: tuple[float, float, float, float]) -> bool:
        return (
            (rect[2] - rect[0]) > 0.92 * page_width
            and (rect[3] - rect[1]) > 0.92 * max(page_h, 1.0)
        )

    ink = [rect for rect in drawings if not _is_rule(rect) and not _is_page_frame(rect)]
    if not ink:
        return []

    def _short_vs_body(line: LineBox) -> bool:
        return line.width < 0.5 * body_width

    short = [line for line in text if _short_vs_body(line)]
    pad = max(line_height, 1.0)
    min_ink = _FIGURE_INK_MIN_DIM_RATIO * pad
    figures: list[FigureRegion] = []
    # 先聚墨迹：图里的标签常常疏得聚不成簇，但它们共享同一团矢量路径。
    for group in _cluster_rects(ink, pad, pad):
        ink_box = _union_rects(group)
        if min(ink_box[2] - ink_box[0], ink_box[3] - ink_box[1]) < min_ink:
            continue
        labels = [
            line for line in short
            if _rects_overlap((line.x0, line.y0, line.x1, line.y1), ink_box, pad, pad)
        ]
        if len(labels) < _FIGURE_MIN_LABELS:
            continue
        box = _union_rects(
            [ink_box] + [(l.x0, l.y0, l.x1, l.y1) for l in labels]
        )
        figures.append(FigureRegion(*box))
    return figures
