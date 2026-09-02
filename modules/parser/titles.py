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
