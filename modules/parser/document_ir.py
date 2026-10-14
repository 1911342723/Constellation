"""DocumentIR — 让「来源 / 阅读 / 导出」三视图共享同一份内容来源（T01）。

为什么要有这一层，用一个具体会坏的例子说清楚：现在标题的 Markdown 标记被**提前写进**
``Block.text`` / ``DocumentNode.content``——DOCX 加粗标题在 ``block.to_markdown()`` 里
就被拼成 ``## **标题**``，图片在提取阶段就编成 ``![caption](data:image/...)``。于是同一个
``content`` 字段里挤着三样本不该混在一起的东西：

1. **源文字**（作者真正写下的字：``标题``、图片的原始字节 / 资源引用）；
2. **解释后的结构**（我们判定它是 H2、是一张带 caption 的图）；
3. **渲染结果**（``##``、``**``、``![...](...)`` 这些是我们加的记号）。

三者一旦挤进一个字符串就再也分不开：想做「来源视图」定位原文时，分不清哪几个字符是作者
写的、哪几个是我们塞的记号；想重新渲染时，又容易把 ``content`` 里**已经可信的** ``**``
再转义一遍（方案第 9.1、第 14 节都点名禁止「重复转义已有可信标记」）。``test_title_and_
markdown_assembly.py`` 里那条「同一标题打两遍」的线上缺陷，根子就是渲染信息和源信息没分家。

DocumentIR 不推翻现有 Block/DocumentNode，而是在其**之上**叠一层显式表示：每个节点把
``source`` / ``structure`` / ``content``（inline span）分三份存，三个视图各取所需再自己渲染，
谁也不用去猜另一份表示里混了什么。

适配器优先接**表格、图片、标题**这三类边界清晰的节点；正文的行内语法（粗体 / 链接 /
行内代码 / 行内公式）由 :mod:`infrastructure.markdown.inline_syntax` 拆成 span 序列，
它以「逐字回吐」为硬不变量，因此接进任何出口都不改变既有产物字节。B10 落地后，
此前那个 ``provisional``（未定）标记已随之删除——它的语义是「inline 还没做」，
做完之后没有生产者，留着就是石山。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from infrastructure.markdown.inline_syntax import InlineKind, InlineSpan, parse_inlines
from infrastructure.models import Block
from modules.parser.schemas import DocumentNode
from modules.parser.titles import strip_title_emphasis

__all__ = [
    "InlineKind",
    "InlineSpan",
    "IRNode",
    "ImageStructure",
    "MARKDOWN_MAX_HEADING",
    "NodeKind",
    "SourceText",
    "TableStructure",
    "VIEW_CONTRACTS",
    "ViewContract",
    "ViewKind",
    "heading_marker_level",
    "ir_from_block",
    "ir_from_document_node",
    "ir_from_document_nodes",
]


class NodeKind(str, Enum):
    """IR 节点类型。

    刻意与 ``Block.type``（物理元素）**不**一一对应：Block 是「物理提取出的一段东西」，
    IR 节点是「解释之后它在文档里扮演的角色」。同一个加粗短段，物理上是 text block，语义上
    可能是 heading，也可能只是段首标签——这个区分正是 A02 想守住的东西，放在 IR 层表达。
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    FORMULA = "formula"
    FOOTNOTE = "footnote"
    BLOCKQUOTE = "blockquote"
    CODE = "code"


