"""
Docx Provider v2 for Constellation — 混合 XML 引擎

混合引擎架构
- 主引擎：python-docx 处理常规段落、表格、图片（稳定可靠）
- 补充引擎：lxml 直接解析 OOXML，捕获 python-docx 忽略的节点：
  - OMML 公式 (w:oMath, w:oMathPara) → Formula Block（附 formula_fidelity 转换质量标记）
  - 浮动文本框 (w:txbxContent) → 递归提取为 Text Block
  - SmartArt / OLE 等未知节点 → 受控可见占位 Block + 附件引用 + warnings

这样既不丢数据，又不用重写整个解析器，兑现"100% 无损率"的承诺。

E05（DOCX 编号 / 脚注 / 嵌套内容 / 原生公式链）在上述基础上补齐：
- 编号体系：解析 word/numbering.xml 的 abstractNum/num、startOverride/restart、
  lvlText/numFmt，算出有效列表符号，并区分"章节编号"与"正文列表编号"。
- 脚注 / 尾注 / 书签 / 内部引用：建立关系并在正文保留引用位置（引用处↔注释处双向可还原）。
- 表格 cell 内嵌内容：图片 / 公式 / 嵌套表格 / 引用真正入块，收掉 B04 遗留的
  "嵌套表内层内容不入块"缺口——内层表内容纳入块流并保留 span 关系，不用重复文字替代结构。
- OMML：保留源表示，standalone/inline 公式块带 formula_fidelity（exact / lossy / source_only）。
- SmartArt/OLE/未知 XML：可见受控占位 + 附件引用 + warnings，不再只藏在会被预览隐藏的 HTML 注释里。
"""
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import io
import zipfile

try:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.table import CT_Tbl
    from docx.document import Document as DocumentType
    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.text.run import Run
except ImportError:
    raise ImportError("请安装 python-docx: pip install python-docx") from None

try:
    from lxml import etree
except ImportError:
    etree = None

from app.core.exceptions import ProviderError
from infrastructure.models import Block, StructuralAtom
from infrastructure.providers.base import InstrumentedProvider
from infrastructure.providers import embedded_spreadsheet as _emb
from infrastructure.providers.spreadsheet_provider import (
    XlsxProvider,
    source_range_report as _sheet_source_range,
)
from modules.parser.heading_candidates import infer_numbering_level

logger = logging.getLogger(__name__)

# OOXML 命名空间
NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'v': 'urn:schemas-microsoft-com:vml',
    'o': 'urn:schemas-microsoft-com:office:office',
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

# 等宽字体名（小写）→ code 判定；模块级常量避免每次调用重建 set
_MONO_FONTS = frozenset({
    'courier',
    'courier new',
    'consolas',
    'monaco',
    'lucida console',
    'dejavu sans mono',
    'menlo',
    'source code pro',
    'fira code',
    'jetbrains mono',
})

# run 级布尔格式属性，style chain 解析与直接值解析共用同一份清单
_RUN_BOOL_PROPS = ("bold", "italic", "underline", "strike", "superscript", "subscript")

# 修订视图：默认 "accepted"（保留 w:ins、丢弃 w:del），与 Word "接受所有修订" 一致。
# 之所以固定一种默认视图而非同时导出多种：下游 canonical 文本必须唯一，
# 否则同一段在纯文本 / 富文本 / atoms 间语义漂移（B01 验收要求"多个输出语义一致"）。
# 原始修订仍逐条记录在 block metadata.revisions 中，保证可审计、可回溯 markup 视图。
REVISION_VIEW = "accepted"

# 段内"包装节点"：其子树里的 w:r 文字属于本段可见内容，但 python-docx 的
# paragraph.runs 只返回顶层 w:r，会把这些文字整段丢掉（B01 根因）。统一 XML
# 顺序迭代器必须下钻这些容器，并支持任意嵌套（如 hyperlink 里再套 ins）。
_INLINE_WRAPPER_TAGS = frozenset({'hyperlink', 'ins', 'del', 'sdt', 'smartTag'})

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
    'sectPr',
})

# ---- E05: 脚注 / 尾注关系类型（OOXML 关系命名空间）----------------------------
FOOTNOTES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
ENDNOTES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
NUMBERING_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"

# ---- E05: 编号格式（numFmt）→ 有效列表符号的求值 ----------------------------
# 只覆盖最常见的 numFmt；未知格式退回十进制并在 metadata 标记 numbering_fidelity。
# 罗马 / 字母是"章节编号 vs 列表编号"判定的重要信号，故单列而非全部拍成 decimal。
_ROMAN_NUMERALS = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)

# ---- E05: 未知元素分类（SmartArt / OLE / 图表 / 其它）------------------------
# 用可读类别驱动"可见受控占位"文案与 warning code，而不是把标签名直接吐给用户。
_UNKNOWN_ELEMENT_KINDS = (
    # (判定用的小写子串, 类别, 中文占位标签)
    ("diagramdata", "smartart", "SmartArt 图形"),
    ("2006/diagram", "smartart", "SmartArt 图形"),
    ("dgm", "smartart", "SmartArt 图形"),
    ("smartart", "smartart", "SmartArt 图形"),
    ("oleobject", "ole", "OLE 嵌入对象"),
    ("}object", "ole", "OLE 嵌入对象"),
    ("chart", "chart", "图表"),
    ("control", "control", "ActiveX 控件"),
)

# E05 对外 warning code 逐字清单（交付报告用）：
#   docx_element_skipped / docx_table_failed / docx_image_failed  —— 既有（R05）
#   docx_unknown_element   —— 未知 XML（SmartArt/OLE/…）以受控占位保留，正文可见
#   docx_formula_lossy     —— OMML 转换有损（结构保留但 LaTeX 近似）
#   docx_formula_source_only —— OMML 无法转 LaTeX，仅保留源片段
#   docx_footnote_unresolved —— 正文引用了脚注/尾注但注释体缺失

STRICT_OOXML_REPLACEMENTS = (
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships/",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    ),
    (
        b"http://purl.oclc.org/ooxml/wordprocessingml/main",
        b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    ),
    (
        b"http://purl.oclc.org/ooxml/drawingml/main",
        b"http://schemas.openxmlformats.org/drawingml/2006/main",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/math",
        b"http://schemas.openxmlformats.org/officeDocument/2006/math",
    ),
)


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
    code: bool = False
    trusted_markup: bool = False

    @property
    def style_key(self) -> Tuple[bool, ...]:
        """Hashable style fingerprint for homogeneous-run merging."""
        return (self.bold, self.italic, self.underline,
                self.strike, self.superscript, self.subscript, self.code,
                self.trusted_markup)

    @property
    def has_formatting(self) -> bool:
        return any(self.style_key)


@dataclass(slots=True)
class _ResolvedRun:
    """Effective per-run formatting resolved exactly once per run.

    Resolving direct + character-style + paragraph-style values used to be
    the hottest path in DOCX extraction (every property access re-walked
    style chains, and python-docx re-scans the styles part on each
    ``.style`` lookup).  This struct lets the rich-text renderer, the
    block-level detectors and the atom builder share one resolution.
    """

    index: int
    text: str
    bold: bool
    italic: bool
    underline: bool
    strike: bool
    superscript: bool
    subscript: bool
    font_size: Optional[float]
    font_name: Optional[str]
    is_code: bool
    style_id: str


