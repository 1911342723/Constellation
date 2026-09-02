"""Fuzzy re-anchoring and anchor-confidence regressions (2026-06-11 audit).

Locks in three behaviours:

1. ``_search_best_match`` resolves the exact match *nearest to the
   claimed id* (spiral search), not the leftmost match in the window —
   TOC echo lines must not hijack anchors from the true heading.
2. ``_compute_anchor_confidence`` blends physical evidence with the
   router/LLM confidence via ``min`` so a downgraded anchor
   (confidence 0.4) actually widens the fuzzy search radius.  The
   previous ``max(score, conf * score)`` was an identity in conf.
3. The pseudo-root fallback is flagged explicitly: a real document
   whose first heading is literally titled "Document" still has its
   heading block skipped from content like any other heading.
"""
from infrastructure.models import Block
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode


def _body(i: int) -> Block:
    return Block(id=i, type="text", text=f"body paragraph {i} " * 3,
                 font_size=10.0)


def test_fuzzy_search_prefers_nearest_exact_match():
    """A TOC echo far from the anchor must lose to the adjacent heading."""
    blocks = [_body(i) for i in range(60)]
    blocks[5] = Block(id=5, type="text",
                      text="3.2 Attention . . . . 12", font_size=10.0)
    blocks[31] = Block(id=31, type="text", text="3.2 Attention",
                       font_size=10.0)
    resolver = IntervalResolver(blocks)

    best_id, score = resolver._search_best_match(30, "3.2 Attention",
                                                 radius=30)

    assert best_id == 31
    assert score >= 0.9


def test_fuzzy_search_still_finds_left_side_match():
    """Spiral order must search both directions, not just rightwards."""
    blocks = [_body(i) for i in range(40)]
    blocks[18] = Block(id=18, type="text", text="4.1 Methods",
                       font_size=10.0)
    resolver = IntervalResolver(blocks)

    best_id, _ = resolver._search_best_match(20, "4.1 Methods", radius=10)

    assert best_id == 18


def test_downgraded_confidence_widens_fuzzy_radius():
    """confidence=0.4 must lower the blended score below the physical
    baseline so the resolver searches a wider window (contract of the
    downgrade channel: reduced confidence => wider re-anchor search)."""
    blocks = [
        Block(id=0, type="text", text="Plain anchor line", font_size=12.0),
        _body(1),
    ]
    resolver = IntervalResolver(blocks)
    plain = ChapterNode(block_id=0, title="Plain anchor line", level=1,
                        snippet="", confidence=1.0)
    downgraded = ChapterNode(block_id=0, title="Plain anchor line", level=1,
                             snippet="", confidence=0.4)

    conf_plain = resolver._compute_anchor_confidence(plain)
    conf_down = resolver._compute_anchor_confidence(downgraded)

    assert conf_down < conf_plain
    assert conf_down == 0.4


def test_real_heading_titled_document_is_not_pseudo_root():
    """A genuine first heading named "Document" must be treated like any
    other heading: its block is skipped from the node content (the title
    is rendered from node.title), with no double inclusion."""
    blocks = [
        Block(id=0, type="text", text="Document", font_size=18.0,
              is_bold=True),
        Block(id=1, type="text", text="Some body text follows here.",
              font_size=10.0),
    ]
    resolver = IntervalResolver(blocks)
    chapters = [ChapterNode(block_id=0, title="Document", level=1,
                            snippet="Document")]

    nodes = resolver.resolve(chapters)

    assert len(nodes) == 1
    assert nodes[0].title == "Document"
    # Heading text must not be duplicated into the content.
    assert "Document" not in nodes[0].content
    assert "Some body text follows here." in nodes[0].content


def test_pseudo_root_fallback_keeps_first_block_content():
    """The synthetic fallback root must NOT skip block 0 — that block is
    real body content, not a heading echo."""
    blocks = [
        Block(id=0, type="text", text="Only body line one.", font_size=10.0),
        Block(id=1, type="text", text="Only body line two.", font_size=10.0),
    ]
    resolver = IntervalResolver(blocks)

    nodes = resolver.resolve([])  # empty anchor list -> pseudo root

    assert len(nodes) == 1
    assert "Only body line one." in nodes[0].content
    assert "Only body line two." in nodes[0].content


def test_fuzzy_crossing_correction_keeps_partition():
    """When fuzzy correction moves an anchor *past* its neighbour
    (claimed 14 -> true 8, crossing the anchor at 10), the resolver
    must re-sort before slicing.  Without the re-sort the unsorted
    pair produced overlapping intervals that emitted block 10 twice —
    an implementation-level violation of Proposition 1 (partition)."""
    blocks = []
    for i in range(20):
        if i == 8:
            blocks.append(Block(id=8, type="text",
                                text="2 Background Theory", font_size=10.0))
        elif i == 10:
            blocks.append(Block(id=10, type="text",
                                text="3 Methodology", font_size=10.0))
        else:
            blocks.append(_body(i))
    resolver = IntervalResolver(blocks)
    chapters = [
        ChapterNode(block_id=10, title="3 Methodology", level=1,
                    snippet="3 Methodology"),
        # Claimed id drifted +6 from the true heading at block 8.
        ChapterNode(block_id=14, title="2 Background Theory", level=1,
                    snippet="2 Background Theory"),
    ]

    nodes = resolver.resolve(chapters)

    flat = []

    def _walk(node):
        flat.append(node)
        for child in node.children:
            _walk(child)

    for root in nodes:
        _walk(root)

    # Anchors resolved to their true blocks, in document order.
    starts = sorted(n.start_block_id for n in flat)
    assert starts == [8, 10]

    # Partition: every block id >= first anchor covered exactly once.
    coverage: dict[int, int] = {}
    for node in flat:
        for bid in range(node.start_block_id, node.end_block_id + 1):
            coverage[bid] = coverage.get(bid, 0) + 1
    assert all(count == 1 for count in coverage.values()), (
        f"blocks covered more than once: "
        f"{ {k: v for k, v in coverage.items() if v > 1} }"
    )
    assert sorted(coverage) == list(range(8, 20))


def test_preamble_sliced_at_resolved_anchor_not_raw_claim():
    """When fuzzy correction moves the first anchor, the preamble must
    follow the *resolved* position. Slicing at the raw LLM claim left
    the blocks between claim and correction in neither the preamble nor
    any chapter (silent content loss at the partition boundary)."""
    from modules.parser.parser import CaliperParser

    blocks = [
        Block(id=i, type="text", text=f"Preamble line {i} with details.",
              font_size=10.0)
        for i in range(10)
    ]
    blocks[6] = Block(id=6, type="text", text="1. Introduction",
                      font_size=14.0, is_bold=True)
    resolver = IntervalResolver(blocks)
    # The LLM claims block 4; the true heading text lives at block 6.
    chapters = [ChapterNode(block_id=4, title="1. Introduction", level=1,
                            snippet="1. Introduction")]

    nodes = resolver.resolve(chapters)
    assert nodes[0].start_block_id == 6, "fuzzy correction should move anchor"

    preamble = CaliperParser._extract_preamble(
        resolver, nodes, had_anchors=True,
    )

    # Every pre-anchor block is in the preamble — nothing falls into
    # the gap between the claimed (4) and corrected (6) anchor.
    for i in range(6):
        assert f"Preamble line {i} " in preamble
    # And the chapter content does not duplicate preamble blocks.
    assert "Preamble line 5 " not in nodes[0].content
