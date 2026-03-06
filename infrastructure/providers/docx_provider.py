"""
Docx Provider v2 for Cursor-Caliper — 混合 XML 引擎

混合引擎架构
- 主引擎：python-docx 处理常规段落、表格、图片（稳定可靠）
- 补充引擎：lxml 直接解析 OOXML，捕获 python-docx 忽略的节点：
  - OMML 公式 (w:oMath, w:oMathPara) → Formula Block
  - 浮动文本框 (w:txbxContent) → 递归提取为 Text Block
  - SmartArt / OLE 等未知节点 → [RAW_XML_NODE: tag] 占位符 Block
  
这样既不丢数据，又不用重写整个解析器，兑现"100% 无损率"的承诺。
"""
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import io

try:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.table import CT_Tbl
    from docx.document import Document as DocumentType
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    raise ImportError("请安装 python-docx: pip install python-docx")

try:
    from lxml import etree
except ImportError:
    etree = None

from infrastructure.models import Block

logger = logging.getLogger(__name__)

# OOXML 命名空间
NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'v': 'urn:schemas-microsoft-com:vml',
    'm': 'http://schemas.openxmlformats.org/officeDocument/2006/math',
    'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
    'wps': 'http://schemas.microsoft.com/office/word/2010/wordprocessingShape',
}

ALIGNMENT_MAP = {
    WD_ALIGN_PARAGRAPH.LEFT: "left",
    WD_ALIGN_PARAGRAPH.CENTER: "center",
    WD_ALIGN_PARAGRAPH.RIGHT: "right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
}

# python-docx 能识别的顶级元素类型
KNOWN_ELEMENT_TYPES = (CT_P, CT_Tbl)

# 已知的无语义 body-level OOXML 元素，静默跳过（纯排版元数据，无可见内容）
SILENT_SKIP_TAGS = frozenset({
    'bookmarkStart', 'bookmarkEnd',
    'proofErr',
    'permStart', 'permEnd',
    'commentRangeStart', 'commentRangeEnd',
    'customXml',
    'moveFromRangeStart', 'moveFromRangeEnd',
    'moveToRangeStart', 'moveToRangeEnd',
    'lastRenderedPageBreak',
})


# ================================================================
# RichSegment — Run 的标准化中间表示 (Phase 1 output)
# ================================================================

@dataclass(frozen=True, slots=True)
class RichSegment:
    """Immutable, provider-agnostic representation of a single text run.

    Decouples formatting detection (Phase 1) from Markdown rendering
    (Phase 3) so each phase can be tested and evolved independently.
    """

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False

    @property
    def style_key(self) -> Tuple[bool, ...]:
        """Hashable style fingerprint for homogeneous-run merging."""
        return (self.bold, self.italic, self.underline,
                self.strike, self.superscript, self.subscript)

    @property
    def has_formatting(self) -> bool:
        return any(self.style_key)


