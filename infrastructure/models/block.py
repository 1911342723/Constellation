"""Block — atomic document element for the Cursor-Caliper pipeline.

Each Block represents a single physical element (paragraph, table,
image, or formula) extracted from the source document.  It carries:

- A globally unique, monotonically increasing ``id``.
- A ``type`` discriminator (text | image | table | formula).
- Raw content (``text``, ``image_data``, ``table_data``).
- Physical formatting features (bold, font_size, alignment, heading
  style) used by the skeleton compressor to generate Meta-Tags.
"""
import re
from typing import Optional, Literal, List
from pydantic import BaseModel, Field


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
    heading_level: Optional[int] = Field(None, description="Heading level (1/2/3…) when is_heading_style is True.")
    
    def get_skeleton_text(self, head_chars: int = 40, tail_chars: int = 30) -> str:
        """Generate a skeleton line for this block with Meta-Tag injection.

        Short text blocks (I-frames) are preserved in full; long text
        blocks (P-frames) are head/tail truncated.  Multimedia elements
        are reduced to typed placeholders.

        Args:
            head_chars: Characters to keep from the start of long text.
            tail_chars: Characters to keep from the end of long text.

        Returns:
            A single skeleton line, e.g. ``[42] <Bold, Size:16> 第一章 绪论``.
        """
        meta_tags = self._build_meta_tags()
        tag_str = f" {meta_tags}" if meta_tags else ""
        
        if self.type == "text" and self.text:
            text = self.text.strip()
            text_len = len(text)
            
            # ===== I帧判定：短文本（可能是标题）全量保留 =====
            if text_len <= head_chars + tail_chars:
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
    
    def _build_meta_tags(self) -> str:
        """Build a Meta-Tag string from physical formatting features.

        Reduces 2-D formatting attributes to a 1-D text label so the
        LLM can detect headings even without explicit Heading styles.

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
        
        # 物理特征：字号突变
        if self.font_size and self.font_size > 12:
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
                return f"$ {body} $" if body else "$ … $"
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
            caption = self.caption or "Image"
            image_ref = self.image_data or "image"
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
            lang = ""
            if self.text:
                first_line = self.text.split('\n', 1)[0].lower()
                if 'python' in first_line or 'def ' in first_line:
                    lang = "python"
                elif 'javascript' in first_line or 'const ' in first_line:
                    lang = "javascript"
                elif 'select ' in first_line or 'sql' in first_line:
                    lang = "sql"
            return f"```{lang}\n{self.text}\n```"
        elif self.type == "formula":
            if self.text:
                return f"$$ {self.text} $$"
            return "[公式]"
        return ""
    
    def _rebuild_markdown_table(self) -> str:
        """Rebuild a Markdown table from ``table_data``."""
        rows = self.table_data.get("rows", [])
        if not rows:
            return ""
        
        lines = []
        header = rows[0]
        lines.append("| " + " | ".join(str(c) for c in header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        
        for row in rows[1:]:
            # 列数补齐
            padded = list(row) + [""] * max(0, len(header) - len(row))
            lines.append("| " + " | ".join(str(c) for c in padded[:len(header)]) + " |")
        
        return "\n".join(lines)
    
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

        # Heading style → unconditional
        if self.is_heading_style:
            return True

        has_large_font = bool(self.font_size and self.font_size > min_body_size)
        has_center = bool(self.alignment and self.alignment.lower() == "center")
        is_short = text_len < 120

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