@dataclass(frozen=True)
class SourceText:
    """一段内容的**源表示**：作者真正写下的字，以及它从哪来。

    这是三分表示里的「源文字」。它绝不含我们加的 Markdown 记号——``raw`` 存的是
    ``标题``，不是 ``## **标题**``。有了它，来源视图才能如实回放原文并定位，
    渲染层也才有一个「不带记号的干净底本」可依，不必去 ``content`` 里反推哪些字符是记号。

    Attributes:
        raw: 未加任何标记的源文字。对图片 / 表格这类非纯文本节点，``raw`` 留空，
            真正的源载荷放在对应结构节点里（见 :class:`ImageStructure` / :class:`TableStructure`）。
        block_id: 源 Block 的 id（可回指原始 Block 数组做审计 / 定位）。``None`` 表示
            该节点不由单个 Block 直接派生（例如前言聚合、库直接构造）。
        provenance: 规范化 / 来源痕迹。刻意与 ``StructuralAtom.provenance`` 同名同义——
            这里记「我们对源文字做过什么变换」（如是否剥过标题强调记号），使变换可回溯
            （方案第 12.1「规范化可追溯」）。
    """

    raw: str = ""
    block_id: Optional[int] = None
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageStructure:
    """图片节点的**解释后结构**：源载荷（资源引用）与展示属性分开。

    源载荷是 ``asset_id`` 优先、退回 ``image_ref``（P05：资源按引用而非内联）；``caption``
    是从源文里读到的说明文字，**不含** ``![...]`` 记号。导出视图据此自行决定编成
    data URI / 相对文件 / 受控 URL，而不是被迫复用 ``block.text`` 里早就拼死的那一版。
    """

    caption: str = ""
    asset_id: Optional[str] = None
    image_ref: Optional[str] = None
    mime: Optional[str] = None
    width: Optional[float] = None
    height: Optional[float] = None


@dataclass(frozen=True)
class TableStructure:
    """表格节点的**解释后结构**：逻辑行列，而非早已拼成一坨的 Markdown 字符串。

    ``rows`` 存源单元格值（来自 ``Block.table_data['rows']``），是「结构」这一份表示；
    渲染视图要 GFM 就走管道渲染器、要精细排版就走 HTML，各取所需。若源只给了预渲染的
    Markdown（provider 早期就拼好放进 ``Block.text``），退而存进 ``markdown_fallback``
    并标 ``rows=None``——如实说明「这份只有渲染结果、没有拿到逻辑行列」，不假装有结构。
    """

    rows: Optional[List[List[str]]] = None
    caption: str = ""
    markdown_fallback: Optional[str] = None


@dataclass
class IRNode:
    """DocumentIR 的一个节点——**源文字 / 结构 / 渲染结果三分**。

    - ``source``：源文字（:class:`SourceText`），来源视图与渲染底本共用；
    - ``structure``：解释后的结构。标题存层级、图片存 :class:`ImageStructure`、
      表格存 :class:`TableStructure`；这一份是「我们判定出的角色和属性」。
    - ``inlines``：正文的 inline span 序列（仅 paragraph / heading / blockquote 等含正文的
      节点有）。**渲染结果不在这里预先固化**——三视图各自把 ``inlines`` + ``structure``
      渲染成自己要的形态，IR 本身不存 ``##`` / ``**`` / ``![]()`` 这类记号。

    这样一来「来源 / 结构 / 渲染」永远分得开：改渲染规则不动源、定位原文不被记号干扰、
    重复转义无从发生。
    """

    kind: NodeKind
    source: SourceText = field(default_factory=SourceText)
    level: Optional[int] = None                       # 仅 HEADING：语义层级（不是 # 记号级别）
    inlines: List[InlineSpan] = field(default_factory=list)
    image: Optional[ImageStructure] = None            # 仅 IMAGE
    table: Optional[TableStructure] = None            # 仅 TABLE
    section_type: str = "section"                     # 承接 DocumentNode.section_type（含 preamble）
    children: List["IRNode"] = field(default_factory=list)

    def iter_preorder(self) -> "list[IRNode]":
        """先序遍历自身与全部后代（供扁平化 / 校验 / 统计用）。"""
        out: List[IRNode] = [self]
        for child in self.children:
            out.extend(child.iter_preorder())
        return out


class ViewKind(str, Enum):
    """三种视图（方案 9.1 表格的三行）。"""

    SOURCE = "source"      # 校对定位：显示源文字与来源痕迹，不美化
    READING = "reading"    # 连续阅读：统一层级 / 段落 / 可读表格
    EXPORT = "export"      # 下游交付：明确 Markdown 方言、资源布局


