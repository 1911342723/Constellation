"""Document Tree — final output of the CaliperParser pipeline.

Provides multiple output formats:
    1. ``to_json()``          — full JSON tree structure.
    2. ``to_markdown()``      — single consolidated Markdown document.
    3. ``to_markdown_sections()`` — per-chapter Markdown documents
       (primary deliverable).
    4. ``to_paper_data()``    — PaperData format for the layout system.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List

from modules.parser.schemas import DocumentNode
from modules.parser.titles import PSEUDO_ROOT_TITLE, normalize_title, strip_title_emphasis

logger = logging.getLogger(__name__)

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
        """
        self.nodes = nodes
        self.doc_title = doc_title
        self.doc_authors = doc_authors
        self.preamble_content = preamble_content
        self.lossless_fallback = lossless_fallback

    # ── Format 1: JSON tree ────────────────────────────────────

    def to_json(self, indent: int = 2) -> str:
        """Serialise the tree to a JSON string."""
        data = {
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "sections": [self._node_to_dict(node) for node in self.nodes],
        }
        return json.dumps(data, ensure_ascii=False, indent=indent)

    def to_dict(self) -> List[dict]:
        """Serialise the tree to a list of dicts."""
        return [self._node_to_dict(node) for node in self.nodes]

    def _node_to_dict(self, node: DocumentNode) -> dict:
        """Recursively convert a node to a dict."""
        d: dict = {
            "title": strip_title_emphasis(node.title),
            "level": node.level,
            "start_block_id": node.start_block_id,
            "end_block_id": node.end_block_id,
            "content": node.content,
            "section_type": node.section_type,
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

        for node in self.nodes:
            self._node_to_markdown(node, lines, heading_shift, skip_heading=hide_root)

        return "\n".join(lines)

    def _node_to_markdown(
        self,
        node: DocumentNode,
        lines: List[str],
        heading_shift: int = 1,
        skip_heading: bool = False,
    ) -> None:
        """Recursively render a node as Markdown."""
        if not skip_heading:
            heading_level = min(max(node.level + heading_shift, 1), 6)
            lines.append(f"{'#' * heading_level} {strip_title_emphasis(node.title)}")
            lines.append("")

        if node.content:
            lines.append(node.content)
            lines.append("")

        for child in node.children:
            self._node_to_markdown(child, lines, heading_shift)

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
                self._render_child_section(child, md_lines, base_level=1)

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
        base_level: int,
    ) -> None:
        """Recursively render a child section.

        Args:
            node: The child node to render.
            lines: Accumulator list for output lines.
            base_level: Parent's level (used to compute relative
                heading depth).
        """
        relative_level = node.level - base_level + 1
        heading_level = min(max(relative_level + 1, 2), 6)

        lines.append(f"{'#' * heading_level} {strip_title_emphasis(node.title)}")
        lines.append("")

        if node.content:
            lines.append(node.content)
            lines.append("")

        for child in node.children:
            self._render_child_section(child, lines, base_level)

    # ── Format 4: PaperData (layout system adapter) ────────────

    def to_paper_data(self) -> Dict[str, Any]:
        """Convert to PaperData format for the layout system.

        Returns:
            Dict with ``title``, ``authors``, and ``sections`` keys.
        """
        sections = [self._node_to_paper_section(node) for node in self.nodes]
        return {
            "title": strip_title_emphasis(self.doc_title),
            "authors": self.doc_authors,
            "sections": sections,
        }

    def _node_to_paper_section(self, node: DocumentNode) -> Dict[str, Any]:
        """Recursively convert a node to a PaperData section dict."""
        section: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "title": strip_title_emphasis(node.title),
            "content": node.content,
            "type": node.section_type,
            "level": node.level,
            "subsections": [
                self._node_to_paper_section(child)
                for child in node.children
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
        }
