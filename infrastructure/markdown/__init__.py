# -*- coding: utf-8 -*-
"""Markdown 方言的**权威实现**（零第三方依赖）。

为什么单独成包而不是塞进 provider 或 parser：方言知识同时被两侧需要——Stage 1 的
``markdown_provider`` 要按方言切块，Stage 4 之上的 ``document_ir`` 要按方言拆行内语法。
放在任一侧都会让另一侧抄第二份（本仓库已经因为「MIME 表两份」「三处各算一遍标题层级」
吃过这个亏）。本包不 import 项目内任何模块，因此两侧都能安全依赖它，且不会成环。
"""
from infrastructure.markdown.inline_syntax import (
    FENCE_LINE_RE,
    InlineKind,
    InlineSpan,
    inline_plain_text,
    parse_inlines,
    render_inlines,
)

__all__ = [
    "FENCE_LINE_RE",
    "InlineKind",
    "InlineSpan",
    "inline_plain_text",
    "parse_inlines",
    "render_inlines",
]