class DocxProvider(InstrumentedProvider):
    """
    Docx 文档提供器 v2（混合 XML 引擎）
    
    职责：
    1. 按照文档物理流顺序遍历 .docx 内容
    2. python-docx 处理段落、表格、图片（主引擎）
    3. lxml 补充捕获 OMML 公式、文本框、未知节点（补充引擎）
    4. 嗅探物理特征（Bold/Size/Center/Heading），注入 Block 元数据
    5. 保持零损耗的内容提取
    """
    
    def __init__(self, embedded_budget_factory=_emb.EmbeddedBudget):
        # 内嵌电子表格的**预算上限**从这里注入（默认用模块默认值）。传的是工厂而不是实例：
        # 预算带计数器、是每次提取一份的账本，共用一个实例会让上一份文档的用量泄漏到下一份。
        self._embedded_budget_factory = embedded_budget_factory
        self.image_counter = 0
        self.image_store: Dict[str, bytes] = {}
        self._doc_rels = None
        self._doc_part = None
        self._chain_cache: Dict[tuple, list] = {}
        self._chain_props_cache: Dict[tuple, dict] = {}
        # E05 编号：abstractNum 定义、num→abstractNum 映射、num 级别 startOverride，
        # 以及一次提取内每个 numId+ilvl 的运行计数器（用于算有效列表符号）。
        self._numbering_defs: Dict[str, dict] = {}
        self._num_to_abstract: Dict[str, dict] = {}
        self._numbering_loaded = False
        self._list_counters: Dict[tuple, int] = {}
        # E05 脚注 / 尾注：id→注释纯文本；正文引用处按出现顺序编号，双向可还原。
        self._footnotes: Dict[str, str] = {}
        self._endnotes: Dict[str, str] = {}
        self._notes_loaded = False
        self._note_ref_counter = 0

    def _reset_state(self) -> None:
        """Reset per-extraction mutable state to avoid cross-document leaks."""
        self.image_counter = 0
        self.image_store = {}
        self._doc_rels = None
        self._doc_part = None
        self._chain_cache = {}
        self._chain_props_cache = {}
        self._numbering_defs = {}
        self._num_to_abstract = {}
        self._numbering_loaded = False
        self._list_counters = {}
        self._footnotes = {}
        self._endnotes = {}
        self._notes_loaded = False
        self._note_ref_counter = 0

    @staticmethod
    def _ensure_supported_path(file_path: str) -> Path:
        path = Path(file_path)
        if path.suffix.lower() != ".docx":
            raise ProviderError("仅支持 .docx 文件；旧版 .doc 暂不支持")
        return path

    @staticmethod
    def _normalize_strict_ooxml(file_bytes: bytes) -> bytes | None:
        source = io.BytesIO(file_bytes)
        output = io.BytesIO()
        replacements_applied = 0

        try:
            with zipfile.ZipFile(source, "r") as input_zip, zipfile.ZipFile(output, "w") as output_zip:
                for zip_info in input_zip.infolist():
                    data = input_zip.read(zip_info.filename)

                    if zip_info.filename.endswith((".xml", ".rels")):
                        updated = data
                        for old, new in STRICT_OOXML_REPLACEMENTS:
                            count = updated.count(old)
                            if count:
                                updated = updated.replace(old, new)
                                replacements_applied += count
                        data = updated

                    output_zip.writestr(zip_info, data)
        except zipfile.BadZipFile:
            return None

        if replacements_applied == 0:
            return None

        return output.getvalue()

    @staticmethod
    def _load_document_from_bytes(file_bytes: bytes):
        if not file_bytes:
            raise ProviderError("上传的 .docx 文件为空")

        try:
            return Document(io.BytesIO(file_bytes))
        except zipfile.BadZipFile as exc:
            raise ProviderError("上传文件不是有效的 .docx 压缩包") from exc
        except (KeyError, ValueError) as exc:
            normalized_bytes = DocxProvider._normalize_strict_ooxml(file_bytes)
            if normalized_bytes is not None:
                logger.info("检测到 Strict Open XML，已转换为 Transitional OOXML 后重试解析")
                try:
                    return Document(io.BytesIO(normalized_bytes))
                except Exception as retry_exc:
                    raise ProviderError(
                        "该 .docx 使用 Strict Open XML 或非标准 OOXML 打包格式，自动兼容转换后仍无法解析；"
                        "请在 Word/WPS 中另存为常规 .docx 后重试"
                    ) from retry_exc

            raise ProviderError(f"无法解析 .docx 文件：{exc}") from exc
    
    def extract(self, file_path: str) -> List[Block]:
        """从文件路径提取 .docx 内容为 Block 列表"""
        self._reset_state()
        path = self._ensure_supported_path(file_path)
        logger.info("开始解析 Docx 文件: %s", path)
        try:
            try:
                doc = Document(str(path))
            except (zipfile.BadZipFile, KeyError, ValueError):
                doc = self._load_document_from_bytes(path.read_bytes())
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
            doc = self._load_document_from_bytes(file_bytes)
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
        self._doc_part = doc.part
        self._chain_cache = {}
        self._chain_props_cache = {}
        # E05：编号定义与脚注/尾注在遍历正文前一次性载入（供每段引用求值）。
        self._load_numbering(doc)
        self._load_notes(doc)
        # 内嵌电子表格：预算与读取缓存都是**文档级**的。预算必须跨对象共享，否则每个附件
        # 各自合法、加起来把内存吃光；缓存按内容哈希只去重「读取」，正文出现位置绝不去重。
        self._embedded_budget = self._embedded_budget_factory()
        self._embedded_cache = _emb.EmbeddedReadCache()
        self._xlsx_provider = XlsxProvider()
        self._list_counters = {}
        self._note_ref_counter = 0
        
        for element_index, element in enumerate(doc.element.body):
            try:
                if isinstance(element, CT_P):
                    # 主引擎：处理段落
                    paragraph = Paragraph(element, doc)

                    # 段内有嵌入对象时走事件顺序分段：文字/对象各自成块且顺序与原文一致。
                    # 这条分支必须在其它特判之前——它是唯一知道「对象在第几段文字之后」的
                    # 路径，落到下面的旧顺序里对象就会被追加到段尾。
                    if self._paragraph_has_embedded_object(element):
                        block_id = self._emit_paragraph_with_objects(
                            paragraph,
                            element_index,
                            block_id,
                            blocks,
                            part=doc.part,
                        )
                        continue
                    
                    # 检查段落内是否包含 OMML 公式
                    formula_blocks = self._extract_omml_from_paragraph(element, block_id)

                    # paragraph.text 是一次 xpath 求值 + 逐 run 拼接，整段流程只算一次。
                    # 但它漏掉 w:ins 等包装节点文字，故 emptiness 判定改用统一遍历得到的
                    # wrapped_text（纯超链接 / 纯插入段落靠它才不被判空跳过，B01 验收）。
                    para_text = paragraph.text
                    wrapped_text = self._paragraph_wrapped_text(element)
                    effective_text = wrapped_text if wrapped_text.strip() else para_text
                    has_plain_text = bool(effective_text.strip())
                    has_inline_images = self._paragraph_has_inline_image(element)
                    has_text = has_plain_text or self._paragraph_has_inline_omml(element) or (
                        has_plain_text and has_inline_images
                    )
                    image_blocks = []
                    if not (has_plain_text and has_inline_images):
                        image_blocks = self._extract_images_from_paragraph(
                            paragraph, block_id + (1 if has_text else 0) + len(formula_blocks)
                        )
                    
                    # 检查段落内是否包含文本框
                    textbox_blocks = self._extract_textbox_from_element(
                        element, block_id + (1 if has_text else 0) + len(formula_blocks) + len(image_blocks)
                    )
                    
                    if has_text:
                        text_block = self._process_paragraph(
                            paragraph, block_id, source_index=element_index,
                            raw_text=effective_text,
                        )
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
                    # 主引擎：处理表格（可能返回父表 + 嵌套表多个块）
                    table = Table(element, doc)
                    table_blocks = self._process_table(table, block_id)
                    for tbl_block in table_blocks:
                        tbl_block.id = block_id
                        blocks.append(tbl_block)
                        block_id += 1
                        
                elif etree is not None and (element.tag == f'{{{NS["m"]}}}oMathPara' or element.tag == f'{{{NS["m"]}}}oMath'):
                    formula_block = self._make_formula_block(element, block_id, "omml_root")
                    if formula_block is not None:
                        blocks.append(formula_block)
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
                # R05：跳过是对的（一个坏元素不该让整篇失败），但只写日志就等于
                # 让调用方把残缺当完整。记一条，交付层据此把结果标成 partial。
                self._note_skip(
                    "docx_element_skipped",
                    "文档中有一个元素无法解析，已跳过；该处内容缺失。",
                    source_span={"block_id": block_id},
                )
                continue
        
        return self._post_process_blocks(blocks)
    
    def _post_process_blocks(self, blocks: List[Block]) -> List[Block]:
        """合并连续 code block —— 分组后一次 join（P02）。

        旧实现"每加入一段就拼接累计文本 + 重扫全部 atoms 重算 offset"，是
        O(段数²) 的累计拼接（P02 点名的反模式，与 PDF 合行同源）。改为：先把
        连续 code 段收进一个分组缓冲（只 append 引用，不拼字符串），分组结束时
        把片段一次 ``"\\n".join``、atoms 一次性拼接、offset 只重算一次。逐字符
        等价用 test_docx_numbering_and_notes.py 里的参考实现对照断言。
        """
        if not blocks:
            return blocks

        merged: List[Block] = []
        group: List[Block] = []  # 当前正在累积的连续 code 段

        def _flush_group() -> None:
            if not group:
                return
            head = group[0]
            if len(group) == 1:
                merged.append(head)
                group.clear()
                return
            # 分组后一次 join：先收集所有片段文本与 atoms，再统一落位。
            texts = [(b.text or "") for b in group]
            combined_text = "\n".join(texts)
            combined_atoms: list[dict] = []
            for b in group:
                combined_atoms.extend((b.metadata or {}).get("atoms", []))
            head_meta = head.metadata or {}
            head_meta["atoms"] = combined_atoms
            head.text = combined_text
            self._recalculate_atom_offsets(combined_text, combined_atoms)
            head.metadata = head_meta
            self._apply_atom_weighted_format(head)
            merged.append(head)
            group.clear()

        for block in blocks:
            if block.type == "code":
                if group:
                    group.append(block)
                else:
                    group = [block]
            else:
                _flush_group()
                merged.append(block)
        _flush_group()

        # 建 旧 id → 新 id 映射：code 合并会让其后的块 id 前移，若不同步更新，
        # 表格跨度关系里存的绝对 id（parent_table.block_id / nested_table_block_ids）
        # 会指向错误的块。先记旧 id，再按新序重编，最后一次性重映射引用。
        old_ids = [block.id for block in merged]
        id_remap = {old: new for new, old in enumerate(old_ids)}

        # Reassign IDs and update only the lightweight atom dictionaries.
        for i, block in enumerate(merged):
            block.id = i
            for atom in (block.metadata or {}).get("atoms", []):
                atom["block_id"] = i

        # 重映射表格父子跨度引用（仅表格块携带，且只有存在嵌套表时才有意义）。
        for block in merged:
            meta = block.metadata or {}
            parent = meta.get("parent_table")
            if isinstance(parent, dict) and parent.get("block_id") in id_remap:
                parent["block_id"] = id_remap[parent["block_id"]]
            for cell in meta.get("cells", []) or []:
                nested_ids = cell.get("nested_table_block_ids")
                if nested_ids:
                    cell["nested_table_block_ids"] = [
                        id_remap.get(nid, nid) for nid in nested_ids
                    ]

        return merged

    @staticmethod
    def _recalculate_atom_offsets(block_text: str, atoms: list[dict]) -> None:
        cursor = 0
        for atom in atoms:
            rendered = str(atom.get("provenance", {}).get("canonical_text", atom.get("text", "")))
            start = block_text.find(rendered, cursor) if rendered else cursor
            if start < 0 and rendered.strip():
                trimmed = rendered.strip()
                trimmed_start = block_text.find(trimmed, cursor)
                if trimmed_start >= 0:
                    start = trimmed_start
                    rendered = trimmed
                    atom.setdefault("provenance", {})["canonical_text"] = trimmed
                    atom["provenance"]["join_normalization"] = "paragraph-edge-whitespace-trimmed"
            if start < 0:
                start = min(cursor, len(block_text))
                end = min(len(block_text), start + len(rendered))
                atom.setdefault("provenance", {})["offset_alignment"] = "approximate"
            else:
                end = start + len(rendered)
            atom["char_start"] = start
            atom["char_end"] = end
            cursor = end

    @staticmethod
    def _visible_char_weight(text: str) -> int:
        return sum(1 for char in (text or "") if not char.isspace())

    @classmethod
    def _apply_atom_weighted_format(cls, block: Block) -> None:
        from collections import Counter

        atoms = (block.metadata or {}).get("atoms", [])
        total_weight = 0
        bold_weight = 0
        sizes: Counter[float] = Counter()
        for atom in atoms:
            weight = cls._visible_char_weight(str(atom.get("text", "")))
            if not weight:
                continue
            total_weight += weight
            if atom.get("is_bold") is True:
                bold_weight += weight
            if atom.get("font_size"):
                sizes[round(float(atom["font_size"]), 1)] += weight
        if total_weight:
            block.is_bold = bold_weight >= total_weight * 0.8
        if sizes:
            block.font_size = sizes.most_common(1)[0][0]
    
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
            formula_block = self._make_formula_block(math_elem, bid, "omml_para")
            if formula_block is not None:
                formula_blocks.append(formula_block)
                bid += 1
        
        return formula_blocks

    def _paragraph_has_inline_omml(self, element) -> bool:
        if etree is None:
            return False

        for math_elem in element.findall(f'.//{{{NS["m"]}}}oMath'):
            parent = math_elem.getparent()
            if parent is not None and parent.tag == f'{{{NS["m"]}}}oMathPara':
                continue
            return True
        return False

    def _paragraph_has_inline_image(self, element) -> bool:
        return element.find(f'.//{{{NS["w"]}}}drawing') is not None or element.find(f'.//{{{NS["w"]}}}pict') is not None
    
    def _omml_to_text(self, element) -> str:
        """把 OMML (Office MathML) 元素降级为 LaTeX 风格的可读文本。

        覆盖常见结构，避免复杂公式被"通用遍历"拍平成无意义字符串
        （如 x² 退化成 x2、√x 退化成 x、Σ 上下限丢失）：

        - ``m:f``    分数        → ``\\frac{num}{den}``
        - ``m:sSup`` / ``m:sSub`` / ``m:sSubSup`` 上下标 → ``base^{..}`` / ``base_{..}``
        - ``m:rad``  根式        → ``\\sqrt[deg]{e}`` / ``\\sqrt{e}``
        - ``m:nary`` 求和 / 积分 → ``op_{sub}^{sup} e``（运算符取 m:chr，默认 ∫）
        - ``m:d``    定界符      → ``(e)``（括号取 m:begChr / m:endChr）
        - ``m:func`` 函数        → ``fName(e)``
        - ``m:limLow`` / ``m:limUpp`` 上下极限 → ``base_{lim}`` / ``base^{lim}``
        - ``m:r``    文本 run    → 拼接其中所有 m:t
        - 其它容器（oMath / e / num / sub ...）→ 递归拼接子节点

        遇到任何异常都安全降级为通用遍历，绝不让单个公式拖垮整篇解析。
        """
        if element is None:
            return ""

        m = NS["m"]
        raw_tag = element.tag
        tag = raw_tag.split('}', 1)[-1] if isinstance(raw_tag, str) else ""

        def _text_of(name: str) -> str:
            node = element.find(f'{{{m}}}{name}')
            return self._omml_to_text(node) if node is not None else ""

        def _attr_val(path: str, default: str) -> str:
            node = element.find(path)
            if node is None:
                return default
            val = node.get(f'{{{m}}}val')
            return val if val else default

        try:
            if tag == "f":
                return f"\\frac{{{_text_of('num')}}}{{{_text_of('den')}}}"
            if tag == "r":
                return "".join(t.text or "" for t in element.findall(f'{{{m}}}t'))
            if tag == "sSup":
                return f"{_text_of('e')}^{{{_text_of('sup')}}}"
            if tag == "sSub":
                return f"{_text_of('e')}_{{{_text_of('sub')}}}"
            if tag == "sSubSup":
                return f"{_text_of('e')}_{{{_text_of('sub')}}}^{{{_text_of('sup')}}}"
            if tag == "rad":
                deg, body = _text_of('deg'), _text_of('e')
                return f"\\sqrt[{deg}]{{{body}}}" if deg else f"\\sqrt{{{body}}}"
            if tag == "nary":
                op = _attr_val(f'{{{m}}}naryPr/{{{m}}}chr', "∫") or "∫"
                sub, sup, body = _text_of('sub'), _text_of('sup'), _text_of('e')
                out = op
                if sub:
                    out += f"_{{{sub}}}"
                if sup:
                    out += f"^{{{sup}}}"
                if body:
                    out += f" {body}"
                return out
            if tag == "d":
                beg = _attr_val(f'{{{m}}}dPr/{{{m}}}begChr', "(")
                end = _attr_val(f'{{{m}}}dPr/{{{m}}}endChr', ")")
                inner = "".join(self._omml_to_text(e) for e in element.findall(f'{{{m}}}e'))
                return f"{beg}{inner}{end}"
            if tag == "func":
                return f"{_text_of('fName')}({_text_of('e')})"
            if tag == "limLow":
                return f"{_text_of('e')}_{{{_text_of('lim')}}}"
            if tag == "limUpp":
                return f"{_text_of('e')}^{{{_text_of('lim')}}}"
        except Exception as exc:  # noqa: BLE001 - 公式结构异常时降级为通用遍历
            logger.debug("[OMML] 结构化解析失败，降级为通用遍历: %s", exc)

        # 通用遍历（容器节点 / 未覆盖结构 / 异常降级）
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
    
    def _classify_unknown_element(self, element) -> tuple[str, str]:
        """把未知节点归到 (类别, 中文标签)。

        判定依据是子孙标签名而非仅顶层标签：SmartArt 的容器常是 w:drawing，
        真正的判别特征 (dgm:*, a:graphicData uri) 在深处；OLE 同理藏在 w:object 里。
        """
        kinds_seen = []
        for child in element.iter():
            raw = child.tag
            if not isinstance(raw, str):
                continue
            # 同时看命名空间 URI 与 local name：SmartArt 的判别特征在
            # ``...drawingml/2006/diagram`` 这个 NS 上（元素 local 名可能只是 relIds），
            # OLE 在 ``...vml/office`` / w:object 上。只看 local 名会漏判。
            full = raw.lower()
            local = raw.split('}', 1)[-1].lower()
            uri = str(child.get("uri", "")).lower()
            haystack = f"{full} {local} {uri}"
            for needle, kind, label in _UNKNOWN_ELEMENT_KINDS:
                if needle in haystack:
                    kinds_seen.append((kind, label))
                    break
            if kinds_seen:
                break
        if kinds_seen:
            return kinds_seen[0]
        return "unknown", "未识别元素"

    def _capture_unknown_element(self, element, block_id: int) -> List[Block]:
        """捕获 python-docx 无法识别的 XML 节点，落成**可见受控占位** + 附件引用。

        方案 E05(5) 明确点名的反模式：把未知元素只塞进会被预览隐藏的 HTML 注释。
        这里改为：
        - 生成正文可见的受控占位（``【SmartArt 图形】`` 之类，见 _UNKNOWN_ELEMENT_KINDS），
          即便预览层只渲染纯文本也不会"消失"；
        - 若节点内有可读文字，附在占位后作为可核对内容；
        - metadata 记 kind / tag / 是否有内部文本，并同时进 warnings（docx_unknown_element），
          让交付层把结果标 partial 并说清此处是被降级保留而非完整还原。
        """
        tag = element.tag if hasattr(element, 'tag') else str(type(element))
        tag_short = tag.split('}')[-1] if '}' in tag else tag

        inner_text = ""
        try:
            if etree is not None:
                t_texts = [t.text for t in element.iter(f'{{{NS["w"]}}}t') if t.text]
                inner_text = "".join(t_texts).strip()
        except Exception:
            pass

        kind, label = self._classify_unknown_element(element)
        # 可见受控占位：方括号包裹的中文标签，前缀 ⚠ 提示这是降级保留。
        if inner_text:
            placeholder = f"【{label}｜内容降级保留】{inner_text}"
        else:
            placeholder = f"【{label}｜此处元素未能完整解析】"

        logger.debug(f"[混合引擎] 捕获未知节点: {tag_short} (kind={kind}), 内容长度: {len(inner_text)}")

        # 进 warnings：调用方据此标 partial（这是 R05 的 warnings 生产端之一）。
        self._note_skip(
            "docx_unknown_element",
            f"文档中有一个未识别元素（{label}），已用可见占位保留位置，未完整还原其结构。",
            source_span={"block_id": block_id, "tag": tag_short},
        )

        return [Block(
            id=block_id,
            type="text",
            text=placeholder,
            metadata={
                "source": "unknown_element",
                "tag": tag_short,
                "unknown_kind": kind,
                "placeholder": True,
                "has_inner_text": bool(inner_text),
            },
        )]
    
    # ================================================================
    # E05 (1): 编号体系 — numbering.xml 解析与有效列表符号求值
    #
    # python-docx 只给 numPr 的 numId/ilvl（见 _paragraph_list_level），
    # 不解析 numbering.xml，于是拿不到 numFmt（decimal/roman/bullet…）、
    # lvlText（"%1.%2"）、start / startOverride（编号重启）。没有这些就
    # 无法：① 算出有效列表符号（"1.2.3"、"a)"、"•"）；② 区分"章节编号"
    # （多级 decimal，进正文层级）与"正文列表编号"（bullet / 单级项目符号）。
    # ================================================================

    def _numbering_part(self, doc):
        """取 numbering 部件的根 XML 元素（无则 None）。"""
        try:
            rels = doc.part.rels
            for rel in rels.values():
                if rel.reltype == NUMBERING_REL_TYPE:
                    part = rel.target_part
                    if etree is not None and hasattr(part, "blob"):
                        return etree.fromstring(part.blob)
                    return getattr(part, "element", None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[E05] 载入 numbering 部件失败: %s", exc)
        return None

    def _load_numbering(self, doc) -> None:
        """解析 word/numbering.xml：abstractNum 各级定义 + num→abstractNum 映射。

        存两张表：
        - ``_numbering_defs[abstractNumId] = {ilvl: {numFmt, lvlText, start}}``
        - ``_num_to_abstract[numId] = {"abstract": abstractNumId,
            "overrides": {ilvl: start}}``  （startOverride 落这里）
        """
        self._numbering_loaded = True
        if etree is None:
            return
        root = self._numbering_part(doc)
        if root is None:
            return
        w = NS["w"]
        try:
            for anum in root.findall(f'{{{w}}}abstractNum'):
                aid = anum.get(f'{{{w}}}abstractNumId')
                if aid is None:
                    continue
                levels: Dict[str, dict] = {}
                for lvl in anum.findall(f'{{{w}}}lvl'):
                    ilvl = lvl.get(f'{{{w}}}ilvl') or "0"
                    levels[ilvl] = {
                        "numFmt": self._read_w_val(lvl, "numFmt") or "decimal",
                        "lvlText": self._read_w_val(lvl, "lvlText") or "",
                        "start": self._read_w_val(lvl, "start") or "1",
                    }
                self._numbering_defs[aid] = levels

            for num in root.findall(f'{{{w}}}num'):
                nid = num.get(f'{{{w}}}numId')
                if nid is None:
                    continue
                abstract_ref = num.find(f'{{{w}}}abstractNumId')
                abstract_id = abstract_ref.get(f'{{{w}}}val') if abstract_ref is not None else None
                overrides: Dict[str, str] = {}
                for lvl_ovr in num.findall(f'{{{w}}}lvlOverride'):
                    ovr_ilvl = lvl_ovr.get(f'{{{w}}}ilvl') or "0"
                    start_ovr = lvl_ovr.find(f'{{{w}}}startOverride')
                    if start_ovr is not None:
                        val = start_ovr.get(f'{{{w}}}val')
                        if val is not None:
                            overrides[ovr_ilvl] = val
                self._num_to_abstract[nid] = {
                    "abstract": abstract_id,
                    "overrides": overrides,
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[E05] 解析 numbering.xml 结构失败: %s", exc)

    def _paragraph_num_id(self, paragraph: Paragraph) -> Optional[str]:
        """取段落有效 numId（直接 pPr 优先，其次 basedOn 链）。numId=0 表示禁用。"""
        sources = []
        try:
            sources.append(paragraph._element.pPr)
        except Exception:
            pass
        for style in self._paragraph_chain(paragraph):
            try:
                sources.append(style.element.pPr)
            except Exception:
                continue
        for p_pr in sources:
            if p_pr is None:
                continue
            num_pr = p_pr.find(f'{{{NS["w"]}}}numPr')
            if num_pr is None:
                continue
            num_id = self._read_w_val(num_pr, "numId")
            if num_id is None:
                continue
            if num_id == "0":
                return None
            return num_id
        return None

    @staticmethod
    def _format_ordinal(value: int, num_fmt: str) -> str:
        """把序号值按 numFmt 渲染成符号。未知格式退回十进制。"""
        fmt = (num_fmt or "decimal").lower()
        if fmt in ("bullet",):
            return "•"
        if fmt in ("decimal", "decimalzero", "decimalfullwidth", "decimalenclosedcircle"):
            return f"{value:02d}" if fmt == "decimalzero" else str(value)
        if fmt in ("lowerletter", "loweralpha"):
            return DocxProvider._to_alpha(value).lower()
        if fmt in ("upperletter", "upperalpha"):
            return DocxProvider._to_alpha(value).upper()
        if fmt in ("lowerroman",):
            return DocxProvider._to_roman(value).lower()
        if fmt in ("upperroman",):
            return DocxProvider._to_roman(value).upper()
        if fmt in ("chinesecounting", "chinesecountingthousand", "ideographdigital", "japanesecounting"):
            return DocxProvider._to_cjk(value)
        if fmt in ("none",):
            return ""
        return str(value)

    @staticmethod
    def _to_alpha(value: int) -> str:
        if value < 1:
            return str(value)
        result = ""
        while value > 0:
            value, rem = divmod(value - 1, 26)
            result = chr(ord("a") + rem) + result
        return result

    @staticmethod
    def _to_roman(value: int) -> str:
        if value < 1 or value > 3999:
            return str(value)
        out = []
        remaining = value
        for amount, symbol in _ROMAN_NUMERALS:
            while remaining >= amount:
                out.append(symbol)
                remaining -= amount
        return "".join(out)

    @staticmethod
    def _to_cjk(value: int) -> str:
        digits = "零一二三四五六七八九"
        if 0 <= value <= 9:
            return digits[value]
        if 10 <= value <= 99:
            tens, ones = divmod(value, 10)
            prefix = "" if tens == 1 else digits[tens]
            return f"{prefix}十{digits[ones] if ones else ''}"
        return str(value)

    def _resolve_numbering(
        self, paragraph: Paragraph, list_level: Optional[int]
    ) -> Optional[dict]:
        """算出该段有效列表符号，并区分章节编号 / 正文列表编号。

        返回 dict（进 Block.metadata['numbering']）：
        - ``num_id`` / ``ilvl`` / ``num_fmt`` / ``lvl_text``
        - ``symbol``     有效符号（如 "1.2"、"a)"、"•"）
        - ``restarted``  该级本段是否发生编号重启（start!=1 或 startOverride 生效）
        - ``category``   "chapter"（多级 decimal/roman/letter，视为章节编号）
                          / "list"（bullet 或单级项目符号，视为正文列表编号）
        - ``numbering_fidelity``  "exact"（命中定义）/ "heuristic"（无定义、退化推断）
        """
        num_id = self._paragraph_num_id(paragraph)
        if num_id is None or list_level is None:
            return None
        ilvl = str(list_level)

        mapping = self._num_to_abstract.get(num_id)
        levels = None
        overrides: Dict[str, str] = {}
        if mapping is not None:
            overrides = mapping.get("overrides", {})
            levels = self._numbering_defs.get(str(mapping.get("abstract")))

        if levels is None:
            # 无 numbering.xml 定义：退化——只知道有 numId/ilvl，符号用 decimal 推断。
            counter_key = (num_id, ilvl)
            self._list_counters[counter_key] = self._list_counters.get(counter_key, 0) + 1
            value = self._list_counters[counter_key]
            return {
                "num_id": num_id,
                "ilvl": list_level,
                "num_fmt": "decimal",
                "lvl_text": "",
                "symbol": str(value),
                "restarted": value == 1,
                "category": "list",
                "numbering_fidelity": "heuristic",
            }

        level_def = levels.get(ilvl, {"numFmt": "decimal", "lvlText": "", "start": "1"})
        num_fmt = level_def["numFmt"]
        lvl_text = level_def["lvlText"]

        # 起始值：startOverride > 定义 start > 1。
        try:
            start_value = int(overrides.get(ilvl, level_def.get("start", "1")))
        except (TypeError, ValueError):
            start_value = 1

        counter_key = (num_id, ilvl)
        if counter_key not in self._list_counters:
            current = start_value
        else:
            current = self._list_counters[counter_key] + 1
        self._list_counters[counter_key] = current
        # 进入更深一级或新起一段时，重置比它更深的级别计数（多级列表语义）。
        self._reset_deeper_levels(num_id, list_level)

        restarted = (current == start_value) and (
            start_value != 1 or ilvl in overrides
        )

        # 组合各级当前值填入 lvlText 的 %1..%9 占位（多级列表符号）。
        symbol = self._render_lvl_text(num_id, levels, list_level, num_fmt, current, lvl_text)

        category = self._numbering_category(levels, num_fmt)

        return {
            "num_id": num_id,
            "ilvl": list_level,
            "num_fmt": num_fmt,
            "lvl_text": lvl_text,
            "symbol": symbol,
            "restarted": bool(restarted),
            "category": category,
            "numbering_fidelity": "exact",
        }

    def _reset_deeper_levels(self, num_id: str, list_level: int) -> None:
        for (nid, ilvl), _ in list(self._list_counters.items()):
            if nid == num_id and int(ilvl) > list_level:
                del self._list_counters[(nid, ilvl)]

    def _render_lvl_text(
        self, num_id: str, levels: dict, list_level: int,
        num_fmt: str, current: int, lvl_text: str,
    ) -> str:
        """按 lvlText 模板（如 "%1.%2"）组合各级当前值，得到多级列表符号。"""
        if not lvl_text:
            rendered = self._format_ordinal(current, num_fmt)
            if num_fmt.lower() == "bullet":
                return rendered
            return f"{rendered}."

        def _sub(match) -> str:
            idx = int(match.group(1)) - 1  # %1 → ilvl 0
            level_def = levels.get(str(idx))
            if level_def is None:
                return ""
            if idx == list_level:
                val = current
            else:
                val = self._list_counters.get((num_id, str(idx)))
                if val is None:
                    try:
                        val = int(level_def.get("start", "1"))
                    except (TypeError, ValueError):
                        val = 1
            return self._format_ordinal(val, level_def.get("numFmt", "decimal"))

        return re.sub(r"%(\d)", _sub, lvl_text)

    @staticmethod
    def _numbering_category(levels: dict, num_fmt: str) -> str:
        """区分章节编号 vs 正文列表编号。

        - bullet（项目符号）永远是正文列表编号。
        - 定义里存在多级（>1 级）且非 bullet → 视为章节编号（如 1 / 1.1 / 1.1.1）。
        - 其余单级数字/字母/罗马 → 正文列表编号。
        判定只用于"这条编号是否参与正文层级"的下游提示，是启发式，故 fidelity 已单列。
        """
        if (num_fmt or "").lower() == "bullet":
            return "list"
        non_bullet_levels = [
            lv for lv in levels.values() if (lv.get("numFmt", "") or "").lower() != "bullet"
        ]
        if len(non_bullet_levels) > 1:
            return "chapter"
        return "list"

    # ================================================================
    # E05 (2): 脚注 / 尾注 / 书签 / 内部引用
    # ================================================================

    def _notes_part_root(self, doc, rel_type: str):
        try:
            for rel in doc.part.rels.values():
                if rel.reltype == rel_type:
                    part = rel.target_part
                    if etree is not None and hasattr(part, "blob"):
                        return etree.fromstring(part.blob)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[E05] 载入注释部件失败 (%s): %s", rel_type, exc)
        return None

    def _load_notes(self, doc) -> None:
        """载入脚注 / 尾注：id → 注释纯文本（用于引用处双向可还原）。"""
        self._notes_loaded = True
        if etree is None:
            return
        w = NS["w"]
        for rel_type, target, elem_tag in (
            (FOOTNOTES_REL_TYPE, self._footnotes, "footnote"),
            (ENDNOTES_REL_TYPE, self._endnotes, "endnote"),
        ):
            root = self._notes_part_root(doc, rel_type)
            if root is None:
                continue
            for note in root.findall(f'{{{w}}}{elem_tag}'):
                note_id = note.get(f'{{{w}}}id')
                note_type = note.get(f'{{{w}}}type')
                # separator / continuationSeparator 是分隔线伪注释，跳过。
                if note_id is None or note_type in ("separator", "continuationSeparator"):
                    continue
                text = "".join(t.text or "" for t in note.iter(f'{{{w}}}t')).strip()
                target[note_id] = text

    def _paragraph_note_references(self, element) -> list[dict]:
        """收集段内脚注 / 尾注引用标记，保留出现顺序与关联注释体。"""
        w = NS["w"]
        refs: list[dict] = []
        for child in element.iter():
            raw = child.tag
            if not isinstance(raw, str):
                continue
            local = raw.split('}', 1)[-1]
            if local == "footnoteReference":
                note_id = child.get(f'{{{w}}}id')
                self._note_ref_counter += 1
                body = self._footnotes.get(note_id)
                if body is None:
                    self._note_skip(
                        "docx_footnote_unresolved",
                        "正文引用了一个脚注，但脚注正文缺失，未能还原注释内容。",
                        source_span={"footnote_id": note_id},
                    )
                refs.append({
                    "kind": "footnote",
                    "note_id": note_id,
                    "marker": self._note_ref_counter,
                    "text": body or "",
                })
            elif local == "endnoteReference":
                note_id = child.get(f'{{{w}}}id')
                self._note_ref_counter += 1
                body = self._endnotes.get(note_id)
                if body is None:
                    self._note_skip(
                        "docx_footnote_unresolved",
                        "正文引用了一个尾注，但尾注正文缺失，未能还原注释内容。",
                        source_span={"endnote_id": note_id},
                    )
                refs.append({
                    "kind": "endnote",
                    "note_id": note_id,
                    "marker": self._note_ref_counter,
                    "text": body or "",
                })
        return refs

    def _paragraph_bookmarks(self, element) -> list[dict]:
        """收集段内 bookmarkStart（内部引用目标：#name 可跳转到这里）。"""
        w = NS["w"]
        bookmarks: list[dict] = []
        for bm in element.iter(f'{{{w}}}bookmarkStart'):
            name = bm.get(f'{{{w}}}name')
            bm_id = bm.get(f'{{{w}}}id')
            if name and not name.startswith("_"):  # _GoBack 等隐藏书签跳过
                bookmarks.append({"name": name, "id": bm_id})
        return bookmarks

    def _paragraph_internal_refs(self, element) -> list[dict]:
        """收集段内指向书签的内部引用（w:hyperlink w:anchor / REF 域）。"""
        w = NS["w"]
        refs: list[dict] = []
        for hl in element.iter(f'{{{w}}}hyperlink'):
            anchor = hl.get(f'{{{w}}}anchor')
            if anchor:
                anchor_text = "".join(t.text or "" for t in hl.iter(f'{{{w}}}t')).strip()
                refs.append({"anchor": anchor, "text": anchor_text})
        return refs

    # ================================================================
    # E05 (4): OMML 转换质量标记 (formula_fidelity)
    # ================================================================

    @staticmethod
    def _classify_formula_fidelity(element, latex: str) -> str:
        """判定 OMML→LaTeX 的转换质量：exact / lossy / source_only。

        - source_only：转换结果为空（拿不到任何可读表示，只能保留源片段）。
        - exact：结构简单（无未覆盖节点），LaTeX 可视为等价。
        - lossy：含 _omml_to_text 未结构化覆盖的节点（矩阵 m:m、分组 m:groupChr、
          带重音 m:acc 等），结果是近似——绝不能只拼字符就声称公式正确。
        """
        if not latex or not latex.strip():
            return "source_only"
        if etree is None:
            return "exact"
        m = NS["m"]
        # 已被 _omml_to_text 结构化覆盖的标签；出现其它 m:* 结构即判 lossy。
        covered = {
            "oMath", "oMathPara", "f", "num", "den", "r", "t", "sSup", "sSub",
            "sSubSup", "e", "sup", "sub", "rad", "deg", "nary", "naryPr", "chr",
            "d", "dPr", "begChr", "endChr", "func", "fName", "limLow", "limUpp",
            "lim", "ctrlPr", "rPr",
        }
        try:
            for node in element.iter():
                raw = node.tag
                if not isinstance(raw, str) or not raw.startswith(f'{{{m}}}'):
                    continue
                local = raw.split('}', 1)[-1]
                if local not in covered:
                    return "lossy"
        except Exception:  # noqa: BLE001
            return "lossy"
        return "exact"

    def _make_formula_block(self, element, block_id: int, source: str) -> Optional[Block]:
        """把一个 OMML 根节点做成带 formula_fidelity 标记的 formula Block。

        source_only 时保留源 OMML 片段（可视化 / 复核用），不静默丢弃、也不假装转成功。
        """
        latex = self._omml_to_text(element)
        fidelity = self._classify_formula_fidelity(element, latex)
        metadata = {"source": source, "formula_fidelity": fidelity}

        if fidelity == "source_only":
            # 拿不到 LaTeX：保留源 XML 片段，正文放可见占位而非空。
            try:
                source_xml = etree.tostring(element, encoding="unicode") if etree is not None else ""
            except Exception:  # noqa: BLE001
                source_xml = ""
            metadata["source_omml"] = source_xml
            self._note_skip(
                "docx_formula_source_only",
                "文档中有一个公式无法转换为 LaTeX，已保留其源片段，未生成可渲染公式。",
                source_span={"block_id": block_id},
            )
            return Block(
                id=block_id, type="formula",
                text="[公式：源表示已保留，无法转换]", metadata=metadata,
            )

        if fidelity == "lossy":
            self._note_skip(
                "docx_formula_lossy",
                "文档中有一个复杂公式转换为 LaTeX 时有损（结构保留，表示为近似）。",
                source_span={"block_id": block_id},
            )
            try:
                metadata["source_omml"] = (
                    etree.tostring(element, encoding="unicode") if etree is not None else ""
                )
            except Exception:  # noqa: BLE001
                pass

        return Block(id=block_id, type="formula", text=latex, metadata=metadata)

    # ================================================================
    # 原有逻辑（保持不变）
    # ================================================================

    @staticmethod
    def _read_w_val(element, tag: str) -> Optional[str]:
        """Read a ``w:val`` attribute from the first child named *tag*."""
        if element is None:
            return None
        child = element.find(f'{{{NS["w"]}}}{tag}')
        if child is None:
            return None
        return child.get(f'{{{NS["w"]}}}val')

    @staticmethod
    def _style_chain(style) -> list:
        """Return closest-first basedOn chain with cycle protection."""
        chain = []
        seen: set[str] = set()
        current = style
        while current is not None:
            key = str(getattr(current, "style_id", "") or id(current))
            if key in seen:
                break
            seen.add(key)
            chain.append(current)
            try:
                current = current.base_style
            except Exception:
                break
        return chain

    # ================================================================
    # 缓存化有效格式解析
    #
    # python-docx 的 paragraph.style / run.style 访问远非 O(1)：无显式
    # style id 时每次都对 styles part 做线性 XML 扫描（Styles.default），
    # 旧实现"每 run × 每属性重建 style chain"把该成本放大成 90%+ 的
    # 提取耗时。这里按 (style_id, style_type) 缓存 basedOn 链与"链上
    # 首个非 None 字体属性"，每文档每样式只解析一次。
    # ================================================================

    def _part_for(self, owner) -> Optional[object]:
        """Return the document part, deriving it lazily for direct calls."""
        if self._doc_part is None:
            try:
                self._doc_part = owner.part
            except Exception:
                return None
        return self._doc_part

    def _chain_for_style_id(self, style_id: Optional[str], style_type) -> list:
        key = (style_id, style_type)
        chain = self._chain_cache.get(key)
        if chain is None:
            style = None
            if self._doc_part is not None:
                try:
                    style = self._doc_part.get_style(style_id, style_type)
                except Exception:
                    style = None
            chain = self._style_chain(style)
            self._chain_cache[key] = chain
        return chain

    def _paragraph_chain(self, paragraph: Paragraph) -> list:
        """Cached closest-first basedOn chain for a paragraph's style."""
        if self._part_for(paragraph) is None:
            return self._style_chain(paragraph.style)
        try:
            style_id = paragraph._p.style
        except Exception:
            style_id = None
        return self._chain_for_style_id(style_id, WD_STYLE_TYPE.PARAGRAPH)

    def _chain_props(self, style_id: Optional[str], style_type) -> dict:
        """First non-None font property along one basedOn chain (cached)."""
        key = (style_id, style_type)
        props = self._chain_props_cache.get(key)
        if props is None:
            props = self._props_from_chain(self._chain_for_style_id(style_id, style_type))
            self._chain_props_cache[key] = props
        return props

    @staticmethod
    def _props_from_chain(chain: list) -> dict:
        props: dict = {name: None for name in _RUN_BOOL_PROPS}
        props["size_pt"] = None
        props["name"] = None
        for style in chain:
            try:
                font = style.font
            except Exception:
                continue
            for name in _RUN_BOOL_PROPS:
                if props[name] is None:
                    try:
                        value = getattr(font, name)
                    except Exception:
                        continue
                    if value is not None:
                        props[name] = bool(value)
            if props["size_pt"] is None:
                try:
                    if font.size is not None:
                        props["size_pt"] = float(font.size.pt)
                except Exception:
                    pass
            if props["name"] is None:
                try:
                    if font.name:
                        props["name"] = str(font.name)
                except Exception:
                    pass
        return props

    def _resolve_paragraph_runs(self, paragraph: Paragraph) -> tuple[list[_ResolvedRun], bool]:
        """Resolve every non-empty run of a paragraph exactly once.

        Returns ``(resolved_runs, had_any_runs)``; the flag preserves the
        legacy distinction between "no runs at all" (rich text falls back
        to ``paragraph.text``) and "runs exist but are all empty".
        """
        runs = paragraph.runs
        # run.text 是一次 xpath 求值，读一遍后直接传入解析，避免重复求值。
        resolved = [
            self._resolve_run(run, index, paragraph, text)
            for index, (run, text) in enumerate((run, run.text) for run in runs)
            if text
        ]
        return resolved, bool(runs)

    def _resolve_inline_runs_in_order(self, paragraph: Paragraph) -> list[_ResolvedRun]:
        """Resolve every VISIBLE run in XML order, descending into wrappers.

        Unlike :meth:`_resolve_paragraph_runs` (top-level ``w:r`` only), this
        walks the shared inline iterator so hyperlink / inserted-revision runs
        become auditable atoms instead of vanishing (B01: 来源顺序可还原).
        The sequential index is the visible-run ordinal, matching the order a
        reader sees the text.
        """
        resolved: list[_ResolvedRun] = []
        index = 0
        for inline in self._iter_paragraph_inline(paragraph._element):
            if not self._inline_run_visible(inline):
                continue
            text = self._inline_run_text(inline)
            if not text:
                continue
            resolved.append(
                self._resolve_run(Run(inline.run_element, paragraph), index, paragraph, text)
            )
            index += 1
        return resolved

    def _resolve_run(self, run, index: int, paragraph: Paragraph, text: str) -> _ResolvedRun:
        if self._part_for(paragraph) is not None:
            try:
                run_style_id = run._r.style
            except Exception:
                run_style_id = None
            try:
                para_style_id = paragraph._p.style
            except Exception:
                para_style_id = None
            run_chain = self._chain_for_style_id(run_style_id, WD_STYLE_TYPE.CHARACTER)
            run_props = self._chain_props(run_style_id, WD_STYLE_TYPE.CHARACTER)
            para_props = self._chain_props(para_style_id, WD_STYLE_TYPE.PARAGRAPH)
        else:
            run_chain = self._style_chain(getattr(run, "style", None))
            run_props = self._props_from_chain(run_chain)
            para_props = self._props_from_chain(self._style_chain(paragraph.style))

        # run 的全部直接格式值（布尔属性 / 字号 / 字体名）都存放在 rPr 里；
        # 无 rPr 的纯文本 run（多数文档的主体）可跳过所有直接值探测。
        try:
            has_rpr = run._r.rPr is not None
        except Exception:
            has_rpr = True

        def effective_bool(name: str) -> bool:
            direct = self._direct_font_value(run, name) if has_rpr else None
            if direct is not None:
                return bool(direct)
            value = run_props[name]
            if value is None:
                value = para_props[name]
            return bool(value) if value is not None else False

        font_size: Optional[float] = None
        if has_rpr:
            try:
                if run.font.size is not None:
                    font_size = float(run.font.size.pt)
            except Exception:
                pass
        if font_size is None:
            font_size = (
                run_props["size_pt"]
                if run_props["size_pt"] is not None
                else para_props["size_pt"]
            )

        font_name = self._get_run_font_name(run) if has_rpr else None
        if not font_name:
            font_name = run_props["name"] or para_props["name"]

        resolved_style = run_chain[0] if run_chain else None

        return _ResolvedRun(
            index=index,
            text=text,
            bold=effective_bool("bold"),
            italic=effective_bool("italic"),
            underline=effective_bool("underline"),
            strike=effective_bool("strike"),
            superscript=effective_bool("superscript"),
            subscript=effective_bool("subscript"),
            font_size=font_size,
            font_name=font_name,
            is_code=bool(font_name and font_name.lower() in _MONO_FONTS),
            style_id=str(getattr(resolved_style, "style_id", "") or ""),
        )

    def _paragraph_outline_level(self, paragraph: Paragraph) -> Optional[int]:
        """Read direct or effective basedOn-chain outline level (0-based)."""
        try:
            value = self._read_w_val(paragraph._element.pPr, "outlineLvl")
            if value is not None:
                return int(value)
        except Exception:
            pass

        for style in self._paragraph_chain(paragraph):
            try:
                value = self._read_w_val(style.element.pPr, "outlineLvl")
                if value is not None:
                    return int(value)
            except Exception:
                continue
        return None

    def _paragraph_list_level(self, paragraph: Paragraph) -> Optional[int]:
        """Read effective direct/style basedOn-chain ``numPr`` (0-based).

        OOXML permits ``numPr`` with a ``numId`` but no explicit ``ilvl``;
        that form denotes level zero.  A direct ``numId=0`` explicitly removes
        numbering and must stop inherited style numbering from leaking through.
        """
        property_sources = []
        try:
            property_sources.append(paragraph._element.pPr)
        except Exception:
            pass
        for style in self._paragraph_chain(paragraph):
            try:
                property_sources.append(style.element.pPr)
            except Exception:
                continue

        for p_pr in property_sources:
            if p_pr is None:
                continue
            try:
                num_pr = p_pr.find(f'{{{NS["w"]}}}numPr')
                if num_pr is None:
                    continue
                num_id = self._read_w_val(num_pr, "numId")
                if num_id == "0":
                    return None
                level = self._read_w_val(num_pr, "ilvl")
                if level is not None:
                    return int(level)
                if num_id is not None:
                    return 0
            except (TypeError, ValueError):
                continue
        return None

    def _effective_style_identity(self, paragraph: Paragraph) -> tuple[str, str, list[str]]:
        """Return the style that contributes heading structure plus full chain."""
        chain = self._paragraph_chain(paragraph)
        chain_ids = [str(getattr(style, "style_id", "") or "") for style in chain]
        heading_pattern = re.compile(
            r"^(?:Heading\s*|标题\s*|標題\s*|Title\s*)(\d+)$",
            re.IGNORECASE,
        )
        for style in chain:
            name = str(getattr(style, "name", "") or "")
            style_id = str(getattr(style, "style_id", "") or "")
            try:
                has_outline = self._read_w_val(style.element.pPr, "outlineLvl") is not None
            except Exception:
                has_outline = False
            if heading_pattern.match(name) or re.match(r"^Heading\d+$", style_id, re.IGNORECASE) or has_outline:
                return name, style_id, chain_ids
        if chain:
            return (
                str(getattr(chain[0], "name", "") or ""),
                str(getattr(chain[0], "style_id", "") or ""),
                chain_ids,
            )
        return "", "", chain_ids

    def _detect_heading_structure(
        self,
        paragraph: Paragraph,
        text: str,
    ) -> tuple[bool, Optional[int], str, str, Optional[int], Optional[int]]:
        """Detect localized/effective Word heading style and structural levels."""
        chain = self._paragraph_chain(paragraph)
        style = chain[0] if chain else None
        style_name = style.name if style else ""
        style_id = getattr(style, "style_id", "") if style else ""

        outline_level = self._paragraph_outline_level(paragraph)
        list_level = self._paragraph_list_level(paragraph)
        visible_numbering_level = infer_numbering_level(text)
        numbering_level = (
            list_level + 1 if list_level is not None else visible_numbering_level
        )

        heading_level: Optional[int] = None
        patterns = (
            r"^Heading\s*(\d+)$",
            r"^标题\s*(\d+)$",
            r"^標題\s*(\d+)$",
            r"^Heading(\d+)$",
            r"^Title\s*(\d+)$",
        )
        for candidate_style in chain:
            candidate_name = str(getattr(candidate_style, "name", "") or "")
            candidate_id = str(getattr(candidate_style, "style_id", "") or "")
            for pattern in patterns:
                match = re.match(pattern, candidate_name, re.IGNORECASE)
                if match:
                    heading_level = int(match.group(1))
                    break
            if heading_level is None:
                match = re.match(r"^Heading(\d+)$", candidate_id, re.IGNORECASE)
                if match:
                    heading_level = int(match.group(1))
            if heading_level is not None:
                break

        if heading_level is None and outline_level is not None:
            heading_level = outline_level + 1

        is_heading = heading_level is not None
        if heading_level is not None:
            heading_level = max(1, min(heading_level, 6))

        return (
            is_heading,
            heading_level,
            style_name,
            style_id,
            outline_level,
            numbering_level,
        )

    # ── 内嵌电子表格对象 ────────────────────────────────────────────────

    def _resolve_embedded_target(self, part, relationship_id: Optional[str]):
        """按关系 ID 取内嵌资源。返回 ``(bytes|None, target_part|None, 外链地址|None)``。

        走 ``part.rels[rId].target_part.blob``，**不拼磁盘路径、不按 embeddings 文件名
        排序猜正文顺序**——文件名的编号顺序与正文出现顺序无关（方案 §3.1），关系 ID 才是
        唯一定位依据。外部关系只回地址、绝不发网络请求。
        """
        if not relationship_id or part is None:
            return None, None, None
        try:
            rel = part.rels[relationship_id]
        except KeyError:
            return None, None, None
        if getattr(rel, "is_external", False):
            return None, None, str(getattr(rel, "target_ref", "") or "")
        try:
            target = rel.target_part
            return target.blob, str(getattr(target, "partname", "") or ""), None
        except Exception:  # noqa: BLE001 - 关系存在但部件损坏/缺失
            return None, None, None

    def _embedded_placeholder_block(
        self, block_id: int, ref, label: str, provenance: dict
    ) -> Block:
        """失败/不支持/外链对象的**正文可见**占位块。

        不静默跳过、也不塞进会被预览隐藏的注释里：读者要能在原位看到"这里本来有一张
        嵌入表格、为什么没读出来"，否则一份少了 20 张表的文档看起来完全正常。
        """
        return Block(
            id=block_id,
            type="text",
            text=f"【嵌入电子表格未解析：{label}】",
            metadata={
                "source": "embedded_spreadsheet_placeholder",
                "embedded_spreadsheet": provenance,
            },
        )

    def _blocks_for_embedded_object(
        self, event: "DocxProvider._InlineObject", position, start_block_id: int, *, part
    ) -> List[Block]:
        """把一次嵌入对象出现解析成有序 Block（成功→表格块；否则→占位块）。"""
        owner_part = str(getattr(getattr(part, "partname", None), "__str__", lambda: "")() or "document")
        ref = _emb.EmbeddedObjectRef(
            occurrence_id="",
            owner_part=owner_part,
            relationship_id=event.relationship_id or "",
            prog_id=event.prog_id,
            ole_type=event.ole_type,
            position=position,
        )
        occurrence_id = _emb.make_occurrence_id(owner_part, ref.relationship_id, position)
        ref = replace(ref, occurrence_id=occurrence_id)

        budget = self._embedded_budget
        if not budget.claim_object():
            prov = _emb.build_provenance(
                ref, status=_emb.STATUS_FAILED, detail=budget.breach or "超出文档级预算"
            )
            self._note_skip(
                _emb.WARN_BUDGET,
                f"内嵌对象超出文档级预算（{budget.breach}），该对象未解析并已在原位留占位。",
                source_span={"occurrence_id": occurrence_id},
                retryable=False,
            )
            return [self._embedded_placeholder_block(start_block_id, ref, "超出文档级预算", prov)]

        blob, target_part, external = self._resolve_embedded_target(part, event.relationship_id)

        if external is not None:
            prov = _emb.build_provenance(ref, status=_emb.STATUS_EXTERNAL, detail=external)
            self._note_skip(
                _emb.WARN_EXTERNAL,
                "内嵌对象是外部链接，按约定不访问网络、不激活 Office 对象，仅登记引用。",
                source_span={"occurrence_id": occurrence_id, "target": external},
                retryable=False,
            )
            return [self._embedded_placeholder_block(start_block_id, ref, "外部链接对象", prov)]

        if blob is None:
            prov = _emb.build_provenance(ref, status=_emb.STATUS_FAILED, detail="关系或部件缺失")
            self._note_skip(
                _emb.WARN_MISSING,
                f"内嵌对象的关系 {event.relationship_id} 无法解析到资源部件（关系缺失或部件损坏）。",
                source_span={"occurrence_id": occurrence_id, "relationship_id": event.relationship_id},
                retryable=False,
            )
            return [self._embedded_placeholder_block(start_block_id, ref, "关系或部件缺失", prov)]

        digest = _emb.content_hash(blob)
        ref = replace(ref, target_part=target_part, content_hash=digest)

        sniff = _emb.sniff_embedded_format(blob)
        if sniff.format != _emb.FORMAT_XLSX_PACKAGE:
            prov = _emb.build_provenance(
                ref, status=_emb.STATUS_UNSUPPORTED, detail=sniff.reason
            )
            self._note_skip(
                _emb.WARN_UNSUPPORTED,
                f"内嵌对象本期不支持：{sniff.reason}（ProgID={event.prog_id or '未声明'}）。",
                source_span={"occurrence_id": occurrence_id, "format": sniff.format},
                retryable=False,
            )
            return [self._embedded_placeholder_block(start_block_id, ref, sniff.reason, prov)]

        if not budget.claim_package(sniff.expanded_bytes, sniff.entry_count):
            prov = _emb.build_provenance(
                ref, status=_emb.STATUS_FAILED, detail=budget.breach or "超出文档级预算"
            )
            self._note_skip(
                _emb.WARN_BUDGET,
                f"内嵌包超出文档级预算（{budget.breach}），已在读取前拒绝并在原位留占位。",
                source_span={"occurrence_id": occurrence_id},
                retryable=False,
            )
            return [self._embedded_placeholder_block(start_block_id, ref, "超出文档级预算", prov)]

        # 同一附件在多处引用：读取按内容哈希去重，但每处各自出块、各自 occurrence_id。
        sheets = self._embedded_cache.get(digest)
        if sheets is None:
            try:
                sheets = self._xlsx_provider.read_embedded(blob, budget=budget)
            except Exception as exc:  # noqa: BLE001 - 单个对象失败不阻断正文
                self._absorb_child_skips(occurrence_id)
                prov = _emb.build_provenance(
                    ref, status=_emb.STATUS_FAILED, detail=str(exc)[:200]
                )
                self._note_skip(
                    _emb.WARN_FAILED,
                    f"内嵌电子表格解析失败：{str(exc)[:120]}；已在原位留占位，正文其余内容不受影响。",
                    source_span={"occurrence_id": occurrence_id},
                    retryable=True,
                )
                return [self._embedded_placeholder_block(start_block_id, ref, "解析失败", prov)]
            self._embedded_cache.put(digest, sheets)
        # 子解析器（XLSX）的告警必须补上对象位置后并入本文档的告警——否则「某张内嵌表
        # 有无缓存公式 / 被截断」只留在子 provider 里，交付层看不到，partial 语义丢失。
        self._absorb_child_skips(occurrence_id)

        if not sheets:
            prov = _emb.build_provenance(ref, status=_emb.STATUS_PARTIAL, detail="工作簿无可读工作表")
            return [self._embedded_placeholder_block(start_block_id, ref, "工作簿无可读内容", prov)]

        blocks: List[Block] = []
        multi = len(sheets) > 1
        for offset, sheet in enumerate(sheets):
            status = _emb.STATUS_PARTIAL if (sheet.truncated_rows or sheet.truncated_cols) else _emb.STATUS_PARSED
            width = max((len(r) for r in sheet.rows), default=0)
            prov = _emb.build_provenance(
                ref,
                status=status,
                sheet_name=sheet.name,
                sheet_index=sheet.index,
                sheet_state=sheet.state,
                source_range=_sheet_source_range(
                    sheet.estimated_rows, sheet.estimated_cols, len(sheet.rows), width
                ),
                read_range={"rows": len(sheet.rows), "cols": width},
                row_indices=sheet.row_indices,
                column_indices=list(range(width)),
                read_truncated=bool(sheet.truncated_rows or sheet.truncated_cols),
                render_sampled=len(sheet.rows) > self._xlsx_provider.max_render_rows,
                detail=f"多工作表第 {sheet.index + 1} 张" if multi else None,
            )
            blocks.append(
                self._xlsx_provider.assemble_embedded_table_block(
                    start_block_id + offset,
                    sheet,
                    extra_metadata={"embedded_spreadsheet": prov},
                )
            )
        return blocks

    def _paragraph_has_embedded_object(self, element) -> bool:
        """段内是否含 ``w:object/o:OLEObject``（含被包装节点或 AlternateContent 包住的）。"""
        for event in self._iter_paragraph_events(element):
            if isinstance(event, DocxProvider._InlineObject):
                return True
        return False

    def _emit_paragraph_with_objects(
        self,
        paragraph: Paragraph,
        element_index: int,
        block_id: int,
        blocks: List[Block],
        *,
        part,
        parent_table_block_id: Optional[int] = None,
        row: Optional[int] = None,
        col: Optional[int] = None,
    ) -> int:
        """按原文顺序把「文字片段 / 嵌入对象」逐个出块，返回下一个可用 block_id。"""
        for event_index, (text, obj) in enumerate(
            self._split_paragraph_by_objects(paragraph)
        ):
            if obj is None:
                text_block = self._process_paragraph(
                    paragraph,
                    block_id,
                    source_index=element_index,
                    raw_text=text,
                    rich_text_override=text,
                )
                if text_block:
                    blocks.append(text_block)
                    block_id += 1
                continue
            position = _emb.SourcePosition(
                element_index=element_index,
                event_index=event_index,
                parent_table_block_id=parent_table_block_id,
                row=row,
                col=col,
                cell_event_index=event_index if parent_table_block_id is not None else None,
            )
            produced = self._blocks_for_embedded_object(
                obj, position, block_id, part=part
            )
            for produced_block in produced:
                produced_block.id = block_id
                blocks.append(produced_block)
                block_id += 1
        return block_id

    def _absorb_child_skips(self, occurrence_id: str) -> None:
        """把内嵌 XLSX 解析器攒下的告警搬进本文档的告警，并补上对象出现位置。

        子 provider 有自己的 SkipReporter；不搬过来的话「这张内嵌表被截断了」只存在于一个
        没人 drain 的对象里，交付层照旧报 complete——正是"容错做对了但告知做没了"。
        """
        for skip in self._xlsx_provider.drain_skips():
            span = dict(getattr(skip, "source_span", None) or {})
            span["occurrence_id"] = occurrence_id
            span["from_embedded_object"] = True
            self._note_skip(
                skip.code,
                skip.message,
                source_span=span,
                retryable=bool(getattr(skip, "retryable", False)),
            )

    def _process_paragraph(
        self,
        paragraph: Paragraph,
        block_id: int,
        source_index: Optional[int] = None,
        raw_text: Optional[str] = None,
        rich_text_override: Optional[str] = None,
    ) -> Optional[Block]:
        """Process a paragraph while retaining run-level physical atoms.

        ``raw_text`` lets ``_extract_blocks`` share its already-computed
        ``paragraph.text``; direct callers omit it.

        ``rich_text_override`` 用于**段内被嵌入对象切开**的文字片段：此时本段有多个
        文字块，正文只能是该片段而不是整段。给它时同时放弃 run 级 atoms 并在 metadata
        里说明原因——atoms 记录的是「字符 → 源 run」的映射，拿整段的 run 去标一个片段
        的字符区间会得到一份看着完整、实则错位的映射，比没有更危险。
        """
        prepared = self._resolve_paragraph_runs(paragraph)
        resolved_runs, _ = prepared
        # 有包装节点时，paragraph.runs 漏掉 hyperlink / ins 里的 run，atoms 与
        # 加粗/字号统计都会缺项。改用统一 XML 顺序遍历得到可见 run 序列，保证
        # atoms 覆盖包装内文字、来源顺序可还原（B01 验收）。
        has_wrapped = self._paragraph_has_wrapped_content(paragraph._element)
        resolved_for_atoms = (
            self._resolve_inline_runs_in_order(paragraph) if has_wrapped else resolved_runs
        )
        if rich_text_override is not None:
            rich_text = rich_text_override
            # 片段的字符区间对不上整段的 run，宁可不给 atoms 也不给错的映射。
            resolved_for_atoms = []
        else:
            rich_text = self._extract_rich_text(paragraph, prepared)
        plain_text = (paragraph.text if raw_text is None else raw_text).strip()
        text = rich_text.strip() if rich_text and rich_text.strip() else plain_text
        if not text:
            return None

        (
            is_heading,
            heading_level,
            style_name,
            style_id,
            outline_level,
            numbering_level,
        ) = self._detect_heading_structure(paragraph, plain_text)

        is_bold = self._detect_bold(resolved_for_atoms)
        is_code = self._detect_code_font(resolved_for_atoms)
        if is_code and plain_text:
            text = plain_text
        font_size = self._detect_font_size(paragraph, resolved_for_atoms)
        alignment = self._detect_alignment(paragraph)
        list_level = self._paragraph_list_level(paragraph)
        effective_style_name, effective_style_id, style_chain = (
            self._effective_style_identity(paragraph)
        )
        paragraph_index = block_id if source_index is None else source_index
        atoms = self._build_docx_run_atoms(
            paragraph_index=paragraph_index,
            block_id=block_id,
            block_text=text,
            alignment=alignment,
            effective_style_id=effective_style_id,
            resolved_runs=resolved_for_atoms,
        )
        revisions = self._collect_paragraph_revisions(paragraph._element) if has_wrapped else []

        # E05：有效列表符号 + 章节/列表编号区分；正文引用的脚注/尾注/书签/内部链接。
        numbering = self._resolve_numbering(paragraph, list_level)
        note_refs = self._paragraph_note_references(paragraph._element)
        bookmarks = self._paragraph_bookmarks(paragraph._element)
        internal_refs = self._paragraph_internal_refs(paragraph._element)

        metadata = {
            "style": style_name,
            "style_id": style_id,
            "effective_style": effective_style_name,
            "effective_style_id": effective_style_id,
            "style_chain": style_chain,
            "outline_level": outline_level,
            "list_level": list_level,
            "numbering_level": numbering_level,
            "source": "paragraph",
            "source_span": {"paragraph": paragraph_index},
            "atoms": atoms,
            "revision_view": REVISION_VIEW,
            "revisions": revisions,
            "provenance": {
                "provider": "python-docx",
                "effective_properties": "direct+run-style+paragraph-basedOn",
            },
        }
        if numbering is not None:
            metadata["numbering"] = numbering
        if note_refs:
            metadata["note_refs"] = note_refs
        if bookmarks:
            metadata["bookmarks"] = bookmarks
        if internal_refs:
            metadata["internal_refs"] = internal_refs

        block = Block(
            id=block_id,
            type="code" if is_code else "text",
            text=text,
            metadata=metadata,
            is_bold=is_bold,
            font_size=font_size,
            alignment=alignment,
            is_heading_style=is_heading,
            heading_level=heading_level,
            has_heading_numbering=numbering_level is not None,
        )
        self._apply_atom_weighted_format(block)
        return block
    
    @staticmethod
    def _direct_font_value(run, property_name: str):
        try:
            if property_name in {"bold", "italic", "underline"}:
                return getattr(run, property_name)
            if property_name in {"superscript", "subscript"}:
                value = getattr(run.font, property_name)
                if value is not None:
                    return bool(value)
                vert_align = run.font._element.find(f'{{{NS["w"]}}}vertAlign')
                if vert_align is not None:
                    val = vert_align.get(f'{{{NS["w"]}}}val')
                    return val == property_name
            return getattr(run.font, property_name)
        except Exception:
            return None

    def _build_docx_run_atoms(
        self,
        *,
        paragraph_index: int,
        block_id: int,
        block_text: str,
        alignment: Optional[str],
        effective_style_id: str,
        resolved_runs: list[_ResolvedRun],
    ) -> list[dict]:
        atoms: list[dict] = []
        for rr in resolved_runs:
            source_span = {"paragraph": paragraph_index, "run": rr.index}
            atom = StructuralAtom.create(
                source="docx_run",
                source_span=source_span,
                block_id=block_id,
                text=rr.text,
                font_family=rr.font_name,
                font_size=rr.font_size,
                is_bold=rr.bold,
                is_italic=rr.italic,
                is_underline=rr.underline,
                is_strike=rr.strike,
                is_superscript=rr.superscript,
                is_subscript=rr.subscript,
                is_code=rr.is_code,
                alignment=alignment,
                region="body",
                provenance={
                    "provider": "python-docx",
                    "run_style_id": rr.style_id,
                    "effective_paragraph_style_id": effective_style_id,
                },
            ).to_metadata()
            atoms.append(atom)

        if not atoms:
            source_span = {"paragraph": paragraph_index}
            atoms.append(StructuralAtom.create(
                source="docx_paragraph",
                source_span=source_span,
                block_id=block_id,
                text=block_text,
                alignment=alignment,
                region="body",
                provenance={
                    "provider": "python-docx",
                    "effective_paragraph_style_id": effective_style_id,
                },
            ).to_metadata())
        self._recalculate_atom_offsets(block_text, atoms)
        return atoms

    # ================================================================
    # B01: 统一 XML 顺序内联内容迭代器
    #
    # 缺陷：w:hyperlink / w:ins 等包装节点里的 w:r 文字被静默丢弃
    # （paragraph.runs 只给顶层 w:r；paragraph.text 又不含 w:ins）。
    # 复现：`PREFIX <hyperlink>MARKERTEXT</hyperlink> SUFFIX` → `PREFIX  SUFFIX`；
    #       `BEFORE <w:ins>INSERTED</w:ins> AFTER` → `BEFORE  AFTER`。
    # 修复：单一迭代器按 XML 顺序下钻包装节点，供"是否有内容"、纯文本、
    # 富文本、atoms 共用，保证四条出口看到同一份可见内容。
    # ================================================================

    @dataclass(slots=True)
    class _InlineRun:
        """一个可见 w:r 及其在段内的包装上下文（超链接 / 修订）。

        携带上下文而非提前渲染，是因为富文本要按 run 样式包裹 Markdown、
        atoms 要记录 source_span、纯文本只取 text —— 三者对同一 run 的需求
        不同，迭代器只负责"按序给出带上下文的 run"，渲染留给各出口。
        """

        run_element: object
        hyperlink_url: Optional[str] = None   # 已解析的目标（外链 URL 或 #bookmark）
        in_ins: bool = False                  # 位于 w:ins 内（被接受的插入）
        in_del: bool = False                  # 位于 w:del 内（被接受视图下应丢弃）

    @dataclass(slots=True)
    class _InlineObject:
        """段内一个嵌入对象（``w:object/o:OLEObject``）及其在段内的出现顺序。

        它和 :class:`_InlineRun` 一起构成段落的**有序事件流**。必须走同一个迭代器：
        对象藏在 ``w:r`` 里，而 ``w:r`` 又可能被 hyperlink / ins / sdt /
        mc:AlternateContent 层层包住——另起一个 ``findall('.//w:object')`` 拿不到「它在
        第几段文字之后」这个位置信息，结果就是对象被统一追加到段尾或全文末尾，
        正文顺序丢失（方案 §3.2 明确禁止）。
        """

        ole_element: object                   # o:OLEObject
        run_element: object                   # 承载它的 w:r（预览图 VML 也在里面）
        relationship_id: Optional[str] = None
        ole_type: str = ""                    # Embed / Link
        prog_id: str = ""                     # 如 Excel.Sheet.12
        in_del: bool = False

    def _resolve_hyperlink_target(self, hyperlink_element) -> Optional[str]:
        """把 w:hyperlink 解析成目标：优先 r:id→外链 URL，其次内部 w:anchor。

        锚文本要能还原成 `[text](url)`，所以 URL 必须在提取期解析——rels 只在
        当前文档打开时可用，延后到渲染期就拿不到 target 了（B01 验收：URL 可还原）。
        """
        r_id = hyperlink_element.get(f'{{{NS["r"]}}}id')
        if r_id and self._doc_rels is not None:
            try:
                rel = self._doc_rels[r_id]
                target = getattr(rel, "target_ref", None) or getattr(rel, "_target", None)
                if target:
                    return str(target)
            except Exception:
                pass
        anchor = hyperlink_element.get(f'{{{NS["w"]}}}anchor')
        if anchor:
            return f"#{anchor}"
        return None

    def _iter_paragraph_events(
        self,
        element,
        *,
        hyperlink_url: Optional[str] = None,
        in_ins: bool = False,
        in_del: bool = False,
    ):
        """按 XML 顺序产出段内每个可见 w:r，并下钻包装节点（可任意嵌套）。

        默认修订视图为 accepted：w:del 仍会产出（标记 in_del=True），由消费方
        据 REVISION_VIEW 决定是否落地——把"是否可见"的决策集中在一处，避免
        纯文本、富文本、atoms 各自实现一套修订过滤而彼此漂移。
        """
        w = NS["w"]
        for child in element:
            raw_tag = child.tag
            if not isinstance(raw_tag, str):
                continue
            tag = raw_tag.split('}', 1)[-1]
            if tag == 'r':
                yield DocxProvider._InlineRun(
                    run_element=child,
                    hyperlink_url=hyperlink_url,
                    in_ins=in_ins,
                    in_del=in_del,
                )
                # 对象藏在 run 里：先给 run（它可能同时带文字），再按 XML 顺序给对象。
                # 这样「文字 → 对象 → 文字」的段落能还原成对应顺序的内容块。
                yield from self._iter_run_objects(child, in_del=in_del)
            elif tag == 'AlternateContent':
                # mc:AlternateContent 有 Choice / Fallback 两套等价内容，同一个对象在
                # 两个分支各出现一次。**只能取一个分支**，否则同一嵌入表格会被抽两遍
                # （方案 §3.1）。优先取第一个 Choice（新版渲染器的首选），没有再取 Fallback。
                branch = child.find(f'{{{NS["mc"]}}}Choice')
                if branch is None:
                    branch = child.find(f'{{{NS["mc"]}}}Fallback')
                if branch is not None:
                    yield from self._iter_paragraph_events(
                        branch,
                        hyperlink_url=hyperlink_url,
                        in_ins=in_ins,
                        in_del=in_del,
                    )
            elif tag in _INLINE_WRAPPER_TAGS:
                child_url = hyperlink_url
                child_ins = in_ins
                child_del = in_del
                if tag == 'hyperlink':
                    child_url = self._resolve_hyperlink_target(child) or hyperlink_url
                elif tag == 'ins':
                    child_ins = True
                elif tag == 'del':
                    child_del = True
                elif tag == 'sdt':
                    # 内容控件的可见内容在 w:sdtContent 里；容器本身无文字。
                    body = child.find(f'{{{w}}}sdtContent')
                    if body is not None:
                        child = body
                yield from self._iter_paragraph_events(
                    child,
                    hyperlink_url=child_url,
                    in_ins=child_ins,
                    in_del=child_del,
                )

    def _iter_paragraph_inline(
        self,
        element,
        *,
        hyperlink_url: Optional[str] = None,
        in_ins: bool = False,
        in_del: bool = False,
    ):
        """:meth:`_iter_paragraph_events` 的**只取 run** 视图。

        纯文本、富文本、atoms、修订收集这四个出口只关心文字，不关心嵌入对象；让它们各自
        写 isinstance 过滤会把同一条判断复制四遍（下一次加事件类型就漏一处）。所以过滤只在
        这里做一次，需要位置信息的排序逻辑直接用事件流。
        """
        for event in self._iter_paragraph_events(
            element, hyperlink_url=hyperlink_url, in_ins=in_ins, in_del=in_del
        ):
            if isinstance(event, DocxProvider._InlineRun):
                yield event

    def _iter_run_objects(self, run_element, *, in_del: bool = False):
        """产出一个 ``w:r`` 内的全部 ``o:OLEObject`` 事件（按 XML 顺序）。

        判别依据是 ``o:OLEObject`` 节点本身，不是文件扩展名也不是 ProgID——后两者只是
        提示，真实格式在字节里判（见 ``embedded_spreadsheet.sniff_embedded_format``）。
        ``ProgID`` 与 ``Type`` 仍照实记录进来源，供审计与「为什么判成不支持」的解释。
        """
        ns_o = NS["o"]
        ns_r = NS["r"]
        for obj in run_element.iter(f'{{{NS["w"]}}}object'):
            for ole in obj.iter(f'{{{ns_o}}}OLEObject'):
                yield DocxProvider._InlineObject(
                    ole_element=ole,
                    run_element=run_element,
                    relationship_id=ole.get(f'{{{ns_r}}}id'),
                    ole_type=str(ole.get("Type") or ""),
                    prog_id=str(ole.get("ProgID") or ""),
                    in_del=in_del,
                )

    @staticmethod
    def _inline_run_visible(inline: "DocxProvider._InlineRun") -> bool:
        """在当前修订视图下该 run 是否可见（accepted 视图丢弃 w:del）。"""
        if inline.in_del and REVISION_VIEW == "accepted":
            return False
        return True

    @staticmethod
    def _inline_run_text(inline: "DocxProvider._InlineRun") -> str:
        """取 run 的物理文字：接受视图取 w:t，删除内容用 w:delText。"""
        w = NS["w"]
        parts = []
        for t in inline.run_element.iter():
            t_tag = t.tag
            if not isinstance(t_tag, str):
                continue
            local = t_tag.split('}', 1)[-1]
            if local == 't' and t_tag == f'{{{w}}}t':
                parts.append(t.text or "")
            elif local == 'delText' and t_tag == f'{{{w}}}delText':
                parts.append(t.text or "")
            elif local == 'tab' and t_tag == f'{{{w}}}tab':
                parts.append("\t")
            elif local in ('br', 'cr') and t_tag == f'{{{w}}}{local}':
                parts.append("\n")
        return "".join(parts)

    def _paragraph_wrapped_text(self, element) -> str:
        """段内 accepted 视图下的完整可见纯文本（含 hyperlink / ins 包装）。

        emptiness 检查与 raw_text 都用它：只靠 paragraph.text 会漏掉 w:ins，
        导致纯插入段落被判空而整段跳过（B01 验收：纯插入段不得被跳过）。
        """
        parts = []
        for inline in self._iter_paragraph_inline(element):
            if self._inline_run_visible(inline):
                parts.append(self._inline_run_text(inline))
        return "".join(parts)

    def _paragraph_has_wrapped_content(self, element) -> bool:
        """段内是否存在包装节点（hyperlink / ins / del / sdt / smartTag）。

        注意：del 在 accepted 视图下不可见、sdt 不置任何修订标记，但两者都算
        "有包装"——否则纯删除段 / 纯内容控件段走不到统一路径，文字或 revisions
        审计信息会丢失。可见性过滤留给渲染阶段，这里只判断结构上是否存在包装。
        直接扫描子孙标签而非依赖 inline 标志，才能覆盖 sdt 这种无标志的容器。
        """
        for child in element.iter():
            raw_tag = child.tag
            if not isinstance(raw_tag, str):
                continue
            if raw_tag.split('}', 1)[-1] in _INLINE_WRAPPER_TAGS:
                return True
        return False

    def _collect_paragraph_revisions(self, element) -> list[dict]:
        """逐条记录段内 w:ins / w:del，保证 accepted 视图之外仍可审计。"""
        revisions: list[dict] = []
        for inline in self._iter_paragraph_inline(element):
            if inline.in_ins or inline.in_del:
                text = self._inline_run_text(inline)
                if text.strip():
                    revisions.append({
                        "type": "ins" if inline.in_ins else "del",
                        "text": text,
                    })
        return revisions

    # ================================================================
    # v3: Three-Phase Rich Text Pipeline
    #
    #   Resolved Runs → [Phase 1: Normalize] → [Phase 2: Merge] → [Phase 3: Render]
    #
    # Phase 1 (_resolve_paragraph_runs + _segment_from_resolved): Convert
    #   python-docx Runs into immutable RichSegment objects via the shared
    #   cached effective-format resolution.  No string manipulation.
    # Phase 2 (_merge_homogeneous_segments): Absorb adjacent segments
    #   with identical style fingerprints into one.
    # Phase 3 (_render_segments): Apply Strip-Safe Markdown wrapping
    #   to each merged segment and concatenate.
    # ================================================================

    def _extract_rich_text(
        self,
        paragraph: Paragraph,
        prepared: Optional[tuple[list[_ResolvedRun], bool]] = None,
    ) -> str:
        """Three-phase rich text pipeline orchestrator.

        Returns a Markdown string with clean inline formatting:
        no ghost spaces, no fragmented bold runs, no marker bleed.
        ``prepared`` lets ``_process_paragraph`` share its run resolution;
        other callers (table cells) resolve on demand.
        """
        # 包装节点（hyperlink / ins / del / sdt）里的文字既不在 paragraph.runs、
        # w:ins 也不在 paragraph.text，走原快路径会整段丢字（B01）。有包装内容或
        # 内联对象时改走 XML 顺序段落遍历，逐 child 处理 run / 包装 / 公式 / 图片。
        if (
            self._paragraph_has_inline_omml(paragraph._element)
            or self._paragraph_has_inline_image(paragraph._element)
            or self._paragraph_has_wrapped_content(paragraph._element)
        ):
            return self._extract_rich_text_with_inline_objects(paragraph)

        if prepared is None:
            prepared = self._resolve_paragraph_runs(paragraph)
        resolved_runs, has_runs = prepared
        if not has_runs:
            return paragraph.text or ""

        # Phase 1 → Phase 2 → Phase 3
        return self._render_rich_segments(
            [self._segment_from_resolved(rr) for rr in resolved_runs]
        )

    @staticmethod
    def _segment_from_resolved(rr: _ResolvedRun) -> RichSegment:
        return RichSegment(
            text=rr.text,
            bold=rr.bold,
            italic=rr.italic,
            underline=rr.underline,
            strike=rr.strike,
            superscript=rr.superscript,
            subscript=rr.subscript,
            code=rr.is_code,
        )

    def _iter_rich_events(self, element, paragraph: Paragraph):
        """按 XML 顺序产出段内的 :class:`RichSegment` 与 ``_InlineObject`` 混合事件流。

        这是段内内容的**唯一**顺序真相源。此前富文本渲染自己走一遍段落 XML、嵌入对象要
        另走一遍，两套遍历一旦对"顺序"的理解出现分歧，就会表现成「文字和对象的相对位置
        错了」而两边各自的测试都是绿的。所以只保留这一个生产者：富文本渲染丢掉对象事件，
        分段逻辑在对象事件处切开，两者吃同一个流。

        包装节点（hyperlink 等）内的对象位置取「包装节点之后」——超链接锚文本必须整体渲染
        成 ``[text](url)``，不能被对象从中间切断。
        """
        w = NS["w"]
        for child in element:
            raw_tag = child.tag
            if not isinstance(raw_tag, str):
                continue
            local = raw_tag.split('}', 1)[-1]
            if raw_tag == f'{{{w}}}r':
                yield from self._normalize_run_element_in_order(child, paragraph)
                yield from self._iter_run_objects(child)
            elif local == 'AlternateContent':
                # 只取一个分支：Choice / Fallback 是同一内容的两套表示，都取会重复抽表。
                branch = child.find(f'{{{NS["mc"]}}}Choice')
                if branch is None:
                    branch = child.find(f'{{{NS["mc"]}}}Fallback')
                if branch is not None:
                    yield from self._iter_rich_events(branch, paragraph)
            elif local in _INLINE_WRAPPER_TAGS:
                yield from self._segments_from_wrapper(child, paragraph)
                for run in child.iter(f'{{{w}}}r'):
                    yield from self._iter_run_objects(run)
            elif raw_tag == f'{{{NS["m"]}}}oMath':
                formula_text = self._omml_to_text(child)
                if formula_text:
                    yield RichSegment(
                        text=Block.render_inline_formula(formula_text), trusted_markup=True,
                    )
            elif raw_tag == f'{{{NS["m"]}}}oMathPara':
                formula_text = self._omml_to_text(child)
                if formula_text:
                    yield RichSegment(
                        text=f"\n{Block.render_block_formula(formula_text)}\n",
                        trusted_markup=True,
                    )

    def _render_rich_segments(self, segments: List[RichSegment]) -> str:
        """RichSegment 列表 → Markdown（合并同构段后渲染）。渲染的唯一入口。"""
        return self._render_segments(self._merge_homogeneous_segments(segments))

    def _extract_rich_text_with_inline_objects(self, paragraph: Paragraph) -> str:
        segments = [
            event
            for event in self._iter_rich_events(paragraph._element, paragraph)
            if isinstance(event, RichSegment)
        ]
        return self._render_rich_segments(segments)

    def _split_paragraph_by_objects(
        self, paragraph: Paragraph
    ) -> list[tuple[str, Optional["DocxProvider._InlineObject"]]]:
        """把含嵌入对象的段落切成有序片段：``[(文字, None) | ("", 对象), ...]``。

        返回的顺序就是原文顺序，所以「前文 → 对象 → 后文」会得到三个片段，
        调用方照序出块即可，不需要（也不允许）把对象统一追加到段尾。
        """
        pieces: list[tuple[str, Optional[DocxProvider._InlineObject]]] = []
        pending: List[RichSegment] = []
        for event in self._iter_rich_events(paragraph._element, paragraph):
            if isinstance(event, RichSegment):
                pending.append(event)
                continue
            text = self._render_rich_segments(pending).strip()
            pending = []
            if text:
                pieces.append((text, None))
            pieces.append(("", event))
        tail = self._render_rich_segments(pending).strip()
        if tail:
            pieces.append((tail, None))
        return pieces

    def _segments_from_wrapper(self, wrapper_element, paragraph: Paragraph) -> List[RichSegment]:
        """把一个包装节点（hyperlink / ins / del / sdt）内的可见 run 渲染成段。

        超链接整体渲染成 `[锚文本](url)`：锚文本可能跨多个样式各异的 run，
        因此先按各 run 样式渲染再拼接，最外层再套一次链接语法（trusted_markup
        防止内部 Markdown 被二次转义）。修订按 accepted 视图过滤 w:del。
        """
        collected: list[tuple[Optional[str], str]] = []
        w_tag = wrapper_element.tag.split('}', 1)[-1] if isinstance(wrapper_element.tag, str) else ""
        root_url = self._resolve_hyperlink_target(wrapper_element) if w_tag == 'hyperlink' else None
        root_ins = w_tag == 'ins'
        root_del = w_tag == 'del'
        source = wrapper_element
        if w_tag == 'sdt':
            body = wrapper_element.find(f'{{{NS["w"]}}}sdtContent')
            if body is not None:
                source = body
        for inline in self._iter_paragraph_inline(
            source, hyperlink_url=root_url, in_ins=root_ins, in_del=root_del,
        ):
            if not self._inline_run_visible(inline):
                continue
            run_text = self._inline_run_text(inline)
            if not run_text:
                continue
            rendered = self._render_segments(
                [self._segment_from_resolved(
                    self._resolve_run(Run(inline.run_element, paragraph), -1, paragraph, run_text)
                )]
            )
            collected.append((inline.hyperlink_url, rendered))

        if not collected:
            return []

        # 相邻同一 URL 的片段合并成一个链接，避免把一个超链接拆成多段。
        segments: List[RichSegment] = []
        buf_url = collected[0][0]
        buf_text = ""
        for url, rendered in collected:
            if url == buf_url:
                buf_text += rendered
            else:
                segments.append(self._wrap_hyperlink(buf_url, buf_text))
                buf_url, buf_text = url, rendered
        segments.append(self._wrap_hyperlink(buf_url, buf_text))
        return segments

    @staticmethod
    def _wrap_hyperlink(url: Optional[str], rendered_text: str) -> RichSegment:
        if url:
            anchor = rendered_text.strip() or url
            return RichSegment(text=f"[{anchor}]({url})", trusted_markup=True)
        return RichSegment(text=rendered_text, trusted_markup=True)

    def _normalize_run_element_in_order(self, run_element, paragraph: Paragraph) -> List[RichSegment]:
        run = Run(run_element, paragraph)
        run_text = run.text
        base_segments = (
            [self._segment_from_resolved(self._resolve_run(run, -1, paragraph, run_text))]
            if run_text else []
        )
        if not self._run_has_inline_image(run_element):
            return base_segments

        segments: List[RichSegment] = []
        text_segments = iter(base_segments)
        for child in run_element:
            if child.tag == f'{{{NS["w"]}}}t':
                try:
                    segments.append(next(text_segments))
                except StopIteration:
                    if child.text:
                        segments.append(RichSegment(text=child.text))
            elif child.tag == f'{{{NS["w"]}}}drawing':
                image_block = self._extract_image_from_drawing(child, -1)
                if image_block:
                    segments.append(RichSegment(text=image_block.to_markdown(), trusted_markup=True))
            elif child.tag == f'{{{NS["w"]}}}pict':
                image_block = self._extract_image_from_pict(child, -1)
                if image_block:
                    segments.append(RichSegment(text=image_block.to_markdown(), trusted_markup=True))

        segments.extend(text_segments)
        return segments

    def _run_has_inline_image(self, run_element) -> bool:
        return run_element.find(f'.//{{{NS["w"]}}}drawing') is not None or run_element.find(f'.//{{{NS["w"]}}}pict') is not None

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
                    code=prev.code,
                    trusted_markup=prev.trusted_markup,
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
                    code=prev.code,
                    trusted_markup=prev.trusted_markup,
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

    @staticmethod
    def _escape_inline_text(text: str) -> str:
        """Escape source characters that could become Markdown/HTML syntax."""
        return re.sub(r"([\\`*_[\]~<>|])", r"\\\1", text)

    @classmethod
    def _render_segments(cls, segments: List[RichSegment]) -> str:
        """Render merged RichSegments into a Markdown string.

        Applies markers inside-out (strike → italic → bold) with
        Strip-Safe wrapping at each layer.
        """
        parts: List[str] = []

        for seg in segments:
            text = seg.text

            if seg.trusted_markup:
                parts.append(text)
                continue

            if seg.code:
                text = text.replace("`", "\\`")
            else:
                text = cls._escape_inline_text(text)

            if not seg.has_formatting:
                parts.append(text)
                continue

            # Apply markers inside-out so nesting is correct
            if seg.code:
                text = cls._wrap_safe(text, "`")
            if seg.strike:
                text = cls._wrap_safe(text, "~~")
            if seg.italic:
                text = cls._wrap_safe(text, "*")
            if seg.bold:
                text = cls._wrap_safe(text, "**")
            if seg.underline:
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

    def _detect_bold(self, resolved_runs: list[_ResolvedRun]) -> bool:
        """Detect paragraph boldness by effective non-whitespace character mass."""
        total_weight = 0
        bold_weight = 0
        for rr in resolved_runs:
            weight = self._visible_char_weight(rr.text)
            if not weight:
                continue
            total_weight += weight
            if rr.bold:
                bold_weight += weight
        return total_weight > 0 and bold_weight >= total_weight * 0.8

    def _detect_code_font(self, resolved_runs: list[_ResolvedRun]) -> bool:
        """Detect code paragraphs by effective monospace character mass (>=80%)."""
        total_weight = 0
        code_weight = 0
        for rr in resolved_runs:
            weight = self._visible_char_weight(rr.text)
            if not weight:
                continue
            total_weight += weight
            if rr.is_code:
                code_weight += weight
        return total_weight > 0 and code_weight >= total_weight * 0.8

    @staticmethod
    def _mono_fonts() -> frozenset[str]:
        return _MONO_FONTS

    def _get_run_font_name(self, run) -> Optional[str]:
        if run.font and run.font.name:
            return run.font.name

        try:
            r_fonts = run._element.rPr.rFonts
            for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                value = getattr(r_fonts, attr, None)
                if value:
                    return value
        except Exception:
            pass

        return None
    
    def _detect_font_size(
        self,
        paragraph: Paragraph,
        resolved_runs: list[_ResolvedRun],
    ) -> Optional[float]:
        """Return the effective font size carrying the most visible characters."""
        from collections import Counter

        sizes: Counter[float] = Counter()
        for rr in resolved_runs:
            weight = self._visible_char_weight(rr.text)
            if not weight:
                continue
            if rr.font_size is not None and rr.font_size > 0:
                sizes[round(rr.font_size, 1)] += weight
        if sizes:
            return sizes.most_common(1)[0][0]

        for style in self._paragraph_chain(paragraph):
            try:
                if style.font.size is not None:
                    return float(style.font.size.pt)
            except Exception:
                continue
        return None
    
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
    
    def _process_table(
        self, table: Table, block_id: int, *, depth: int = 0, parent_ref: Optional[dict] = None
    ) -> List[Block]:
        """处理表格 → [父表 Block, 内层表 Block, ...]（收掉 B04 嵌套表遗留缺口）。

        B04 基础修复已算对 row_span（见 _parse_table_grid）。本次 E05(3) 再补两块：

        1) **cell 内嵌内容**：单元格不再只提 cell.paragraphs 的富文本——同时探测
           图片 / 公式 / 引用（脚注/尾注/书签/内部链接）/ 嵌套表，把它们记进
           cell['content']，让下游知道这格里除了文字还有什么。

        2) **嵌套表真正入块**（原缺口："嵌套表内层内容仍不入块"）：内层表不再被
           丢弃，而是递归成**独立 Block 追加到块流**，并与父格双向建 span 关系
           （父格记 nested_table_block_ids，子表记 parent_table {block_id,row,col}）。
           绝不把内层文字拼进父格文字冒充结构——那正是方案点名的反模式。

        返回列表：``[父表, 内层表1, 内层表1的更内层表, 内层表2, ...]``（深度优先，
        保持"父在前、子紧随"的阅读顺序）。空表返回 ``[]``。
        """
        try:
            grid = self._parse_table_grid(table._tbl)
            try:
                grid_cells = table._cells
            except Exception:
                grid_cells = None

            # tc → python-docx Cell，用于取直属段落富文本与嵌套表 / 内嵌内容探测。
            cell_by_tc: Dict[int, object] = {}
            if grid_cells is not None:
                for cell in grid_cells:
                    cell_by_tc.setdefault(id(cell._tc), cell)

            def cell_own_text(tc) -> str:
                """只取该格**直属**段落的富文本（不含嵌套表——嵌套表另成块）。"""
                cell = cell_by_tc.get(id(tc))
                if cell is not None:
                    rich_parts = []
                    for cell_paragraph in cell.paragraphs:
                        rendered = self._extract_rich_text(cell_paragraph).strip()
                        if rendered:
                            rich_parts.append(rendered)
                    if rich_parts:
                        return "\n".join(rich_parts).strip()
                    # 直属段落无文字：用 tc 直属 w:p 的 w:t 兜底（避免抓到嵌套表文字）。
                    return self._tc_direct_text(tc)
                return self._tc_direct_text(tc)

            cells_info: list[dict] = []
            n_cols = grid["n_cols"]
            n_rows = len(grid["rows"])
            gfm_grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]

            nested_blocks: List[Block] = []
            # 父表自身的引用锚（供内层表回指）：block_id 稍后由调用方回填，
            # 这里先用传入的 block_id（调用方保证父表拿到该 id）。
            self_ref = {"block_id": block_id}
            has_inline_content = False

            for origin in grid["origins"]:
                tc = origin["tc"]
                cell = cell_by_tc.get(id(tc))
                text = cell_own_text(tc)
                r, c = origin["row"], origin["col"]
                if r < n_rows and c < n_cols:
                    gfm_grid[r][c] = text

                # --- E05(3) cell 内嵌内容探测 ---
                content = self._extract_cell_inline_content(tc, cell)
                cell_record = {
                    "row": r,
                    "col": c,
                    "text": text,
                    "row_span": origin["row_span"],
                    "col_span": origin["col_span"],
                    "vmerge": origin["vmerge"],
                    "covered": origin["covered"],
                }
                if content:
                    cell_record["content"] = content
                    has_inline_content = True

                # --- 嵌套表：递归成独立 Block，与父格建 span 关系 ---
                nested_tables = self._cell_nested_tables(tc, table)
                nested_ids: list[int] = []
                for nested_table in nested_tables:
                    child_block_id = block_id + 1 + len(nested_blocks)
                    child_parent_ref = {
                        "block_id": self_ref["block_id"],
                        "row": r,
                        "col": c,
                    }
                    produced = self._process_table(
                        nested_table, child_block_id,
                        depth=depth + 1, parent_ref=child_parent_ref,
                    )
                    if produced:
                        nested_ids.append(produced[0].id)
                        nested_blocks.extend(produced)
                if nested_ids:
                    cell_record["nested_table_block_ids"] = nested_ids

                # --- 单元格内的嵌入电子表格对象 ---
                # 不能无位置地追加到外层表格之后（方案 §3.2）：单元格里引用标记 +
                # 外层表格后紧邻的子表块，靠父表 ID + 行列坐标建立关联，展示与导出据此还原。
                embedded_ids: list[int] = []
                for cell_para in self._cell_paragraphs(tc, table):
                    if not self._paragraph_has_embedded_object(cell_para._element):
                        continue
                    for event_index, (_text, obj) in enumerate(
                        self._split_paragraph_by_objects(cell_para)
                    ):
                        if obj is None:
                            continue
                        child_block_id = block_id + 1 + len(nested_blocks)
                        position = _emb.SourcePosition(
                            element_index=self_ref["block_id"],
                            event_index=event_index,
                            parent_table_block_id=self_ref["block_id"],
                            row=r,
                            col=c,
                            cell_event_index=event_index,
                        )
                        produced = self._blocks_for_embedded_object(
                            obj, position, child_block_id, part=self._doc_part
                        )
                        for offset, produced_block in enumerate(produced):
                            produced_block.id = child_block_id + offset
                            embedded_ids.append(produced_block.id)
                            nested_blocks.append(produced_block)
                if embedded_ids:
                    cell_record["embedded_spreadsheet_block_ids"] = embedded_ids
                    has_inline_content = True

                cells_info.append(cell_record)

            rows = gfm_grid
            if not rows:
                return []

            markdown_table = self._table_to_markdown(rows)
            has_vertical_merge = any(cell["row_span"] > 1 for cell in cells_info)
            # E04：逻辑单元格模型是表格的权威表示，GFM 只是派生的兼容视图。
            # DOCX 这边跨度、表头区域、单元格内嵌块都已经算好了，直接落成模型即可；
            # 此前这些信息只存在于 metadata['cells'] 这份自定义结构里，消费方要各自解读。
            table_model = self._build_table_model(cells_info, n_rows, n_cols, rows)
            metadata = {
                "source": "docx",
                "cells": cells_info,
                "artifact": False,
                "in_table": True,
                "canonical": True,
                "grid_size": {"rows": n_rows, "cols": n_cols},
                "gfm_downgrade": {
                    "spans_representable": False,
                    "covered_cells_emptied": True,
                    "has_vertical_merge": has_vertical_merge,
                },
                "table_depth": depth,
                "has_inline_content": has_inline_content,
            }
            if parent_ref is not None:
                # 内层表回指父格：跨度关系双向可还原。
                metadata["parent_table"] = parent_ref
            if nested_blocks:
                metadata["has_nested_tables"] = True

            parent_block = Block(
                id=block_id,
                type="table",
                text=markdown_table,
                metadata=metadata,
                table_data={"rows": rows, "model": table_model.model_dump()},
            )
            return [parent_block, *nested_blocks]
        except Exception as e:
            logger.warning(f"处理表格时出错: {str(e)}")
            # 返回 [] 会让这张表在块流里彻底消失——比丢一个段落更严重，
            # 因为表格通常承载字段定义。必须让调用方看见。
            self._note_skip(
                "docx_table_failed",
                "文档中有一张表格无法解析，整张表未纳入结果。",
            )
            return []

    def _tc_direct_text(self, tc) -> str:
        """取 w:tc **直属** w:p 里的 w:t 文本（跳过嵌套 w:tbl 内的文字）。

        不能用 ``tc.iter(w:t)``：那会把嵌套表单元格的文字也抓进来，导致父格文字
        污染（嵌套表内容应另成块，不能拼进父格冒充结构）。故只遍历直属段落。
        """
        w = NS["w"]
        parts = []
        for p in tc.findall(f'{{{w}}}p'):
            parts.append("".join(t.text or "" for t in p.iter(f'{{{w}}}t')))
        return "".join(parts).strip()

    def _cell_nested_tables(self, tc, parent_table: Table) -> List[Table]:
        """取该 w:tc **直属**的嵌套 w:tbl（不含更深层——递归会处理更深）。"""
        w = NS["w"]
        nested: List[Table] = []
        for child in tc:
            raw = child.tag
            if isinstance(raw, str) and raw == f'{{{w}}}tbl':
                try:
                    nested.append(Table(child, parent_table))
                except Exception:  # noqa: BLE001
                    continue
        return nested

    def _cell_paragraphs(self, tc, parent_table: Table) -> List[Paragraph]:
        """取该 ``w:tc`` **直属**的段落（不下钻嵌套表——那由嵌套表自己的递归处理）。

        与 :meth:`_cell_nested_tables` 同一取法：直属子节点判定，避免把内层表的段落
        当成本格内容，那会让同一段被算两遍。
        """
        w = NS["w"]
        out: List[Paragraph] = []
        for child in tc:
            raw = child.tag
            if isinstance(raw, str) and raw == f'{{{w}}}p':
                try:
                    out.append(Paragraph(child, parent_table))
                except Exception:  # noqa: BLE001
                    continue
        return out

    def _extract_cell_inline_content(self, tc, cell) -> list[dict]:
        """探测单元格直属段落里的图片 / 公式 / 引用，记为结构化 content 列表。

        只扫直属 w:p（不下钻嵌套表），保证 content 描述的是"这一格里有什么"，
        而不是把内层表的东西也算进来。图片保留 embed_id、公式带 formula_fidelity、
        引用保留 note_id / anchor —— 全部是"结构信息进 metadata"，不改 Block 字段。
        """
        w = NS["w"]
        m = NS["m"]
        content: list[dict] = []
        for p in tc.findall(f'{{{w}}}p'):
            # 图片
            if p.find(f'.//{{{w}}}drawing') is not None or p.find(f'.//{{{w}}}pict') is not None:
                for blip in p.findall(f'.//{{{NS["a"]}}}blip'):
                    embed = blip.get(f'{{{NS["r"]}}}embed')
                    if embed:
                        content.append({"kind": "image", "embed_id": embed})
                for imagedata in p.findall(f'.//{{{NS["v"]}}}imagedata'):
                    embed = imagedata.get(f'{{{NS["r"]}}}id')
                    if embed:
                        content.append({"kind": "image", "embed_id": embed})
            # 行内公式
            for math_elem in p.findall(f'.//{{{m}}}oMath'):
                parent = math_elem.getparent()
                if parent is not None and parent.tag == f'{{{m}}}oMathPara':
                    continue
                latex = self._omml_to_text(math_elem)
                content.append({
                    "kind": "formula",
                    "latex": latex,
                    "formula_fidelity": self._classify_formula_fidelity(math_elem, latex),
                })
            for math_para in p.findall(f'.//{{{m}}}oMathPara'):
                latex = self._omml_to_text(math_para)
                content.append({
                    "kind": "formula",
                    "latex": latex,
                    "formula_fidelity": self._classify_formula_fidelity(math_para, latex),
                })
            # 脚注 / 尾注引用
            for fn in p.findall(f'.//{{{w}}}footnoteReference'):
                content.append({"kind": "footnote_ref", "note_id": fn.get(f'{{{w}}}id')})
            for en in p.findall(f'.//{{{w}}}endnoteReference'):
                content.append({"kind": "endnote_ref", "note_id": en.get(f'{{{w}}}id')})
            # 内部引用（指向书签）
            for hl in p.findall(f'.//{{{w}}}hyperlink'):
                anchor = hl.get(f'{{{w}}}anchor')
                if anchor:
                    content.append({"kind": "internal_ref", "anchor": anchor})
        return content

    def _build_table_model(
        self, cells_info: list[dict], n_rows: int, n_cols: int, rows: list[list[str]]
    ) -> "TableModel":
        """把已算好的网格落成逻辑单元格模型（E04）。

        只取**起始格**：``cells_info`` 本来就只由 ``grid['origins']`` 构成，被合并覆盖的
        位置不单独成条，覆盖关系由 ``row_span`` / ``col_span`` 表达。注意其中的
        ``covered`` 字段是「**本格覆盖了哪些位置**」的列表，不是「本格被覆盖」的布尔——
        照后者理解会把合并起始格整个丢掉（第一版就踩了，覆盖不变量立刻判红）。

        表头区域按 DOCX 惯例取首行——DOCX 没有可靠的「表头行数」声明，取一行是保守判断，
        不假装识别出了多行表头。
        """
        from infrastructure.models import LogicalCell, TableModel

        logical: list[LogicalCell] = []
        for cell in cells_info:
            row, col = int(cell["row"]), int(cell["col"])
            value = str(cell.get("text") or "")
            logical.append(LogicalCell(
                row=row,
                col=col,
                row_span=max(1, int(cell.get("row_span") or 1)),
                col_span=max(1, int(cell.get("col_span") or 1)),
                raw_value=value,
                display_value=value,
                is_header=row == 0,
                block_ids=[
                    *(cell.get("nested_table_block_ids") or []),
                    *(cell.get("embedded_spreadsheet_block_ids") or []),
                ],
            ))
        return TableModel(
            n_rows=n_rows,
            n_cols=n_cols,
            cells=logical,
            header_rows=1 if n_rows else 0,
            total_rows=n_rows,
        )

    def _parse_table_grid(self, tbl_element) -> dict:
        """从真实 w:tr/w:tc 树解析逻辑网格，算出每个起始格的真实 row_span。

        为什么不用 python-docx 的 _cells：它把 vMerge=continue 的延续格映射回
        起始格，纵向延续关系彻底不可见（B04 根因）。这里按 OOXML 语义自建网格：
        - gridSpan → 该 tc 占据的网格列数（col_span）；
        - vMerge=restart（或带文字的起始）开启一段纵向合并；
        - vMerge=continue 把上方同列的起始格 row_span 加一，自身为被覆盖位置。
        occupancy 记录被上一行纵向合并延续下来的列，保证列坐标对齐。
        """
        w = NS["w"]
        tr_elements = tbl_element.findall(f'{{{w}}}tr')
        n_rows = len(tr_elements)

        # active[col] = 当前正在纵向延续的起始格记录（None 表示该列空闲）。
        active: Dict[int, dict] = {}
        origins: list[dict] = []
        # 每行占据的最大列数，用于推断总列宽。
        max_cols = 0

        for r_idx, tr in enumerate(tr_elements):
            col = 0
            for tc in tr.findall(f'{{{w}}}tc'):
                tc_pr = tc.find(f'{{{w}}}tcPr')
                col_span = 1
                vmerge_val = 'none'
                if tc_pr is not None:
                    grid_span = tc_pr.find(f'{{{w}}}gridSpan')
                    if grid_span is not None:
                        try:
                            col_span = int(grid_span.get(f'{{{w}}}val', '1'))
                        except (TypeError, ValueError):
                            col_span = 1
                    vmerge = tc_pr.find(f'{{{w}}}vMerge')
                    if vmerge is not None:
                        val = vmerge.get(f'{{{w}}}val')
                        vmerge_val = val if val else 'continue'

                if vmerge_val == 'continue':
                    # 被覆盖位置：找同列（col 起点）正在延续的起始格并延长其跨度。
                    origin = active.get(col)
                    if origin is not None:
                        origin["row_span"] += 1
                        for cc in range(col, col + col_span):
                            origin["covered"].append({"row": r_idx, "col": cc})
                    col += col_span
                    continue

                origin = {
                    "tc": tc,
                    "row": r_idx,
                    "col": col,
                    "col_span": col_span,
                    "row_span": 1,
                    "vmerge": vmerge_val,
                    "covered": [],
                }
                # gridSpan 横跨的其余列也是被覆盖位置（与纵向覆盖同等对待），
                # 下游还原矩形跨度时需要完整的覆盖集，不能只有纵向那部分。
                for cc in range(col + 1, col + col_span):
                    origin["covered"].append({"row": r_idx, "col": cc})
                origins.append(origin)
                if vmerge_val == 'restart':
                    # 起始格：登记为该 col 段的活跃纵向合并，后续 continue 挂到它。
                    for cc in range(col, col + col_span):
                        active[cc] = origin
                else:
                    # 普通格：清掉这些列上遗留的活跃合并（合并到此结束）。
                    for cc in range(col, col + col_span):
                        active.pop(cc, None)
                col += col_span
            max_cols = max(max_cols, col)

        return {"rows": tr_elements, "origins": origins, "n_cols": max_cols}
    
    def _table_to_markdown(self, rows: List[List[str]]) -> str:
        """将表格数据渲染为 GFM 表格。

        委托全管道唯一入口 :meth:`Block.render_markdown_table`：按最大列宽对齐、
        **超宽行不截断**（旧实现以表头列数截断会丢列）、单元格内换行 → ``<br>``、
        竖线转义。
        """
        return Block.render_markdown_table(rows)
    
    def _extract_images_from_paragraph(
        self, paragraph: Paragraph, start_block_id: int
    ) -> List[Block]:
        """从段落中提取图片"""
        image_blocks = []
        block_id = start_block_id

        # 常见情形是段落根本没有图片；两次子树查找即可跳过逐 run 扫描。
        if not self._paragraph_has_inline_image(paragraph._element):
            return image_blocks

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
                            self._note_skip(
                                "docx_image_failed",
                                "文档中有一张图片无法提取，该图缺失。",
                                source_span={"block_id": block_id},
                            )
                    
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
            image_blob: Optional[bytes] = None
            image_mime = "image/png"
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
                        image_blob = blob
                        image_mime = content_type
                except Exception as e:
                    logger.debug(f"提取图片实体数据失败，使用占位符: {str(e)}")
            
            caption = ""
            ns_wp = f'{{{NS["wp"]}}}'
            desc_elem = drawing.find(f'.//{ns_wp}docPr')
            if desc_elem is None:
                desc_elem = drawing.find(f'.//{ns_a}docPr')
            if desc_elem is not None:
                caption = desc_elem.get('descr', '') or desc_elem.get('name', '')
            
            block = Block(
                id=block_id,
                type="image",
                image_data=image_data_str,
                caption=caption,
                metadata={"embed_id": embed_id, "uuid": image_uuid},
            )
            # 走 AssetStore 入口时收编字节（P05）：DOCX 里同一 embed_id 被复用
            # （公司 logo、重复插图）时字节按内容哈希去重，image_data 仍照填以兼容。
            if image_blob is not None:
                self._register_image_asset(block, content=image_blob, mime=image_mime)
            return block
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
