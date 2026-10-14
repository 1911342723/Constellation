"""Document Tree — final output of the CaliperParser pipeline.

Provides multiple output formats:
    1. ``to_json()``          — full JSON tree structure.
    2. ``to_markdown()``      — single consolidated Markdown document.
    3. ``to_markdown_sections()`` — per-chapter Markdown documents
       (primary deliverable).
    4. ``to_paper_data()``    — PaperData format for the layout system.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List

from modules.parser.document_ir import (
    MARKDOWN_MAX_HEADING,
    ViewKind,
    heading_marker_level,
    ir_from_document_nodes,
)
from modules.parser.ir_render import render_markdown, render_source_view
from modules.parser.schemas import DocumentNode
from modules.parser.titles import PSEUDO_ROOT_TITLE, normalize_title, strip_title_emphasis

logger = logging.getLogger(__name__)

# Markdown 只有 H1–H6。语义层级可以更深（JSON 里保留真实 level），但一旦要打成
# ``#`` 记号就必须钳到 6 级——否则 ``'#' * 7`` 在 CommonMark 里根本不是标题，会被
# 当成普通段落，标题静默消失。这是「语义层级 → 导出视图标题级别」映射的物理上限。


# B03：前言（封面 / 作者 / 摘要前说明 / 目录前导）在文档里客观存在，但既不是章节
# 也不该被当成普通章节重复打印。给它一个显式的区域类型，让所有结构化出口都能声明
# 「我这份产物包含前言」，MCP 侧也据此把它当成 index 0 的可读、可检索节点。
PREAMBLE_SECTION_TYPE = "preamble"


def _stable_section_id(
    doc_identity: str,
    start_block_id: Any,
    end_block_id: Any,
    structural_path: tuple[int, ...],
) -> str:
    """由「文档身份 + 来源跨度 + 结构位置」确定性地派生 section_id（B08）。

    以前 ``_node_to_paper_section`` 每次序列化都 ``uuid.uuid4()``，于是对同一棵树连续调用
    两次 ``to_paper_data()`` 得到的同一章节 ID 都不一样——缓存命中、再次下载、切换预览后
    编辑器持有的评论 / 锚点 / 折叠状态全部对不上号。这里改成纯函数：相同文档同版本、
    同一节点位置永远得到同一个 ID。

    刻意**不**用标题哈希：标题会重复（多个「Abstract」）、会被编辑，拿它当身份会在改标题
    时错误地换掉 ID、在重名时错误地并成同一 ID。来源跨度 + 树中结构路径才是稳定且唯一的
    身份——它就是这个节点在这份文档里的物理位置。
    """
    path = ".".join(str(i) for i in structural_path)
    raw = f"{doc_identity}|{start_block_id}-{end_block_id}|{path}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # 收敛成 UUID 形状：下游布局系统（PaperData 消费方）历来把 section id 当 UUID 存，
    # 换成任意长度的 hex 可能撑破其字段约束。取前 32 位十六进制排成 8-4-4-4-12。
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

# Image blocks serialise as ``![caption](data:image/...;base64,...)`` whose
# base64 payload can reach tens of MB; counting it would let a single image
# dominate the body-text statistics. Matched markup is excluded from char
# counts so the numbers reflect prose, not encoded binary. Base64 data URIs
# never contain ``)``, so the lazy ``[^)]*`` stays inside one image construct.
_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def content_text_length(content: str) -> int:
    """Return the body-text character count, excluding embedded image markup."""
    return len(_IMAGE_MARKDOWN_RE.sub("", content))


class DocumentTree:
    """Document tree — the CaliperParser's final product.

    Wraps the parsed document hierarchy and exposes several output
    formats.  The primary deliverable is ``to_markdown_sections()``,
    which splits the document into one independent Markdown file per
    top-level chapter.
    """

    def __init__(
        self,
        nodes: List[DocumentNode],
        doc_title: str = "",
        doc_authors: str = "",
        preamble_content: str = "",
        lossless_fallback: bool = False,
        source_identity: str | None = None,
    ):
        """Initialise the document tree.

        Args:
            nodes: Top-level :class:`DocumentNode` list.
            doc_title: Document title (extracted by the LLM).
            doc_authors: Document authors (extracted by the LLM).
            preamble_content: Metadata content preceding the first
                chapter (title page, abstract, etc.).
            lossless_fallback: 本树来自「零特征文档」的无损兜底（伪根承载全篇），
                渲染时不把内部哨兵标题当成文档里真实存在的标题打出来。
            source_identity: 源文档 + 管线版本的身份串（解析器传入的块内容指纹）。
                B08 的 section_id 由它派生；见 :meth:`doc_identity`。
        """
        self.nodes = nodes
        self.doc_title = doc_title
        self.doc_authors = doc_authors
        self.preamble_content = preamble_content
        self.lossless_fallback = lossless_fallback
        self.source_identity = source_identity

    # ── Document identity & regions ────────────────────────────

    def doc_identity(self) -> str:
        """本文档同一版本的稳定身份串（B08 的 section_id 派生依赖它）。

        身份必须同时满足两件事，而它们会互相拉扯：**相同文档同版本要稳定**，
        **不同文档不能串**。首选解析器传入的 ``source_identity`` —— 那是
        ``_compute_blocks_hash`` 对全部 Block 载荷 + 管线配置 + 模型名算出的指纹，
        既唯一标识源文档与管线版本，又**不含解析出来的节点跨度**。

        这一点是刻意的：身份若掺进全部节点的跨度，任何一处局部纠错（把某节的
        起止块挪一格）都会改变身份，于是**整篇每个 section_id 全变**，评论、锚点、
        折叠状态一起丢 —— 正是 B08 验收里「局部纠错只改变必要节点」要禁止的事。
        跨度已经进了单个 section 的 id（见 :func:`_stable_section_id`），在身份里
        再算一次只会把局部改动放大成全局改动。

        没有 ``source_identity`` 时（库用法直接构造 DocumentTree、测试）退回
        标题 + 作者。这在同名文档之间可能撞车，但它同样不随局部纠错漂移；要跨文档
        绝对不串就把源指纹传进来。
        """
        if self.source_identity:
            return hashlib.sha256(
                self.source_identity.encode("utf-8")
            ).hexdigest()[:16]
        raw = f"{normalize_title(self.doc_title)}|{self.doc_authors}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _preamble_span(self) -> tuple[int, int] | None:
        """前言在原始 Block 数组里的来源跨度 ``[0, 第一章起始块-1]``。

        解析器（resolver）不把 span 单独传进来，但前言按定义就是「第一章之前的一切」，
        所以跨度可由第一个章节的 ``start_block_id`` 反推，无需改动上游。第一章从 0 号块
        开始（没有前置块）时返回 None，避免造出 ``[0, -1]`` 这种空跨度。
        """
        if not self.preamble_content or not self.nodes:
            return None
        first_block = self.nodes[0].start_block_id
        if first_block is None or first_block <= 0:
            return None
        return (0, int(first_block) - 1)

    def preamble_dict(self) -> dict | None:
        """前言的结构化表示（front matter 节点），带来源跨度；无前言时返回 None。

        section_type=``preamble`` 让所有出口都能声明「本产物含前言」，又把它和普通章节
        区分开——渲染器据此不把它当章节重复打印，MCP 据此把它当 index 0 的可读 / 可检索
        节点。id 与章节走同一套确定性派生，前言在多次序列化间同样保持稳定。
        """
        if not self.preamble_content:
            return None
        span = self._preamble_span()
        start, end = span if span else (0, 0)
        return {
            "id": _stable_section_id(self.doc_identity(), start, end, (-1,)),
            "title": "",
            "level": 0,
            "start_block_id": start,
            "end_block_id": end,
            "content": self.preamble_content,
            "section_type": PREAMBLE_SECTION_TYPE,
            "children": [],
        }

    def outline(self) -> List[dict]:
        """轻量大纲：结构与来源跨度，**不含正文**（P06）。

        正文可能几十 MB，而「这篇文档有哪些章、各多长」通常只需几 KB。把它做成
        树自己的方法而不是在装配层遍历 ``self.nodes``：大纲是文档树该回答的问题，
        放在装配层就迫使每个替身/适配器都暴露内部节点结构。

        ``content_chars`` 让调用方据此决定要不要再去取某一章的正文。
        """
        items: List[dict] = []

        def _walk(nodes: List[DocumentNode]) -> None:
            for node in nodes:
                items.append({
                    "title": node.title,
                    "level": node.level,
                    "start_block_id": node.start_block_id,
                    "end_block_id": node.end_block_id,
                    "content_chars": len(node.content or ""),
                    "section_type": node.section_type,
                })
                _walk(node.children)

        _walk(self.nodes)
        return items

    def document_regions(self) -> List[str]:
        """本树包含的文档区域清单，供每个出口自我声明「我含哪些区域」。

        B03 要求「所有出口声明自己包含哪些文档区域」：消费者据此判断某产物是否已含前言，
        而不必靠「碰运气正文里搜不搜得到」。
        """
        regions: List[str] = []
        if self.preamble_content:
            regions.append(PREAMBLE_SECTION_TYPE)
        if self.nodes:
            regions.append("body")
        return regions

    # ── Format 1: JSON tree ────────────────────────────────────

    def to_json(self, indent: int = 2) -> str:
        """Serialise the tree to a JSON string.

        ``preamble`` / ``document_regions`` 显式声明本产物含哪些文档区域（B03）：结构化
        消费者不必再靠「正文里搜不搜得到」去猜前言是否被保留。前言绝不混进 ``sections``
        重复打印，而是独占一个 front matter 字段。
        """
        preamble = self.preamble_dict()
        data = {
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "document_regions": self.document_regions(),
            "preamble": preamble,
            "sections": [self._node_to_dict(node) for node in self.nodes],
            "quality_contract": {
                "content_coverage_scope": "extracted_blocks_only",
                "semantic_correctness_verified": False,
                "confidence_is_accuracy": False,
            },
            "quality_report": self.quality_report(),
        }
        return json.dumps(data, ensure_ascii=False, indent=indent)

    def to_dict(self) -> List[dict]:
        """Serialise the tree to a list of dicts.

        前言以 ``section_type=preamble`` 的 front matter 节点排在最前（B03）。这样
        下游的章节索引把它索引成 index 0，按章节读取和检索的接口就能单独读取和检索
        前言——以前它只可能出现在 ``full.md`` 里，
        没有对应的可寻址章节。它带 ``preamble`` 类型，渲染器据此不把它当普通章节重复打印。
        """
        dicts = [self._node_to_dict(node) for node in self.nodes]
        preamble = self.preamble_dict()
        if preamble is not None:
            dicts.insert(0, preamble)
        return dicts

    def _node_to_dict(self, node: DocumentNode) -> dict:
        """Recursively convert a node to a dict."""
        d: dict = {
            "title": strip_title_emphasis(node.title),
            "level": node.level,
            "start_block_id": node.start_block_id,
            "end_block_id": node.end_block_id,
            "content": node.content,
            "section_type": node.section_type,
            "confidence_details": node.confidence_details,
            "content_relations": node.content_relations,
            "children": [
                self._node_to_dict(child) for child in node.children
            ],
        }
        return d

    # ── Format 2: single Markdown document ─────────────────────

    def hides_pseudo_root(self) -> bool:
        """伪根节点的标题行是否应当隐去。

        零特征文档（识别不到任何标题信号）的无损兜底会造一个伪根承载全篇正文。它的标题是
        解析器的内部哨兵——文档里并不存在这一行字，渲染成 ``# Document`` 等于凭空给用户
        加了个假标题。判定用显式 flag 而不是只比标题文本，否则一份真的以「Document」
        为首标题的文档会被误伤。
        """
        return (
            self.lossless_fallback
            and len(self.nodes) == 1
            and self.nodes[0].title == PSEUDO_ROOT_TITLE
            and not self.nodes[0].children
        )

    def title_is_first_heading(self) -> bool:
        """``doc_title`` 是否就是第一个章节标题本身，而不是一个独立的文档大标题。

        文档里没有独立 Title 样式段时，``doc_title`` 正是从第一个一级标题兜底来的
        （LLM 也倾向于把首个标题当文档名）。此时它和 ``nodes[0]`` 是同一行文字，不是两层结构。
        """
        if not self.nodes or not self.doc_title:
            return False
        return normalize_title(self.doc_title) == normalize_title(self.nodes[0].title)

    def to_markdown(self) -> str:
        """Render the entire tree as a single Markdown document.

        标题层级只在「文档大标题独占 H1」时整体下移一级。若 ``doc_title`` 就是第一个章节
        标题本身，单独再打一行 H1 会让同一个标题连着出现两遍（曾经的真实缺陷），此时把 H1
        让给章节自己，层级不再下移——章节 level 1 就渲染成 ``#``。
        """
        lines: List[str] = []

        hide_root = self.hides_pseudo_root()
        own_title_line = bool(self.doc_title) and not self.title_is_first_heading()
        heading_shift = 1 if own_title_line else 0

        if own_title_line:
            lines.append(f"# {strip_title_emphasis(self.doc_title)}")
            lines.append("")

        if self.doc_authors:
            lines.append(f"*{self.doc_authors}*")
            lines.append("")

        if self.preamble_content:
            lines.append(self.preamble_content)
            lines.append("")

        # T01：正文与标题一律走 IR 渲染器。此前这里自己递归拼字符串，与分章视图、
        # PaperData、MCP 各拼一遍——三视图的规则表（VIEW_CONTRACTS）躺着没人读，
        # 于是同一棵树在不同出口可能得到不同的标题层级与表格/图片形态。现在只有
        # ir_render 一处实现，视图差异只体现在 contract 与 root_marker 上。
        body = render_markdown(
            ir_from_document_nodes(self.nodes),
            ViewKind.EXPORT,
            root_semantic_level=1,
            root_marker=1 + heading_shift,
            skip_top_level_headings=hide_root,
        )
        if body:
            lines.append(body)
            # 旧实现每渲染一块就追加一个空行，因此产物末尾恒有一个换行。保持逐字节一致：
            # 这份 Markdown 是对外产物，尾部空白也算契约。
            lines.append("")

        return "\n".join(lines)

    # ── Format 3: per-chapter Markdown documents ───────────────

    def to_markdown_sections(self) -> List[Dict[str, str]]:
        """Split the tree into one Markdown document per top-level chapter.

        This is the primary deliverable of the CaliperParser.  Each
        top-level node (level 1) becomes an independent Markdown
        document containing its own content and all sub-sections.

        Returns:
            List of dicts, each with keys ``title``, ``content``,
            ``section_type``, and ``level``.
        """
        sections: List[Dict[str, str]] = []
        hide_root = self.hides_pseudo_root()

        for node in self.nodes:
            md_lines: List[str] = []
            title = "" if hide_root else strip_title_emphasis(node.title)

            if title:
                md_lines.append(f"# {title}")
                md_lines.append("")

            if node.content:
                md_lines.append(node.content)
                md_lines.append("")

            for child in node.children:
                self._render_child_section(child, md_lines, root_semantic_level=node.level)

            sections.append({
                "title": title,
                "content": "\n".join(md_lines),
                "section_type": node.section_type,
                "level": node.level,
            })

        logger.debug(
            "[DocumentTree] Split into %d Markdown sections", len(sections),
        )
        for i, sec in enumerate(sections):
            logger.debug(
                "  [%d] %s (%s, %d chars)",
                i, sec["title"], sec["section_type"], len(sec["content"]),
            )

        return sections

    def _render_child_section(
        self,
        node: DocumentNode,
        lines: List[str],
        root_semantic_level: int,
    ) -> None:
        """Recursively render a child section within a per-chapter export view.

        分章视图的根（章节本身）是 H1；后代按「语义层级 − 根语义层级」的差平移。以前这里写
        ``相对级别 + 1`` 又外加一次 ``+ 1``，导致 L1 根下的 L2 子节被打成 ``### Child``（多降
        一级，B02 的原始缺陷）。

        T01 之后这里不再自己拼字符串：与全文视图共用 ``ir_render``，两条出口的差别只剩
        ``root_semantic_level`` / ``root_marker`` 这两个视图参数——渲染实现只有一份，
        偏移不可能再各自漂移。

        Args:
            node: The child node to render.
            lines: Accumulator list for output lines.
            root_semantic_level: 本分章视图根节点（章节自身）的语义层级。
        """
        rendered = render_markdown(
            ir_from_document_nodes([node]),
            ViewKind.EXPORT,
            root_semantic_level=root_semantic_level,
            root_marker=1,
        )
        if rendered:
            lines.append(rendered)
            lines.append("")

    def to_source_view(self) -> List[Dict[str, Any]]:
        """来源视图：逐节点给出 block_id、源文字、层级与规范化痕迹（方案 9.1）。

        三视图里这一档此前完全缺位——只有阅读/导出两条出口在用 IR 之外的字符串拼装。
        它与全文/分章视图共用同一棵 IR，所以"同一份结构、三种用途"这句话才成立：
        校对时看到的 block_id 与正文渲染出来的内容一定来自同一个节点。
        """
        return render_source_view(ir_from_document_nodes(self.nodes))

    # ── Format 4: PaperData (layout system adapter) ────────────

    def to_paper_data(self) -> Dict[str, Any]:
        """Convert to PaperData format for the layout system.

        ``document_regions`` / ``preamble`` 声明并携带前言（B03）；section id 由文档身份 +
        来源跨度 + 结构位置确定性派生（B08），对同一棵树重复调用恒等，编辑器持有的评论 /
        锚点 / 折叠状态跨缓存命中、再次下载、切换预览都不会因换 ID 而丢失。

        Returns:
            Dict with ``title``, ``authors``, ``document_regions``,
            ``preamble`` and ``sections`` keys.
        """
        doc_identity = self.doc_identity()
        sections = [
            self._node_to_paper_section(node, doc_identity, (i,))
            for i, node in enumerate(self.nodes)
        ]
        preamble = self.preamble_dict()
        return {
            "title": strip_title_emphasis(self.doc_title),
            "authors": self.doc_authors,
            "document_regions": self.document_regions(),
            # 前言不进 sections 重复打印；作为独立 front matter 字段，带稳定 id 与来源跨度。
            "preamble": (
                {
                    "id": preamble["id"],
                    "content": preamble["content"],
                    "type": PREAMBLE_SECTION_TYPE,
                    "start_block_id": preamble["start_block_id"],
                    "end_block_id": preamble["end_block_id"],
                }
                if preamble is not None else None
            ),
            "sections": sections,
        }

    def _node_to_paper_section(
        self,
        node: DocumentNode,
        doc_identity: str,
        structural_path: tuple[int, ...],
    ) -> Dict[str, Any]:
        """Recursively convert a node to a PaperData section dict.

        ``id`` 走 :func:`_stable_section_id`（文档身份 + 来源跨度 + 结构路径），取代原来的
        ``uuid.uuid4()``——后者让同一棵树的两次序列化拿到不同 ID（B08 的原始缺陷）。
        """
        section: Dict[str, Any] = {
            "id": _stable_section_id(
                doc_identity, node.start_block_id, node.end_block_id, structural_path,
            ),
            "title": strip_title_emphasis(node.title),
            "content": node.content,
            "type": node.section_type,
            "level": node.level,
            "subsections": [
                self._node_to_paper_section(child, doc_identity, structural_path + (i,))
                for i, child in enumerate(node.children)
            ],
        }
        return section

    # ── Statistics ─────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate statistics for the document tree.

        Returns:
            Dict with ``doc_title``, ``doc_authors``,
            ``top_level_sections``, ``total_sections``,
            ``total_content_chars``, and ``max_depth``.
        """
        total_sections = 0
        total_content_chars = 0
        max_depth = 0

        def _count(nodes: List[DocumentNode], depth: int) -> None:
            nonlocal total_sections, total_content_chars, max_depth
            for node in nodes:
                total_sections += 1
                total_content_chars += content_text_length(node.content)
                max_depth = max(max_depth, depth)
                _count(node.children, depth + 1)

        _count(self.nodes, 1)

        return {
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "top_level_sections": len(self.nodes),
            "total_sections": total_sections,
            "total_content_chars": total_content_chars,
            "max_depth": max_depth,
            "quality_report": self.quality_report(),
        }

    def quality_report(self) -> dict:
        nodes = []
        def collect(items):
            for node in items:
                nodes.append(node)
                collect(node.children)
        collect(self.nodes)
        return {
            "semantic_correctness_verified": False,
            "content_coverage_scope": "extracted_blocks_only",
            "needs_confirmation_section_ids": [n.start_block_id for n in nodes
                if n.confidence_details.get("needs_confirmation")
                or any(r.get("status") == "needs_confirmation" for r in n.content_relations)],
            "confidence_quality": "domain_scoped" if any(
                n.confidence_details.get("heading", {}).get("quality") == "calibrated_on_holdout" for n in nodes
            ) else "heuristic",
            "content_relation_count": sum(len(n.content_relations) for n in nodes),
        }
