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

#: 资源引用的 URI scheme（P05）。挂了 AssetStore 时，Markdown 里的图片写成
#: ``asset://<asset_id>``，由导出层按 ``image_mode`` 决定编成 data URI / 相对
#: 文件 / 受控 URL。刻意选一个自定义 scheme 而不是裸哈希：裸哈希无法与真实
#: 相对路径区分，导出层就没法判断「这是待解析的引用」还是「作者本来就写了
#: 这么个文件名」。
ASSET_URI_SCHEME = "asset://"


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
    # -- 资源引用（P05）------------------------------------------------------
    # 图片字节自 provider 边界起收进 AssetStore，Block 只持一条轻引用。这些字段
    # 是「按引用而非按内联」交付的钩子：导出层据 ``asset_id`` 去 store 取一次字节，
    # 再决定编成 data URI / 相对文件 / 受控 URL。``image_data`` 必须**同时保留**——
    # 大量现有用例与 MCP ``extract_blocks`` 工具直接读它，贸然去掉会全线打断（见
    # tests/test_image_packaging.py::test_full_endpoint_returns_stage1_blocks_...）。
    # 因此新老并存：走 AssetStore 入口的路径两者都填；不走的老路径 asset_id 留空，
    # 行为与改动前逐字节相同。
    asset_id: Optional[str] = Field(None, description="资源内容的完整 sha256（AssetStore 去重键）。")
    asset_mime: Optional[str] = Field(None, description="资源 MIME 类型。")
    asset_width: Optional[float] = Field(None, description="资源像素/点宽度（如可得）。")
    asset_height: Optional[float] = Field(None, description="资源像素/点高度（如可得）。")
    metadata: Optional[dict] = Field(default_factory=dict, description="Provider-specific metadata.")
    # -- Markdown 方言：源文字 / 结构 / 渲染分离（B10）------------------------
    # 方案硬约束：源文字、解释后的结构、渲染后的 Markdown 必须**分开保存**，不能
    # 让提前写进 ``text`` 的 Markdown 标记成为唯一源表示（否则重渲染只能对既成
    # 标记做二次转义，源信息一旦丢就找不回）。两个字段都是可选、默认空，老路径
    # 与既有 provider 一字节不受影响：
    #   * ``source_text``：解析该块时占用的**原始源切片**（未经 strip / 去缩进）。
    #     保留它是为了「先分类再看内容」而不是「先 strip 再分类」——后者正是把
    #     ``    # code`` 误判成标题的 B10 根因。诊断 / 无损回放时可读它。
    #   * ``md_structure``：容器块（引用块 blockquote、列表 list）与需要精确往返
    #     的构造，把**解释后的结构**存这里（如引用嵌套深度、列表项及其子块），
    #     ``to_markdown()`` 由此结构重新渲染，而不是回吐一份预先拼死的字符串。
    #     结构未知的普通块该字段为空，行为与改动前完全一致。
    source_text: Optional[str] = Field(None, description="解析该块时的原始源切片（未 strip / 未去缩进），用于诊断与无损回放。")
    md_structure: Optional[dict] = Field(None, description="Markdown 容器/构造的解释后结构（引用块、列表等），to_markdown 由此重新渲染。")

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

    # ── B10 Markdown 容器渲染：全部由 md_structure（解释后结构）驱动 ─────────
    #
    # 通用约定：这些渲染器**只**读 ``md_structure``，产出规范 CommonMark。子块
    # 递归调用 ``Block.to_markdown()``，因此列表项里的代码块、引用里的列表都能
    # 复用各自类型已有的无损渲染（如 code 的自适应围栏）。渲染出来的文本再被
    # 同一套 provider 扫描时应得到结构等价的块——这是 B10 的往返验收口径。

    @staticmethod
    def _child_blocks_from_structure(children: Optional[list]) -> "list[Block]":
        """把 md_structure 里的子块字典还原成 Block 实例，供递归渲染。"""
        result: list[Block] = []
        for idx, raw in enumerate(children or []):
            if isinstance(raw, Block):
                result.append(raw)
            elif isinstance(raw, dict):
                data = dict(raw)
                data.setdefault("id", idx)
                data.setdefault("type", "text")
                result.append(Block(**data))
        return result

    def _render_children_markdown(self, children: Optional[list]) -> str:
        """渲染一串子块，块间以空行分隔（CommonMark 块级分隔）。"""
        rendered = [b.to_markdown() for b in self._child_blocks_from_structure(children)]
        return "\n\n".join(part for part in rendered if part is not None)

    def _render_blockquote(self, structure: dict) -> str:
        """引用块：把内部子块渲染出来后，每一行前加 ``> ``（含空行）。

        嵌套引用天然成立——内层引用块作为子块渲染后本身已带一层 ``>``，外层再
        统一加一层前缀即得 ``> > ``。引用内的列表 / 代码块同理，因为它们各自的
        渲染结果被逐行加引用前缀。
        """
        inner = self._render_children_markdown(structure.get("children"))
        out_lines = []
        for line in inner.split("\n"):
            out_lines.append(">" if line == "" else f"> {line}")
        return "\n".join(out_lines)

    def _render_list(self, structure: dict) -> str:
        """列表容器：有序 / 无序、嵌套、松散 vs 紧凑，均由结构字段决定。

        结构字段：
          * ``ordered``：True=有序，False=无序。
          * ``start``：有序列表起始序号（默认 1）。
          * ``bullet``：无序标记（``-`` / ``*`` / ``+``，默认 ``-``）。
          * ``tight``：紧凑列表（True，项间无空行）或松散（False，项间空行）。
          * ``items``：每项是 ``{"children": [...子块...]}``。
        """
        ordered = bool(structure.get("ordered"))
        tight = structure.get("tight", True)
        bullet = structure.get("bullet") or "-"
        start = int(structure.get("start", 1) or 1)
        items = structure.get("items") or []

        rendered_items: list[str] = []
        for i, item in enumerate(items):
            marker = f"{start + i}." if ordered else bullet
            indent = " " * (len(marker) + 1)  # 连续行按标记宽度悬挂缩进
            body = self._render_children_markdown((item or {}).get("children"))
            item_lines = body.split("\n")
            first = f"{marker} {item_lines[0]}" if item_lines and item_lines[0] != "" else marker
            rest = [(indent + ln if ln != "" else "") for ln in item_lines[1:]]
            rendered_items.append("\n".join([first, *rest]))

        sep = "\n\n" if not tight else "\n"
        return sep.join(rendered_items)

    def _render_link_definitions(self, structure: dict) -> str:
        """引用式链接定义：``[label]: url "title"``，逐条按源顺序回吐。

        结构字段 ``defs``：``[{"label":..,"url":..,"title":..}, ...]``。title 可空。
        使用处（正文里的 ``[text][label]``）留在普通段落文本中原样往返，定义与
        使用因此都保持——满足「定义与使用都要往返保持」的验收。
        """
        lines = []
        for d in structure.get("defs") or []:
            label = str(d.get("label", "")).strip()
            url = str(d.get("url", "")).strip()
            title = d.get("title")
            if title:
                lines.append(f'[{label}]: {url} "{title}"')
            else:
                lines.append(f"[{label}]: {url}")
        return "\n".join(lines)

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
        # B10：容器块 / 需精确往返的构造，一律由**解释后的结构**重新渲染，而不是
        # 回吐 ``text`` 里预拼的字符串。结构缺席时（普通块、其它 provider）直接
        # 跳过，走下方原有的按 type 分支——行为逐字节不变。
        if self.md_structure:
            kind = self.md_structure.get("kind")
            if kind == "blockquote":
                return self._render_blockquote(self.md_structure)
            if kind == "list":
                return self._render_list(self.md_structure)
            if kind == "link_definitions":
                return self._render_link_definitions(self.md_structure)
        if self.type == "text" and self.text:
            # 水平分割线直接输出，不走 sanitize 流程
            if self.metadata and self.metadata.get("source") == "horizontal_rule":
                return "---"
            body = self._sanitize_raw_placeholders(self.text)
            # 标题块重新渲染回 Markdown 标题标记：源解析成 heading 却按纯文本
            # 回吐会丢掉 ``#``（B10）。setext 保留其下划线形态，ATX 用 ``#``。
            if self.is_heading_style and self.heading_level:
                level = max(1, min(6, self.heading_level))
                syntax = (self.metadata or {}).get("heading_syntax")
                if syntax == "setext" and level in (1, 2):
                    underline = ("=" if level == 1 else "-") * max(3, len(body))
                    return f"{body}\n{underline}"
                return f"{'#' * level} {body}"
            return body
        elif self.type == "image":
            # caption 内的 `]`/换行会破坏 `![..](..)`，image_ref 内的空格/换行同理
            caption = (
                (self.caption or "Image")
                .replace("\r", " ").replace("\n", " ").replace("]", "\\]").strip()
            )
            # P05：挂了 AssetStore 时输出**资源引用**而不是 base64 data URI。
            #
            # 这是 P05 收益的兑现点。base64 文本是原图的 4/3 倍，而同一段
            # Markdown 会被复制进 sections、full_markdown、paper_data 三份大
            # 字符串（再叠 JSON/HTML 转义），一张 3MB 的截图在响应里能占到
            # 十几 MB。此前 packaging 到最后一刻才用正则把这些 data URI 一一
            # 解码、哈希、改写回相对路径——峰值已经产生过了，改写只是事后打捞。
            #
            # 改为在这里就只写 ``asset://<asset_id>``：编成 data URI / 相对文件
            # / 受控 URL 的决定权交给导出层（``image_mode`` 兼容适配器），字节
            # 始终待在 AssetStore 里、按哈希只存一份。
            #
            # 没挂 store（库用法、老入口、绝大多数既有用例）时 asset_id 为空，
            # 逐字节走原来的 image_data 路径——行为完全不变。
            raw_ref = f"{ASSET_URI_SCHEME}{self.asset_id}" if self.asset_id else self.image_data
            image_ref = (raw_ref or "image").replace("\r", "").replace("\n", "").strip()
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
            # 语言优先取源 info string（``rust`` 等）；只有源里完全没有时才从
            # 内容推断——旧实现无条件重猜，把围栏声明的 ``rust`` 丢成空 (B10)。
            lang = str((self.metadata or {}).get("code_lang") or "").strip()
            if not lang and text:
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
