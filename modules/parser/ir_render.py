"""IR → 各视图的**唯一**渲染实现（方案 T01）。

T01 的缺口不是"没有 IR"，而是"IR 不是任何出口的渲染来源"：``DocumentIR`` 与
``VIEW_CONTRACTS`` 早就在了，但全文 Markdown、分章 Markdown、PaperData、MCP 章节渲染
各自直接拼字符串——三视图的规则表躺在那儿没人读，于是"同一件事多实现"的老问题在渲染层
重现：同一棵树在不同出口可能得到不同的标题层级、不同的表格形态、不同的图片落地方式。

本模块只做一件事：**吃 IR 森林 + 一份 ViewContract，吐该视图的产物**。所有出口都从这里
过，视图之间的差异只体现在 contract 与视图参数（root_marker / heading_shift）上，
映射规则本身只有一份。

来源视图（SOURCE）此前根本不存在——方案 9.1 明列的三视图里它是"校对和定位"那一档，
需要暴露 block_id、跨度与规范化痕迹。这里补上，它与阅读/导出视图共用同一棵 IR。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from infrastructure.markdown.inline_syntax import render_inlines
from modules.parser.document_ir import (
    VIEW_CONTRACTS,
    IRNode,
    NodeKind,
    ViewContract,
    ViewKind,
    heading_marker_level,
)


def _heading_line(node: IRNode, *, root_semantic_level: int, root_marker: int) -> str:
    """标题行：层级映射复用 :func:`heading_marker_level`，不在这里另算一遍。"""
    marker = heading_marker_level(
        max(1, node.level or 1),
        root_semantic_level=root_semantic_level,
        root_marker=root_marker,
    )
    return f"{'#' * marker} {node.source.raw}".rstrip()


def _table_markdown(node: IRNode, contract: ViewContract) -> str:
    """表格按 contract 选表示：``rows`` 走逻辑行列，``markdown`` 走 GFM 兼容视图。

    拿不到逻辑行列时退回 ``markdown_fallback`` 并**如实**保持这一退化——IR 里已经把
    ``rows=None`` 记下来了，渲染层不该假装自己重建了结构。
    """
    table = node.table
    if table is None:
        return ""
    if contract.table_representation == "rows" and table.rows:
        lines = []
        for index, row in enumerate(table.rows):
            lines.append("| " + " | ".join(row) + " |")
            if index == 0:
                lines.append("| " + " | ".join("---" for _ in row) + " |")
        rendered = "\n".join(lines)
    else:
        rendered = table.markdown_fallback or ""
        if not rendered and table.rows:
            lines = []
            for index, row in enumerate(table.rows):
                lines.append("| " + " | ".join(row) + " |")
                if index == 0:
                    lines.append("| " + " | ".join("---" for _ in row) + " |")
            rendered = "\n".join(lines)
    if table.caption:
        rendered = f"{rendered}\n\n*{table.caption}*" if rendered else f"*{table.caption}*"
    return rendered


def _image_markdown(node: IRNode, contract: ViewContract) -> str:
    """图片按 contract 落地：``ref`` 只给资源引用，``inline`` 才编成可直接嵌入的形态。

    这正是 P05 的收益兑现点：渲染层决定"编成什么形态"，而不是让 provider 早早把
    base64 拼死在 ``block.text`` 里。
    """
    image = node.image
    if image is None:
        return ""
    caption = image.caption or ""
    if contract.image_delivery == "inline" and image.image_ref:
        return f"![{caption}]({image.image_ref})"
    if image.asset_id:
        return f"![{caption}](asset://{image.asset_id})"
    if image.image_ref:
        return f"![{caption}]({image.image_ref})"
    return f"![{caption}]()" if caption else ""


def _body_text(node: IRNode, contract: ViewContract) -> str:
    """正文取源文字还是渲染形态，由 contract 决定（来源视图要的是原样源文字）。

    渲染形态经 ``render_inlines`` 回吐：它是 ``parse_inlines`` 的严格逆运算，因此接上
    行内解析（B10）之后导出的字节与从前逐字相同——语义分类是净增信息，不改产物。
    此前这里写 ``"".join(span.text)``，那只在「整段是一个未拆 span」时才等价；一旦真的
    拆出 ``STRONG``，join 就会静默吃掉 ``**``。
    """
    if contract.text_representation == "source":
        return node.source.raw
    if node.inlines:
        return render_inlines(node.inlines)
    return node.source.raw


def render_nodes(
    nodes: Sequence[IRNode],
    contract: ViewContract,
    *,
    root_semantic_level: int = 1,
    root_marker: int = 1,
    skip_top_level_headings: bool = False,
) -> List[str]:
    """把 IR 森林渲染成 Markdown 段落列表（不 join，便于调用方控制空行）。

    ``skip_top_level_headings`` 供"隐藏伪根"这一既有视图参数使用：零特征文档兜底出来的
    合成根不该作为真实标题渲染出去。
    """
    out: List[str] = []

    def walk(node: IRNode, *, skip_heading: bool) -> None:
        if node.kind == NodeKind.HEADING:
            if not skip_heading:
                out.append(_heading_line(
                    node, root_semantic_level=root_semantic_level, root_marker=root_marker,
                ))
            for child in node.children:
                walk(child, skip_heading=False)
            return
        if node.kind == NodeKind.TABLE:
            rendered = _table_markdown(node, contract)
        elif node.kind == NodeKind.IMAGE:
            rendered = _image_markdown(node, contract)
        else:
            rendered = _body_text(node, contract)
        if rendered:
            out.append(rendered)
        for child in node.children:
            walk(child, skip_heading=False)

    for node in nodes:
        walk(node, skip_heading=skip_top_level_headings)
    return out


def render_markdown(
    nodes: Sequence[IRNode],
    view: ViewKind = ViewKind.EXPORT,
    *,
    root_semantic_level: int = 1,
    root_marker: int = 1,
    skip_top_level_headings: bool = False,
) -> str:
    """便捷入口：按视图种类渲染成一份 Markdown 文本。"""
    parts = render_nodes(
        nodes,
        VIEW_CONTRACTS[view],
        root_semantic_level=root_semantic_level,
        root_marker=root_marker,
        skip_top_level_headings=skip_top_level_headings,
    )
    return "\n\n".join(parts)


def render_source_view(nodes: Sequence[IRNode]) -> List[Dict[str, Any]]:
    """来源视图：逐节点给出 block_id、源文字与规范化痕迹，供校对与定位。

    方案 9.1 的三视图里这一档此前完全缺位。它与阅读/导出视图共用同一棵 IR——这正是
    T01 的意义：三种用途、一份结构来源，不再各自解释一遍文档。
    """
    contract = VIEW_CONTRACTS[ViewKind.SOURCE]
    rows: List[Dict[str, Any]] = []

    def walk(node: IRNode, depth: int) -> None:
        entry: Dict[str, Any] = {
            "kind": node.kind.value if hasattr(node.kind, "value") else str(node.kind),
            "depth": depth,
            "block_id": node.source.block_id,
            "raw": node.source.raw,
        }
        if node.level:
            entry["level"] = node.level
        if contract.show_provenance and node.source.provenance:
            entry["provenance"] = dict(node.source.provenance)
        if node.kind == NodeKind.TABLE and node.table is not None:
            entry["table_rows"] = len(node.table.rows) if node.table.rows else None
            entry["markdown_fallback"] = bool(node.table.markdown_fallback)
        if node.kind == NodeKind.IMAGE and node.image is not None:
            entry["asset_id"] = node.image.asset_id
        if node.inlines:
            # 报真实的行内构成，而不是从前那个「还没拆」的 provisional 计数（B10 落地后
            # 它恒为 0，恒为 0 的计数没有信息量）。``verbatim_inlines`` 是**如实**声明：
            # 围栏代码块 / 代码块 / 公式块这类区域里行内语法不成立，没有解析它们。
            entry["inline_spans"] = len(node.inlines)
            entry["inline_kinds"] = sorted({
                (s.kind.value if hasattr(s.kind, "value") else str(s.kind))
                for s in node.inlines
            })
            verbatim = sum(1 for s in node.inlines if s.attrs.get("verbatim"))
            if verbatim:
                entry["verbatim_inlines"] = verbatim
        rows.append(entry)
        for child in node.children:
            walk(child, depth + 1)

    for node in nodes:
        walk(node, 0)
    return rows
