"""Markdown provider for the Constellation pipeline.

Markdown 已经自带结构信号：``#`` 标题带明确层级、围栏代码块、GFM 表格、
分割线。这里把它们逐一映射成 Stage-1 的标准 Block，而不是拍平成纯文本——

- ``#{1,6}``          → heading block（``is_heading_style`` + ``heading_level``），
                        让标题候选与层级修复直接命中，几乎不依赖 LLM 猜层级。
- 围栏代码块          → ``type="code"``（``to_markdown`` 会用自适应围栏无损回吐）。
- GFM 表格            → ``type="table"``，正文原样进 ``text``（表格 Markdown 无损）。
- ``---`` 分割线      → ``horizontal_rule`` 元数据块（``to_markdown`` 输出 ``---``）。
- ``![caption](src)`` → ``type="image"``（图片引用不丢）。
- ``>`` 引用块        → ``type="text"`` + ``md_structure={kind:"blockquote"}``（含嵌套）。
- ``- / 1.`` 列表     → ``type="text"`` + ``md_structure={kind:"list"}``（含嵌套 / 列表内代码）。
- ``[id]: url``       → 汇入一个 ``kind:"link_definitions"`` 块，定义与使用都往返保持。
- 其余连续行          → 段落块；行尾两空格 / 反斜杠的硬换行以 ``\\`` 形态保留。

方案来自一个观察：Markdown 是「结构已显式声明」的格式，Stage 1 就该把作者
声明的结构交给管线，而不是让 Stage 3 再去猜一遍已经写明的东西。

方言与范围（B10）：块级扫描按 CommonMark 0.31.2 语义，手写扫描器、不引入
第三方解析依赖（保持零依赖约束）。**关键取舍**：源文字、解释后的结构、渲染后
的 Markdown 分开保存——容器块把解释后的结构放进 ``Block.md_structure``，由
``to_markdown()`` 依结构重新渲染，绝不让提前拼进 ``text`` 的标记成为唯一源
表示，也不对已有可信标记重复转义。分类一律对**原始行**判定（不先 strip 缩进
再分类，这是「``    # code`` 被误判成标题」的 B10 根因）。

已覆盖的块级构造：ATX 标题（含最多 3 空格前导缩进，>=4 空格按缩进代码处理）、
Setext 标题、缩进代码块、围栏代码块（反引号与波浪 ``~~~``、长围栏 >=4、保留
info string 语言，源信息优先）、GFM 表格、水平线、图片引用、引用块（含嵌套、
引用内列表 / 代码）、列表容器（有序 / 无序、嵌套、松散 vs 紧凑、列表内代码块）、
引用式链接定义、硬换行。**本子集刻意未覆盖**的部分见文件末 ``# 方言覆盖说明``。
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.core.exceptions import ProviderError
from infrastructure.markdown.inline_syntax import FENCE_LINE_RE
from infrastructure.models import Block

_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
_TABLE_DELIMITER_RE = re.compile(r"^ {0,3}\|? *:?-{3,}.*\|.*$")
_TABLE_ROW_RE = re.compile(r"^ {0,3}\|.*\|\s*$")
# 围栏行的正则住在 infrastructure/markdown/inline_syntax.py：块级扫描（这里）与行内解析
# （判断「这段是代码，里面的 * 不是强调」）用的是同一条方言事实，只维护一处。
# tests/test_layering.py 钉住两侧是同一个对象，防止某天又抄出第二份。
# Setext 下划线：仅由 ``=``(L1) 或 ``-``(L2) 组成、位于非空段落行下方。
# CommonMark 0.31.2 §4.3。旧扫描器完全不处理，``Title`` + ``=====`` 不产标题。
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
# 缩进代码块：>= 4 个前导空格（Tab 记 4）且有内容。CommonMark 0.31.2 §4.4。
# 必须对**原始行**判定——先 strip 再分类正是把
# ``    # this is indented code`` 误判成 L1 标题的根因（B10）。
_INDENT_CODE_RE = re.compile(r"^(?: {4}|\t)(.*)$")
# 引用块起始行：最多 3 空格前导 + ``>``（``>`` 后可有一个可选空格）。§5.1。
_BLOCKQUOTE_RE = re.compile(r"^ {0,3}> ?(.*)$")
# 列表项标记：无序 ``- * +`` 或有序 ``1.`` / ``1)``。捕获前导缩进、标记、内容。§5.2/§5.3。
_LIST_ITEM_RE = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])( +|\t)(.*)$")
# 空列表项（标记后直接换行）。
_LIST_ITEM_EMPTY_RE = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])\s*$")
# 引用式链接定义：``[label]: url`` 可带可选标题（"..." / '...' / (...)）。§4.7。
_LINK_DEF_RE = re.compile(
    r'^ {0,3}\[([^\]]+)\]:\s*(\S+)(?:\s+(?:"([^"]*)"|\'([^\']*)\'|\(([^)]*)\)))?\s*$'
)


_TEXT_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "gb18030",
    "gbk",
    "big5",
    "latin-1",
)


class MarkdownProvider:
    """Convert a Markdown file into Stage-1 blocks, honouring its declared structure."""

    def extract(self, file_path: str) -> List[Block]:
        from pathlib import Path

        source = Path(file_path)
        if source.suffix.lower() not in (".md", ".markdown"):
            raise ProviderError("Only .md / .markdown files are supported")
        return self.extract_from_bytes(source.read_bytes())

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        return self._markdown_to_blocks(self._decode_bytes(file_bytes))

    def _decode_bytes(self, file_bytes: bytes) -> str:
        for encoding in _TEXT_ENCODINGS:
            try:
                return file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ProviderError("Unable to decode markdown file; use UTF-8, UTF-16, or GB encodings")

    # ------------------------------------------------------------------
    # 顶层：把整份文本扫成一串结构节点，再实体化为 Block（分配全局 id）。
    # ------------------------------------------------------------------
    def _markdown_to_blocks(self, text: str) -> List[Block]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        nodes = self._scan_blocks(lines)

        blocks: List[Block] = []
        # 引用式链接定义按源顺序汇聚成一个块，放在它们首次出现的位置。
        for node in nodes:
            self._node_to_block(node, blocks)
        return blocks

    def _node_to_block(self, node: dict, blocks: List[Block]) -> None:
        """把一个结构节点实体化为 Block 并追加到 ``blocks``（在此分配 id）。"""
        kind = node["kind"]
        bid = len(blocks)
        src = node.get("source")

        if kind == "heading_atx":
            blocks.append(Block(
                id=bid, type="text", text=node["text"], is_heading_style=True,
                heading_level=node["level"], source_text=src,
                metadata={"source": "md", "heading_syntax": "atx"},
            ))
        elif kind == "heading_setext":
            blocks.append(Block(
                id=bid, type="text", text=node["text"], is_heading_style=True,
                heading_level=node["level"], source_text=src,
                metadata={"source": "md", "heading_syntax": "setext"},
            ))
        elif kind == "code_indented":
            blocks.append(Block(
                id=bid, type="code", text=node["text"], source_text=src,
                metadata={"source": "md", "code_syntax": "indented"},
            ))
        elif kind == "code_fenced":
            meta = {"source": "md", "code_syntax": "fenced"}
            if node.get("lang"):
                meta["code_lang"] = node["lang"]
            if node.get("fence_char") == "~":
                meta["fence_char"] = "~"
            blocks.append(Block(
                id=bid, type="code", text=node["text"], source_text=src, metadata=meta,
            ))
        elif kind == "hr":
            blocks.append(Block(
                id=bid, type="text", text="---", source_text=src,
                metadata={"source": "horizontal_rule"},
            ))
        elif kind == "table":
            blocks.append(Block(
                id=bid, type="table", text=node["text"], caption=None,
                source_text=src, metadata={"source": "md"},
            ))
        elif kind == "image":
            blocks.append(Block(
                id=bid, type="image", image_data=node["ref"],
                caption=node["caption"] or "Image", source_text=src,
                metadata={"source": "md"},
            ))
        elif kind == "paragraph":
            blocks.append(Block(
                id=bid, type="text", text=node["text"], source_text=src,
                metadata={"source": "md"},
            ))
        elif kind == "blockquote":
            blocks.append(Block(
                id=bid, type="text", text=node.get("preview", ""), source_text=src,
                md_structure={"kind": "blockquote", "children": node["children"]},
                metadata={"source": "md", "container": "blockquote"},
            ))
        elif kind == "list":
            blocks.append(Block(
                id=bid, type="text", text=node.get("preview", ""), source_text=src,
                md_structure={
                    "kind": "list",
                    "ordered": node["ordered"],
                    "start": node.get("start", 1),
                    "bullet": node.get("bullet", "-"),
                    "tight": node.get("tight", True),
                    "items": node["items"],
                },
                metadata={"source": "md", "container": "list"},
            ))
        elif kind == "link_definitions":
            blocks.append(Block(
                id=bid, type="text", text=node.get("preview", ""), source_text=src,
                md_structure={"kind": "link_definitions", "defs": node["defs"]},
                metadata={"source": "md", "container": "link_definitions"},
            ))

    # ------------------------------------------------------------------
    # 递归块级扫描器：输入若干（已去掉一层容器前缀的）行，输出结构节点列表。
    # 供顶层与容器（引用块 / 列表项）复用，从而支持任意深度嵌套。
    # ------------------------------------------------------------------
    def _scan_blocks(self, lines: List[str]) -> List[dict]:
        nodes: List[dict] = []
        paragraph: List[str] = []
        pending_defs: List[dict] = []  # 连续的链接定义汇聚成一个节点

        def flush_defs() -> None:
            if pending_defs:
                nodes.append({
                    "kind": "link_definitions",
                    "defs": list(pending_defs),
                    "source": "\n".join(
                        (f'[{d["label"]}]: {d["url"]} "{d["title"]}"' if d.get("title")
                         else f'[{d["label"]}]: {d["url"]}')
                        for d in pending_defs
                    ),
                })
                pending_defs.clear()

        def flush_paragraph(setext_level: Optional[int] = None) -> None:
            flush_defs()
            raw = "\n".join(paragraph)
            paragraph.clear()
            body = raw.strip()
            if not body:
                return
            if setext_level is not None:
                nodes.append({
                    "kind": "heading_setext", "level": setext_level,
                    "text": body, "source": raw,
                })
            else:
                nodes.append({
                    "kind": "paragraph",
                    "text": self._apply_hard_breaks(body),
                    "source": raw,
                })

        index = 0
        total = len(lines)
        while index < total:
            line = lines[index]
            stripped = line.strip()

            # 1) 缩进代码块（原始行判定，先于任何 strip 分类）。
            if not paragraph and not pending_defs and _INDENT_CODE_RE.match(line) and stripped:
                index = self._consume_indented_code(lines, index, nodes)
                continue

            # 2) Setext 下划线（仅当上方有累积段落）。
            setext = _SETEXT_RE.match(line)
            if setext and paragraph:
                level = 1 if setext.group(1).startswith("=") else 2
                flush_paragraph(setext_level=level)
                index += 1
                continue

            # 3) ATX 标题。
            heading = _HEADING_RE.match(line)
            if heading and not _INDENT_CODE_RE.match(line):
                flush_paragraph()
                nodes.append({
                    "kind": "heading_atx", "level": len(heading.group(1)),
                    "text": (heading.group(2) or "").strip(), "source": line,
                })
                index += 1
                continue

            # 4) 水平线。
            if _HR_RE.match(stripped):
                flush_paragraph()
                nodes.append({"kind": "hr", "source": line})
                index += 1
                continue

            # 5) 围栏代码块（反引号 / 波浪、长围栏、info string）。
            if FENCE_LINE_RE.match(line):
                flush_paragraph()
                index = self._consume_fence(lines, index, nodes)
                continue

            # 6) GFM 表格。
            if _TABLE_ROW_RE.match(stripped) and index + 1 < total and _TABLE_DELIMITER_RE.match(
                lines[index + 1].strip()
            ):
                flush_paragraph()
                index = self._consume_table(lines, index, nodes)
                continue

            # 7) 图片引用。
            image = _IMAGE_RE.match(stripped)
            if image:
                flush_paragraph()
                nodes.append({
                    "kind": "image", "caption": image.group(1).strip(),
                    "ref": image.group(2).strip(), "source": line,
                })
                index += 1
                continue

            # 8) 引用块（含嵌套）。
            if _BLOCKQUOTE_RE.match(line):
                flush_paragraph()
                index = self._consume_blockquote(lines, index, nodes)
                continue

            # 9) 列表容器（含嵌套、列表内代码）。
            if _LIST_ITEM_RE.match(line) or _LIST_ITEM_EMPTY_RE.match(line):
                flush_paragraph()
                index = self._consume_list(lines, index, nodes)
                continue

            # 10) 引用式链接定义（连续多条汇聚）。
            link_def = _LINK_DEF_RE.match(line)
            if link_def and not paragraph:
                pending_defs.append({
                    "label": link_def.group(1).strip(),
                    "url": link_def.group(2).strip(),
                    "title": (link_def.group(3) or link_def.group(4) or link_def.group(5) or "").strip() or None,
                })
                index += 1
                continue

            # 11) 空行：段落 / 定义分隔。
            if not stripped:
                flush_paragraph()
                flush_defs()
                index += 1
                continue

            # 12) 普通段落续行。
            flush_defs()
            paragraph.append(line)
            index += 1

        flush_paragraph()
        return nodes

    # ---- 各构造的 consume 助手：返回下一个待处理行号 ---------------------

    def _consume_indented_code(self, lines: List[str], index: int, nodes: List[dict]) -> int:
        total = len(lines)
        raw_lines: List[str] = []
        code_lines: List[str] = []
        while index < total:
            raw = lines[index]
            m = _INDENT_CODE_RE.match(raw)
            if m:
                code_lines.append(m.group(1))
                raw_lines.append(raw)
                index += 1
            elif raw.strip() == "":
                lookahead = index + 1
                while lookahead < total and lines[lookahead].strip() == "":
                    lookahead += 1
                if lookahead < total and _INDENT_CODE_RE.match(lines[lookahead]):
                    code_lines.append("")
                    raw_lines.append(raw)
                    index += 1
                else:
                    break
            else:
                break
        code_body = "\n".join(code_lines).strip("\n")
        if code_body.strip():
            nodes.append({
                "kind": "code_indented", "text": code_body,
                "source": "\n".join(raw_lines),
            })
        return index

    def _consume_fence(self, lines: List[str], index: int, nodes: List[dict]) -> int:
        total = len(lines)
        fence = FENCE_LINE_RE.match(lines[index])
        marker_full = fence.group(1)            # 完整围栏（长度即最小闭合长度）
        fence_char = marker_full[0]             # ``` 或 ~
        info = (fence.group(2) or "").strip()
        # info string：只有波浪围栏允许内含反引号；这里统一取首词为语言名。
        lang = info.split()[0] if info else ""
        raw_lines = [lines[index]]
        code_lines: List[str] = []
        index += 1
        # 闭合：同字符、长度 >= 起始长度、无 info。
        close_re = re.compile(r"^ {0,3}" + re.escape(fence_char) + "{" + str(len(marker_full)) + r",}\s*$")
        while index < total and not close_re.match(lines[index]):
            code_lines.append(lines[index])
            raw_lines.append(lines[index])
            index += 1
        if index < total:  # 收进闭合围栏
            raw_lines.append(lines[index])
            index += 1
        code_body = "\n".join(code_lines).strip("\n")
        if code_body.strip():
            node = {
                "kind": "code_fenced", "text": code_body,
                "source": "\n".join(raw_lines), "fence_char": fence_char,
            }
            if lang:
                node["lang"] = lang
            nodes.append(node)
        return index

    def _consume_table(self, lines: List[str], index: int, nodes: List[dict]) -> int:
        total = len(lines)
        table_lines: List[str] = []
        raw_lines: List[str] = []
        while index < total and _TABLE_ROW_RE.match(lines[index].strip()):
            table_lines.append(lines[index].strip())
            raw_lines.append(lines[index])
            index += 1
        nodes.append({
            "kind": "table", "text": "\n".join(table_lines),
            "source": "\n".join(raw_lines),
        })
        return index

    def _consume_blockquote(self, lines: List[str], index: int, nodes: List[dict]) -> int:
        """收集连续的 ``>`` 行，剥掉一层前缀后递归扫描内部为子块。"""
        total = len(lines)
        inner_lines: List[str] = []
        raw_lines: List[str] = []
        while index < total:
            m = _BLOCKQUOTE_RE.match(lines[index])
            if m:
                inner_lines.append(m.group(1))
                raw_lines.append(lines[index])
                index += 1
            elif lines[index].strip() == "":
                # 引用块以空行结束（不做 lazy continuation 的复杂情形）。
                break
            else:
                # 惰性延续：非空且未加 ``>`` 的行，若上一行属于引用段落，仍并入。
                # 简化处理：遇到明显的块级起始（标题/围栏/列表/引用外）则停止。
                if paragraph_continuation_stops(lines[index]):
                    break
                inner_lines.append(lines[index])
                raw_lines.append(lines[index])
                index += 1
        children = [self._node_to_child(n) for n in self._scan_blocks(inner_lines)]
        preview = "\n".join(inner_lines).strip()
        nodes.append({
            "kind": "blockquote", "children": children,
            "preview": preview, "source": "\n".join(raw_lines),
        })
        return index

    def _consume_list(self, lines: List[str], index: int, nodes: List[dict]) -> int:
        """收集一个列表容器：同类标记的连续项，每项内容（含悬挂缩进的续行、
        子列表、代码块）剥掉列表缩进后递归扫描。松散 vs 紧凑由项间空行判定。"""
        total = len(lines)
        first = _LIST_ITEM_RE.match(lines[index]) or _LIST_ITEM_EMPTY_RE.match(lines[index])
        first_marker = first.group(2)
        ordered = first_marker[-1] in ".)"
        bullet = None if ordered else first_marker
        start = int(first_marker[:-1]) if ordered else 1

        items: List[dict] = []
        raw_lines: List[str] = []
        loose = False
        blank_seen_between = False

        while index < total:
            m = _LIST_ITEM_RE.match(lines[index])
            me = _LIST_ITEM_EMPTY_RE.match(lines[index])
            if not m and not me:
                if lines[index].strip() == "":
                    # 记一个可能的项间空行；下一行仍是列表项才算松散。
                    blank_seen_between = True
                    raw_lines.append(lines[index])
                    index += 1
                    continue
                break

            match = m or me
            marker = match.group(2)
            this_ordered = marker[-1] in ".)"
            if this_ordered != ordered:
                break  # 有序 / 无序切换 → 新列表

            if blank_seen_between:
                loose = True
                blank_seen_between = False

            indent_len = len(match.group(1))
            if m:
                content_indent = indent_len + len(marker) + len(m.group(3))
                item_body_lines = [m.group(4)]
            else:
                content_indent = indent_len + len(marker) + 1
                item_body_lines = []
            raw_lines.append(lines[index])
            index += 1

            # 续行：缩进 >= content_indent 的行、或空行，都属于本项。
            while index < total:
                cur = lines[index]
                if cur.strip() == "":
                    # 空行：向前看下一非空行是否仍缩进属于本项。
                    look = index + 1
                    while look < total and lines[look].strip() == "":
                        look += 1
                    if look < total and _leading_spaces(lines[look]) >= content_indent:
                        item_body_lines.append("")
                        raw_lines.append(cur)
                        index += 1
                        continue
                    break
                if _leading_spaces(cur) >= content_indent:
                    item_body_lines.append(cur[content_indent:])
                    raw_lines.append(cur)
                    index += 1
                elif (_LIST_ITEM_RE.match(cur) or _LIST_ITEM_EMPTY_RE.match(cur)):
                    break  # 同级下一项
                else:
                    break  # 列表结束
            children = [self._node_to_child(n) for n in self._scan_blocks(item_body_lines)]
            items.append({"children": children})

        node = {
            "kind": "list", "ordered": ordered, "items": items,
            "tight": not loose, "source": "\n".join(raw_lines),
            "preview": "\n".join(raw_lines).strip(),
        }
        if ordered:
            node["start"] = start
        else:
            node["bullet"] = bullet
        nodes.append(node)
        return index

    def _node_to_child(self, node: dict) -> dict:
        """把内部结构节点转成 md_structure 里可存的子块字典（不分配全局 id）。

        子块在渲染期由 ``Block._child_blocks_from_structure`` 重新实体化，因此
        这里只需保存重建一个 Block 所需的最小字段。"""
        kind = node["kind"]
        if kind in ("heading_atx", "heading_setext"):
            return {
                "type": "text", "text": node["text"], "is_heading_style": True,
                "heading_level": node["level"],
                "metadata": {"source": "md",
                             "heading_syntax": "atx" if kind == "heading_atx" else "setext"},
            }
        if kind == "code_indented":
            return {"type": "code", "text": node["text"],
                    "metadata": {"source": "md", "code_syntax": "indented"}}
        if kind == "code_fenced":
            meta = {"source": "md", "code_syntax": "fenced"}
            if node.get("lang"):
                meta["code_lang"] = node["lang"]
            return {"type": "code", "text": node["text"], "metadata": meta}
        if kind == "hr":
            return {"type": "text", "text": "---", "metadata": {"source": "horizontal_rule"}}
        if kind == "table":
            return {"type": "table", "text": node["text"], "metadata": {"source": "md"}}
        if kind == "image":
            return {"type": "image", "image_data": node["ref"],
                    "caption": node["caption"] or "Image", "metadata": {"source": "md"}}
        if kind == "blockquote":
            return {"type": "text",
                    "md_structure": {"kind": "blockquote", "children": node["children"]},
                    "metadata": {"source": "md", "container": "blockquote"}}
        if kind == "list":
            return {"type": "text",
                    "md_structure": {
                        "kind": "list", "ordered": node["ordered"],
                        "start": node.get("start", 1), "bullet": node.get("bullet", "-"),
                        "tight": node.get("tight", True), "items": node["items"],
                    },
                    "metadata": {"source": "md", "container": "list"}}
        if kind == "link_definitions":
            return {"type": "text",
                    "md_structure": {"kind": "link_definitions", "defs": node["defs"]},
                    "metadata": {"source": "md", "container": "link_definitions"}}
        # 段落：保留硬换行
        return {"type": "text", "text": node["text"], "metadata": {"source": "md"}}

    @staticmethod
    def _apply_hard_breaks(body: str) -> str:
        r"""硬换行归一：CommonMark §6.7 里行尾两个及以上空格、或行尾反斜杠，都是
        硬换行。段落 ``.strip()`` 会吃掉行尾空格形态，故统一归一成反斜杠形态
        ``\`` 保存进 ``text``——反斜杠不会被 strip 吃掉，且是 CommonMark 认可的硬
        换行写法，往返再扫描仍是硬换行。行内其余语法不改写（见文件末覆盖说明）。"""
        out_lines = []
        parts = body.split("\n")
        for i, ln in enumerate(parts):
            if i == len(parts) - 1:
                out_lines.append(ln)
                continue
            # 两空格结尾 → 反斜杠；已是反斜杠结尾 → 原样保留。
            if re.search(r" {2,}$", ln):
                out_lines.append(ln.rstrip() + "\\")
            elif ln.endswith("\\"):
                out_lines.append(ln)
            else:
                out_lines.append(ln)
        return "\n".join(out_lines)

    def _make_text_block(self, block_id: int, body: str) -> Block:
        return Block(id=block_id, type="text", text=body, metadata={"source": "md"})


def _leading_spaces(line: str) -> int:
    """行首空白宽度（Tab 记 4，与缩进代码判定一致）。"""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def paragraph_continuation_stops(line: str) -> bool:
    """引用块惰性延续的终止判据：遇到明确的块级起始就停止把裸行并进引用。"""
    s = line.strip()
    if not s:
        return True
    if _HEADING_RE.match(line) or _HR_RE.match(s) or FENCE_LINE_RE.match(line):
        return True
    if _LIST_ITEM_RE.match(line) or _LIST_ITEM_EMPTY_RE.match(line):
        return True
    return False


# ── 方言覆盖说明（B10 · 如实声明）─────────────────────────────────────────
# 目标方言：CommonMark 0.31.2 的**块级**子集 + GFM 表格扩展。
#
# 已覆盖（块级，往返保持语义）：
#   ATX / Setext 标题、缩进代码、围栏代码（反引号 / 波浪 ~~~、长围栏 >=4、
#   info string 语言且源信息优先）、水平线、GFM 表格、图片引用、引用块（含嵌套、
#   引用内列表 / 代码）、列表容器（有序 / 无序、嵌套、松散 vs 紧凑、列表内代码块）、
#   引用式链接定义（定义与使用都保持）、硬换行（行尾两空格 / 反斜杠 → 归一为反斜杠）。
#
# 刻意未覆盖（本零依赖手写子集不做，并说明原因）：
#   1) HTML 块（``<div>`` 等原始 HTML）：并入普通段落原样往返，不做 HTML 块识别
#      （行内 HTML 标签已由 inline_syntax 认出，块级容器没有）。
#   2) 引用块的复杂惰性延续（lazy continuation）与列表项跨空行的段落归并的
#      全部边角情形：只实现常见形态，罕见的 CommonMark 边角用例可能与参考实现
#      判定不同，但源文字经 ``source_text`` 始终可无损取回。
#   3) 脚注**定义**块（``[^1]: 正文``）、任务列表 ``- [ ]``、删除线 ``~~x~~`` 等其它
#      GFM 扩展：未声明支持，按普通文本 / 无序列表原样往返（``~~`` 不与 ``~~~`` 围栏
#      冲突，因围栏要求 >=3 且独占一行）。脚注**引用** ``[^1]`` 已由 inline_syntax 认出。
#
# 行内语法（强调 / 行内代码 / 行内链接与图片 / 自动链接 / 行内 HTML / 行内公式 /
# 脚注引用 / 硬换行两形态）由 ``infrastructure/markdown/inline_syntax.py`` 负责拆解，
# 以「逐字回吐」为硬不变量；本模块只管块级切分，两侧共用同一条围栏正则。
