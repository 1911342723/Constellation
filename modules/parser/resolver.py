"""Interval Resolver — Cursor-Caliper Stage 4.

Converts LLM-produced heading anchors back into a lossless document
tree by performing three sequential operations:

1. **Fuzzy anchor correction** — cross-validates each ``block_id``
   against its ``snippet`` using Levenshtein distance and auto-corrects
   off-by-one (or larger) errors within a configurable search radius.
2. **Hierarchy validation** — detects and repairs level jumps (e.g.
   L1 → L3) to guarantee a well-formed tree with no gaps.
3. **Forced-closure interval slicing** — computes non-overlapping,
   exhaustive ``[start, end]`` intervals and assembles lossless
   Markdown content for each section.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from infrastructure.models import Block
from modules.parser.schemas import ChapterNode, DocumentNode
from modules.parser.config import ResolverConfig
from app.core.exceptions import AssemblerError

import logging
import re

logger = logging.getLogger(__name__)


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein similarity ratio (0.0–1.0, 1.0 = identical).

    Uses the C extension ``python-Levenshtein`` when available;
    falls back to a pure-Python two-row DP implementation otherwise.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    
    try:
        from rapidfuzz.distance import Levenshtein as RapidFuzzLevenshtein
        return RapidFuzzLevenshtein.normalized_similarity(s1, s2)
    except ImportError:
        pass
    
    try:
        from Levenshtein import ratio
        return ratio(s1, s2)
    except ImportError:
        pass
    
    # 纯 Python 降级实现
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return 0.0
    if len2 == 0:
        return 0.0
    
    # 优化：只保留两行
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    
    for i in range(1, len1 + 1):
        curr[0] = i
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,       # 删除
                curr[j - 1] + 1,   # 插入
                prev[j - 1] + cost  # 替换
            )
        prev, curr = curr, prev
    
    distance = prev[len2]
    max_len = max(len1, len2)
    return 1.0 - (distance / max_len)


class IntervalResolver:
    """Resolve LLM anchors into a lossless document tree.

    Pipeline: fuzzy anchor correction → hierarchy validation →
    forced-closure interval slicing → Markdown assembly → tree build.
    """
    
    def __init__(self, blocks: List[Block], config: ResolverConfig | None = None):
        cfg = config or ResolverConfig()
        self.blocks = blocks
        self.block_map = {block.id: block for block in blocks}
        self.total_blocks = len(blocks)
        self.max_block_id = max(b.id for b in blocks) if blocks else 0
        self.fuzzy_radius = cfg.fuzzy_anchor_radius
        self.fuzzy_min_similarity = cfg.fuzzy_min_similarity
        self.anchor_match_min_length = cfg.anchor_match_min_length
        self.anchor_match_levenshtein_threshold = cfg.anchor_match_levenshtein_threshold
        self.level_jump_font_size_tolerance = cfg.level_jump_font_size_tolerance
    
    def resolve(self, chapters: List[ChapterNode]) -> List[DocumentNode]:
        """Resolve *chapters* into a tree of :class:`DocumentNode`.

        Applies fuzzy correction, hierarchy repair, forced-closure
        slicing, and stack-based tree construction in sequence.

        Raises:
            AssemblerError: If *chapters* is empty or resolution fails.
        """
        if not chapters:
            raise AssemblerError("章节列表为空，无法解析")
        
        try:
            # 按 block_id 排序
            chapters = sorted(chapters, key=lambda x: x.start_block_id)
            
            # v2: 模糊锚定纠偏
            chapters = self._fuzzy_anchor_correction(chapters)
            
            # v2: 层级合规性修复
            chapters = self._validate_hierarchy(chapters)
            
            # 验证锚点有效性
            self._validate_anchors(chapters)
            
            # 去重（纠偏后可能出现重复 block_id）
            chapters = self._deduplicate_anchors(chapters)
            
            # 强制闭合切割
            intervals = self._compute_intervals(chapters)
            
            # 提取内容并创建 DocumentNode
            flat_nodes = self._build_flat_nodes(intervals)
            
            # 构建树状结构
            tree_nodes = self._build_tree(flat_nodes)
            
            logger.info(f"[区间解析] 完成：{len(flat_nodes)} 个章节 → {len(tree_nodes)} 个顶级节点")
            
            return tree_nodes
            
        except AssemblerError:
            raise
        except Exception as e:
            raise AssemblerError(f"区间解析失败: {str(e)}")
    
    # ================================================================
    # v2 新增：模糊锚定纠偏 (Fuzzy Anchoring Correction)
    # ================================================================
    
    def _fuzzy_anchor_correction(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Cross-validate each anchor's ``block_id`` against its ``snippet``.

        Mismatches trigger a sliding-window search within ``±fuzzy_radius``
        blocks using Levenshtein distance to find the correct anchor.
        """
        corrected = []
        correction_count = 0
        
        for ch in chapters:
            original_id = ch.start_block_id
            snippet = ch.snippet.strip() if ch.snippet else ch.title.strip()
            
            if not snippet:
                corrected.append(ch)
                continue
            
            # 检查当前 block_id 是否匹配
            if self._is_anchor_match(original_id, snippet):
                corrected.append(ch)
                continue
            
            # 不匹配 → 在滑轨区间内搜索最佳匹配
            best_id, best_score = self._search_best_match(original_id, snippet)
            
            if best_id is not None and best_id != original_id:
                logger.warning(
                    f"[模糊锚定] 纠偏: block_id {original_id} → {best_id} "
                    f"(相似度: {best_score:.2f}, snippet: '{snippet[:30]}...')"
                )
                ch.start_block_id = best_id
                correction_count += 1
            elif best_id is None:
                logger.warning(
                    f"[模糊锚定] 无法纠偏 block_id={original_id}, "
                    f"snippet='{snippet[:30]}...', 保持原值"
                )
            
            corrected.append(ch)
        
        if correction_count > 0:
            logger.info(f"[模糊锚定] 共纠偏 {correction_count} 个锚点")
        else:
            logger.info("[模糊锚定] 所有锚点验证通过，无需纠偏")
        
        return corrected
    
    def _is_anchor_match(self, block_id: int, snippet: str) -> bool:
        """Return ``True`` if the block at *block_id* matches *snippet*.

        For ultra-short snippets (≤ 5 chars), only exact substring
        matching is used because Levenshtein distance is unreliable
        on short strings (a single-char difference can swing the
        ratio from 1.0 to 0.5).
        """
        if block_id not in self.block_map:
            return False
        
        block = self.block_map[block_id]
        block_text = (block.text or "").strip()
        
        if not block_text:
            return False
        
        # Exact substring / prefix check (always performed)
        if snippet in block_text or block_text.startswith(snippet[:20]):
            return True
        
        # Ultra-short snippets: exact match only — Levenshtein is too noisy
        if len(snippet) <= self.anchor_match_min_length:
            return False
        
        # Levenshtein fuzzy check for longer snippets
        block_head = block_text[:len(snippet) + 10]
        ratio = _levenshtein_ratio(snippet.lower(), block_head.lower())
        return ratio >= self.anchor_match_levenshtein_threshold
    
    def _search_best_match(self, center_id: int, snippet: str) -> Tuple[Optional[int], float]:
        """Search ``[center_id ± radius]`` for the best Levenshtein match.

        Returns:
            ``(block_id, score)`` of the best match, or ``(None, 0.0)``.
        """
        best_id = None
        best_score = 0.0
        
        search_start = max(0, center_id - self.fuzzy_radius)
        search_end = min(self.max_block_id, center_id + self.fuzzy_radius)
        
        for bid in range(search_start, search_end + 1):
            if bid not in self.block_map:
                continue
            
            block = self.block_map[bid]
            block_text = (block.text or "").strip()
            
            if not block_text:
                continue
            
            # 取 block 文本前 N 字与 snippet 比较
            block_head = block_text[:len(snippet) + 10]
            score = _levenshtein_ratio(snippet.lower(), block_head.lower())
            
            # 精确包含加分
            if snippet in block_text or block_text.startswith(snippet[:15]):
                score = max(score, 0.95)
                
            # 位置相对跨度惩罚 (Position Relative Penalty)
            # 如果偏移量过大（例如超过 radius 的一半），基于偏移比例对 score 进行轻微衰减
            # 这样可以在有多个雷同段落（例如“本章小结”）时，优先选择最近的那一个
            offset = abs(bid - center_id)
            if offset > (self.fuzzy_radius * 0.3) and score < 1.0:
                penalty = (offset / self.fuzzy_radius) * 0.1  # 最大惩罚 0.1
                score -= penalty
            
            if score > best_score:
                best_score = score
                best_id = bid
        
        if best_score >= self.fuzzy_min_similarity:
            return best_id, best_score
        
        return None, 0.0
    
    # ================================================================
    # v2 新增：层级合规性修复 (Hierarchy Validation)
    # ================================================================
    
    def _validate_hierarchy(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Repair level jumps to guarantee a well-formed heading tree.

        Rules: first chapter is forced to level 1; subsequent chapters
        may increase by at most 1 level; decreases are always legal.

        Physical-feature assist: when a jump is detected, the block's
        ``font_size`` is compared against the nearest same-level
        ancestor.  If the sizes match, the node is promoted to that
        ancestor's level instead of being blindly clamped to
        ``max_allowed``.
        """
        if not chapters:
            return chapters
        
        fixed = []
        fix_count = 0
        
        # 第一个章节强制为 level 1
        first = chapters[0]
        if first.level != 1:
            logger.warning(
                f"[层级修复] 首章节 '{first.title}' level={first.level} → 1"
            )
            first.level = 1
            fix_count += 1
        fixed.append(first)
        
        # level → font_size mapping (for physical-feature cross-check)
        level_font: dict[int, float] = {}
        blk = self.block_map.get(first.start_block_id)
        if blk and blk.font_size:
            level_font[first.level] = blk.font_size
        
        # 维护当前已出现的最大层级栈
        level_stack = [first.level]
        
        for ch in chapters[1:]:
            # 当前允许的最大层级 = 栈顶 + 1
            max_allowed = level_stack[-1] + 1
            
            if ch.level > max_allowed:
                old_level = ch.level
                # Physical-feature assist: check if font_size matches
                # an existing level (the LLM may have typed the wrong
                # number but the physical signal is correct).
                resolved_level = max_allowed  # default: clamp
                blk = self.block_map.get(ch.start_block_id)
                if blk and blk.font_size and level_font:
                    for lv in sorted(level_font.keys()):
                        if abs(blk.font_size - level_font[lv]) <= self.level_jump_font_size_tolerance:
                            resolved_level = lv
                            break
                
                ch.level = resolved_level
                logger.warning(
                    f"[层级修复] '{ch.title}' level={old_level} → {ch.level} "
                    f"(跳跃修复，前一层级为 {level_stack[-1]}"
                    f"{', 字号辅助' if resolved_level != max_allowed else ''})"
                )
                fix_count += 1
            
            # Record font_size for this level
            blk = self.block_map.get(ch.start_block_id)
            if blk and blk.font_size and ch.level not in level_font:
                level_font[ch.level] = blk.font_size
            
            # 更新层级栈
            if ch.level > level_stack[-1]:
                level_stack.append(ch.level)
            else:
                # 回退：弹出栈中所有 >= 当前 level 的
                while level_stack and level_stack[-1] >= ch.level:
                    level_stack.pop()
                level_stack.append(ch.level)
            
            fixed.append(ch)
        
        if fix_count > 0:
            logger.info(f"[层级修复] 共修复 {fix_count} 个层级跳跃")
        else:
            logger.info("[层级修复] 层级结构合规，无需修复")
        
        return fixed
    
    # ================================================================
    # 原有逻辑（优化版）
    # ================================================================
    
    def _validate_anchors(self, chapters: List[ChapterNode]):
        """Clamp out-of-range ``block_id`` values to ``[0, max_block_id]``."""
        for ch in chapters:
            if ch.start_block_id < 0 or ch.start_block_id > self.max_block_id:
                logger.warning(
                    f"[区间解析] 锚点 block_id={ch.start_block_id} 超出范围 "
                    f"(0~{self.max_block_id})，将修正"
                )
                ch.start_block_id = max(0, min(ch.start_block_id, self.max_block_id))
    
    def _deduplicate_anchors(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Remove duplicate ``block_id`` entries, keeping the first occurrence."""
        seen = set()
        result = []
        for ch in chapters:
            if ch.start_block_id not in seen:
                seen.add(ch.start_block_id)
                result.append(ch)
            else:
                logger.warning(
                    f"[去重] 移除重复锚点: block_id={ch.start_block_id}, title='{ch.title}'"
                )
        return result
    
    def _compute_intervals(self, chapters: List[ChapterNode]) -> List[dict]:
        """Compute forced-closure ``[start_id, end_id]`` intervals and perform Inverse Audit."""
        intervals = []
        
        for i, chapter in enumerate(chapters):
            start_id = chapter.start_block_id
            
            if i + 1 < len(chapters):
                end_id = chapters[i + 1].start_block_id - 1
            else:
                end_id = self.max_block_id
            
            if end_id < start_id:
                end_id = start_id
            
            intervals.append({
                "chapter": chapter,
                "start_id": start_id,
                "end_id": end_id,
            })
            
        # Inverse Audit: 探测被闭合吞并的异常段落
        self._inverse_audit_intervals(intervals)
        
        return intervals
        
    def _inverse_audit_intervals(self, intervals: List[dict]):
        """反向特征审计: 检查每一个强制闭合区间的内部片段。
        如果在区间内发现某个普通文本的字号异常大于(或等于且加粗)当前章节的标题，
        极大概念是 LLM 漏标（Attention Drop）导致标题被作为正文吞并，此处将输出强警告。
        """
        audit_count = 0
        for interval in intervals:
            chapter = interval["chapter"]
            start_id = interval["start_id"]
            end_id = interval["end_id"]
            
            # 获取本章节锚点本身的字号
            anchor_block = self.block_map.get(start_id)
            if not anchor_block or not anchor_block.font_size:
                continue
                
            anchor_size = anchor_block.font_size
            
            # 扫描被闭合的下属内容 [start_id + 1, end_id]
            for bid in range(start_id + 1, end_id + 1):
                child_block = self.block_map.get(bid)
                if not child_block or child_block.type != "text" or not child_block.text:
                    continue
                    
                # 预警条件：该下属文本的字号 > 锚点字号，或者是完全相等的字号但具备更强烈的视觉强调（全粗体，且文本短小像个标题）
                child_size = child_block.font_size or 0
                is_larger = child_size > anchor_size + 0.5
                is_same_but_bold = (
                    abs(child_size - anchor_size) <= 0.5 
                    and child_block.is_bold 
                    and not anchor_block.is_bold
                    and len(child_block.text) < 40  # 类似标题
                )
                
                if (is_larger or is_same_but_bold) and child_block.is_potential_title():
                    logger.warning(
                        f"[反向特征审计] 吞词孤岛预警！章节 '{chapter.title[:20]}' "
                        f"(Size: {anchor_size}) 吞并了下方可能漏标的隐藏标题: "
                        f"ID={bid}, '{child_block.text[:20]}...' (Size: {child_size})"
                    )
                    audit_count += 1
                    
        if audit_count > 0:
            logger.warning(f"[反向特征审计] 发现 {audit_count} 处潜在的 LLM 漏标吞词现象。建议重点核对。")
    
    def _build_flat_nodes(self, intervals: List[dict]) -> List[DocumentNode]:
        """Extract content for each interval and build flat :class:`DocumentNode` list."""
        nodes = []
        
        for interval in intervals:
            chapter = interval["chapter"]
            start_id = interval["start_id"]
            end_id = interval["end_id"]
            
            section_type = self._infer_section_type(chapter.title)
            content = self._extract_content(start_id, end_id, skip_title_id=start_id)
            
            node = DocumentNode(
                title=chapter.title,
                level=chapter.level,
                start_block_id=start_id,
                end_block_id=end_id,
                content=content,
                children=[],
                section_type=section_type,
            )
            
            nodes.append(node)
        
        return nodes
    
    def _build_tree(self, flat_nodes: List[DocumentNode]) -> List[DocumentNode]:
        """Build a tree from flat nodes using a stack-based algorithm (O(n))."""
        root_nodes: List[DocumentNode] = []
        stack: List[Tuple[int, DocumentNode]] = []
        
        for node in flat_nodes:
            level = node.level
            
            while stack and stack[-1][0] >= level:
                stack.pop()
            
            if not stack:
                root_nodes.append(node)
            else:
                _, parent = stack[-1]
                parent.children.append(node)
            
            stack.append((level, node))
        
        return root_nodes
    
    def _extract_content(
        self, 
        start_id: int, 
        end_id: int, 
        skip_title_id: Optional[int] = None
    ) -> str:
        """Losslessly render blocks ``[start_id, end_id]`` as Markdown."""
        content_parts = []
        
        for block_id in range(start_id, end_id + 1):
            if block_id not in self.block_map:
                continue
            
            block = self.block_map[block_id]
            
            if block_id == skip_title_id and block.type == "text":
                continue
            
            markdown = block.to_markdown()
            if markdown:
                content_parts.append(markdown)
        
        return "\n\n".join(content_parts)
    
    def _infer_section_type(self, title: str) -> str:
        """Infer semantic section type from the heading title."""
        lower_title = title.lower().strip()
        
        if any(kw in lower_title for kw in ["abstract", "摘要"]):
            return "abstract"
        elif any(kw in lower_title for kw in ["reference", "参考文献", "bibliography"]):
            return "reference"
        elif any(kw in lower_title for kw in ["appendix", "附录"]):
            return "appendix"
        elif any(kw in lower_title for kw in ["acknowledgment", "致谢"]):
            return "acknowledgment"
        
        return "section"
    
    def get_preamble_blocks(self, first_chapter_start_id: int) -> List[Block]:
        """Return blocks before the first chapter (title page, metadata, etc.)."""
        preamble = []
        for block in self.blocks:
            if block.id < first_chapter_start_id:
                preamble.append(block)
            else:
                break
        return preamble
