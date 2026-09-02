# -*- coding: utf-8 -*-
"""Repro: fuzzy anchor correction can reorder anchors; resolver never
re-sorts afterwards, so interval slicing can duplicate blocks and
violate Proposition 1 (partition).

Scenario:
- Anchor A claims block 10 and matches it exactly (no correction).
- Anchor B claims block 14 (text there does not match its snippet),
  the true heading is at block 8 (within fuzzy radius 5).
- After correction the chapter list is [10, 8] (unsorted).
- Interval for chapter 0 becomes [10, 7] -> clamped to [10, 10];
  interval for chapter 1 becomes [8, max] -> block 10 is emitted twice.

Run: python scripts/manual/repro_fuzzy_reorder.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from infrastructure.models import Block
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode

blocks = []
for i in range(20):
    if i == 8:
        text = "2 Background Theory"
    elif i == 10:
        text = "3 Methodology"
    else:
        text = f"Plain body paragraph number {i} with ordinary filler text."
    blocks.append(Block(id=i, type="text", text=text, font_size=10.0))

chapters = [
    ChapterNode(block_id=10, title="3 Methodology", level=1,
                snippet="3 Methodology"),
    # LLM claims id=14 but snippet belongs to block 8 (drift of -6,
    # within the widened radius for a plain block).
    ChapterNode(block_id=14, title="2 Background Theory", level=1,
                snippet="2 Background Theory"),
]

resolver = IntervalResolver(blocks)
nodes = resolver.resolve(chapters)


def walk(node, depth=0):
    print("  " * depth + f"[{node.start_block_id},{node.end_block_id}] "
          f"L{node.level} {node.title!r}")
    for c in node.children:
        walk(c, depth + 1)


for n in nodes:
    walk(n)

# Count how many times each block id is covered by intervals.
coverage = {}


def count(node):
    for bid in range(node.start_block_id, node.end_block_id + 1):
        coverage[bid] = coverage.get(bid, 0) + 1
    for c in node.children:
        count(c)


for n in nodes:
    count(n)

first = min(n.start_block_id for n in nodes)
dups = {k: v for k, v in coverage.items() if v > 1}
missing = [i for i in range(first, 20) if i not in coverage]
print("\nfirst resolved anchor:", first)
print("duplicated block ids:", dups or "none")
print("uncovered block ids (>= first anchor):", missing or "none")
if dups or missing:
    print("\n*** PARTITION VIOLATED (Proposition 1 broken by reordering) ***")
else:
    print("\npartition holds")