@dataclass(frozen=True)
class ViewContract:
    """一个视图「取哪一份表示、按什么规则渲染」的显式声明。

    三视图共享同一棵 IR，差别只在这份契约。把差别收敛成数据（而非散落在各出口的
    if/else）后，「来源视图为何显示记号、导出视图为何不显示 provenance」这些规则一眼可查，
    也保证三视图对同一棵树的结构判断（层级、节点归属）恒等——B02 要守的「多出口一致」
    在 IR 层天然成立，因为它们读的是同一个 ``level`` / 同一份 ``inlines``。

    Attributes:
        kind: 视图种类。
        text_representation: 正文取哪一份 —— ``'source'`` 用源文字原样（来源视图校对用），
            ``'rendered'`` 用带语义记号的渲染形态（阅读 / 导出用）。
        show_provenance: 是否暴露规范化 / 来源痕迹（仅来源视图为 True）。
        image_delivery: 图片如何落地 —— ``'ref'`` 只给资源引用（阅读视图，按需取），
            ``'inline'`` 编成可直接嵌入的形态（导出视图）。
        table_representation: 表格取 ``'rows'``（逻辑行列，供精细渲染）还是
            ``'markdown'``（GFM 兼容视图）。
    """

    kind: ViewKind
    text_representation: str
    show_provenance: bool
    image_delivery: str
    table_representation: str


# 三视图的规则表（方案 9.1「视图 × 用户用途 × 规则」的机器可读版）。
# 立在这里而不是散进各渲染函数：规则集中、可比对、可测试；后续真正接前端时，前端读这份
# 契约即可，不必各自重写一套 if/else。
VIEW_CONTRACTS: Dict[ViewKind, ViewContract] = {
    ViewKind.SOURCE: ViewContract(
        kind=ViewKind.SOURCE,
        text_representation="source",
        show_provenance=True,
        image_delivery="ref",
        table_representation="rows",
    ),
    ViewKind.READING: ViewContract(
        kind=ViewKind.READING,
        text_representation="rendered",
        show_provenance=False,
        image_delivery="ref",
        table_representation="rows",
    ),
    ViewKind.EXPORT: ViewContract(
        kind=ViewKind.EXPORT,
        text_representation="rendered",
        show_provenance=False,
        image_delivery="inline",
        table_representation="markdown",
    ),
}


# ── 适配器：把现有字段渐进接入 IR（优先 表格 / 图片 / 标题）─────────────────


#: 属于「来源」而非「样式」的 Block metadata 键，会被带进 :attr:`SourceText.provenance`。
#: 白名单而不是整份 metadata：IR 不能变成 metadata 垃圾场，但这些键是**唯一**记录着
#: 「这段内容从哪个部件/哪张工作表/哪个角色来、读全了没有」的地方——只留在 Provider 里
#: 就等于下游拿不到，方案第 4 节明确禁止「仅在 Provider 内可见」。
_SOURCE_PROVENANCE_KEYS = (
    "source",
    "embedded_spreadsheet",
    "sheet",
    "sheet_state",
    "read_truncated_rows",
    "read_truncated_cols",
    "read_range",
    "estimated_source_range",
    "source_role",
    "role_confidence",
    "role_reason",
    "reading_order_uncertain",
    "content_relations",
    "source_type",
    "semantic_role",
    "text_fidelity",
    "ocr_confidence",
    "grid_like",
    "figure_like",
    "formula_fidelity",
    "parent_table",
    "nested_table_block_ids",
    "embedded_spreadsheet_block_ids",
)


def _source_provenance(block: Block) -> Dict[str, Any]:
    """从 Block metadata 里摘出来源痕迹（白名单），供 IR 的来源视图与审计使用。"""
    metadata = block.metadata or {}
    return {key: metadata[key] for key in _SOURCE_PROVENANCE_KEYS if key in metadata}


