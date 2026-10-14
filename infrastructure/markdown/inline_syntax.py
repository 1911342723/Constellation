# -*- coding: utf-8 -*-
r"""B10 · 行内语法：把正文拆成 :class:`InlineSpan`，并保证**逐字回吐**。

B10 的块级部分（缩进代码不误判成标题、Setext、围栏与 info string、列表 / 引用容器、
引用式链接定义、硬换行）此前已在 ``markdown_provider`` 落地；**行内**语法则一直原样留在
段落 ``text`` 里，IR 侧以一个 ``provisional`` 文本 span 兜住——那是「先立契约、复杂 inline
留后」的显式停点。本模块把这个停点走完。

## 为什么必须先有「逐字回吐」这条不变量

拆行内语法的风险不是「拆得不够语义」，而是「拆完拼不回去」：一旦 ``**粗体**`` 被拆成
``STRONG(粗体)`` 而渲染器忘了把 ``**`` 补回来，导出 Markdown 就静默少了两个字符——这类
损耗不会让任何功能用例变红，只会让下游看到一份「差一点」的文档。所以本模块的核心契约是

    ``render_inlines(parse_inlines(s)) == s``   对**任意**字符串成立

而不是「解析结果看起来对」。有了它，把解析接进任何出口都不会改变既有产物字节，
语义分类是净增信息；解析器认不出的构造一律退化成 ``TEXT``（原样保留），永远不会丢字符。

## 三分表示怎么落到 span 上

``InlineSpan.text`` 只装**源文字**，记号（``*`` / 反引号 / ``[](...)``）一律不进 ``text``，
而是以结构信息进 ``attrs``：``marker`` 记作者用的是 ``*`` 还是 ``_``、``fence`` 记反引号
个数、``form`` 记链接是行内式还是引用式。这不是把记号换个地方存——记号的**形态**是源文档
的事实（``*em*`` 与 ``_em_`` 语义相同但源不同），零损耗要求它可回溯；而 ``text`` 保持干净，
渲染层套记号时就不可能对已有可信记号重复转义（方案第 9.1 / 第 14 节点名禁止的事）。

反斜杠转义同理：``\*`` 存成 ``TEXT(text="*", escaped=True)``——``text`` 是作者真正想显示的
字符，``escaped`` 记「源里它带着反斜杠」。于是「把所有 TEXT span 的 text 接起来」直接得到
纯文本，不必再反解一遍转义。

## 目标方言与如实声明的边界

目标方言：[CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/) 的行内子集
+ 本项目既有的两个扩展（``$...$`` 行内公式，见 ``Block.render_inline_formula``；
``[^id]`` 脚注引用）。**刻意未覆盖**的部分列在文件末尾 ``# 行内覆盖说明``，
未覆盖即原样留在 ``TEXT`` 里，不假装解析。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "FENCE_LINE_RE",
    "InlineKind",
    "InlineSpan",
    "inline_plain_text",
    "parse_inlines",
    "render_inlines",
]


# ── 方言常量：围栏行 ───────────────────────────────────────────────────────
#
# 围栏代码块的起始 / 闭合行（CommonMark 0.31.2 §4.5）：最多 3 空格前导 + 至少 3 个
# 反引号或波浪线；group(2) 是 info string（语言名等）。
#
# 这一条正则是**块级与行内共用的唯一一份**：``markdown_provider`` 用它切块，本模块用它
# 认出「这段是围栏代码块，里面的 ``*`` 不是强调」。曾经的教训是同一件事两处各写一份
# （MIME 表、三处标题层级），漂移时症状轻微到没人写断言，所以这里只留一处，由
# ``tests/test_layering.py`` 钉住两侧是同一个对象。
FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*?)\s*$")


class InlineKind(str, Enum):
    """一段源文字在行内扮演的角色。

    ``TEXT`` 是**终态**而不是「还没分类」：解析器认不出的构造、以及围栏代码块这类
    「行内语法在其中不成立」的区域，都是确定的 TEXT（后者带 ``attrs['verbatim']``
    说明原因）。此前那个 ``provisional`` 标记随本模块落地一并删除——它的语义是
    「inline 还没做」，做完之后没有任何生产者，留着就是石山。
    """

    TEXT = "text"
    EMPHASIS = "emphasis"          # 斜体 *em* / _em_
    STRONG = "strong"              # 粗体 **strong** / __strong__
    CODE = "code"                  # 行内代码 `code`
    LINK = "link"                  # [锚文本](目标) / [锚文本][标签]
    IMAGE = "image"                # ![alt](目标)
    AUTOLINK = "autolink"          # <https://example.com> / <a@b.com>
    HTML = "html"                  # 行内 HTML 标签 / 注释，原样承载
    MATH = "math"                  # 行内公式 $...$（text 不含 $ 定界符）
    FOOTNOTE_REF = "footnote_ref"  # [^id]
    HARD_BREAK = "hard_break"      # 行尾反斜杠 / 两个及以上空格 + 换行


@dataclass(frozen=True)
class InlineSpan:
    """行内一段内容——源文字（``text``）与我们判定的角色（``kind`` + ``attrs``）分开存。

    Attributes:
        kind: 这段源文字扮演的行内角色。
        text: **源文字本身**，不含记号 / 定界符。容器类（``EMPHASIS`` / ``STRONG`` /
            ``LINK`` / ``IMAGE``）的内容在 ``children`` 里，它们的 ``text`` 为空。
        attrs: 角色附带的结构信息。除语义字段（``href`` / ``title`` / ``ref_id``）外，
            还包括**回吐所需的源形态**：``marker``（``*`` 或 ``_``）、``fence``（反引号
            个数）、``padded``（代码内容两侧各吃掉一个空格）、``form``（链接形态）、
            ``escaped``（源里带反斜杠）、``verbatim``（此区域未做行内解析及其原因）。
        children: 容器类的内部 span 序列（可嵌套，如 ``**粗体里的 `代码` **``）。
    """

    kind: InlineKind
    text: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)
    children: List["InlineSpan"] = field(default_factory=list)


# ── 字符分类（CommonMark 的「Unicode 空白 / 标点」）────────────────────────

#: CommonMark 认定的 Unicode 标点：类别 P* 与 S*（0.30 起把符号也算进来）。
#: 用 ``unicodedata`` 而非 ASCII 白名单：汉字是 ``Lo``（既非空白也非标点），
#: 中文全角标点是 ``Po``——中文文档里 ``**粗体**，`` 的闭合判定就依赖这个。
_PUNCT_CATEGORY_PREFIXES = ("P", "S")

#: 可被反斜杠转义的 ASCII 标点（CommonMark §2.4）。
_ASCII_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

#: 触发「可能是行内语法」的字符。落在这些字符之外的连续片段直接成一段 TEXT。
#: 空格也在其中，因为「两个空格 + 换行」是硬换行，必须在纯文本扫描里被看见。
_SPECIALS = set("`$<![*_\\ ")


def _is_ws(ch: Optional[str]) -> bool:
    """文本边界（``None``）按空白算——与 CommonMark「行首 / 行尾视作空白」一致。"""
    return ch is None or ch.isspace()


def _is_punct(ch: Optional[str]) -> bool:
    return ch is not None and unicodedata.category(ch).startswith(_PUNCT_CATEGORY_PREFIXES)


# ── 顶层入口 ──────────────────────────────────────────────────────────────


def parse_inlines(text: str) -> List[InlineSpan]:
    """把一段正文拆成 span 序列。

    入参可以是单个段落，也可以是 resolver 拼好的多块 Markdown（``DocumentNode.content``
    就是后者）。围栏代码块整段作为 ``verbatim`` 的 TEXT 保留——里面的 ``*`` 是代码而不是
    强调，把它标成 STRONG 虽然回吐一致，语义上却是假的。

    Returns:
        span 序列。``render_inlines`` 作用于它必然还原出**逐字相同**的 ``text``。
    """
    if not text:
        return []
    spans: List[InlineSpan] = []
    for verbatim, chunk in _split_verbatim(text):
        if not chunk:
            continue
        if verbatim:
            spans.append(InlineSpan(
                kind=InlineKind.TEXT, text=chunk, attrs={"verbatim": "fenced_code"},
            ))
        else:
            spans.extend(_parse_segment(chunk))
    return _merge_text(spans)


def render_inlines(spans: Sequence[InlineSpan]) -> str:
    """把 span 序列回吐成 Markdown 源——``parse_inlines`` 的严格逆运算。"""
    return "".join(_render_one(span) for span in spans)


def inline_plain_text(spans: Sequence[InlineSpan]) -> str:
    """去掉全部记号后的纯文本（检索 / 比对 / 摘要用）。

    容器递归取内容；``CODE`` / ``MATH`` / ``AUTOLINK`` 的源文字算正文；``HARD_BREAK``
    还原成一个换行；``HTML`` 标签本身不是正文，不计入。
    """
    out: List[str] = []
    for span in spans:
        if span.children:
            out.append(inline_plain_text(span.children))
        elif span.kind is InlineKind.HARD_BREAK:
            out.append("\n")
        elif span.kind is InlineKind.HTML:
            continue
        else:
            out.append(span.text)
    return "".join(out)


# ── 围栏区域切分 ──────────────────────────────────────────────────────────


def _split_verbatim(text: str) -> List[Tuple[bool, str]]:
    """按围栏代码块切成 ``(是否 verbatim, 原文切片)``，切片顺序拼接**逐字等于**入参。

    未闭合的围栏不成区域（按普通正文处理）——宁可少标一次，也不把后文全部吞成代码。
    """
    out: List[Tuple[bool, str]] = []
    line_starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            line_starts.append(index + 1)

    def line_at(position: int) -> str:
        start = line_starts[position]
        end = text.find("\n", start)
        return text[start:] if end == -1 else text[start:end]

    plain_start = 0
    cursor = 0
    total_lines = len(line_starts)
    while cursor < total_lines:
        opener = FENCE_LINE_RE.match(line_at(cursor))
        if opener:
            marker = opener.group(1)
            close_re = re.compile(
                r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$"
            )
            probe = cursor + 1
            close_line: Optional[int] = None
            while probe < total_lines:
                if close_re.match(line_at(probe)):
                    close_line = probe
                    break
                probe += 1
            if close_line is not None:
                region_start = line_starts[cursor]
                region_end = text.find("\n", line_starts[close_line])
                if region_end == -1:
                    region_end = len(text)
                if region_start > plain_start:
                    out.append((False, text[plain_start:region_start]))
                out.append((True, text[region_start:region_end]))
                plain_start = region_end
                cursor = close_line + 1
                continue
        cursor += 1
    if plain_start < len(text):
        out.append((False, text[plain_start:]))
    return out


# ── 分段扫描 ──────────────────────────────────────────────────────────────


@dataclass
class _Delim:
    """一段 ``*`` / ``_`` 定界符游程，等待 :func:`_process_emphasis` 配对。"""

    char: str
    count: int
    can_open: bool
    can_close: bool


def _text(value: str) -> InlineSpan:
    return InlineSpan(kind=InlineKind.TEXT, text=value)


def _parse_segment(text: str) -> List[InlineSpan]:
    """扫描一段（不含围栏代码块的）正文：先出叶子与定界符，再配对强调。"""
    items: List[Any] = []
    index = 0
    total = len(text)
    while index < total:
        char = text[index]

        # 硬换行：行尾反斜杠。必须先于「反斜杠转义」判定——``\`` + 换行不是转义换行符。
        if char == "\\" and index + 1 < total and text[index + 1] == "\n":
            items.append(InlineSpan(kind=InlineKind.HARD_BREAK, attrs={"marker": "\\"}))
            index += 2
            continue

        # 空格游程：两个及以上 + 换行是硬换行，否则就是普通空白。
        if char == " ":
            run_end = index
            while run_end < total and text[run_end] == " ":
                run_end += 1
            if run_end - index >= 2 and run_end < total and text[run_end] == "\n":
                items.append(InlineSpan(
                    kind=InlineKind.HARD_BREAK, attrs={"marker": text[index:run_end]},
                ))
                index = run_end + 1
                continue
            items.append(_text(text[index:run_end]))
            index = run_end
            continue

        # 反斜杠转义：text 存被转义的字符本身，反斜杠这一事实记进 attrs。
        if char == "\\" and index + 1 < total and text[index + 1] in _ASCII_PUNCT:
            items.append(InlineSpan(
                kind=InlineKind.TEXT, text=text[index + 1], attrs={"escaped": True},
            ))
            index += 2
            continue

        # 行内代码优先级最高（CommonMark §6.1：代码跨度先于强调）。
        if char == "`":
            span, nxt = _scan_code(text, index)
            if span is not None:
                items.append(span)
                index = nxt
                continue

        if char == "$":
            span, nxt = _scan_math(text, index)
            if span is not None:
                items.append(span)
                index = nxt
                continue

        if char == "<":
            span, nxt = _scan_angle(text, index)
            if span is not None:
                items.append(span)
                index = nxt
                continue

        if char == "!" and index + 1 < total and text[index + 1] == "[":
            span, nxt = _scan_link(text, index + 1, image=True)
            if span is not None:
                items.append(span)
                index = nxt
                continue

        if char == "[":
            span, nxt = _scan_footnote(text, index)
            if span is not None:
                items.append(span)
                index = nxt
                continue
            span, nxt = _scan_link(text, index, image=False)
            if span is not None:
                items.append(span)
                index = nxt
                continue

        if char in "*_":
            run_end = index
            while run_end < total and text[run_end] == char:
                run_end += 1
            items.append(_make_delim(
                char,
                run_end - index,
                text[index - 1] if index > 0 else None,
                text[run_end] if run_end < total else None,
            ))
            index = run_end
            continue

        # 普通文本：吃到下一个可能起语法作用的字符为止（起始字符必然被吃掉，
        # 因此上面任何一次「扫描失败」都会在这里退化成字面量，不会死循环）。
        run_end = index + 1
        while run_end < total and text[run_end] not in _SPECIALS:
            run_end += 1
        items.append(_text(text[index:run_end]))
        index = run_end

    return _process_emphasis(items)


def _make_delim(char: str, count: int, before: Optional[str], after: Optional[str]) -> _Delim:
    """按 CommonMark §6.2 的左/右侧靠拢（flanking）规则判定这段游程能开能闭。"""
    after_ws, before_ws = _is_ws(after), _is_ws(before)
    after_punct, before_punct = _is_punct(after), _is_punct(before)
    left = (not after_ws) and ((not after_punct) or before_ws or before_punct)
    right = (not before_ws) and ((not before_punct) or after_ws or after_punct)
    if char == "*":
        return _Delim(char=char, count=count, can_open=left, can_close=right)
    # ``_`` 不能用于词内强调（``snake_case_name`` 不该变斜体）。
    return _Delim(
        char=char,
        count=count,
        can_open=left and ((not right) or before_punct),
        can_close=right and ((not left) or after_punct),
    )


# ── 各构造扫描器：失败一律回 ``(None, 原位置)``，由调用方退化成字面量 ────────


def _scan_code(text: str, start: int) -> Tuple[Optional[InlineSpan], int]:
    """行内代码：N 个反引号由**恰好** N 个反引号闭合（CommonMark §6.1）。"""
    total = len(text)
    open_end = start
    while open_end < total and text[open_end] == "`":
        open_end += 1
    count = open_end - start
    cursor = open_end
    while cursor < total:
        if text[cursor] != "`":
            cursor += 1
            continue
        run_end = cursor
        while run_end < total and text[run_end] == "`":
            run_end += 1
        if run_end - cursor == count:
            content = text[open_end:cursor]
            attrs: Dict[str, Any] = {"fence": count}
            # 两侧各一个空格是给「内容以反引号开头」留的呼吸位，不属于内容本身。
            if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip():
                content = content[1:-1]
                attrs["padded"] = True
            return InlineSpan(kind=InlineKind.CODE, text=content, attrs=attrs), run_end
        cursor = run_end
    return None, start


def _scan_math(text: str, start: int) -> Tuple[Optional[InlineSpan], int]:
    r"""行内公式 ``$...$``（本项目扩展，与 ``Block.render_inline_formula`` 同一方言）。

    定界符两侧不得紧邻空白（避免把 ``价格 $ 5`` 里的货币符号当公式），不跨换行。
    内部 ``\$`` 原样留在源表达式里——那正是 ``render_inline_formula`` 的转义产物。
    """
    total = len(text)
    if start + 1 >= total or text[start + 1].isspace():
        return None, start
    cursor = start + 1
    while cursor < total:
        char = text[cursor]
        if char == "\\" and cursor + 1 < total:
            cursor += 2
            continue
        if char == "\n":
            return None, start
        if char == "$":
            if text[cursor - 1].isspace():
                return None, start
            return InlineSpan(kind=InlineKind.MATH, text=text[start + 1:cursor]), cursor + 1
        cursor += 1
    return None, start


_AUTOLINK_URI_RE = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\x00-\x20]*)>")
_AUTOLINK_EMAIL_RE = re.compile(
    r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9]"
    r"(?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*)>"
)
_HTML_INLINE_RE = re.compile(
    r"<(?:"
    r"[A-Za-z][A-Za-z0-9\-]*"
    r"(?:\s+[^\s\"'>/=]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*"
    r"\s*/?"
    r"|/[A-Za-z][A-Za-z0-9\-]*\s*"
    r"|!--(?:(?!-->).)*--"
    r"|\?.*?\?"
    r"|![A-Za-z]+[^>]*"
    r"|!\[CDATA\[(?:(?!\]\]>).)*\]\]"
    r")>",
    re.DOTALL,
)


def _scan_angle(text: str, start: int) -> Tuple[Optional[InlineSpan], int]:
    """``<...>``：先试自动链接，再试行内 HTML。都不是就退化成字面 ``<``。"""
    match = _AUTOLINK_URI_RE.match(text, start)
    if match:
        target = match.group(1)
        return InlineSpan(
            kind=InlineKind.AUTOLINK, text=target, attrs={"href": target},
        ), match.end()
    match = _AUTOLINK_EMAIL_RE.match(text, start)
    if match:
        address = match.group(1)
        return InlineSpan(
            kind=InlineKind.AUTOLINK, text=address,
            attrs={"href": f"mailto:{address}", "form": "email"},
        ), match.end()
    match = _HTML_INLINE_RE.match(text, start)
    if match:
        # HTML 标签的「源文字」就是标签本身：它不是正文，回吐时原样输出。
        return InlineSpan(kind=InlineKind.HTML, text=match.group(0)), match.end()
    return None, start


_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\]")


def _scan_footnote(text: str, start: int) -> Tuple[Optional[InlineSpan], int]:
    match = _FOOTNOTE_REF_RE.match(text, start)
    if not match:
        return None, start
    return InlineSpan(
        kind=InlineKind.FOOTNOTE_REF, attrs={"ref_id": match.group(1)},
    ), match.end()


def _find_close_bracket(text: str, start: int) -> int:
    """从 ``text[start] == '['`` 起找配对的 ``]``，计深度、跳转义与代码跨度。"""
    depth = 0
    cursor = start
    total = len(text)
    while cursor < total:
        char = text[cursor]
        if char == "\\" and cursor + 1 < total:
            cursor += 2
            continue
        if char == "`":
            _, nxt = _scan_code(text, cursor)
            if nxt > cursor:
                cursor = nxt
                continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return -1


def _scan_link(text: str, start: int, *, image: bool) -> Tuple[Optional[InlineSpan], int]:
    """行内式 ``[t](dest "title")`` 与引用式 ``[t][label]`` / ``[t][]``。

    **刻意不接**捷径引用式 ``[t]``：它是否成链取决于文档另一处是否存在同名定义，
    而本模块只看一段正文。把它当普通文本原样留住，比猜一个可能不存在的链接诚实。
    """
    close = _find_close_bracket(text, start)
    if close < 0:
        return None, start
    label_source = text[start + 1:close]
    after = close + 1
    total = len(text)
    if after < total and text[after] == "(":
        parsed = _parse_inline_destination(text, after)
        if parsed is None:
            return None, start
        attrs, end = parsed
        attrs["form"] = "inline"
    elif after < total and text[after] == "[":
        label_close = text.find("]", after + 1)
        if label_close < 0:
            return None, start
        label = text[after + 1:label_close]
        attrs = {"form": "reference", "label": label} if label else {"form": "collapsed"}
        end = label_close + 1
    else:
        return None, start
    return InlineSpan(
        kind=InlineKind.IMAGE if image else InlineKind.LINK,
        attrs=attrs,
        children=_parse_segment(label_source) if label_source else [],
    ), end


_DEST_WS = " \t\n"


def _parse_inline_destination(text: str, start: int) -> Optional[Tuple[Dict[str, Any], int]]:
    """解析 ``(dest "title")``。空白游程原样记进 attrs，才能逐字回吐。"""
    total = len(text)
    cursor = start + 1
    pad_start = cursor
    while cursor < total and text[cursor] in _DEST_WS:
        cursor += 1
    pad_before = text[pad_start:cursor]

    bracketed = False
    if cursor < total and text[cursor] == "<":
        end = text.find(">", cursor + 1)
        if end < 0 or "\n" in text[cursor + 1:end]:
            return None
        destination = text[cursor + 1:end]
        bracketed = True
        cursor = end + 1
    else:
        depth = 0
        dest_start = cursor
        while cursor < total:
            char = text[cursor]
            if char == "\\" and cursor + 1 < total:
                cursor += 2
                continue
            if char in _DEST_WS:
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            cursor += 1
        destination = text[dest_start:cursor]

    pad_start = cursor
    while cursor < total and text[cursor] in _DEST_WS:
        cursor += 1
    pad_between = text[pad_start:cursor]

    title: Optional[str] = None
    title_delim: Optional[str] = None
    # 标题必须与目标之间有空白，否则 ``(url"x")`` 里的引号属于目标本身。
    if pad_between and cursor < total and text[cursor] in "\"'(":
        opener = text[cursor]
        closer = ")" if opener == "(" else opener
        probe = cursor + 1
        while probe < total:
            if text[probe] == "\\" and probe + 1 < total:
                probe += 2
                continue
            if text[probe] == closer:
                break
            probe += 1
        if probe < total:
            title = text[cursor + 1:probe]
            title_delim = opener
            cursor = probe + 1

    pad_start = cursor
    while cursor < total and text[cursor] in _DEST_WS:
        cursor += 1
    pad_after = text[pad_start:cursor]

    if cursor >= total or text[cursor] != ")":
        return None

    attrs: Dict[str, Any] = {"href": destination}
    if bracketed:
        attrs["dest_bracketed"] = True
    if title is not None:
        attrs["title"] = title
        attrs["title_delim"] = title_delim
    if pad_before:
        attrs["pad_before"] = pad_before
    if pad_between and title is not None:
        attrs["pad_between"] = pad_between
    elif pad_between:
        attrs["pad_after"] = pad_between + pad_after
        pad_after = ""
    if pad_after:
        attrs["pad_after"] = pad_after
    return attrs, cursor + 1


# ── 强调配对（CommonMark §6.2 的 process emphasis）────────────────────────


def _delim_to_text(delim: _Delim) -> InlineSpan:
    return _text(delim.char * delim.count)


def _process_emphasis(items: List[Any]) -> List[InlineSpan]:
    """把定界符游程配成 EMPHASIS / STRONG，未配上的退化成字面量。

    ``openers_bottom`` 在每次成对后清空：它本是防止重复回溯的加速表，用绝对下标索引，
    而配对会重排列表使下标失效。清空只损失一点速度，不会误判——每轮至少消耗一个定界符
    字符，循环必然收敛。
    """
    position = 0
    openers_bottom: Dict[Tuple[str, int, bool], int] = {}
    while position < len(items):
        closer = items[position]
        if not isinstance(closer, _Delim) or not closer.can_close:
            position += 1
            continue
        bottom = openers_bottom.get((closer.char, closer.count % 3, closer.can_open), -1)
        found = -1
        for probe in range(position - 1, bottom, -1):
            candidate = items[probe]
            if not isinstance(candidate, _Delim):
                continue
            if candidate.char != closer.char or not candidate.can_open:
                continue
            # 「3 的倍数」规则：任一侧既能开又能闭时，长度之和是 3 的倍数则不配对，
            # 除非两侧长度都是 3 的倍数（CommonMark §6.2 最后两条）。
            if (closer.can_open or candidate.can_close) \
                    and (candidate.count + closer.count) % 3 == 0 \
                    and not (candidate.count % 3 == 0 and closer.count % 3 == 0):
                continue
            found = probe
            break
        if found < 0:
            openers_bottom[(closer.char, closer.count % 3, closer.can_open)] = position - 1
            if not closer.can_open:
                items[position] = _delim_to_text(closer)
            position += 1
            continue

        opener = items[found]
        use = 2 if (opener.count >= 2 and closer.count >= 2) else 1
        inner = _finalize(items[found + 1:position])
        span = InlineSpan(
            kind=InlineKind.STRONG if use == 2 else InlineKind.EMPHASIS,
            attrs={"marker": closer.char},
            children=inner,
        )
        opener.count -= use
        closer.count -= use
        head: List[Any] = items[:found]
        if opener.count > 0:
            head.append(opener)
        head.append(span)
        tail: List[Any] = [closer] if closer.count > 0 else []
        tail.extend(items[position + 1:])
        items = head + tail
        position = len(head)
        openers_bottom.clear()

    return _finalize(items)


def _finalize(items: Sequence[Any]) -> List[InlineSpan]:
    """收尾：剩下的定界符没配上，按字面量输出；相邻纯文本合并。"""
    return _merge_text([
        _delim_to_text(item) if isinstance(item, _Delim) else item for item in items
    ])


def _merge_text(spans: Sequence[InlineSpan]) -> List[InlineSpan]:
    """合并相邻的**无 attrs** TEXT span。带 attrs 的（转义 / verbatim）不合并——
    它们各自记着不同的源事实，合起来就分不清哪段带反斜杠了。"""
    out: List[InlineSpan] = []
    for span in spans:
        if (span.kind is InlineKind.TEXT and not span.attrs and not span.children
                and out and out[-1].kind is InlineKind.TEXT
                and not out[-1].attrs and not out[-1].children):
            out[-1] = _text(out[-1].text + span.text)
        else:
            out.append(span)
    return out


# ── 回吐 ──────────────────────────────────────────────────────────────────


def _render_one(span: InlineSpan) -> str:
    kind = span.kind
    if kind is InlineKind.TEXT:
        return ("\\" + span.text) if span.attrs.get("escaped") else span.text
    if kind is InlineKind.HARD_BREAK:
        return str(span.attrs.get("marker", "\\")) + "\n"
    if kind is InlineKind.CODE:
        fence = "`" * int(span.attrs.get("fence", 1))
        pad = " " if span.attrs.get("padded") else ""
        return f"{fence}{pad}{span.text}{pad}{fence}"
    if kind is InlineKind.MATH:
        return f"${span.text}$"
    if kind is InlineKind.AUTOLINK:
        return f"<{span.text}>"
    if kind is InlineKind.HTML:
        return span.text
    if kind is InlineKind.FOOTNOTE_REF:
        return f"[^{span.attrs.get('ref_id', '')}]"
    if kind in (InlineKind.EMPHASIS, InlineKind.STRONG):
        marker = str(span.attrs.get("marker", "*")) * (1 if kind is InlineKind.EMPHASIS else 2)
        return marker + render_inlines(span.children) + marker
    if kind is InlineKind.IMAGE:
        return "!" + _render_link_body(span)
    if kind is InlineKind.LINK:
        return _render_link_body(span)
    return span.text


def _render_link_body(span: InlineSpan) -> str:
    attrs = span.attrs
    label = "[" + render_inlines(span.children) + "]"
    form = attrs.get("form", "inline")
    if form == "reference":
        return f"{label}[{attrs.get('label', '')}]"
    if form == "collapsed":
        return f"{label}[]"
    destination = str(attrs.get("href", ""))
    if attrs.get("dest_bracketed"):
        destination = f"<{destination}>"
    body = str(attrs.get("pad_before", "")) + destination
    if attrs.get("title") is not None:
        opener = str(attrs.get("title_delim", '"'))
        closer = ")" if opener == "(" else opener
        body += str(attrs.get("pad_between", " ")) + opener + str(attrs["title"]) + closer
    body += str(attrs.get("pad_after", ""))
    return f"{label}({body})"


# ── 行内覆盖说明（B10 · 如实声明）─────────────────────────────────────────
# 目标方言：CommonMark 0.31.2 行内子集 + 本项目两个扩展（``$...$`` 行内公式、
# ``[^id]`` 脚注引用）。所有情形都满足「逐字回吐」不变量。
#
# 已覆盖（拆成语义 span）：
#   反斜杠转义、行内代码（任意长度反引号、两侧呼吸空格）、行内公式、自动链接
#   （URI 与邮箱）、行内 HTML（标签 / 注释 / PI / 声明 / CDATA）、行内图片、
#   行内式链接（含 ``<>`` 包裹的目标、三种标题定界符、任意空白布局）、引用式与
#   折叠式链接、脚注引用、强调 / 粗体（``*`` 与 ``_``、左右靠拢规则、词内 ``_``
#   不成强调、「3 的倍数」规则、嵌套如 ``***x***``）、硬换行（反斜杠与两空格两种
#   形态各自保留）。
#
# 刻意未覆盖（原样留在 TEXT，不假装解析，理由随附）：
#   1) 捷径引用式链接 ``[label]``：成不成链取决于文档别处有无同名定义，本模块只见
#      一段正文，猜错会造出一个不存在的链接。引用式与折叠式形态明确，故接。
#   2) HTML 实体引用（``&amp;`` / ``&#42;``）：不做实体解码。解码后回吐要还原成
#      原始写法（``&#x26;`` 与 ``&amp;`` 同义不同源），得再存一份源形态，收益
#      不抵复杂度；作为普通文本原样往返，语义上不丢字符。
#   3) GFM 删除线 ``~~x~~``、任务列表标记 ``[ ]``：方言声明里就没有它们
#      （见 ``markdown_provider`` 的方言覆盖说明），保持一致不单方面扩张。
#   4) 代码跨度内容首尾的**换行**吞除（CommonMark 只在两侧同为空格 / 换行时各吃一个，
#      本模块只处理空格）：多行行内代码在真实文档里极少，且不处理不会丢字符。
#   5) 围栏代码块**内部**不做行内解析（整段 verbatim）；未闭合围栏不成区域，按正文处理。
