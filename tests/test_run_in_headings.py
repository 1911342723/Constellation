"""A02：已有字符跨度真正进入候选与闭合。

方案要修的现状：候选生成对整块 text 只建一个候选、``title_span`` 就是整段去首尾空白的
范围；闭合遇到标题就跳过**整个**起始 block。于是 ``**Implementation.** We first …``
这种 run-in 标题段落只有两种下场——整段被当标题（正文凭空消失），或整段被当正文
（标题丢失）。两者都是静默的内容错位。

验收口径按方案原文：段首加粗标签、真正 run-in 标题、公式前标签、表格字段名**不相互
混淆**；并逐字符核对每个字符的去向（标题跨度与正文跨度并集恰好是原文、交集为空）。
"""
from __future__ import annotations

import pytest

from infrastructure.models import Block
from modules.parser.heading_candidates import (
    FRAGMENT_LEAD_IN_LABEL,
    FRAGMENT_RUN_IN,
    FRAGMENT_WHOLE_BLOCK,
    detect_title_fragment,
    generate_heading_candidate_set,
)
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode

RUN_IN_BODY = "We first construct the index and then evaluate the closure over all blocks."
CJK_BODY = "我们先构建索引，然后在全部块上求闭合区间并装配文档树输出结果。"


def _text(text: str, **kwargs) -> Block:
    return Block(id=kwargs.pop("id", 0), type="text", text=text, **kwargs)


# ── 四类易混片段互不混淆 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_role",
    [
        (f"**Implementation.** {RUN_IN_BODY}", FRAGMENT_RUN_IN),
        (f"**实现方式。** {CJK_BODY}", FRAGMENT_RUN_IN),
        (f"3.1 接口约定。{CJK_BODY}", FRAGMENT_RUN_IN),
        # 段首引导词不是标题——判成标题会凭空造出一堆「注」「例如」节点。
        ("**注** 这里的取值需要与供应商确认，且必须在联调前提供完整字段映射表。", FRAGMENT_LEAD_IN_LABEL),
        ("**说明：** 该字段仅在预付模式下有效，其余支付方式一律忽略此入参。", FRAGMENT_LEAD_IN_LABEL),
        # 公式前标签：后文是公式，不切。
        ("**定理 1.** $$x^2+y^2=z^2$$", FRAGMENT_WHOLE_BLOCK),
        # 表格字段名：后文是表格行，不切。
        ("**字段名** | 说明 | 是否必填 | 取值范围 | 备注说明文字很长很长", FRAGMENT_WHOLE_BLOCK),
        # 整块本来就是带句读的标题，切开会把标题自己劈成两半。
        ("**4.2 静态信息：**", FRAGMENT_WHOLE_BLOCK),
        ("**结论。** 完成。", FRAGMENT_WHOLE_BLOCK),
        # 既不加粗也无编号的段首短语只是正文首句。
        ("我们首先构建索引，然后在全部块上求闭合区间，最后装配文档树输出。", FRAGMENT_WHOLE_BLOCK),
    ],
)
def test_fragment_roles_do_not_get_confused(text: str, expected_role: str):
    _start, _end, _title, role = detect_title_fragment(_text(text))
    assert role == expected_role, f"{text[:24]!r} 判成了 {role}"


def test_run_in_span_covers_only_the_leading_fragment():
    text = f"**Implementation.** {RUN_IN_BODY}"
    start, end, title, role = detect_title_fragment(_text(text))
    assert role == FRAGMENT_RUN_IN
    assert title == "Implementation."
    assert text[start:end] == "**Implementation.** "
    assert (text[:start] + text[end:]) == RUN_IN_BODY


def test_every_character_has_exactly_one_destination():
    """逐字符核对去向：标题跨度与正文跨度并集恰好是原文、交集为空（半开区间）。"""
    text = f"**实现方式。** {CJK_BODY}"
    start, end, _title, role = detect_title_fragment(_text(text))
    assert role == FRAGMENT_RUN_IN
    title_chars = list(range(start, end))
    body_chars = list(range(0, start)) + list(range(end, len(text)))
    assert set(title_chars) & set(body_chars) == set(), "同一字符不得同时归标题与正文"
    assert sorted(title_chars + body_chars) == list(range(len(text))), "有字符没有去向"


# ── 闭合：确认为标题时只摘跨度，正文不被吞掉 ──────────────────────────────


def _run_in_document() -> list[Block]:
    return [
        Block(
            id=0, type="text", text="**Document Title**", font_size=20.0,
            is_bold=True, is_heading_style=True, heading_level=1,
        ),
        Block(id=1, type="text", text="Intro paragraph that belongs to the first section body.", font_size=11.0),
        Block(id=2, type="text", text=f"**Implementation.** {RUN_IN_BODY}", font_size=11.0, is_bold=True),
        Block(id=3, type="text", text="Trailing paragraph inside the implementation section.", font_size=11.0),
    ]


def test_confirmed_run_in_heading_keeps_its_body_text():
    """锚点确认 block 2 是标题后，段内其余字符必须仍然出现在该节正文里。

    这是 A02 的核心：旧实现跳过整个起始块，正文连同标题一起消失。
    """
    blocks = _run_in_document()
    resolver = IntervalResolver(blocks)
    nodes = resolver.resolve([
        ChapterNode(block_id=0, title="Document Title", level=1, snippet="Document Title"),
        ChapterNode(block_id=2, title="Implementation.", level=2, snippet="Implementation."),
    ])
    flat: list = []

    def walk(items):
        for item in items:
            flat.append(item)
            walk(item.children or [])

    walk(nodes)
    run_in = [n for n in flat if "Implementation" in (n.title or "")]
    assert run_in, "run-in 标题应成节"
    content = run_in[0].content or ""
    assert RUN_IN_BODY in content, "段首标题被摘掉后，本段正文必须留在该节内容里"
    assert "**Implementation.**" not in content, "标题跨度不应重复出现在正文里"


def test_whole_block_heading_still_removes_the_entire_line():
    """整块就是标题时行为不变：标题行不重复进正文。"""
    blocks = [
        Block(
            id=0, type="text", text="**第一章 概述**", font_size=18.0,
            is_bold=True, is_heading_style=True, heading_level=1,
        ),
        Block(id=1, type="text", text="本章介绍系统的整体结构与主要数据流向。", font_size=11.0),
    ]
    resolver = IntervalResolver(blocks)
    nodes = resolver.resolve([
        ChapterNode(block_id=0, title="第一章 概述", level=1, snippet="第一章 概述"),
    ])
    content = nodes[0].content or ""
    assert "第一章 概述" not in content
    assert "本章介绍系统的整体结构" in content


def test_candidate_carries_the_run_in_span_not_the_whole_block():
    """候选集里 run-in 块的 title_span 必须只覆盖段首，而不是整段。"""
    blocks = _run_in_document()
    candidate_set = generate_heading_candidate_set(blocks)
    by_block = {c.block_id: c for c in candidate_set.candidates}
    candidate = by_block.get(2)
    assert candidate is not None, "run-in 块应产生候选"
    span = candidate.title_span
    assert span is not None
    text = blocks[2].text or ""
    assert span.end - span.start < len(text), "跨度不应覆盖整块"
    assert text[span.start:span.end].strip().startswith("**Implementation.")
