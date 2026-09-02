"""Block — atomic document element for the Constellation pipeline.

Each Block represents a single physical element (paragraph, table,
image, or formula) extracted from the source document.  It carries:

- A globally unique, monotonically increasing ``id``.
- A ``type`` discriminator (text | image | table | formula).
- Raw content (``text``, ``image_data``, ``table_data``).
- Physical formatting features (bold, font_size, alignment, heading
  style) used by the skeleton compressor to generate Meta-Tags.
"""
import re
from typing import Optional, Literal
from pydantic import BaseModel, Field

from infrastructure.models.table_markdown import rows_to_markdown, sanitize_table_cell


BlockType = Literal["text", "image", "table", "formula", "code"]

_RAW_XML_RE = re.compile(r"\[RAW_XML_NODE:\s*([^\]]+)\]\s*(.*)")


class Block(BaseModel):
    """Standardised document block — the universal exchange format.

    All providers must convert their parsed output into this schema.
    Physical formatting features are injected as Meta-Tags during
    skeleton compression, enabling the LLM to identify headings even
    when the author never used built-in Word heading styles.
    """

    id: int = Field(..., description="Globally unique, monotonically increasing block ID.")
    type: BlockType = Field(..., description="Element type discriminator.")
    text: Optional[str] = Field(None, description="Text content (for text / table blocks).")
    image_data: Optional[str] = Field(None, description="Image payload (base64, URL, or placeholder).")
    caption: Optional[str] = Field(None, description="Image or table caption.")
    table_data: Optional[dict] = Field(None, description="Raw table data ({rows: [[...], ...]}).")
    metadata: Optional[dict] = Field(default_factory=dict, description="Provider-specific metadata.")

    # -- Physical formatting features (populated by the provider) ----------
    is_bold: bool = Field(default=False, description="Whether the majority of runs are bold.")
    font_size: Optional[float] = Field(None, description="Font size in pt; used for high-pass filtering.")
    alignment: Optional[str] = Field(None, description="Paragraph alignment: left|center|right|justify.")
    is_heading_style: bool = Field(default=False, description="True if a Word Heading style is applied.")
    heading_level: Optional[int] = Field(None, description="Heading level (1/2/3...) when is_heading_style is True.")
    has_heading_numbering: bool = Field(default=False, description="True if text starts with a heading numbering pattern (e.g. '1.', '2.1', '3.2.1').")
    
    def get_skeleton_text(
        self,
        head_chars: int = 40,
        tail_chars: int = 30,
        preserve_full_text: bool = False,
        body_font_size: Optional[float] = None,
    ) -> str:
        """Generate a skeleton line for this block with Meta-Tag injection.

        Short text blocks (I-frames) are preserved in full; long text
        blocks (P-frames) are head/tail truncated.  Multimedia elements
        are reduced to typed placeholders.

        Args:
            head_chars: Characters to keep from the start of long text.
            tail_chars: Characters to keep from the end of long text.
            preserve_full_text: Keep the full text even when it exceeds the
                truncation threshold. Used for I-frames such as headings.
            body_font_size: Document body font size used as the baseline
                for the relative ``Size:`` tag. ``None`` falls back to the
                legacy absolute threshold.

        Returns:
            A single skeleton line, e.g. ``[42] <Bold, Size:16> 第一章 绪论``.
        """
        meta_tags = self._build_meta_tags(body_font_size=body_font_size)
        tag_str = f" {meta_tags}" if meta_tags else ""
        
        if self.type == "text" and self.text:
            text = self.text.strip()
            text_len = len(text)
            
            # ===== I帧判定：短文本（可能是标题）全量保留 =====
            if preserve_full_text or text_len <= head_chars + tail_chars:
                return f"[{self.id}]{tag_str} {text}"
            else:
                # ===== P帧截断：长文本头尾保留，中段切除 =====
                head = text[:head_chars]
                tail = text[-tail_chars:]
                omitted = text_len - head_chars - tail_chars
                return f"[{self.id}]{tag_str} {head}...[省略{omitted}字]...{tail}"
                
        elif self.type == "image":
            caption_text = f" [Caption: {self.caption}]" if self.caption else ""
            return f"[{self.id}] <Image>{caption_text}"
            
        elif self.type == "table":
            # 提取表头作为锚点
            header_text = self._get_table_header_text()
            caption_text = f" [Caption: {self.caption}]" if self.caption else ""
            header_hint = f" [Header: {header_text}]" if header_text else ""
            return f"[{self.id}] <Table>{caption_text}{header_hint}"
            
        elif self.type == "code":
            code_preview = ""
            if self.text:
                code_text = self.text.replace("\n", " ")
                code_preview = f" [{code_text[:40].strip()}]" if len(code_text) > 40 else f" [{code_text.strip()}]"
            return f"[{self.id}] <Code>{code_preview}"
            
        elif self.type == "formula":
            formula_preview = ""
            if self.text:
                formula_preview = f" [{self.text[:40]}]" if len(self.text) > 40 else f" [{self.text}]"
            return f"[{self.id}] <Formula>{formula_preview}"
            
        else:
            return f"[{self.id}] <Unknown>"
    
    def _build_meta_tags(self, body_font_size: Optional[float] = None) -> str:
        """Build a Meta-Tag string from physical formatting features.

        Reduces 2-D formatting attributes to a 1-D text label so the
        LLM can detect headings even without explicit Heading styles.

        Args:
            body_font_size: Baseline body font size in pt. When provided,
                the ``Size:`` tag fires for any font measurably larger
                than the body (relative spike detection — works for 9pt
                LaTeX papers and 16pt CJK official documents alike).
                When ``None``, falls back to the legacy absolute
                threshold (> 12pt).

        Returns:
            Tag string such as ``"<Bold, Size:16, Center>"``, or ``""``.
        """
        tags = []
        
        # 最高优先级：Heading 样式
        if self.is_heading_style and self.heading_level:
            tags.append(f"Heading {self.heading_level}")
        
        # 物理特征：加粗
        if self.is_bold:
            tags.append("Bold")
        
        # 物理特征：字号突变（相对正文基准；无基准时退回绝对阈值）
        if self.font_size:
            if body_font_size and body_font_size > 0:
                size_spike = self.font_size >= body_font_size * 1.05
            else:
                size_spike = self.font_size > 12
            if size_spike:
                tags.append(f"Size:{self.font_size:.0f}")
        
        # 物理特征：居中对齐
        if self.alignment and self.alignment.lower() == "center":
            tags.append("Center")
        
        if not tags:
            return ""
        
        return f"<{', '.join(tags)}>"
    
    def _get_table_header_text(self) -> str:
        """Extract the first row of a table as a summary for skeleton anchoring."""
        if self.table_data and "rows" in self.table_data:
            rows = self.table_data["rows"]
            if rows and len(rows) > 0:
                header_cells = rows[0]
                header = " | ".join(str(c) for c in header_cells[:5])  # 最多取前5列
                if len(header) > 60:
                    header = header[:60] + "..."
                return header
        return ""
    
    @staticmethod
    def _sanitize_raw_placeholders(text: str) -> str:
        """Convert raw XML placeholders into clean Markdown.

        Mapping:
        - ``[RAW_XML_NODE: txbxContent] …`` → blockquote
        - ``[RAW_XML_NODE: oMath…] …``      → inline math
        - ``[RAW_XML_NODE: *] …``            → HTML comment
        """
        def _replace(m: re.Match) -> str:
            tag = m.group(1).strip()
            body = (m.group(2) or "").strip()
            if "txbx" in tag.lower():
                return f"> **[文本框]** {body}" if body else "> **[文本框]**"
            if "math" in tag.lower() or "oMath" in tag:
                return Block.render_inline_formula(body) or "$ … $"
            return f"<!-- 未识别元素: {tag} -->" if not body else f"<!-- {tag}: {body} -->"

        return _RAW_XML_RE.sub(_replace, text)

    def to_markdown(self) -> str:
        """Render this block as lossless Markdown for Stage 4 assembly.

        Applies a sanitization pass to convert any ``[RAW_XML_NODE: …]``
        placeholders into presentable Markdown constructs.
        """
        if self.type == "text" and self.text:
            # 水平分割线直接输出，不走 sanitize 流程
            if self.metadata and self.metadata.get("source") == "horizontal_rule":
                return "---"
            return self._sanitize_raw_placeholders(self.text)
        elif self.type == "image":
            # caption 内的 `]`/换行会破坏 `![..](..)`，image_ref 内的空格/换行同理
            caption = (
                (self.caption or "Image")
                .replace("\r", " ").replace("\n", " ").replace("]", "\\]").strip()
            )
            image_ref = (self.image_data or "image").replace("\r", "").replace("\n", "").strip()
            if " " in image_ref:  # 含空格的 URL/路径需用尖括号，否则破坏链接语法
                image_ref = f"<{image_ref}>"
            return f"![{caption}]({image_ref})"
        elif self.type == "table":
            # 优先使用 Markdown 表格文本
            if self.text:
                return self.text
            # 备选：从 table_data 重建
            if self.table_data and "rows" in self.table_data:
                return self._rebuild_markdown_table()
            return f"[表格: {self.caption or '未命名'}]"
        elif self.type == "code":
            text = self.text or ""
            lang = ""
            if text:
                first_line = text.split('\n', 1)[0].lower()
                if 'python' in first_line or 'def ' in first_line:
                    lang = "python"
                elif 'javascript' in first_line or 'const ' in first_line:
                    lang = "javascript"
                elif 'select ' in first_line or 'sql' in first_line:
                    lang = "sql"
            # 动态围栏：内容含连续反引号时用更长围栏，避免提前闭合代码块
            longest_backticks = 0
            run = 0
            for ch in text:
                if ch == "`":
                    run += 1
                    longest_backticks = max(longest_backticks, run)
                else:
                    run = 0
            fence = "`" * max(3, longest_backticks + 1)
            return f"{fence}{lang}\n{text}\n{fence}"
        elif self.type == "formula":
            return self.render_block_formula(self.text)
        return ""
    
    @staticmethod
    def _md_table_cell(value: object) -> str:
        """Compatibility wrapper around the canonical table-cell sanitizer."""
        return sanitize_table_cell(value)

    @staticmethod
    def render_markdown_table(rows: Optional[list]) -> str:
        """Render rows through the pipeline's canonical GFM table renderer."""
        return rows_to_markdown(rows or [])

    def _rebuild_markdown_table(self) -> str:
        """Rebuild a Markdown table from ``table_data`` via :meth:`render_markdown_table`."""
        return self.render_markdown_table((self.table_data or {}).get("rows", []))

    @staticmethod
    def render_inline_formula(text: Optional[str]) -> str:
        r"""行内公式安全渲染为 ``$...$``——全管道行内公式的唯一入口。

        健壮性约定：

        - 折行 → 空格：行内公式跨裸换行会被 Markdown 拆成多段，丢失公式。
        - 剥离外层已有的成对 ``$`` 包裹，避免 ``$$x$$`` 这类二次包裹。
        - 内部裸 ``$`` 转义为 ``\$``，避免提前闭合定界符破坏后文。

        空内容返回 ``""``，交由调用方决定占位。
        """
        core = (text or "").replace("\r", " ").replace("\n", " ").strip()
        while len(core) >= 2 and core[0] == "$" and core[-1] == "$":
            inner = core[1:-1].strip()
            if not inner:
                break
            core = inner
        if not core:
            return ""
        return f"${core.replace('$', chr(92) + '$')}$"

    @staticmethod
    def render_block_formula(text: Optional[str]) -> str:
        r"""块级公式安全渲染为 ``$$ ... $$``——全管道块级公式的唯一入口。

        与 :meth:`render_inline_formula` 同源的健壮处理：折行归一为空格
        （块级公式内的裸换行在部分渲染器会断裂）、剥离外层成对 ``$$``、
        转义内部裸 ``$``。空内容返回 ``[公式]`` 占位而非空串。
        """
        core = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        core = re.sub(r"\s*\n\s*", " ", core)
        while len(core) >= 4 and core.startswith("$$") and core.endswith("$$"):
            inner = core[2:-2].strip()
            if not inner:
                break
            core = inner
        if not core:
            return "[公式]"
        return f"$$ {core.replace('$', chr(92) + '$')} $$"
    
    def is_potential_title(self, min_body_size: float = 12.0) -> bool:
        """Heuristic check: could this block be a section heading?

        Used exclusively by the skeleton compressor for I-frame / P-frame
        classification and RLE fold interruption.  This method only
        examines **physical formatting features** — semantic analysis
        (chapter numbering, keywords like "摘要") is the LLM router's
        responsibility and must not be duplicated here.

        A block is considered a potential title if *any* of:

        1. Has an explicit Word Heading style.
        2. Short text (< 120 chars) with compound physical signals
           (bold + large font, bold + centered, large font alone,
           centered alone).
        3. Medium text (< 200 chars) with bold + large font or
           bold + centered.

        Args:
            min_body_size: Baseline body font size in pt.

        Returns:
            ``True`` if the block's physical features suggest a heading.
        """
        if self.type != "text" or not self.text:
            return False

        text_len = len(self.text.strip())
        is_short = text_len < 120

        # Heading style → unconditional
        if self.is_heading_style:
            return True

        # Numbering pattern (e.g. "1.", "2.1", "3.2.1") + short text
        if self.has_heading_numbering and is_short:
            return True

        has_large_font = bool(self.font_size and self.font_size > min_body_size)
        has_center = bool(self.alignment and self.alignment.lower() == "center")

        # Short text + compound physical signals
        if is_short and self.is_bold and (has_large_font or has_center):
            return True
        if is_short and has_large_font:
            return True
        if is_short and has_center:
            return True

        # Medium text (120–200) + strong compound signals only
        is_medium = text_len < 200
        if is_medium and self.is_bold and has_large_font:
            return True
        if is_medium and self.is_bold and has_center:
            return True

        return False