class DocxProvider:
    """
    Docx 文档提供器 v2（混合 XML 引擎）
    
    职责：
    1. 按照文档物理流顺序遍历 .docx 内容
    2. python-docx 处理段落、表格、图片（主引擎）
    3. lxml 补充捕获 OMML 公式、文本框、未知节点（补充引擎）
    4. 嗅探物理特征（Bold/Size/Center/Heading），注入 Block 元数据
    5. 保持零损耗的内容提取
    """
    
    def __init__(self):
        self.image_counter = 0
        self.image_store: Dict[str, bytes] = {}
        self._doc_rels = None

    def _reset_state(self) -> None:
        """Reset per-extraction mutable state to avoid cross-document leaks."""
        self.image_counter = 0
        self.image_store = {}
        self._doc_rels = None
    
    def extract(self, file_path: str) -> List[Block]:
        """从文件路径提取 .docx 内容为 Block 列表"""
        self._reset_state()
        logger.info("开始解析 Docx 文件: %s", file_path)
        try:
            doc = Document(file_path)
            blocks = self._extract_blocks(doc)
            logger.info("解析完成，共提取 %d 个 Block", len(blocks))
            self._log_debug_info(blocks)
            return blocks
        except Exception as e:
            logger.error(f"解析 Docx 文件失败: {str(e)}")
            raise
    
    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        """从字节流提取 .docx 内容为 Block 列表"""
        self._reset_state()
        logger.info("开始解析 Docx 字节流")
        try:
            doc = Document(io.BytesIO(file_bytes))
            blocks = self._extract_blocks(doc)
            logger.info(f"解析完成，共提取 {len(blocks)} 个 Block")
            return blocks
        except Exception as e:
            logger.error(f"解析 Docx 字节流失败: {str(e)}")
            raise
    
    def _extract_blocks(self, doc: DocumentType) -> List[Block]:
        """
        从 Document 对象提取 Block 列表（混合引擎）
        
        遍历 w:body 的每个子元素：
        - CT_P (段落) → python-docx 主引擎处理
        - CT_Tbl (表格) → python-docx 主引擎处理
        - 其他未知元素 → lxml 补充引擎捕获
        """
        blocks: List[Block] = []
        block_id = 0
        self._doc_rels = doc.part.rels
        
        for element in doc.element.body:
            try:
                if isinstance(element, CT_P):
                    # 主引擎：处理段落
                    paragraph = Paragraph(element, doc)
                    
                    # 检查段落内是否包含 OMML 公式
                    formula_blocks = self._extract_omml_from_paragraph(element, block_id)
                    
                    has_text = bool(paragraph.text.strip())
                    image_blocks = self._extract_images_from_paragraph(
                        paragraph, block_id + (1 if has_text else 0) + len(formula_blocks)
                    )
                    
                    # 检查段落内是否包含文本框
                    textbox_blocks = self._extract_textbox_from_element(
                        element, block_id + (1 if has_text else 0) + len(formula_blocks) + len(image_blocks)
                    )
                    
                    if has_text:
                        text_block = self._process_paragraph(paragraph, block_id)
                        if text_block:
                            blocks.append(text_block)
                            block_id += 1
                    elif self._detect_horizontal_rule(element):
                        # 空段落 + 底部边框 = Word 自动生成的水平分割线
                        blocks.append(Block(
                            id=block_id,
                            type="text",
                            text="---",
                            metadata={"source": "horizontal_rule"},
                        ))
                        block_id += 1
                    
                    for fb in formula_blocks:
                        fb.id = block_id
                        blocks.append(fb)
                        block_id += 1
                    
                    for img_block in image_blocks:
                        img_block.id = block_id
                        blocks.append(img_block)
                        block_id += 1
                    
                    for tb in textbox_blocks:
                        tb.id = block_id
                        blocks.append(tb)
                        block_id += 1
                
                elif isinstance(element, CT_Tbl):
                    # 主引擎：处理表格
                    table = Table(element, doc)
                    table_block = self._process_table(table, block_id)
                    if table_block:
                        blocks.append(table_block)
                        block_id += 1
                        
                elif etree is not None and (element.tag == f'{{{NS["m"]}}}oMathPara' or element.tag == f'{{{NS["m"]}}}oMath'):
                    formula_text = self._omml_to_text(element)
                    if formula_text:
                        blocks.append(Block(
                            id=block_id,
                            type="formula",
                            text=formula_text,
                            metadata={"source": "omml_root"},
                        ))
                        block_id += 1
                
                else:
                    # 前置过滤：静默跳过已知的无语义元数据节点
                    tag_short = element.tag.split('}')[-1] if '}' in str(element.tag) else str(element.tag)
                    if tag_short in SILENT_SKIP_TAGS:
                        logger.debug(f"[混合引擎] 静默跳过已知元数据节点: {tag_short}")
                        continue
                    
                    # 补充引擎：捕获 python-docx 无法识别的节点
                    raw_blocks = self._capture_unknown_element(element, block_id)
                    for rb in raw_blocks:
                        rb.id = block_id
                        blocks.append(rb)
                        block_id += 1
                        
            except Exception as e:
                logger.warning(f"处理元素时出错，安全跳过: {str(e)}")
                continue
        
        return self._post_process_blocks(blocks)
    
    def _post_process_blocks(self, blocks: List[Block]) -> List[Block]:
        """合并连续的代码块，并重新分配连续递增的 block_id"""
        if not blocks:
            return blocks
            
        merged = []
        for block in blocks:
            if block.type == "code" and merged and merged[-1].type == "code":
                merged[-1].text = (merged[-1].text or "") + "\n" + (block.text or "")
            else:
                merged.append(block)
                
        # 重新分配 ID，确保严格单调递增
        for i, block in enumerate(merged):
            block.id = i
            
        return merged
    
    # ================================================================
    # v2 新增：OMML 公式提取
    # ================================================================
    
    def _extract_omml_from_paragraph(self, element, start_block_id: int) -> List[Block]:
        """
        从段落 XML 中提取 OMML 公式 (w:oMath / w:oMathPara)
        
        python-docx 会忽略公式节点，这里用 lxml 直接捕获。
        """
        if etree is None:
            return []
        
        formula_blocks = []
        bid = start_block_id
        
        # 查找段落内的 oMathPara（独立公式块）和 oMath（行内公式）
        for math_elem in element.findall(f'.//{{{NS["m"]}}}oMathPara'):
            formula_text = self._omml_to_text(math_elem)
            if formula_text:
                formula_blocks.append(Block(
                    id=bid,
                    type="formula",
                    text=formula_text,
                    metadata={"source": "omml_para"},
                ))
                bid += 1
        
        # 行内公式（不在 oMathPara 内的独立 oMath）
        for math_elem in element.findall(f'.//{{{NS["m"]}}}oMath'):
            # 跳过已被 oMathPara 包含的
            parent = math_elem.getparent()
            if parent is not None and parent.tag == f'{{{NS["m"]}}}oMathPara':
                continue
            formula_text = self._omml_to_text(math_elem)
            if formula_text:
                formula_blocks.append(Block(
                    id=bid,
                    type="formula",
                    text=formula_text,
                    metadata={"source": "omml_inline"},
                ))
                bid += 1
        
        return formula_blocks
    
    def _omml_to_text(self, element) -> str:
        """从 OMML 元素转化为可读文本（带有简单 LaTeX 降级支持），专门支持 fraction 等"""
        if element is None:
            return ""
            
        tag = element.tag
        if tag == f'{{{NS["m"]}}}f':
            num = element.find(f'{{{NS["m"]}}}num')
            den = element.find(f'{{{NS["m"]}}}den')
            num_t = self._omml_to_text(num) if num is not None else ""
            den_t = self._omml_to_text(den) if den is not None else ""
            return f"\\frac{{{num_t}}}{{{den_t}}}"
        elif tag == f'{{{NS["m"]}}}r':
            t = element.find(f'{{{NS["m"]}}}t')
            return t.text if t is not None and t.text else ""
        
        # 通用遍历
        texts = []
        for child in element:
            t = self._omml_to_text(child)
            if t:
                texts.append(t)
        return "".join(texts).strip()
    
    # ================================================================
    # v2 新增：浮动文本框提取
    # ================================================================
    
    def _extract_textbox_from_element(self, element, start_block_id: int) -> List[Block]:
        """
        从元素中提取浮动文本框 (w:txbxContent) 的内容
        
        文本框内容在 python-docx 中被忽略，这里递归提取。
        """
        if etree is None:
            return []
        
        textbox_blocks = []
        bid = start_block_id
        
        # 搜索所有 txbxContent 节点
        for txbx in element.iter(f'{{{NS["w"]}}}txbxContent'):
            # 递归提取文本框内的段落文本
            paragraphs_text = []
            for p_elem in txbx.findall(f'{{{NS["w"]}}}p'):
                p_text = self._extract_text_from_xml_paragraph(p_elem)
                if p_text.strip():
                    paragraphs_text.append(p_text.strip())
            
            if paragraphs_text:
                combined = "\n".join(paragraphs_text)
                textbox_blocks.append(Block(
                    id=bid,
                    type="text",
                    text=combined,
                    metadata={"source": "textbox"},
                ))
                bid += 1
        
        return textbox_blocks
    
    def _extract_text_from_xml_paragraph(self, p_element) -> str:
        """从 XML 段落元素中提取纯文本"""
        texts = []
        for t_elem in p_element.iter(f'{{{NS["w"]}}}t'):
            if t_elem.text:
                texts.append(t_elem.text)
        return "".join(texts)
    
    # ================================================================
    # v2 新增：未知节点捕获
    # ================================================================
    
    def _capture_unknown_element(self, element, block_id: int) -> List[Block]:
        """
        捕获 python-docx 无法识别的 XML 节点
        
        对于 SmartArt、OLE、自定义 XML 等复杂节点，
        记录其标签名和内部文本作为占位符 Block，
        确保即便我们不理解这是什么，最终组装时也能原封不动地保留位置。
        """
        tag = element.tag if hasattr(element, 'tag') else str(type(element))
        
        # 清理命名空间前缀，提取可读标签名
        tag_short = tag.split('}')[-1] if '}' in tag else tag
        
        # 尝试提取内部文本
        inner_text = ""
        try:
            if etree is not None:
                # 提取所有 w:t 文本节点
                t_texts = []
                for t_elem in element.iter(f'{{{NS["w"]}}}t'):
                    if t_elem.text:
                        t_texts.append(t_elem.text)
                inner_text = "".join(t_texts).strip()
        except Exception:
            pass
        
        if not inner_text:
            # 如果没有文本内容，记录为纯占位符
            placeholder = f"[RAW_XML_NODE: {tag_short}]"
        else:
            placeholder = f"[RAW_XML_NODE: {tag_short}] {inner_text}"
        
        logger.debug(f"[混合引擎] 捕获未知节点: {tag_short}, 内容长度: {len(inner_text)}")
        
        return [Block(
            id=block_id,
            type="text",
            text=placeholder,
            metadata={"source": "raw_xml", "tag": tag_short},
        )]
    
    # ================================================================
    # 原有逻辑（保持不变）
    # ================================================================
    
    def _process_paragraph(self, paragraph: Paragraph, block_id: int) -> Optional[Block]:
        """处理段落元素，嗅探物理特征 + 提取行内富文本"""
        plain_text = paragraph.text.strip()
        if not plain_text:
            return None
        
        rich_text = self._extract_rich_text(paragraph)
        text = rich_text.strip() if rich_text and rich_text.strip() else plain_text
        
        style_name = paragraph.style.name if paragraph.style else ""
        
        is_heading = False
        heading_level = None
        heading_match = re.match(r'^Heading\s+(\d+)$', style_name, re.IGNORECASE)
        if heading_match:
            is_heading = True
            heading_level = int(heading_match.group(1))
        
        is_bold = self._detect_bold(paragraph)
        is_code = self._detect_code_font(paragraph)
        font_size = self._detect_font_size(paragraph)
        alignment = self._detect_alignment(paragraph)
        
        list_level = None
        try:
            pPr = paragraph._element.get_or_add_pPr()
            numPr = pPr.find(f'{{{NS["w"]}}}numPr')
            if numPr is not None:
                ilvl = numPr.find(f'{{{NS["w"]}}}ilvl')
                if ilvl is not None:
                    list_level = int(ilvl.get(f'{{{NS["w"]}}}val', '0'))
        except Exception:
            pass
        
        return Block(
            id=block_id,
            type="code" if is_code else "text",
            text=text,
            metadata={"style": style_name, "list_level": list_level, "source": "paragraph"},
            is_bold=is_bold,
            font_size=font_size,
            alignment=alignment,
            is_heading_style=is_heading,
            heading_level=heading_level,
        )
    
    # ================================================================
    # v3: Three-Phase Rich Text Pipeline
    #
    #   Raw Runs → [Phase 1: Normalize] → [Phase 2: Merge] → [Phase 3: Render]
    #
    # Phase 1 (_normalize_runs): Convert python-docx Runs into
    #   immutable RichSegment objects.  No string manipulation.
    # Phase 2 (_merge_homogeneous_segments): Absorb adjacent segments
    #   with identical style fingerprints into one.
    # Phase 3 (_render_segments): Apply Strip-Safe Markdown wrapping
    #   to each merged segment and concatenate.
    # ================================================================

    def _extract_rich_text(self, paragraph: Paragraph) -> str:
        """Three-phase rich text pipeline orchestrator.

        Returns a Markdown string with clean inline formatting:
        no ghost spaces, no fragmented bold runs, no marker bleed.
        """
        runs = paragraph.runs
        if not runs:
            return paragraph.text or ""

        suppress_bold = self._detect_bold(paragraph)

        # Phase 1 → Phase 2 → Phase 3
        segments = self._normalize_runs(runs, suppress_bold)
        segments = self._merge_homogeneous_segments(segments)
        return self._render_segments(segments)

    # -- Phase 1: Normalize ------------------------------------------------

    def _normalize_runs(self, runs, suppress_bold: bool) -> List[RichSegment]:
        """Convert raw python-docx Runs into RichSegment objects.

        Args:
            runs: ``paragraph.runs`` sequence.
            suppress_bold: If ``True``, bold is suppressed (paragraph
                is already detected as all-bold at the Block level).
        """
        segments: List[RichSegment] = []

        for run in runs:
            text = run.text
            if not text:
                continue

            is_bold = run.bold is True and not suppress_bold
            is_italic = run.italic is True
            is_underline = run.underline is True
            is_strike = (run.font.strike is True) if run.font else False

            is_superscript = False
            is_subscript = False
            try:
                vert_align = run.font._element.find(
                    f'{{{NS["w"]}}}vertAlign'
                )
                if vert_align is not None:
                    val = vert_align.get(f'{{{NS["w"]}}}val')
                    if val == 'superscript':
                        is_superscript = True
                    elif val == 'subscript':
                        is_subscript = True
            except Exception:
                pass

            segments.append(RichSegment(
                text=text,
                bold=is_bold,
                italic=is_italic,
                underline=is_underline,
                strike=is_strike,
                superscript=is_superscript,
                subscript=is_subscript,
            ))

        return segments

    # -- Phase 2: Homogeneous-Run Merge ------------------------------------

    @staticmethod
    def _merge_homogeneous_segments(segments: List[RichSegment]) -> List[RichSegment]:
        """Absorb adjacent segments with identical style fingerprints.

        ``**A** **B** **C**`` becomes ``**A B C**``.
        Pure-whitespace segments between two same-style segments are
        absorbed into the preceding segment to prevent orphaned spaces.
        """
        if not segments:
            return segments

        merged: List[RichSegment] = [segments[0]]

        for seg in segments[1:]:
            prev = merged[-1]

            if seg.style_key == prev.style_key:
                # Same style → merge text
                merged[-1] = RichSegment(
                    text=prev.text + seg.text,
                    bold=prev.bold,
                    italic=prev.italic,
                    underline=prev.underline,
                    strike=prev.strike,
                    superscript=prev.superscript,
                    subscript=prev.subscript,
                )
            elif not seg.text.strip() and not seg.has_formatting:
                # Pure whitespace with no formatting — absorb into prev
                merged[-1] = RichSegment(
                    text=prev.text + seg.text,
                    bold=prev.bold,
                    italic=prev.italic,
                    underline=prev.underline,
                    strike=prev.strike,
                    superscript=prev.superscript,
                    subscript=prev.subscript,
                )
            else:
                merged.append(seg)

        return merged

    # -- Phase 3: Strip-Safe Render ----------------------------------------

    @staticmethod
    def _wrap_safe(text: str, marker: str) -> str:
        """Wrap *text* with *marker*, keeping whitespace outside.

        ``_wrap_safe(" hello ", "**")`` → ``" **hello** "``

        Prevents the ghost-space problem where Markdown markers
        bleed into surrounding whitespace.
        """
        if not text or not text.strip():
            return text  # pure whitespace — never wrap

        prefix = text[:len(text) - len(text.lstrip())]
        suffix = text[len(text.rstrip()):]
        core = text.strip()
        return f"{prefix}{marker}{core}{marker}{suffix}"

    @classmethod
    def _render_segments(cls, segments: List[RichSegment]) -> str:
        """Render merged RichSegments into a Markdown string.

        Applies markers inside-out (strike → italic → bold) with
        Strip-Safe wrapping at each layer.
        """
        parts: List[str] = []

        for seg in segments:
            text = seg.text

            if not seg.has_formatting:
                parts.append(text)
                continue

            # Apply markers inside-out so nesting is correct
            if seg.strike:
                text = cls._wrap_safe(text, "~~")
            if seg.italic:
                text = cls._wrap_safe(text, "*")
            if seg.bold:
                text = cls._wrap_safe(text, "**")
            if seg.underline and not seg.bold and not seg.italic:
                stripped = text.strip()
                if stripped:
                    prefix = text[:len(text) - len(text.lstrip())]
                    suffix = text[len(text.rstrip()):]
                    text = f"{prefix}<u>{stripped}</u>{suffix}"
            if seg.superscript:
                text = f"<sup>{text.strip()}</sup>"
            if seg.subscript:
                text = f"<sub>{text.strip()}</sub>"

            parts.append(text)

        return "".join(parts)

    # -- Bold detection (v3: stricter threshold) ---------------------------

    def _detect_bold(self, paragraph: Paragraph) -> bool:
        """Detect whether the paragraph is "all bold" (≥80% of runs bold).

        v3 change: threshold raised from 50% to 80% to reduce false
        positives on paragraphs where the author only emphasized a few
        keywords.  Pure-whitespace runs are excluded from the count.
        """
        runs = [r for r in paragraph.runs if r.text.strip()]
        if not runs:
            return False

        bold_count = 0
        for run in runs:
            if run.bold is True:
                bold_count += 1
            elif run.bold is None:
                try:
                    if paragraph.style and paragraph.style.font and paragraph.style.font.bold:
                        bold_count += 1
                except Exception:
                    pass

        return bold_count >= len(runs) * 0.8
    
    def _detect_code_font(self, paragraph: Paragraph) -> bool:
        """Detect whether the paragraph is 'all code' (≥80% of runs use monospace font)."""
        runs = [r for r in paragraph.runs if r.text.strip()]
        if not runs:
            return False
            
        code_count = 0
        mono_fonts = {'courier', 'courier new', 'consolas', 'monaco', 'lucida console', 'dejavu sans mono'}
        for run in runs:
            font_name = None
            if run.font and run.font.name:
                font_name = run.font.name
            elif paragraph.style and paragraph.style.font and paragraph.style.font.name:
                font_name = paragraph.style.font.name
                
            if font_name and font_name.lower() in mono_fonts:
                code_count += 1
                
        return code_count >= len(runs) * 0.8
    
    def _detect_font_size(self, paragraph: Paragraph) -> Optional[float]:
        """嗅探段落字号（取最大值）"""
        max_size = None
        for run in paragraph.runs:
            if run.text.strip():
                try:
                    if run.font.size:
                        size_pt = run.font.size.pt
                        if max_size is None or size_pt > max_size:
                            max_size = size_pt
                except Exception:
                    pass
        
        if max_size is None:
            try:
                if paragraph.style and paragraph.style.font and paragraph.style.font.size:
                    max_size = paragraph.style.font.size.pt
            except Exception:
                pass
        
        return max_size
    
    def _detect_horizontal_rule(self, paragraph_element) -> bool:
        """检测段落是否是 Word 自动生成的水平分割线。

        Word 中输入 --- 回车生成的分割线本质上是一个空段落，
        其段落属性中带有底部边框 (w:pBdr > w:bottom)。
        """
        pPr = paragraph_element.find(f'{{{NS["w"]}}}pPr')
        if pPr is None:
            return False
        pBdr = pPr.find(f'{{{NS["w"]}}}pBdr')
        if pBdr is None:
            return False
        bottom = pBdr.find(f'{{{NS["w"]}}}bottom')
        return bottom is not None

    def _detect_alignment(self, paragraph: Paragraph) -> Optional[str]:
        """嗅探段落对齐方式"""
        try:
            if paragraph.alignment is not None:
                return ALIGNMENT_MAP.get(paragraph.alignment, None)
        except Exception:
            pass
        return None
    
    def _process_table(self, table: Table, block_id: int) -> Optional[Block]:
        """处理表格元素，转换为 Markdown 格式并提取完整的合并单元格信息。"""
        try:
            rows = []
            cells_info = []
            for r_idx, row in enumerate(table.rows):
                cells_in_row = []
                c_idx = 0
                prev_tc = None
                for cell in row.cells:
                    tc = cell._tc
                    colspan = 1
                    try:
                        grid_span = tc.get_or_add_tcPr().find(f'{{{NS["w"]}}}gridSpan')
                        if grid_span is not None:
                            colspan = int(grid_span.get(f'{{{NS["w"]}}}val', '1'))
                    except Exception:
                        pass
                        
                    vmerge_val = 'none'
                    try:
                        vmerge = tc.get_or_add_tcPr().find(f'{{{NS["w"]}}}vMerge')
                        if vmerge is not None:
                            val = vmerge.get(f'{{{NS["w"]}}}val')
                            vmerge_val = val if val else 'continue'
                    except Exception:
                        pass
                    
                    if tc is prev_tc:
                        # 跳过合并单元格的重复引用文本叠加，但是为了对齐我们放入空字串
                        cells_in_row.append("")
                    else:
                        prev_tc = tc
                        text = cell.text.strip()
                        cells_in_row.append(text)
                        
                        # 仅对主单元格录入 cell_info
                        cells_info.append({
                            "row": r_idx,
                            "col": c_idx,
                            "text": text,
                            "row_span": 1,
                            "col_span": colspan,
                            "vmerge": vmerge_val
                        })
                    c_idx += 1
                rows.append(cells_in_row)
            
            if not rows:
                return None
            
            markdown_table = self._table_to_markdown(rows)
            return Block(
                id=block_id,
                type="table",
                text=markdown_table,
                metadata={"cells": cells_info},
                table_data={"rows": rows},
            )
        except Exception as e:
            logger.warning(f"处理表格时出错: {str(e)}")
            return None
    
    def _table_to_markdown(self, rows: List[List[str]]) -> str:
        """将表格数据转换为 Markdown 格式"""
        if not rows:
            return ""
        lines = []
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows[1:]:
            while len(row) < len(header):
                row.append("")
            lines.append("| " + " | ".join(row[:len(header)]) + " |")
        return "\n".join(lines)
    
    def _extract_images_from_paragraph(
        self, paragraph: Paragraph, start_block_id: int
    ) -> List[Block]:
        """从段落中提取图片"""
        image_blocks = []
        block_id = start_block_id
        
        try:
            for run in paragraph.runs:
                if hasattr(run, '_element'):
                    ns_w = f'{{{NS["w"]}}}'
                    for drawing in run._element.findall(f'.//{ns_w}drawing'):
                        try:
                            img_block = self._extract_image_from_drawing(drawing, block_id)
                            if img_block:
                                image_blocks.append(img_block)
                                block_id += 1
                        except Exception as e:
                            logger.warning(f"提取图片时出错: {str(e)}")
                    
                    for pict in run._element.findall(f'.//{ns_w}pict'):
                        try:
                            img_block = self._extract_image_from_pict(pict, block_id)
                            if img_block:
                                image_blocks.append(img_block)
                                block_id += 1
                        except Exception as e:
                            logger.warning(f"提取 VML 图片时出错: {str(e)}")
        except Exception as e:
            logger.warning(f"从段落提取图片时出错: {str(e)}")
        
        return image_blocks
    
    def _extract_image_from_drawing(self, drawing, block_id: int) -> Optional[Block]:
        """从 drawing 元素提取图片"""
        try:
            ns_a = f'{{{NS["a"]}}}'
            ns_r = f'{{{NS["r"]}}}'
            
            blip = drawing.find(f'.//{ns_a}blip')
            if blip is None:
                return None
            
            # Word 2016+ 在插入 SVG 时，会将真正的 SVG 存放在 a:extLst 中，
            # 而把自动生成的 PNG 后备图存放在 blip 的默认 r:embed 中。
            # 为了能够真正显示 SVG，我们必须优先尝试获取 SVG 的 embed_id：
            embed_id = None
            for ext in blip.findall(f'.//{ns_a}ext'):
                asvg_blip = ext.find('.//{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip')
                if asvg_blip is not None:
                    embed_id = asvg_blip.get(f'{ns_r}embed')
                    if embed_id:
                        break
            
            # 如果没有找到 SVG，或者非 SVG 格式，则回退读取默认图片资源
            if not embed_id:
                embed_id = blip.get(f'{ns_r}embed')
                
            if not embed_id:
                return None
            
            image_uuid = f"img_{self.image_counter}_{hashlib.md5(embed_id.encode()).hexdigest()[:8]}"
            self.image_counter += 1
            
            image_data_str = f"[IMAGE_PLACEHOLDER: {image_uuid}]"
            if self._doc_rels and embed_id in self._doc_rels:
                try:
                    rel = self._doc_rels[embed_id]
                    if hasattr(rel, 'target_part') and hasattr(rel.target_part, 'blob'):
                        blob = rel.target_part.blob
                        self.image_store[image_uuid] = blob
                        import base64
                        content_type = getattr(rel.target_part, 'content_type', 'image/png')
                        b64_data = base64.b64encode(blob).decode('utf-8')
                        image_data_str = f"data:{content_type};base64,{b64_data}"
                except Exception as e:
                    logger.debug(f"提取图片实体数据失败，使用占位符: {str(e)}")
            
            caption = ""
            ns_wp = f'{{{NS["wp"]}}}'
            desc_elem = drawing.find(f'.//{ns_wp}docPr')
            if desc_elem is None:
                desc_elem = drawing.find(f'.//{ns_a}docPr')
            if desc_elem is not None:
                caption = desc_elem.get('descr', '') or desc_elem.get('name', '')
            
            return Block(
                id=block_id,
                type="image",
                image_data=image_data_str,
                caption=caption,
                metadata={"embed_id": embed_id, "uuid": image_uuid},
            )
        except Exception as e:
            logger.warning(f"提取图片详情时出错: {str(e)}")
            return None
    
    def _extract_image_from_pict(self, pict, block_id: int) -> Optional[Block]:
        """从旧版 VML pict 元素提取图片"""
        try:
            ns_r = f'{{{NS["r"]}}}'
            ns_v = f'{{{NS["v"]}}}'
            
            imagedata = pict.find(f'.//{ns_v}imagedata')
            if imagedata is None:
                return None
            
            embed_id = imagedata.get(f'{ns_r}id')
            if not embed_id:
                return None
            
            image_uuid = f"img_{self.image_counter}_{hashlib.md5(embed_id.encode()).hexdigest()[:8]}"
            self.image_counter += 1
            
            image_data_str = f"[IMAGE_PLACEHOLDER: {image_uuid}]"
            if self._doc_rels and embed_id in self._doc_rels:
                try:
                    rel = self._doc_rels[embed_id]
                    if hasattr(rel, 'target_part') and hasattr(rel.target_part, 'blob'):
                        blob = rel.target_part.blob
                        self.image_store[image_uuid] = blob
                        import base64
                        content_type = getattr(rel.target_part, 'content_type', 'image/png')
                        b64_data = base64.b64encode(blob).decode('utf-8')
                        image_data_str = f"data:{content_type};base64,{b64_data}"
                except Exception as e:
                    logger.debug(f"提取 VML 图片实体数据失败: {str(e)}")
            
            title = imagedata.get('title', '')
            return Block(
                id=block_id,
                type="image",
                image_data=image_data_str,
                caption=title,
                metadata={"embed_id": embed_id, "uuid": image_uuid},
            )
        except Exception as e:
            logger.warning(f"提取 VML 图片详情时出错: {str(e)}")
            return None
    
    def get_image_data(self, uuid: str) -> Optional[bytes]:
        """获取图片的二进制数据"""
        return self.image_store.get(uuid)
    
    def _log_debug_info(self, blocks: List[Block]):
        """输出调试信息"""
        logger.info("=" * 80)
        logger.info(f"前 10 个 Block 的详细信息（共 {len(blocks)} 个）：")
        for block in blocks[:10]:
            style = block.metadata.get("style", "N/A") if block.metadata else "N/A"
            source = block.metadata.get("source", "") if block.metadata else ""
            text_preview = (block.text[:50] + "...") if block.text and len(block.text) > 50 else (block.text or "N/A")
            features = []
            if block.is_heading_style:
                features.append(f"H{block.heading_level}")
            if block.is_bold:
                features.append("Bold")
            if block.font_size:
                features.append(f"Size:{block.font_size:.0f}")
            if block.alignment:
                features.append(f"Align:{block.alignment}")
            if source:
                features.append(f"Src:{source}")
            feature_str = f" [{', '.join(features)}]" if features else ""
            logger.info(f"  Block {block.id}: type={block.type}, style={style}{feature_str}, text={text_preview}")
        logger.info("=" * 80)