def ir_from_block(block: Block) -> IRNode:
    """把单个物理 :class:`Block` 适配成一个 IR 节点。

    本轮**完整**接入的三类（方案指定的优先顺序）：

    - **表格**：取 ``table_data['rows']`` 作为逻辑行列存进 :class:`TableStructure`；只有
      预渲染 Markdown（``text``）时退回 ``markdown_fallback`` 并如实标 ``rows=None``。
    - **图片**：资源引用（``asset_id`` 优先、退回 ``image_data``）与 caption 分开存进
      :class:`ImageStructure`——**不**在这里拼 ``![...]()``，把「编成什么形态」的决定权
      交回导出视图（P05）。
    - **标题**：这里指物理上被判成 heading 样式的 text block；``level`` 存语义层级，标题
      文字剥掉强调记号后放进源文字（``strip_title_emphasis`` 是全管道剥离标题记号的既有
      口径，沿用它避免又造一套）。

    其余 text / code / formula 段落：段落正文交给
    :func:`infrastructure.markdown.inline_syntax.parse_inlines` 拆成 span 序列（B10）。
    代码块与公式块**整块是源载荷**——行内语法在其中不成立（``a*b*c`` 是乘法不是强调），
    所以它们只产一个标着 ``verbatim`` 的 TEXT span，如实说明「这段没做行内解析及其原因」，
    而不是从前那个含糊的 ``provisional``。
    """
    if block.type == "table":
        rows = None
        table_data = block.table_data or {}
        if isinstance(table_data.get("rows"), list):
            rows = [[str(cell) for cell in row] for row in table_data["rows"]]
        return IRNode(
            kind=NodeKind.TABLE,
            source=SourceText(block_id=block.id, provenance=_source_provenance(block)),
            table=TableStructure(
                rows=rows,
                caption=block.caption or "",
                # 只有在没拿到逻辑行列时才依赖预渲染字符串，并如实记录这一退化。
                markdown_fallback=(block.text if rows is None else None),
            ),
        )

    if block.type == "image":
        return IRNode(
            kind=NodeKind.IMAGE,
            source=SourceText(block_id=block.id, provenance=_source_provenance(block)),
            image=ImageStructure(
                caption=block.caption or "",
                asset_id=block.asset_id,
                image_ref=block.image_data,
                mime=block.asset_mime,
                width=block.asset_width,
                height=block.asset_height,
            ),
        )

    if block.type == "text" and block.is_heading_style and block.heading_level:
        clean = strip_title_emphasis(block.text or "")
        return IRNode(
            kind=NodeKind.HEADING,
            level=max(1, min(6, block.heading_level)),
            source=SourceText(
                raw=clean,
                block_id=block.id,
                # 剥离是对源文字的一次规范化，与来源痕迹合并记进 provenance 以便回溯。
                provenance={
                    **_source_provenance(block),
                    **({"stripped_title_emphasis": True} if clean != (block.text or "") else {}),
                },
            ),
        )

    # 其余：段落 / 代码 / 公式等纯文本承载。
    kind = {
        "code": NodeKind.CODE,
        "formula": NodeKind.FORMULA,
    }.get(block.type, NodeKind.PARAGRAPH)
    text = block.text or ""
    if not text:
        inlines: List[InlineSpan] = []
    elif kind is NodeKind.PARAGRAPH:
        inlines = parse_inlines(text)
    else:
        inlines = [InlineSpan(
            kind=InlineKind.TEXT, text=text,
            attrs={"verbatim": "code_block" if kind is NodeKind.CODE else "formula"},
        )]
    return IRNode(
        kind=kind,
        source=SourceText(raw=text, block_id=block.id, provenance=_source_provenance(block)),
        inlines=inlines,
    )


