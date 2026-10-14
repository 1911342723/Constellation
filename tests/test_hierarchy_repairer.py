"""Unified HierarchyRepairer behaviour (P2: three-implementation merge).

Locks in the consolidated semantics:
- best-diff font matching (closest level wins, not first within tolerance);
- single repair pass shared by jump repair / sibling promotion / orphans;
- authoritative numbering hints outrank LLM levels.
"""
from infrastructure.models import Block
from modules.parser.config import ResolverConfig
from modules.parser.hierarchy import HierarchyRepairer
from modules.parser.schemas import ChapterNode


def _blocks(font_map: dict[int, float], bold: set[int] = frozenset()) -> list[Block]:
    return [
        Block(id=i, type="text", text=f"Heading {i}", font_size=size,
              is_bold=(i in bold))
        for i, size in font_map.items()
    ]


def test_best_diff_beats_first_match():
    """A jump block whose font sits within tolerance of BOTH levels must
    resolve to the closest one (best-diff), not the first within
    tolerance (which sorted iteration would make L1)."""
    blocks = _blocks({0: 12.4, 1: 12.0, 2: 12.15})
    repairer = HierarchyRepairer(blocks, config=ResolverConfig(
        level_jump_font_size_tolerance=0.3,
    ))
    chapters = [
        ChapterNode(block_id=0, title="H1", level=1),
        ChapterNode(block_id=1, title="H2", level=2),
        # Illegal jump to L4; 12.15 is within 0.3 of L1 (12.4, diff .25)
        # AND L2 (12.0, diff .15) — best-diff must pick L2.
        ChapterNode(block_id=2, title="H3", level=4),
    ]

    repaired = repairer.repair(chapters)

    assert repaired[2].level == 2


def test_jump_clamps_without_font_evidence():
    """No font match -> clamp to stack top + 1."""
    blocks = _blocks({0: 16.0, 1: 9.0})
    repairer = HierarchyRepairer(blocks, config=ResolverConfig())
    chapters = [
        ChapterNode(block_id=0, title="H1", level=1),
        ChapterNode(block_id=1, title="H2", level=5),
    ]

    repaired = repairer.repair(chapters)

    assert repaired[1].level == 2


def test_jump_repair_font_match_cannot_recreate_illegal_level():
    """Font evidence matching a *historical* deep level must not override
    tree legality: after the stack returns to L1, a block whose font
    matches old L3 sizes still cannot nest as L3 directly under L1.

    Regression for the 2026-06-11 audit finding: jump repair accepted
    any best-diff font level, so 'L1 -> L3' jumps survived "repair"
    whenever a deep level with a similar font had been seen earlier,
    yielding an illegal tree ('repaired' L3 -> L3).
    """
    blocks = _blocks({0: 20.0, 1: 16.0, 2: 12.0, 3: 20.0, 4: 12.0})
    repairer = HierarchyRepairer(blocks, config=ResolverConfig())
    chapters = [
        ChapterNode(block_id=0, title="C1", level=1),
        ChapterNode(block_id=1, title="S1.1", level=2),
        ChapterNode(block_id=2, title="S1.1.1", level=3),
        ChapterNode(block_id=3, title="C2", level=1),
        # LLM claims L3; font (12.0) matches the historical L3 median,
        # but stack top is L1 so the legal maximum is 2.
        ChapterNode(block_id=4, title="Deep", level=3),
    ]

    repaired = repairer.repair(chapters)

    assert repaired[4].level == 2

    # The whole sequence must be a legal tree: each level rises by <= 1.
    stack = [repaired[0].level]
    for ch in repaired[1:]:
        assert ch.level <= stack[-1] + 1
        while stack and stack[-1] >= ch.level:
            stack.pop()
        stack.append(ch.level)


def test_numbering_hint_outranks_llm_level():
    """Visible '2.1' numbering forces level 2 regardless of LLM's claim."""
    blocks = [
        Block(id=0, type="text", text="1 Introduction", font_size=14.0),
        Block(id=1, type="text", text="2.1 Background Work", font_size=14.0),
    ]
    repairer = HierarchyRepairer(blocks, config=ResolverConfig())
    chapters = [
        ChapterNode(block_id=0, title="1 Introduction", level=1),
        ChapterNode(block_id=1, title="2.1 Background Work", level=1),
    ]

    repaired = repairer.repair(chapters)

    assert repaired[1].level == 2


def test_orphan_level_reuses_repair_evidence():
    """Orphan inference uses the font medians accumulated by repair()."""
    blocks = _blocks({0: 16.0, 1: 12.0, 2: 12.1})
    repairer = HierarchyRepairer(blocks, config=ResolverConfig())
    repairer.repair([
        ChapterNode(block_id=0, title="H1", level=1),
        ChapterNode(block_id=1, title="H2", level=2),
    ])

    # 12.1pt orphan matches L2 (12.0) within tolerance.
    assert repairer.infer_orphan_level(blocks[2], parent_level=1) == 2


def test_orphan_falls_back_to_child_level():
    """No font evidence -> parent_level + 1, never the parent's own level."""
    blocks = _blocks({0: 16.0})
    repairer = HierarchyRepairer(blocks, config=ResolverConfig())

    orphan = Block(id=1, type="text", text="Orphan", font_size=None)
    assert repairer.infer_orphan_level(orphan, parent_level=2) == 3
