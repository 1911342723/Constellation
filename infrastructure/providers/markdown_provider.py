"""Markdown provider for the Constellation pipeline.

Markdown 已经自带结构信号：``#`` 标题带明确层级、围栏代码块、GFM 表格、
分割线。这里把它们逐一映射成 Stage-1 的标准 Block，而不是拍平成纯文本——

- ``#{1,6}``          → heading block（``is_heading_style`` + ``heading_level``），
                        让标题候选与层级修复直接命中，几乎不依赖 LLM 猜层级。
- 围栏代码块          → ``type="code"``（``to_markdown`` 会用自适应围栏无损回吐）。
- GFM 表格            → ``type="table"``，正文原样进 ``text``（表格 Markdown 无损）。
- ``---`` 分割线      → ``horizontal_rule`` 元数据块（``to_markdown`` 输出 ``---``）。
- ``![caption](src)`` → ``type="image"``（图片引用不丢）。
- 其余连续行          → 段落块；列表项之间的换行保留，避免清单被拍平成一句。

方案来自一个观察：Markdown 是「结构已显式声明」的格式，Stage 1 就该把作者
声明的结构交给管线，而不是让 Stage 3 再去猜一遍已经写明的东西。
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.core.exceptions import ProviderError
from infrastructure.models import Block

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
_TABLE_DELIMITER_RE = re.compile(r"^ {0,3}\|? *:?-{3,}.*\|.*$")
_TABLE_ROW_RE = re.compile(r"^ {0,3}\|.*\|\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

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

    def _markdown_to_blocks(self, text: str) -> List[Block]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")

        blocks: List[Block] = []
        paragraph: List[str] = []

        def flush_paragraph() -> None:
            body = "\n".join(paragraph).strip()
            paragraph.clear()
            if body:
                blocks.append(self._make_text_block(len(blocks), body))

        index = 0
        total = len(lines)
        while index < total:
            line = lines[index]
            stripped = line.strip()

            heading = _HEADING_RE.match(stripped)
            if heading:
                flush_paragraph()
                blocks.append(
                    Block(
                        id=len(blocks),
                        type="text",
                        text=heading.group(2).strip(),
                        is_heading_style=True,
                        heading_level=len(heading.group(1)),
                        metadata={"source": "md"},
                    )
                )
                index += 1
                continue

            if _HR_RE.match(stripped):
                flush_paragraph()
                blocks.append(
                    Block(
                        id=len(blocks),
                        type="text",
                        text="---",
                        metadata={"source": "horizontal_rule"},
                    )
                )
                index += 1
                continue

            fence = _FENCE_RE.match(stripped)
            if fence:
                flush_paragraph()
                marker = fence.group(1)[:3]
                code_lines: List[str] = []
                index += 1
                while index < total and not lines[index].strip().startswith(marker):
                    code_lines.append(lines[index])
                    index += 1
                index += 1  # 跳过闭合围栏；未闭合则到文件尾
                code_body = "\n".join(code_lines).strip("\n")
                if code_body.strip():
                    blocks.append(
                        Block(id=len(blocks), type="code", text=code_body, metadata={"source": "md"})
                    )
                continue

            if _TABLE_ROW_RE.match(stripped) and index + 1 < total and _TABLE_DELIMITER_RE.match(
                lines[index + 1].strip()
            ):
                flush_paragraph()
                table_lines: List[str] = []
                while index < total and _TABLE_ROW_RE.match(lines[index].strip()):
                    table_lines.append(lines[index].strip())
                    index += 1
                blocks.append(
                    Block(
                        id=len(blocks),
                        type="table",
                        text="\n".join(table_lines),
                        caption=None,
                        metadata={"source": "md"},
                    )
                )
                continue

            image = _IMAGE_RE.match(stripped)
            if image:
                flush_paragraph()
                blocks.append(
                    Block(
                        id=len(blocks),
                        type="image",
                        image_data=image.group(2).strip(),
                        caption=image.group(1).strip() or "Image",
                        metadata={"source": "md"},
                    )
                )
                index += 1
                continue

            if not stripped:
                flush_paragraph()
                index += 1
                continue

            paragraph.append(line)
            index += 1

        flush_paragraph()
        return blocks

    def _make_text_block(self, block_id: int, body: str) -> Block:
        return Block(id=block_id, type="text", text=body, metadata={"source": "md"})