def ir_from_document_node(node: DocumentNode) -> IRNode:
    """把一个已解析的 :class:`DocumentNode`（Stage 4 树节点）适配成 IR 标题节点及其正文。

    ``DocumentNode`` 是「标题 + 它辖下的一段正文 Markdown」的合体。适配时**把标题和正文
    拆成两层**：节点本身是 HEADING（``level`` 存语义层级、标题剥掉强调记号进源文字），正文
    作为一个 PARAGRAPH 子节点挂在标题下，行内语法由 ``parse_inlines`` 拆成 span 序列。

    ``node.content`` 是 resolver 拼好的逐字保真 Markdown（可能含多个块：表格、图片、
    围栏代码）。这里**不**把它按块级重新解析——那会让 resolver 的拼装结果与重新解析的结果
    成为两个权威；行内解析对它是安全的，因为「逐字回吐」不变量保证渲染出来的字节不变，
    而围栏代码块整段被标 ``verbatim``（里面的 ``*`` 是代码，不是强调）。
    """
    clean_title = strip_title_emphasis(node.title)
    ir = IRNode(
        kind=NodeKind.HEADING,
        level=max(1, node.level),
        source=SourceText(
            raw=clean_title,
            block_id=node.start_block_id,
            provenance=({"stripped_title_emphasis": True} if clean_title != node.title else {}),
        ),
        section_type=node.section_type,
    )
    if node.content:
        ir.children.append(
            IRNode(
                kind=NodeKind.PARAGRAPH,
                source=SourceText(raw=node.content, block_id=node.start_block_id),
                inlines=parse_inlines(node.content),
            )
        )
    for child in node.children:
        ir.children.append(ir_from_document_node(child))
    return ir


def ir_from_document_nodes(nodes: Sequence[DocumentNode]) -> List[IRNode]:
    """顶层入口：把一棵 DocumentNode 森林整体适配成 IR 森林。"""
    return [ir_from_document_node(node) for node in nodes]


# ── 视图参数：语义层级 → Markdown 记号级别 ──────────────────────────
#
# 这个映射原本住在 document_tree 里。搬到这里的理由：它描述的是「本视图的根语义层级
# 映射到哪一级 #」，与 VIEW_CONTRACTS 是同一类东西——视图规则；而渲染实现（ir_render）
# 要用它，留在 document_tree 会让 document_tree ←→ ir_render 成环。规则集中在契约侧，
# 渲染侧单向依赖，是唯一不产生环又只有一份实现的位置。

MARKDOWN_MAX_HEADING = 6


def heading_marker_level(semantic_level: int, root_semantic_level: int, root_marker: int) -> int:
    """把「文档里的语义层级」映射成「本次导出视图里的 ``#`` 记号级别」。

    这是 B02 的单一真相源。以前三处出口各自算偏移：``to_markdown`` 用
    ``level + heading_shift``、``to_markdown_sections`` 用 ``相对级别 + 1``、MCP
    ``render_section_markdown`` 用 ``level + 1``——三个 ``+1`` 语义各不相同却长得一样，
    于是一棵 L1→L2 的树在分章视图里被打成 ``# Root`` + ``### Child``（多降一级），在
    MCP 视图里又变成 ``## Root``（根都不是 H1），两条出口彼此都对不上。改成显式映射后，
    每个出口只需声明「本视图的根语义层级映射到哪一级 ``#``」，后代按语义层级差平移，
    偏移不可能再各自漂移。

    Args:
        semantic_level: 当前节点在文档里的真实层级（``DocumentNode.level``）。
        root_semantic_level: 本视图根节点的语义层级（分章视图＝章节自身的 level；
            全文视图＝顶层节点的 level）。
        root_marker: 根节点映射到的 ``#`` 记号级别（分章根为 1；全文含独立大标题时
            顶层章节为 2，无独立大标题时为 1）。

    Returns:
        钳到 [1, 6] 的 ``#`` 记号级别。语义层级更深、映射后超过 6 的节点仍在 JSON 里
        保留真实 level，只是 Markdown 视图无法再往下打——导出方需自行声明这一映射。
    """
    depth = semantic_level - root_semantic_level
    marker = root_marker + max(depth, 0)
    return min(max(marker, 1), MARKDOWN_MAX_HEADING)
