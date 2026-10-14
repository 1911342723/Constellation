"""PDF 阅读顺序图（E02）——在动态分栏之上用区域图定序。

方案 E02 的前提：现有 PDF 提取已按 x 起点聚类、全宽 band、动态列排序（不是只能
处理双栏）。本模块在其上再加一层"页面区域图"来对付它失效的场景：居中小标题、
跨栏图表、边栏、脚注、混合栏宽、旋转/竖排文本。

做法：把每个块看成图的一个节点，用**物理关系**建有向先后边——

1. **同栏在上**：同一 layout_column 内，bbox 更靠上者在先。
2. **全宽跨栏分隔**：一条全宽块（band 为奇数）把它上方的所有块排在它前面、
   下方的所有块排在它后面（跨栏图表 / 居中小标题的典型形态）。
3. **注释附属**：脚注（source_role=footnote）排到本页所有正文之后。
4. **页序**：不同页之间严格按页码先后（页间不建细粒度边，避免跨页噪声）。

然后做**稳定拓扑排序**（Kahn，出队时按 (page, band, column, y0, x0) 这个既有排序
键选最小者，保证与旧确定性顺序尽量一致）。存在冲突（成环）时，退回该页既有的
确定性排序键，并给参与环的块打 ``reading_order_uncertain=True`` + 备选顺序信息，
**不丢块、不硬定一个可能错的序**。

不确定性有两路来源：① 本模块发现的环；② 版面分区阶段（``pdf_layout``）写在
``layout_uncertain_reasons`` 里的几何歧义（同时贴合多栏 / 压住 gutter / 栏支撑弱 /
局部栏数变化 / 旋转文字 / 跨栏图表夹在栏内正文中间）。两路都折成
``reading_order_uncertain=True`` 并在 ``reading_order_uncertain_reasons`` 里列明——
"没有环"不等于"顺序正确"。

只写 ``Block.metadata``（``reading_order_uncertain`` / ``reading_order_uncertain_reasons`` /
``reading_order_index`` / ``reading_order_fallback``），不给 Block 加字段——本工作包与
并行 agent 共享 ``block.py``。
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.models import Block


def _sort_key(entry) -> tuple:
    """与 provider 的 _entry_sort_key 同口径的既有确定性排序键。"""
    metadata = entry[4].metadata or {}
    return (
        entry[0],                               # page
        int(metadata.get("layout_band", 0)),    # 全宽 band
        entry[1],                                # column
        entry[2],                                # y0
        entry[3],                                # x0
    )


def _bbox(block) -> tuple[float, float, float, float]:
    bbox = (block.metadata or {}).get("bbox") or [0, 0, 0, 0]
    if len(bbox) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))


def _build_page_edges(indexed: list[tuple[int, object]]) -> dict[int, set[int]]:
    """为一页的块建先后边，返回邻接表 ``{u: {v, ...}}`` 表示 u 在 v 之前。

    ``indexed`` 是 ``[(global_index, entry), ...]``（仅本页）。
    """
    edges: dict[int, set[int]] = defaultdict(set)
    # 全宽分隔块：band 为奇数（provider 约定 full-width band = 分隔序号*2+1）。
    fullwidth = [
        (gi, entry) for gi, entry in indexed
        if int((entry[4].metadata or {}).get("layout_band", 0)) % 2 == 1
    ]

    for gi, entry in indexed:
        meta = entry[4].metadata or {}
        role = meta.get("source_role")
        _, y0_i, _, y1_i = _bbox(entry[4])
        col_i = meta.get("layout_column")
        for gj, other in indexed:
            if gi == gj:
                continue
            ometa = other[4].metadata or {}
            _, oy0, _, oy1 = _bbox(other[4])
            # 1. 同栏在上：同列且 i 明显在 j 上方 → i 先于 j。
            if col_i is not None and col_i == ometa.get("layout_column"):
                if y1_i <= oy0 + 0.5:
                    edges[gi].add(gj)
            # 3. 注释附属：脚注排到非脚注正文之后。
            if role == "footnote" and ometa.get("source_role") != "footnote":
                edges[gj].add(gi)

    # 2. 全宽跨栏分隔：分隔块把上方块排前、下方块排后。
    for gi, entry in fullwidth:
        _, sep_y0, _, sep_y1 = _bbox(entry[4])
        for gj, other in indexed:
            if gj == gi:
                continue
            _, oy0, _, oy1 = _bbox(other[4])
            if oy1 <= sep_y0 + 0.5:       # other 在分隔块上方
                edges[gj].add(gi)
            elif oy0 >= sep_y1 - 0.5:     # other 在分隔块下方
                edges[gi].add(gj)
    return edges


def _stable_toposort(
    nodes: list[int],
    edges: dict[int, set[int]],
    tiebreak: dict[int, tuple],
) -> tuple[list[int], set[int]]:
    """稳定拓扑排序（Kahn）。返回 (顺序, 参与环的节点集合)。

    出队时在所有入度为 0 的候选里按 ``tiebreak`` 键取最小者——这让无约束处的顺序
    与既有确定性排序键一致。若排完仍有剩余节点（成环），把它们按 tiebreak 顺序追加
    到末尾，并作为"不确定"节点回报。
    """
    indeg: dict[int, int] = {n: 0 for n in nodes}
    for u in nodes:
        for v in edges.get(u, ()):  # u 在 v 之前 → v 的入度 +1
            if v in indeg:
                indeg[v] += 1

    ready = [n for n in nodes if indeg[n] == 0]
    ready_set = set(ready)
    order: list[int] = []
    while ready:
        ready.sort(key=lambda n: tiebreak[n])
        u = ready.pop(0)
        ready_set.discard(u)
        order.append(u)
        for v in sorted(edges.get(u, ()), key=lambda n: tiebreak[n]):
            if v not in indeg:
                continue
            indeg[v] -= 1
            if indeg[v] == 0 and v not in ready_set:
                ready.append(v)
                ready_set.add(v)

    placed = set(order)
    cyclic = {n for n in nodes if n not in placed}
    if cyclic:
        # 成环：保留备选（既有确定性）顺序追加，不硬定一个可能错的序。
        order.extend(sorted(cyclic, key=lambda n: tiebreak[n]))
    return order, cyclic


def order_entries(entries: list) -> list:
    """对全体 entries 做基于区域图的稳定阅读排序，返回重排后的 entries。

    页间严格按页序；页内用区域图 + 稳定拓扑排序。冲突（成环）页的相关块打
    ``reading_order_uncertain=True`` 并保留既有确定性顺序作为备选。
    """
    if not entries:
        return entries

    indexed = list(enumerate(entries))
    tiebreak = {gi: _sort_key(entry) for gi, entry in indexed}

    by_page: dict[int, list[tuple[int, object]]] = defaultdict(list)
    for gi, entry in indexed:
        by_page[entry[0]].append((gi, entry))

    final_order: list[int] = []
    for page in sorted(by_page):
        page_indexed = by_page[page]
        page_nodes = [gi for gi, _ in page_indexed]
        edges = _build_page_edges(page_indexed)
        order, cyclic = _stable_toposort(page_nodes, edges, tiebreak)
        for rank, gi in enumerate(order):
            block = entries[gi][4]
            meta = block.metadata or {}
            meta["reading_order_index"] = len(final_order) + rank
            # 不确定性来源两路：① 区域图成环；② 版面分区阶段的几何歧义
            # （layout_uncertain_reasons：同时贴合多栏 / 压住 gutter / 栏支撑弱 /
            # 局部栏数变化 / 旋转文字 / 跨栏图表夹在栏内正文中间）。"没有环"不等于
            # "顺序正确"，②让错误但无环的顺序也能进下游的复审门控。
            reasons = list(meta.get("layout_uncertain_reasons") or [])
            if gi in cyclic:
                reasons.append("reading_order_cycle")
            if reasons:
                meta["reading_order_uncertain"] = True
                meta["reading_order_uncertain_reasons"] = reasons
                meta["reading_order_fallback"] = list(tiebreak[gi])
            block.metadata = meta
        final_order.extend(order)

    return [entries[gi] for gi in final_order]
