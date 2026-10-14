# -*- coding: utf-8 -*-

"""标题文本的规范化：把「排版记号」从「结构字段」里剥出去。

DOCX provider 为了零损耗会把加粗 run 渲染成 ``**…**``，而标题段几乎总是加粗，于是
``**`` 会一路渗进 ``doc_title`` / ``DocumentNode.title`` / 章节大纲 / ``paper_data`` ——
这些字段回答的是「这一节叫什么名字」，属于**结构**，不是排版。

刻意只作用于标题字段：正文 Markdown 必须逐字保真，所以 ``Block.text`` 与
``node.content`` 绝不经过这里。
"""

from __future__ import annotations

import re

# 零特征文档的无损兜底会造一个「伪根」节点承载全篇。这个标题是解析器的**内部哨兵**
# （evaluation 与 resolver 都按它判定伪根），不是文档的名字，不能作为 doc_title 对外。
PSEUDO_ROOT_TITLE = "Document"

# 成对的行内强调记号：``**粗**`` / ``*斜*`` / ``__粗__`` / ``` `代码` ``` 。
_PAIRED_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|`+)(.*?)\1")
# 只清理首尾残留的单边记号。中间的星号可能就是标题的一部分（如「5*3 的乘法」），
# 无条件删掉所有星号会改写标题本身。
_EDGE_MARKERS = re.compile(r"^[\s*_`]+|[\s*_`]+$")

# 机器生成的名字：UUID，或被一长串 16 进制/base32 主导的串（内容哈希、产物 id）。
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_HASH_RUN = re.compile(r"[0-9a-f]{12,}", re.IGNORECASE)
# 上游生成名常见的无意义前缀（产物 id、临时上传名）：它们读起来像词，但不是文档的名字。
_MACHINE_PREFIX = re.compile(r"^(art|artifact|upload|tmp|temp|file|doc|blob|obj|attachment)[-_]", re.IGNORECASE)


def strip_title_emphasis(text: str | None) -> str:
    """剥掉标题里的行内 Markdown 强调记号，返回纯标题文本。"""
    cleaned = text or ""
    # 嵌套强调（``**_名_**``）一轮只去掉外层，迭代到不动点。
    while True:
        once = _PAIRED_EMPHASIS.sub(r"\2", cleaned)
        if once == cleaned:
            break
        cleaned = once
    return _EDGE_MARKERS.sub("", cleaned)


def normalize_title(text: str | None) -> str:
    """标题的比较形态：剥记号 + 压缩空白，用于判断两个标题是否「同一个名字」。"""
    return " ".join(strip_title_emphasis(text).split())


def structural_text(text: str | None) -> str:
    """**形状判断**（长度 / 句读 / 词数）唯一该用的文本形态。

    ``Block.text`` 同时承担「无损搬运」与「特征判断」两种职责：provider 为零损耗
    把加粗 run 渲染成 ``**…**``，而标题几乎总是加粗。于是 ``text.endswith('。')``
    这类闸门在加粗块上**永远为假**——判断的是排版记号，不是内容。全篇加粗的文档
    里这会让"以句读结尾的不是标题"整条防线失效。

    判断走这里，搬运仍走 ``Block.text``；两条路必须显式分开，不能靠"大家都用
    block.text"的巧合。
    """
    return normalize_title(text)


def looks_like_machine_name(text: str | None) -> bool:
    """这个名字是否是机器生成的标识，不配当文档标题。

    上传名不一定是人起的：调用方常用产物 id / 内容哈希 / UUID 作文件名
    （如 ``art-userinput-554258c67a503744.docx``）。把这种串当标题，用户看到的
    就是一串无意义字符——宁可没有标题，也不要给一个假名字。
    """
    value = (text or "").strip()
    if not value:
        return True
    if _UUID.search(value) or _MACHINE_PREFIX.match(value):
        return True
    # 去掉哈希样片段与分隔符后没剩下可读内容，就是纯标识。
    remainder = re.sub(r"[\s\-_.]+", "", _HASH_RUN.sub("", value))
    return len(remainder) < 2


# 可见节号：``1`` / ``2.3`` / ``4.1.2`` 后接标题词。节号后的第一个记号不能又是数值
# 或运算符——``2.44 2.75 3.54`` 是表格数值行，``0.5 × 10`` 是公式片段，它们与节号
# 共用同一个前缀形状，此前被当成二级编号标题大批送进候选。
SECTION_NUMBERING_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)*)(?:[.)、．]|)\s+(?![\d.,%+\-−±×=<>(\[])\S+"
)
# 印刷目录行的点线引导符：``3.8 NLI . . . . . 12`` / ``Intro ……… 3``。
_DOT_LEADER_RE = re.compile(r"(?:\.\s*){4,}\d*\s*$|…{2,}\s*\d*\s*$")


def _is_math_alphanumeric(char: str) -> bool:
    # U+1D400–U+1D7FF：PDF 公式抽出来的数学斜体/黑体字母（𝑡、𝛼、x𝑡）。
    return 0x1D400 <= ord(char) <= 0x1D7FF


def is_non_lexical(text: str | None) -> bool:
    """文本几乎没有词：页码、公式编号、数值行、公式碎片。

    标题由词构成（任何文字系统的字母、汉字都算 ``isalpha``）。字母不足两个、或
    非空白字符里字母占比不足三成、或含数学字母符号的块，不可能是一个节标题——
    它是排版残片，不该让「短行」「加粗」这类形状证据把它推成候选。
    """
    value = structural_text(text)
    chars = [c for c in value if not c.isspace()]
    if not chars:
        return True
    if any(_is_math_alphanumeric(c) for c in chars):
        return True
    letters = sum(1 for c in chars if c.isalpha())
    if letters < 2 or letters / len(chars) < 0.30:
        return True
    # A displayed formula ("λc,1 = 1 − γc", "π1(Si(uv))") has letters but no
    # word: no run of three letters, and an operator or a Greek symbol.  CJK
    # text is lexical even in two-character headings ("引言").
    if any("぀" <= c <= "鿿" or "가" <= c <= "힯" for c in chars):
        return False
    if _WORD_RE.search(value):
        return False
    return any(c in _MATH_SYMBOLS or "Ͱ" <= c <= "Ͽ" for c in chars)


_WORD_RE = re.compile(r"[^\W\d_]{3,}")
_MATH_SYMBOLS = set("=<>+−∗*/^≤≥≈≡≠∑∏∫√∂∇±×÷→←↔⇒∈∉⊂⊆∪∩∞|")
_METADATA_LINE_RE = re.compile(
    r"\S+@\S+\.\w+"
    r"|^\(?(?:dated|received|accepted|revised|published|preprint)\b\s*(?::|on\b|\d"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|^\(?(?:corresponding author|e-?mail)\b|^arxiv:\s*\d|^https?://\S+$",
    re.IGNORECASE,
)


def is_metadata_line(text: str | None) -> bool:
    """E-mail / date stamp / arXiv stamp / bare URL lines of the front matter."""
    return bool(_METADATA_LINE_RE.search(structural_text(text)))


def has_dot_leader(text: str | None) -> bool:
    """块以点线引导符（可带页码）结尾——印刷目录行，而非正文里的标题本身。"""
    return bool(_DOT_LEADER_RE.search(structural_text(text)))
