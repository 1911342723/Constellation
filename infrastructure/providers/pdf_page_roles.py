"""PDF 页面角色判定（E01）——从"删除噪声"改为"保留来源 + 按角色展示"。

方案 E01 的核心判断：重复页眉页脚**被直接从块流删除**对干净正文有好处，但位于
页边缘的真实标题、两页文档里重复出现的小节标题，会与页眉模式混淆而被误删；且删除
不可逆。因此这里不再丢弃判定出的边缘块，而是给它们打角色标签，交付默认视图仍只返回
canonical 正文（调用方行为不变），另给一个审计视图入口能拿到含被排除块的完整序列。

设计要点：

- **只写 metadata，不给 Block 加字段**（本工作包与并行 agent 共享 ``block.py``，
  只能用 ``Block.metadata`` 这个 dict）。写入三个键：``source_role`` / ``role_confidence``
  / ``role_reason``。
- **判定用物理证据，不猜语义**：所在垂直分区（页顶 6% / 页底 6%）、跨页重复次数、
  页码归一化后的文本、字号相对正文、行长。角色枚举见 :data:`ROLE_*`。
- **真实页顶标题不被误判成页眉**：只出现在一页、不重复的页顶大字号行不会被判成
  header——重复次数是 header/footer 的必要条件（见 :func:`classify_marginal_roles`）。
- **页码归一化的坑**：``Page 3`` / ``Page 4`` 归一成 ``page #`` 后会被合并统计成"同一
  条重复行"——这是对的（它们确实是同一个页脚模式）；但归一化只用于**分组计数**，
  展示与恢复用的是每个块各自的原文，不会把 3 显示成 4。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 避免运行时循环导入
    from infrastructure.models import Block

# ── 角色枚举（source_role 的取值，逐字清单见报告）───────────────────────
ROLE_HEADER = "header"
ROLE_FOOTER = "footer"
ROLE_TOC = "toc"
ROLE_FOOTNOTE = "footnote"
ROLE_WATERMARK = "watermark"
ROLE_MARGINAL = "marginal"  # 边注 / 侧栏短行，够不上上述任一具体角色

#: 判定为"非正文角色"的所有取值——审计/正文视图据此切分。
NONBODY_ROLES = frozenset(
    {ROLE_HEADER, ROLE_FOOTER, ROLE_TOC, ROLE_FOOTNOTE, ROLE_WATERMARK, ROLE_MARGINAL}
)

# 页顶 / 页底判定用的垂直分区比例（与旧 _filter_repeating_marginal_text 对齐）。
_HEADER_ZONE_RATIO = 0.06
_FOOTER_ZONE_RATIO = 0.94
# 太长的行不是页眉页脚（正文段落的首/末行也可能压在边缘）。
_MARGINAL_MAX_CHARS = 120

_PAGE_NUMBER_RE = re.compile(r"\b\d+\b")
# 目录行的物理特征：以点导引线 + 页码结尾（``1.1 引言 ........ 3``）。
_TOC_LEADER_RE = re.compile(r"[.\u2026·]{3,}\s*\d+\s*$")
# 脚注行的物理特征：以脚注序号起头（``1 ...`` / ``[1] ...`` / 上标数字）。
_FOOTNOTE_LEAD_RE = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)]?)\s+\S")
# 水印常见词（不区分大小写）。
_WATERMARK_WORDS = frozenset({
    "confidential", "draft", "机密", "内部", "样例", "sample", "watermark", "草稿",
})
# 水印词的常见搭配（``CONFIDENTIAL — INTERNAL USE ONLY`` / ``DRAFT COPY`` / ``机密文件``）。
# 整行只由水印词 + 这些搭配词组成才算水印；出现在一句普通标题里的 ``Samples`` 不算。
_WATERMARK_COMPANIONS = frozenset({
    "copy", "only", "internal", "use", "version", "do", "not", "distribute",
    "for", "review", "preview", "文件", "资料", "版本", "请勿", "外传", "勿", "传阅",
})
_WATERMARK_SPLIT_RE = re.compile(r"[\s\-–—:：|/,，。.]+")


def _zone_of(block: "Block") -> str:
    """返回块所在的垂直分区：``header`` / ``footer`` / ``""``（正文区）。"""
    metadata = block.metadata or {}
    bbox = metadata.get("bbox") or [0, 0, 0, 0]
    page_height = metadata.get("page_height") or 0
    if not page_height or len(bbox) < 4:
        return ""
    y0, y1 = float(bbox[1]), float(bbox[3])
    if y0 <= page_height * _HEADER_ZONE_RATIO:
        return "header"
    if y1 >= page_height * _FOOTER_ZONE_RATIO:
        return "footer"
    return ""


def normalise_marginal_text(text: Optional[str]) -> str:
    """归一化页眉页脚文本用于跨页重复计数。

    小写、折叠空白、把任意整数替换成 ``#``——``Page 3`` 与 ``Page 4`` 归一成同一
    ``page #`` 才能被识别为"同一个页脚模式在多页重复"。**仅用于分组计数**，展示与
    恢复始终用每个块各自的原文，不会把编号张冠李戴。
    """
    value = " ".join((text or "").strip().lower().split())
    value = _PAGE_NUMBER_RE.sub("#", value)
    return value


def _set_role(
    block: "Block", role: str, confidence: float, reason: str
) -> None:
    """把角色标签写进 ``block.metadata``（不加字段），并同步到其 atoms。"""
    metadata = block.metadata or {}
    metadata["source_role"] = role
    metadata["role_confidence"] = round(float(confidence), 3)
    metadata["role_reason"] = reason
    # canonical 由角色决定：非正文角色不参与正文闭合统计，但块**不删除**。
    metadata["canonical"] = role not in NONBODY_ROLES and metadata.get("canonical", True)
    for atom in metadata.get("atoms", []):
        provenance = atom.setdefault("provenance", {})
        provenance["source_role"] = role
    block.metadata = metadata


def _looks_like_toc(text: str) -> bool:
    return bool(_TOC_LEADER_RE.search(text.strip()))


def _looks_like_watermark(block: "Block", text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    low = stripped.lower()
    if len(stripped) > 40 or not any(word in low for word in _WATERMARK_WORDS):
        return False
    # 旋转文本里含水印词：水印最典型的形态（斜着盖在页面上）。
    if (block.metadata or {}).get("writing_direction") == "rotated":
        return True
    # 横排文本必须**整行就是水印**：≤ 5 个词，每个词要么是水印词（允许 ``samples`` /
    # ``机密文件`` 这样的短变体），要么是常见搭配词。旧规则"含水印词且 ≤ 40 字符"
    # 把附录标题 ``F Additional Samples from GPT-3`` 判成了水印并从正文里剔除。
    tokens = [token for token in _WATERMARK_SPLIT_RE.split(low) if token]
    if not tokens or len(tokens) > 5:
        return False

    def _is_watermark_token(token: str) -> bool:
        if token in _WATERMARK_COMPANIONS:
            return True
        if any(word in token and len(token) <= len(word) + 4 for word in _WATERMARK_WORDS):
            return True
        # CJK 不以空格分词：``机密文件`` / ``请勿外传`` 整段由水印词 + 搭配词拼成。
        remainder = token
        for word in sorted(_WATERMARK_WORDS | _WATERMARK_COMPANIONS, key=len, reverse=True):
            remainder = remainder.replace(word, "")
        return not remainder and any(word in token for word in _WATERMARK_WORDS | _WATERMARK_COMPANIONS)

    return all(_is_watermark_token(token) for token in tokens)


def _looks_like_footnote(block: "Block", text: str, body_font_size: float) -> bool:
    """页底、字号明显小于正文、以脚注序号起头的短行。"""
    if _zone_of(block) != "footer":
        return False
    size = block.font_size or 0.0
    smaller = bool(size and body_font_size and size <= body_font_size * 0.9)
    return smaller and bool(_FOOTNOTE_LEAD_RE.match(text))


def classify_marginal_roles(
    entries: list[tuple[int, int, float, float, "Block"]],
    page_count: int,
    body_font_size: float = 0.0,
) -> list[tuple[int, int, float, float, "Block"]]:
    """给页眉/页脚/目录/脚注/水印/边注打角色标签——**不删除任何块**。

    返回的 entries 列表与入参同长、同序（仅在被判定块的 metadata 上写角色字段）。
    这是 E01 的核心：原始 atom / 页面内容整体留在源层，由阅读视图决定是否显示；
    正文闭合只统计 canonical 内容，而审计视图能看到被排除项。

    判定顺序（先具体后一般）：目录 → 脚注 → 水印 → 重复页眉/页脚。真实页顶标题
    （只出现一页、不重复的页顶行）不满足"重复次数 ≥ 阈值"，因此不会被判成 header。
    """
    if not entries:
        return entries

    # 第一遍：跨页统计页眉页脚区的归一化文本重复次数。
    occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)
    for page_index, _, _, _, block in entries:
        if block.type != "text" or not block.text:
            continue
        zone = _zone_of(block)
        if not zone or len(block.text.strip()) > _MARGINAL_MAX_CHARS:
            continue
        normalized = normalise_marginal_text(block.text)
        if normalized:
            occurrences[(normalized, zone)].add(page_index)

    # 单页文档没有"跨页重复"可言，重复阈值退化：只按具体角色（目录/脚注/水印）判。
    threshold = min(page_count, max(2, page_count // 2)) if page_count >= 2 else 10 ** 9
    repeated = {key for key, pages in occurrences.items() if len(pages) >= threshold}

    for page_index, _, _, _, block in entries:
        if block.type != "text" or not block.text:
            continue
        text = block.text
        zone = _zone_of(block)
        short_enough = len(text.strip()) <= _MARGINAL_MAX_CHARS
        # 重复页眉/页脚优先：在页顶/页底区且归一化文本跨页重复 ≥ 阈值。它必须先于
        # 水印词表判定——"Confidential Report"这类每页重复的行是页眉，不是零星水印，
        # 词表命中会喧宾夺主。
        if zone and short_enough:
            key = (normalise_marginal_text(text), zone)
            if key in repeated:
                pages = len(occurrences[key])
                role = ROLE_HEADER if zone == "header" else ROLE_FOOTER
                _set_role(
                    block, role,
                    confidence=min(0.95, 0.5 + 0.1 * pages),
                    reason=(
                        f"该{'页眉' if zone == 'header' else '页脚'}文本归一化后在 "
                        f"{pages} 页重复出现（阈值 {threshold}），"
                        "判为重复页眉/页脚而非正文；仅出现一页的页顶行不会命中此规则。"
                    ),
                )
                continue
        # 其余具体角色：目录 → 脚注 → 水印（非重复的零星短行）。
        if _looks_like_toc(text):
            _set_role(block, ROLE_TOC, 0.8,
                      "行以点导引线加页码结尾，符合目录条目的物理形态。")
            continue
        if _looks_like_footnote(block, text, body_font_size):
            _set_role(block, ROLE_FOOTNOTE, 0.75,
                      "位于页底、字号小于正文且以脚注序号起头。")
            continue
        if _looks_like_watermark(block, text):
            _set_role(block, ROLE_WATERMARK, 0.6,
                      "含水印常见词且为短行、且未跨页重复出现。")
            continue
    return entries


def split_canonical_and_excluded(
    blocks: list["Block"],
) -> tuple[list["Block"], list["Block"]]:
    """把已打角色的块流切成 (canonical 正文, 被排除项)。

    - **canonical 视图**：正文及有内容的脚注 / 边注保留；页眉、页脚、水印、印刷目录
      排除。角色与内容关系独立声明，不能因不属于正文而丢掉有效来源内容。
    - **审计视图**：被排除的非正文角色块，含其 ``role_reason``，供校对/恢复。
    """
    canonical: list["Block"] = []
    excluded: list["Block"] = []
    for block in blocks:
        role = (block.metadata or {}).get("source_role")
        # Footnotes and sidebars contain source content, not disposable page
        # decoration. Keep them in delivery with explicit attachment metadata.
        if role in NONBODY_ROLES and role not in {ROLE_FOOTNOTE, ROLE_MARGINAL}:
            excluded.append(block)
        else:
            canonical.append(block)
    return canonical, excluded
