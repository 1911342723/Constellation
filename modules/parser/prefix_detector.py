"""Multi-Tier Prefix Detector — Dynamic Grammar Prefix Lookahead Engine.

Replaces the hardcoded regex ``^(\\[[\\d,\\s-]+\\]|\\([\\d,\\s-]+\\))\\s*``
with an extensible, priority-ordered rule chain covering ten major
categories of document prefixes encountered in industrial documents.

All patterns are pre-compiled at init time so the per-call cost inside
the RLE fold loop is a handful of ``Pattern.match`` invocations (no
compilation overhead).

Categories (priority-ordered):
    1. Numeric dot/paren index     (1.  1.2.3  1)  1、 1．)
    2. Bracket citations           ([1]  [1,2]  [1-3])
    3. Parenthesised ordinals      ((1)  (a)  (iii))
    4. CJK / full-width ordinals   (（一） 一、 第三章 第1节)
    5. Latin / Roman ordinals       (A.  B)  iv.  III))
    6. Unicode bullet symbols       (➢ ❖ ★ • ▶ ※ ■ ►)
    7. Legal / official numbering   (Article 5.  第五条 §3.2 附件一)
    8. Markdown leftovers           (###  -  *  > )
    9. Compound nested numbering    (1.2.3.4  A-1-a  Ⅱ-3)
   10. Dash-style bullets           (--  —  ──)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrefixMatch:
    """Result of a successful prefix detection.

    Attributes:
        end_pos:      Character position where the prefix (including
                      trailing whitespace) ends.  This is the value
                      previously returned by ``match.end()``.
        category:     Human-readable category label for logging /
                      diagnostics.
        matched_text: The exact substring that was matched.
    """
    end_pos: int
    category: str
    matched_text: str


class PrefixDetector:
    """Extensible multi-tier prefix detection engine.

    Replaces the single hardcoded regex with an ordered rule chain that
    is evaluated top-to-bottom.  The first match wins, so higher-
    priority rules shadow lower ones.

    All patterns are anchored to the start of the string (``^``) and
    include a trailing ``\\s*`` capture so that the returned
    ``end_pos`` skips past any whitespace separating the prefix from
    the body text.

    Args:
        custom_patterns: Optional list of regex pattern strings to
            prepend at the highest priority.  These are compiled with
            ``re.UNICODE`` and anchored to ``^`` automatically if not
            already anchored.
    """

    def __init__(self, custom_patterns: Optional[List[str]] = None):
        self._rules: List[Tuple[str, re.Pattern]] = self._build_rules(
            custom_patterns or []
        )
        logger.debug(
            "[PrefixDetector] 初始化完成，共 %d 条规则",
            len(self._rules),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, text: str) -> Optional[PrefixMatch]:
        """Detect a structural prefix at the start of *text*.

        Returns a :class:`PrefixMatch` on the first successful rule, or
        ``None`` if no rule matches.
        """
        if not text:
            return None

        for category, pattern in self._rules:
            m = pattern.match(text)
            if m:
                return PrefixMatch(
                    end_pos=m.end(),
                    category=category,
                    matched_text=m.group(),
                )
        return None

    def detect_length(self, text: str) -> int:
        """Backward-compatible helper: return prefix length or ``0``."""
        result = self.detect(text)
        return result.end_pos if result else 0

    # ------------------------------------------------------------------
    # Rule construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_rules(
        custom_patterns: List[str],
    ) -> List[Tuple[str, re.Pattern]]:
        """Build the ordered rule chain.

        Custom patterns are prepended at the highest priority.
        """
        rules: List[Tuple[str, re.Pattern]] = []

        # -- Priority 0: user-injected custom rules --------------------
        for i, pat in enumerate(custom_patterns):
            anchored = pat if pat.startswith("^") else f"^{pat}"
            # Ensure trailing whitespace consumption
            if not anchored.endswith(r"\s*"):
                anchored += r"\s*"
            rules.append(
                (f"custom_{i}", re.compile(anchored, re.UNICODE))
            )

        # -- Priority 1: Compound nested numbering (1.2.3.4, A-1-a) ---
        # Must come before simple numeric dot to avoid partial match.
        rules.append((
            "compound_nested",
            re.compile(
                r"^(?:"
                r"[A-Za-z0-9\u2160-\u217F]+"         # first segment
                r"(?:[.\-\u2014\uFF0E][A-Za-z0-9\u2160-\u217F]+){2,}"  # 2+ more
                r")"
                r"[.\s)）、]*\s*",                    # trailing
                re.UNICODE,
            ),
        ))

        # -- Priority 2: CJK / full-width ordinals --------------------
        # （一） （1） 一、 第三章 第1节
        rules.append((
            "cjk_ordinal",
            re.compile(
                r"^(?:"
                r"[（\(][一二三四五六七八九十百千\d\uFF10-\uFF19]+[）\)]\s*"
                r"|[一二三四五六七八九十百千]+[、，：:．.]\s*"
                r"|第[一二三四五六七八九十百千\d\uFF10-\uFF19]+[章节条款部分编篇卷]\s*"
                r"|附[件录表图]\s*[一二三四五六七八九十百千\d\uFF10-\uFF19]*[：:、]?\s*"
                r")",
                re.UNICODE,
            ),
        ))

        # -- Priority 3: Legal / official numbering --------------------
        # Article 5.  §3.2  Section 12
        rules.append((
            "legal_official",
            re.compile(
                r"^(?:"
                r"(?:Article|Section|Clause|Part|Chapter|Schedule|Annex|Appendix)"
                r"\s+\d+(?:\.\d+)*[.:]?\s*"
                r"|§\s*\d+(?:\.\d+)*[.:]?\s*"
                r")",
                re.UNICODE | re.IGNORECASE,
            ),
        ))

        # -- Priority 4: Bracket citations [1] [1,2] [1-3] ------------
        rules.append((
            "bracket_citation",
            re.compile(
                r"^\[[\d,\s\-\u2013\u2014]+\]\s*",
                re.UNICODE,
            ),
        ))

        # -- Priority 5: Parenthesised ordinals (1) (a) (iii) ---------
        rules.append((
            "paren_ordinal",
            re.compile(
                r"^\("
                r"(?:[\d,\s\-]+"
                r"|[a-zA-Z]"
                r"|[ivxlcdmIVXLCDM]+"
                r")\)\s*",
                re.UNICODE,
            ),
        ))

        # -- Priority 6: Latin / Roman ordinals  A. B) iv. III) --------
        rules.append((
            "latin_roman",
            re.compile(
                r"^(?:"
                r"[A-Z]{1,3}[.)]\s*"
                r"|[a-z][.)]\s*"
                r"|(?:(?=[IVXLCDM])M*(?:D?C{0,3}|C[DM])(?:L?X{0,3}|X[LC])"
                r"(?:V?I{0,3}|I[VX]))[.)]\s*"
                r"|(?:(?=[ivxlcdm])m*(?:d?c{0,3}|c[dm])(?:l?x{0,3}|x[lc])"
                r"(?:v?i{0,3}|i[vx]))[.)]\s*"
                r")",
                re.UNICODE,
            ),
        ))

        # -- Priority 7: Numeric dot/paren index  1. 1) 1、 1．---------
        rules.append((
            "numeric_index",
            re.compile(
                r"^\d+(?:\.\d+)*"
                r"[.)\u3001\uFF0E]?\s*",
                re.UNICODE,
            ),
        ))

        # -- Priority 8: Unicode bullet symbols ➢ ❖ ★ • ▶ etc. --------
        rules.append((
            "unicode_bullet",
            re.compile(
                r"^["
                r"\u2022\u2023\u2043\u204C\u204D"   # • ‣ ⁃ ⁌ ⁍
                r"\u2219\u25AA\u25AB\u25B6\u25B8"   # ∙ ▪ ▫ ▶ ▸
                r"\u25BA\u25CB\u25CF\u25E6"         # ► ○ ● ◦
                r"\u2605\u2606\u2610\u2611\u2612"   # ★ ☆ ☐ ☑ ☒
                r"\u2713\u2714\u2716\u2717\u2718"   # ✓ ✔ ✖ ✗ ✘
                r"\u2756\u2795\u27A2\u27A4"         # ❖ ➕ ➢ ➤
                r"\u203B"                           # ※
                r"\u25A0\u25A1\u25A2\u25B2\u25BC"   # ■ □ ▢ ▲ ▼
                r"\u2666\u2665\u2663\u2660"         # ♦ ♥ ♣ ♠
                r"]\s*",
                re.UNICODE,
            ),
        ))

        # -- Priority 9: Markdown leftovers  ### - * > -----------------
        rules.append((
            "markdown_leftover",
            re.compile(
                r"^(?:"
                r"#{1,6}\s+"                        # ### heading
                r"|[-*+]\s+"                        # - item  * item
                r"|>\s+"                            # > quote
                r"|\d+\.\s+"                        # 1. ordered (MD style)
                r")",
                re.UNICODE,
            ),
        ))

        # -- Priority 10: Dash-style bullets  -- — ── -----------------
        rules.append((
            "dash_bullet",
            re.compile(
                r"^(?:"
                r"-{2,3}\s+"
                r"|\u2014{1,2}\s+"                  # — ——
                r"|\u2500{2,}\s+"                   # ──
                r"|\uFF0D{1,2}\s+"                  # －
                r")",
                re.UNICODE,
            ),
        ))

        return rules
