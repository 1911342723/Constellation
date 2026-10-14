"""Comp-HRDoc (HRDoc-Hard test) section-hierarchy evaluation.

Ground truth: ``test_eval_toc/<arxiv>.json`` from microsoft/CompHRDoc (MIT),
derived from HRDoc-Hard line annotations; it is independent of every system
evaluated here and never edited.  PDFs are fetched from arXiv by identifier
(``fetch_pdfs``); a document is admitted only if its ground-truth section lines
are found on the annotated pages of the fetched PDF (version check).

Systems
  outline       the PDF's embedded bookmarks, read verbatim (no parsing)
  det           Constellation, deterministic decoder only (no LLM)
  model         Constellation, full pipeline with the configured LLM
  *-nooutline   same, with embedded bookmarks hidden from the provider
  llm_markdown  generative baseline: the LLM rewrites the page text as
                Markdown; headings are read from the Markdown

Matching (one-to-one, greedy by score): a prediction matches a ground-truth
section if their pages differ by at most one and their normalized titles agree
on the shorter prefix (ratio >= 0.85), with or without the leading section
number.  Ground-truth lines that continue a heading (``connect``) are merged.
TEDS is computed with APTED (unit costs, as in the official Comp-HRDoc tool) on
section trees whose matched nodes carry the ground-truth label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# ── ground truth ─────────────────────────────────────────────────────────────

@dataclass
class Section:
    index: int
    text: str
    page: int  # 0-based
    level: int
    parent: int  # index into the section list, -1 for root


def load_gt(path: Path) -> list[Section]:
    items = json.loads(path.read_text(encoding="utf-8"))
    head: dict[int, int] = {}
    level: dict[int, int] = {}
    parent: dict[int, int] = {}
    texts: dict[int, str] = {}
    order: list[int] = []
    for i, item in enumerate(items):
        p = item["parent_id"]
        p_head = head.get(p, -1) if p != -1 else -1
        if item["relation"] == "connect" and p_head != -1:
            head[i] = p_head
            texts[p_head] += " " + item["text"].strip()
            continue
        head[i] = i
        texts[i] = item["text"].strip()
        if item["relation"] == "contain" or p_head == -1:
            level[i] = level[p_head] + 1 if p_head != -1 else 1
            parent[i] = p_head
        else:  # equality: sibling of p
            level[i] = level[p_head]
            parent[i] = parent[p_head]
        order.append(i)
    remap = {old: new for new, old in enumerate(order)}
    return [Section(remap[i], texts[i], items[i]["page"], min(level[i], 6),
                    remap.get(parent[i], -1)) for i in order]


# ── text normalization and matching ──────────────────────────────────────────

_LEAD_NUMBER = re.compile(r"^\s*(?:(?:\d+|[A-Z]|[IVXLC]+)(?:[.．]\d+)*[.．)]?\s+|appendix\s+[a-z0-9]+[.:]?\s*)", re.I)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in text if ch.isalnum())


def variants(text: str) -> set[str]:
    stripped = _LEAD_NUMBER.sub("", (text or "").replace("**", ""))
    return {v for v in (norm(text), norm(stripped)) if v}


def title_score(gt_text: str, pred_text: str) -> float:
    best = 0.0
    for g in variants(gt_text):
        for p in variants(pred_text):
            k = min(len(g), len(p))
            if k < 4:
                score = 1.0 if g == p else 0.0
            else:
                score = SequenceMatcher(None, g[:k], p[:k], autojunk=False).ratio()
            best = max(best, score)
    return best


@dataclass
class Pred:
    text: str
    page: int  # 0-based
    level: int
    parent: int = -1  # index into prediction list


def match(gt: list[Section], preds: list[Pred], threshold: float = 0.85) -> dict[int, int]:
    pairs = []
    for gi, g in enumerate(gt):
        for pi, p in enumerate(preds):
            if abs(p.page - g.page) > 1:
                continue
            score = title_score(g.text, p.text)
            if score >= threshold:
                pairs.append((score, -abs(p.page - g.page), gi, pi))
    pairs.sort(reverse=True)
    used_g, used_p, result = set(), set(), {}
    for _score, _page, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        result[gi] = pi
    return result


def parents_from_levels(preds: list[Pred]) -> list[Pred]:
    stack: list[int] = []
    for i, p in enumerate(preds):
        while stack and preds[stack[-1]].level >= p.level:
            stack.pop()
        p.parent = stack[-1] if stack else -1
        stack.append(i)
    return preds


# ── TEDS (APTED, unit costs as in Comp-HRDoc's teds_eval) ────────────────────

class _Node:
    def __init__(self, name):
        self.name = name
        self.children = []


def _tree(labels: list[str], parents: list[int]) -> tuple[_Node, int]:
    nodes = [_Node(label) for label in labels]
    root = _Node("ROOT")
    for node, parent in zip(nodes, parents):
        (nodes[parent] if parent >= 0 else root).children.append(node)
    return root, len(nodes) + 1


def teds(gt: list[Section], preds: list[Pred], mapping: dict[int, int]) -> float:
    from apted import APTED, Config
    inverse = {pi: gi for gi, pi in mapping.items()}
    gt_root, gt_n = _tree([f"g{g.index}" for g in gt], [g.parent for g in gt])
    pred_root, pred_n = _tree([f"g{inverse[i]}" if i in inverse else f"p{i}" for i in range(len(preds))],
                              [p.parent for p in preds])
    distance = APTED(pred_root, gt_root, Config()).compute_edit_distance()
    return 1.0 - distance / max(gt_n, pred_n)


def score(gt: list[Section], preds: list[Pred]) -> dict:
    mapping = match(gt, preds)
    tp = len(mapping)
    inverse = {pi: gi for gi, pi in mapping.items()}
    level_ok = sum(gt[gi].level == preds[pi].level for gi, pi in mapping.items())
    parent_ok = 0
    for gi, pi in mapping.items():
        gp, pp = gt[gi].parent, preds[pi].parent
        parent_ok += (gp == -1 and pp == -1) or (pp != -1 and inverse.get(pp) == gp)
    return {"gt": len(gt), "pred": len(preds), "tp": tp, "fp": len(preds) - tp, "fn": len(gt) - tp,
            "level_correct": level_ok, "parent_correct": parent_ok, "teds": teds(gt, preds, mapping),
            "unmatched_gt": [gt[i].text[:80] for i in range(len(gt)) if i not in mapping],
            "unmatched_pred": [preds[i].text[:80] for i in range(len(preds)) if i not in inverse]}


# ── version check ────────────────────────────────────────────────────────────

def version_check(pdf_bytes: bytes, gt: list[Section]) -> tuple[float, int]:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = [norm(page.get_text()) for page in doc]
    found = 0
    for g in gt:
        key = norm(g.text)[:24]
        found += any(0 <= g.page + d < len(pages) and key in pages[g.page + d] for d in (0, -1, 1))
    return found / max(len(gt), 1), len(pages)


# ── systems ──────────────────────────────────────────────────────────────────

def predict_outline(pdf_bytes: bytes) -> tuple[list[Pred], dict]:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    preds = [Pred(title, max(page - 1, 0), min(level, 6)) for level, title, page, *_ in doc.get_toc(simple=False)]
    return parents_from_levels(preds), {}


def _blocks(pdf_bytes: bytes, use_outline: bool):
    from infrastructure.providers.registry import get_provider
    provider = get_provider("pdf")
    provider.use_outline = use_outline
    return provider.extract_from_bytes(pdf_bytes)


def _tree_preds(tree, blocks) -> list[Pred]:
    page_of = {b.id: int((b.metadata or {}).get("page", 1)) - 1 for b in blocks}
    preds: list[Pred] = []

    def walk(nodes, parent):
        for node in nodes:
            if node.title == "Document" and tree.lossless_fallback:
                walk(node.children, parent)
                continue
            preds.append(Pred(node.title, page_of.get(node.start_block_id, 0), node.level, parent))
            walk(node.children, len(preds) - 1)

    walk(tree.nodes, -1)
    return preds


def content_preservation(tree, blocks) -> float:
    """Share of source text characters present, in order, in the exported Markdown."""
    exported = norm(tree.to_markdown())
    cursor, kept, total = 0, 0, 0
    for block in blocks:
        if block.type != "text" or not block.text:
            continue
        key = norm(block.text)
        total += len(key)
        position = exported.find(key, cursor)
        if position >= 0:
            kept += len(key)
            cursor = position + len(key)
    return kept / max(total, 1)


def predict_constellation(pdf_bytes: bytes, use_outline: bool, with_model: bool,
                          **config_overrides) -> tuple[list[Pred], dict]:
    from modules.parser.config import ParserConfig
    from modules.parser.document_tree import DocumentTree
    from modules.parser.heading_candidates import generate_heading_candidate_set
    from modules.parser.parser import CaliperParser
    from modules.parser.resolver import IntervalResolver
    from infrastructure.ai.usage import UsageAccumulator, use_usage_accumulator

    blocks = _blocks(pdf_bytes, use_outline)
    info: dict = {"blocks": len(blocks)}
    config = ParserConfig(**config_overrides)
    if with_model:
        parser = CaliperParser(parser_config=config)
        parser.clear_cache()
        usage = UsageAccumulator()
        with use_usage_accumulator(usage):
            tree = parser.parse(blocks)
        info.update(model=parser.router._client.model, model_calls=parser.router._actual_model_calls,
                    usage=usage.snapshot())
    else:
        candidates = generate_heading_candidate_set(blocks)
        resolver = IntervalResolver(blocks, parser_config=config, heading_candidates=candidates)
        nodes = resolver.resolve([])
        tree = DocumentTree(nodes, lossless_fallback=resolver.lossless_fallback,
                            preamble_content=CaliperParser._extract_preamble(resolver, nodes, had_anchors=False))
    info["content_preservation"] = content_preservation(tree, blocks)
    return _tree_preds(tree, blocks), info


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE_MARK = re.compile(r"<<page (\d+)>>")


def predict_llm_markdown(pdf_bytes: bytes) -> tuple[list[Pred], dict]:
    """Generative baseline: the model regenerates the document as Markdown."""
    import fitz
    from infrastructure.ai.llm_client import _completion_kwargs, get_llm_client

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    source = "\n".join(f"<<page {i + 1}>>\n{page.get_text()}" for i, page in enumerate(doc))
    client = get_llm_client()
    system = ("Convert the extracted text of a scientific paper into clean Markdown. Preserve all body text. "
              "Mark every section and subsection heading with #, ##, ### according to its depth. "
              "Keep the <<page N>> markers exactly where they occur. Output Markdown only.")
    started = time.perf_counter()
    kwargs = _completion_kwargs(client._cfg["base_url"], client.model,
                                [{"role": "system", "content": system}, {"role": "user", "content": source}],
                                0.0, 32768)
    completion = client.client.chat.completions.create(**kwargs)
    output = completion.choices[0].message.content or ""
    usage = completion.usage
    page = 0
    preds: list[Pred] = []
    for line in output.splitlines():
        marker = _PAGE_MARK.search(line)
        if marker:
            page = int(marker.group(1)) - 1
            continue
        heading = _MD_HEADING.match(line.strip())
        if heading:
            preds.append(Pred(heading.group(2), page, len(heading.group(1))))
    source_words = re.findall(r"\w+", _PAGE_MARK.sub(" ", source).casefold())
    output_words = re.findall(r"\w+", _PAGE_MARK.sub(" ", output).casefold())
    matcher = SequenceMatcher(None, source_words, output_words, autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    source_norm = norm(source)
    verbatim = sum(norm(p.text) in source_norm for p in preds)
    return parents_from_levels(preds), {
        "model": client.model, "finish_reason": completion.choices[0].finish_reason,
        "usage": {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else None,
        "elapsed_llm_seconds": time.perf_counter() - started,
        "content_preservation": kept / max(len(source_words), 1),
        "inserted_word_share": 1 - kept / max(len(output_words), 1),
        "verbatim_heading_share": verbatim / max(len(preds), 1),
    }


DOCLING_DIR: Path | None = None


def predict_docling(doc_id: str) -> tuple[list[Pred], dict]:
    """Read predictions written by ``docling_predict`` (separate environment)."""
    record = json.loads((DOCLING_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    if "error" in record:
        raise RuntimeError(record["error"])
    preds = [Pred(h["text"], h["page"], min(max(h["level"], 1), 6)) for h in record["headings"]]
    return parents_from_levels(preds), {"docling": record["docling"], "docling_seconds": record["seconds"]}


SYSTEMS = {
    "outline": lambda data: predict_outline(data),
    "det": lambda data: predict_constellation(data, True, False),
    "det-nooutline": lambda data: predict_constellation(data, False, False),
    "model": lambda data: predict_constellation(data, True, True),
    "model-nooutline": lambda data: predict_constellation(data, False, True),
    # Ablation: the reader's silence on a routed candidate is ignored.
    "model-nooutline-nosilence": lambda data: predict_constellation(
        data, False, True, llm_silence_likelihood_ratio=1.0),
    "llm_markdown": lambda data: predict_llm_markdown(data),
    "docling": None,  # reads cached predictions by document id
}


# ── driver ───────────────────────────────────────────────────────────────────

def admitted_documents(toc_dir: Path, pdf_dir: Path, min_found: float = 0.8) -> list[dict]:
    rows = []
    for gt_path in sorted(toc_dir.glob("*.json")):
        pdf = pdf_dir / f"{gt_path.stem}.pdf"
        gt = load_gt(gt_path)
        row = {"doc": gt_path.stem, "sections": len(gt)}
        if not pdf.exists():
            row["status"] = "pdf_unavailable"
        elif not gt:
            row["status"] = "no_sections"
        else:
            data = pdf.read_bytes()
            found, pages = version_check(data, gt)
            row.update(found=round(found, 3), pages=pages, pdf_sha256=hashlib.sha256(data).hexdigest(),
                       status="admitted" if found >= min_found else "version_mismatch")
        rows.append(row)
    return rows


def summarize(records: list[dict]) -> dict:
    ok = [r for r in records if "metrics" in r]
    total = {k: sum(r["metrics"][k] for r in ok) for k in ("gt", "pred", "tp", "fp", "fn", "level_correct", "parent_correct")}
    p = total["tp"] / max(total["pred"], 1)
    rcl = total["tp"] / max(total["gt"], 1)

    def macro(fn):
        values = [fn(r["metrics"]) for r in ok]
        return sum(values) / max(len(values), 1)

    def f1(m):
        pp, rr = m["tp"] / max(m["pred"], 1), m["tp"] / max(m["gt"], 1)
        return 2 * pp * rr / max(pp + rr, 1e-12)

    out = {"documents": len(ok), "failed": len(records) - len(ok), **total,
           "micro_precision": p, "micro_recall": rcl, "micro_f1": 2 * p * rcl / max(p + rcl, 1e-12),
           "macro_f1": macro(f1), "level_accuracy": total["level_correct"] / max(total["tp"], 1),
           "parent_accuracy": total["parent_correct"] / max(total["tp"], 1), "macro_teds": macro(lambda m: m["teds"])}
    infos = [r.get("info", {}) for r in ok]
    calls = [i.get("model_calls") for i in infos if i.get("model_calls") is not None]
    if calls:
        out["model_calls"] = sum(calls)
    usage = [i["usage"] for i in infos if i.get("usage")]
    if usage:
        out["prompt_tokens"] = sum(u.get("prompt_tokens", 0) for u in usage)
        out["completion_tokens"] = sum(u.get("completion_tokens", 0) for u in usage)
    preservation = [i["content_preservation"] for i in infos if "content_preservation" in i]
    if preservation:
        out["mean_content_preservation"] = sum(preservation) / len(preservation)
        out["min_content_preservation"] = min(preservation)
    out["mean_seconds"] = sum(r["seconds"] for r in ok) / max(len(ok), 1)
    return out


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--pdf-dir", type=Path, required=True)
    cli.add_argument("--system", choices=sorted(SYSTEMS), required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--docs", type=Path, help="JSON list of document ids (default: all admitted)")
    cli.add_argument("--shard", default="0/1", help="i/n: process every n-th admitted document")
    cli.add_argument("--docling-dir", type=Path, help="predictions from docling_predict")
    args = cli.parse_args()
    global DOCLING_DIR
    DOCLING_DIR = args.docling_dir
    args.output.mkdir(parents=True, exist_ok=True)
    admission_path = args.output.parent / "admission.json"
    if admission_path.exists():
        admission = json.loads(admission_path.read_text())
    else:
        admission = admitted_documents(args.toc_dir, args.pdf_dir)
        admission_path.write_text(json.dumps(admission, indent=1))
    ids = [row["doc"] for row in admission if row["status"] == "admitted"]
    if args.docs:
        wanted = set(json.loads(args.docs.read_text()))
        ids = [i for i in ids if i in wanted]
    shard, shards = (int(x) for x in args.shard.split("/"))
    ids = ids[shard::shards]
    for doc_id in ids:
        target = args.output / f"{doc_id}.json"
        if target.exists():
            continue
        record = {"doc": doc_id, "system": args.system}
        started = time.perf_counter()
        try:
            gt = load_gt(args.toc_dir / f"{doc_id}.json")
            if args.system == "docling":
                preds, info = predict_docling(doc_id)
            else:
                preds, info = SYSTEMS[args.system]((args.pdf_dir / f"{doc_id}.pdf").read_bytes())
            record.update(metrics=score(gt, preds), info=info,
                          predictions=[p.__dict__ for p in preds])
        except Exception as exc:  # recorded; never print upstream payloads
            record["error"] = type(exc).__name__
        record["seconds"] = time.perf_counter() - started
        target.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        m = record.get("metrics")
        print(f"{doc_id}: " + (f"tp={m['tp']} fp={m['fp']} fn={m['fn']} teds={m['teds']:.3f}" if m else record["error"]), flush=True)


def report(run_dirs: list[Path]) -> dict:
    result = {}
    for run in run_dirs:
        records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("*.json"))]
        result[run.name] = summarize(records)
    return result


if __name__ == "__main__":
    main()
