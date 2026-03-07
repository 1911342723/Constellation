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

import copy
from typing import List, Optional, Tuple

from infrastructure.models import Block
from modules.parser.schemas import ChapterNode, DocumentNode
from modules.parser.config import ResolverConfig
from app.core.exceptions import AssemblerError

import logging

logger = logging.getLogger(__name__)


# ── Levenshtein implementation resolution (once at module load) ──

def _pure_python_levenshtein(s1: str, s2: str) -> float:
    """Pure-Python two-row DP Levenshtein similarity ratio."""
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    for i in range(1, len1 + 1):
        curr[0] = i
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    distance = prev[len2]
    return 1.0 - (distance / max(len1, len2))


def _resolve_levenshtein_impl():
    """Resolve the fastest available Levenshtein backend once."""
    try:
        from rapidfuzz.distance import Levenshtein as _RL
        return _RL.normalized_similarity
    except ImportError:
        pass
    try:
        from Levenshtein import ratio
        return ratio
    except ImportError:
        pass
    return _pure_python_levenshtein


_lev_impl = _resolve_levenshtein_impl()


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein similarity ratio (0.0-1.0, 1.0 = identical).

    The underlying C-extension backend is resolved once at module load
    to avoid repeated try/except import overhead on the hot path.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    return _lev_impl(s1, s2)


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
        self.orphan_bold_max_text_len = cfg.orphan_bold_max_text_len
        self._level_font: dict[int, float] = {}
    
    def resolve(self, chapters: List[ChapterNode]) -> List[DocumentNode]:
        """Resolve *chapters* into a tree of :class:`DocumentNode`.

        Applies fuzzy correction, hierarchy repair, forced-closure
        slicing, and stack-based tree construction in sequence.

        The input list is deep-copied to avoid mutating the caller's
        data (important for ablation experiments that reuse chapters).

        Raises:
            AssemblerError: If *chapters* is empty or resolution fails.
        """
        if not chapters:
            logger.warning("[区间解析] 章节列表为空，自动回退为单一 Root 根节点")
            first_text_block = next((b for b in self.blocks if getattr(b, 'type', '') == 'text' and getattr(b, 'text', '')), None)
            snippet = first_text_block.text.strip()[:40] if first_text_block else "Document"
            chapters = [ChapterNode(
                block_id=0, 
                title="Document", 
                level=1, 
                snippet=snippet
            )]
        
        try:
            chapters = copy.deepcopy(chapters)
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
            
            logger.info("[区间解析] 完成：%d 个章节 → %d 个顶级节点", len(flat_nodes), len(tree_nodes))
            
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
            logger.info("[模糊锚定] 共纠偏 %d 个锚点", correction_count)
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
                "[层级修复] 首章节 '%s' level=%d → 1", first.title, first.level,
            )
            first.level = 1
            fix_count += 1
        fixed.append(first)
        
        # level → font_size mapping (for physical-feature cross-check)
        # Stored as instance attr so _infer_orphan_level can reuse it.
        level_font: dict[int, float] = {}
        self._level_font = level_font
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
                    "[层级修复] '%s' level=%d → %d (跳跃修复，前一层级为 %d%s)",
                    ch.title, old_level, ch.level, level_stack[-1],
                    ", 字号辅助" if resolved_level != max_allowed else "",
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
            logger.info("[层级修复] 共修复 %d 个层级跳跃", fix_count)
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
            
        # Inverse Audit: detect and auto-repair swallowed headings
        intervals = self._inverse_audit_and_repair(intervals)
        
        return intervals
        
    def _infer_orphan_level(self, block: Block, parent_level: int) -> int:
        """Infer the correct heading level for a swallowed orphan block.

        Uses the ``_level_font`` mapping built during hierarchy validation
        to find the best matching level by font size.  Falls back to
        ``parent_level + 1`` (child) when no match is found, avoiding
        the old behaviour of assigning ``parent_level`` (sibling) which
        corrupted the tree.
        """
        level_font = getattr(self, "_level_font", {})
        if block.font_size and level_font:
            best_level = None
            best_diff = float("inf")
            for lv, fs in level_font.items():
                diff = abs(block.font_size - fs)
                if diff <= self.level_jump_font_size_tolerance and diff < best_diff:
                    best_diff = diff
                    best_level = lv
            if best_level is not None:
                return best_level
        return min(parent_level + 1, 6)

    def _inverse_audit_and_repair(self, intervals: List[dict]) -> List[dict]:
        """Inverse audit with automatic orphan-node promotion.

        Scans each forced-closure interval for child blocks whose
        physical features (font size, bold) indicate they are
        swallowed headings the LLM missed.  Instead of just warning,
        the offending block is promoted to an independent chapter node,
        splitting the parent interval in two.

        Returns a new interval list with orphan nodes inserted and
        boundaries recomputed.
        """
        orphan_chapters: List[ChapterNode] = []

        for interval in intervals:
            chapter = interval["chapter"]
            start_id = interval["start_id"]
            end_id = interval["end_id"]

            anchor_block = self.block_map.get(start_id)
            if not anchor_block or not anchor_block.font_size:
                continue

            anchor_size = anchor_block.font_size

            for bid in range(start_id + 1, end_id + 1):
                child_block = self.block_map.get(bid)
                if not child_block or child_block.type != "text" or not child_block.text:
                    continue

                child_size = child_block.font_size or 0
                is_larger = child_size > anchor_size + 0.5
                is_same_but_bold = (
                    abs(child_size - anchor_size) <= 0.5
                    and child_block.is_bold
                    and not anchor_block.is_bold
                    and len(child_block.text) < self.orphan_bold_max_text_len
                )

                if (is_larger or is_same_but_bold) and child_block.is_potential_title():
                    logger.warning(
                        "[反向审计修复] 章节 '%s' (Size: %.1f) 吞并了漏标标题: "
                        "ID=%d, '%s' (Size: %.1f) → 自动提升为独立章节",
                        chapter.title[:20], anchor_size,
                        bid, child_block.text[:30], child_size,
                    )
                    orphan_level = self._infer_orphan_level(
                        child_block, chapter.level,
                    )
                    orphan_chapters.append(ChapterNode(
                        start_block_id=bid,
                        title=child_block.text.strip(),
                        level=orphan_level,
                        snippet=child_block.text.strip()[:40],
                    ))

        if not orphan_chapters:
            logger.info("[反向审计修复] 未发现漏标吞词，无需修复")
            return intervals

        logger.info("[反向审计修复] 自动提升 %d 个孤岛节点为独立章节", len(orphan_chapters))

        # Merge orphan chapters with existing ones and recompute intervals
        existing_chapters = [iv["chapter"] for iv in intervals]
        all_chapters = existing_chapters + orphan_chapters
        all_chapters.sort(key=lambda ch: ch.start_block_id)

        # Deduplicate (orphan may coincide with existing)
        seen_ids = set()
        deduped = []
        for ch in all_chapters:
            if ch.start_block_id not in seen_ids:
                seen_ids.add(ch.start_block_id)
                deduped.append(ch)

        # Rebuild intervals from the expanded chapter list
        new_intervals = []
        for i, chapter in enumerate(deduped):
            s_id = chapter.start_block_id
            e_id = deduped[i + 1].start_block_id - 1 if i + 1 < len(deduped) else self.max_block_id
            if e_id < s_id:
                e_id = s_id
            new_intervals.append({"chapter": chapter, "start_id": s_id, "end_id": e_id})

        return new_intervals
    
    def _build_flat_nodes(self, intervals: List[dict]) -> List[DocumentNode]:
        """Extract content for each interval and build flat :class:`DocumentNode` list."""
        nodes = []
        
        for interval in intervals:
            chapter = interval["chapter"]
            start_id = interval["start_id"]
            end_id = interval["end_id"]
            
            section_type = self._infer_section_type(chapter.title)
            
            # 如果是无特征文档回退生成的虚拟根节点，不应跳过原文正文内容
            skip_title_id = start_id if chapter.title != "Document" else None
            content = self._extract_content(start_id, end_id, skip_title_id=skip_title_id)
            
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
