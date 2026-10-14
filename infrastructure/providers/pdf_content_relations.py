"""Content attachment graph, separate from the linear reading-order graph.

Only physically corroborated, unambiguous attachments get a target. Ambiguous
or missing targets remain explicit review findings; source blocks never move,
disappear, or get duplicated as a side effect of attaching content.
"""
from __future__ import annotations

import re
from infrastructure.models import Block

_CAPTION = re.compile(r"^\s*(figure|fig\.?|图|table|表)\s*[\d一二三四五六七八九十]+", re.I)
_NOTE = re.compile(r"^\s*\[?(\d+)\]?[.)、]?\s+")
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def annotate_content_relations(blocks: list[Block]) -> None:
    pages: dict[int, list[Block]] = {}
    for block in blocks:
        meta = block.metadata or {}
        meta.pop("content_relations", None)
        if meta.get("page") is not None:
            pages.setdefault(meta["page"], []).append(block)
    for page_blocks in pages.values():
        for block in page_blocks:
            meta = block.metadata or {}
            text = meta.get("raw_text") or block.text or ""
            role = meta.get("source_role")
            relation = None
            caption = _CAPTION.match(text)
            if role == "footnote":
                match = _NOTE.match(text)
                targets = []
                if match:
                    number = match.group(1)
                    marker = re.compile(r"\[\s*" + re.escape(number) + r"\s*\]")
                    for other in page_blocks:
                        ometa = other.metadata or {}
                        if other.id == block.id or ometa.get("source_role"):
                            continue
                        raw = ometa.get("raw_text") or other.text or ""
                        superscripts = re.findall(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", raw)
                        if marker.search(raw) or number in [s.translate(_SUPERSCRIPT) for s in superscripts]:
                            targets.append(other.id)
                relation = _relation("footnote_reference", targets, "explicit_reference_marker")
            elif caption:
                asset_type = "table" if caption.group(1).lower() in {"table", "表"} else "image"
                bbox = meta.get("bbox")
                scored = []
                if bbox and len(bbox) == 4:
                    for other in page_blocks:
                        obox = (other.metadata or {}).get("bbox")
                        if other.type != asset_type or not obox or len(obox) != 4:
                            continue
                        overlap = max(0.0, min(bbox[2], obox[2]) - max(bbox[0], obox[0]))
                        width = max(1.0, min(bbox[2] - bbox[0], obox[2] - obox[0]))
                        gap = max(0.0, bbox[1] - obox[3], obox[1] - bbox[3])
                        if overlap / width >= 0.2 and gap <= 80:
                            scored.append((gap, other.id))
                scored.sort()
                targets = [bid for gap, bid in scored if gap <= scored[0][0] + 4] if scored else []
                relation = _relation("caption_of", targets, "same_page_adjacent_geometry")
            elif role in {"sidebar", "marginalia", "margin_note", "marginal"}:
                relation = _relation("sidebar_context", [], "independent_reading_stream")
            if relation:
                block.metadata = {**meta, "content_relations": [relation]}


def _relation(kind: str, targets: list[int], evidence: str) -> dict:
    return {"kind": kind, "target_block_id": targets[0] if len(targets) == 1 else None,
            "candidate_target_block_ids": targets,
            "status": "resolved" if len(targets) == 1 else "needs_confirmation",
            "evidence": evidence}
