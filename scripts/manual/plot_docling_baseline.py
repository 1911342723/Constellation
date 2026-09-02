"""Chart Docling vs Constellation candidate-layer heading recall.

Reads ``experiment_results/docling_baseline_raw.json`` (produced by
``evaluation/docling_baseline.py``) and emits a grouped-bar SVG to
``scripts/manual/output/docling_baseline.svg``.

The figure is generated rather than hand-placed so every coordinate and every
printed number derives from the same raw JSON the paper's Table 8 quotes; run it
again after any baseline re-run and the SVG stays in lock-step with the report.

Visual language matches fig6 (Okabe-Ito, colorblind-safe):
  * Docling bars are GRAY  -- a terminal deterministic decision;
  * candidate bars are BLUE -- the high-recall, non-terminal input to the LLM.

Usage:  python scripts/manual/plot_docling_baseline.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "experiment_results" / "docling_baseline_raw.json"
OUT = ROOT / "scripts" / "manual" / "output" / "docling_baseline.svg"

# Short labels mirror fig6 so the two per-document figures read on one axis.
SHORT = {
    "attention_is_all_you_need": "attn",
    "bert": "bert",
    "chain_of_thought": "cot",
    "diffusion_models": "diff",
    "gpt3": "gpt3",
    "llm_survey": "llms",
    "nist_cybersecurity": "nist",
    "openai_gpt4_tech_report": "gpt4",
    "reinforcement_learning": "rl",
    "resnet": "rsnt",
    "transformer_survey": "tsvy",
    "vit": "vit",
}

# Geometry (shared with fig6's panel (a)).
W = 820
PL, PR = 70.0, 800.0           # plot left / right
Y0, YTOP = 230.0, 50.0         # 0% baseline / 100% top
S = (Y0 - YTOP) / 100.0        # px per percent = 1.8
BAR_W, INNER_GAP = 22.0, 3.0

# Okabe-Ito.
GRAY = "#BBBBBB"               # Docling: terminal deterministic detector
BLUE = "#0072B2"              # Candidate layer: high-recall LLM input


def _y(v: float) -> float:
    return Y0 - v * S


def _fmt(v: float) -> str:
    """Trim trailing .0 so 100.0 -> 100, 95.7 stays 95.7."""
    return f"{v:.1f}".rstrip("0").rstrip(".")


def build_svg(rows: list[dict]) -> tuple[str, dict]:
    n = len(rows)
    group_w = (PR - PL) / n
    pad = (group_w - (2 * BAR_W + INNER_GAP)) / 2.0

    # Aggregates straight from the raw rows (same arithmetic as the report).
    gt = sum(r["gt_titles"] for r in rows)
    doc_micro = 100.0 * sum(r["docling_tp"] for r in rows) / gt
    cand_micro = 100.0 * sum(r["cand_tp"] for r in rows) / gt
    doc_macro = sum(r["docling_recall"] for r in rows) / n
    cand_macro = sum(r["cand_recall"] for r in rows) / n
    stats = {
        "docling_micro": doc_micro,
        "docling_macro": doc_macro,
        "cand_micro": cand_micro,
        "cand_macro": cand_macro,
        "gt": gt,
    }

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="360" '
        f'viewBox="0 0 {W} 360" role="img" '
        f'aria-label="Per-document heading recall, Docling versus Constellation '
        f'candidate layer, grayscale academic style, deterministic and LLM-free">'
    )
    parts.append(
        "  <defs><style>"
        ".title{font-family:Helvetica,Arial,sans-serif;font-size:10px;font-weight:bold;fill:#1a1a1a}"
        ".ax{font-family:Helvetica,Arial,sans-serif;font-size:8px;fill:#444}"
        ".val{font-family:Helvetica,Arial,sans-serif;font-size:7px;fill:#1a1a1a}"
        ".lab{font-family:Consolas,'Courier New',monospace;font-size:7.8px;fill:#444}"
        ".note{font-family:Helvetica,Arial,sans-serif;font-size:8px;fill:#555}"
        ".grid{stroke:#e2e2e2;stroke-width:.6}"
        ".axline{stroke:#1a1a1a;stroke-width:.9}"
        f".barD{{fill:{GRAY};stroke:#1a1a1a;stroke-width:.5}}"
        f".barC{{fill:{BLUE};stroke:#1a1a1a;stroke-width:.5}}"
        f".refD{{stroke:#555;stroke-width:.9;stroke-dasharray:6 3}}"
        f".refC{{stroke:{BLUE};stroke-width:.9;stroke-dasharray:6 3}}"
        "</style></defs>"
    )

    parts.append(
        '  <text x="70" y="26" class="title">Per-document heading recall: Docling '
        "(terminal deterministic) vs candidate layer (LLM input) &#8212; one "
        "title-matching discipline</text>"
    )

    # Gridlines + y ticks at 0/25/50/75/100.
    for v in (25, 50, 75, 100):
        y = _y(v)
        parts.append(f'  <line x1="{PL}" y1="{y:.1f}" x2="{PR}" y2="{y:.1f}" class="grid"/>')
    parts.append(f'  <line x1="{PL}" y1="{Y0}" x2="{PR}" y2="{Y0}" class="axline"/>')
    parts.append(f'  <line x1="{PL}" y1="44" x2="{PL}" y2="{Y0}" class="axline"/>')
    for v in (0, 25, 50, 75, 100):
        parts.append(
            f'  <text x="63" y="{_y(v)+3:.1f}" text-anchor="end" class="ax">{v}</text>'
        )
    parts.append('  <text x="30" y="150" class="ax" transform="rotate(-90 30 150)">recall %</text>')

    # Bars + value labels + x labels.
    for i, r in enumerate(rows):
        gs = PL + i * group_w
        b1x = gs + pad                       # Docling
        b2x = b1x + BAR_W + INNER_GAP        # Candidate
        dv, cv = r["docling_recall"], r["cand_recall"]
        parts.append(
            f'  <rect x="{b1x:.1f}" y="{_y(dv):.1f}" width="{BAR_W}" '
            f'height="{dv*S:.1f}" class="barD"/>'
        )
        parts.append(
            f'  <rect x="{b2x:.1f}" y="{_y(cv):.1f}" width="{BAR_W}" '
            f'height="{cv*S:.1f}" class="barC"/>'
        )
        parts.append(
            f'  <text x="{b1x+BAR_W/2:.1f}" y="{_y(dv)-3:.1f}" text-anchor="middle" '
            f'class="val">{_fmt(dv)}</text>'
        )
        # Candidate bars are mostly 100; label only the ones that are not.
        if cv < 99.95:
            parts.append(
                f'  <text x="{b2x+BAR_W/2:.1f}" y="{_y(cv)-3:.1f}" text-anchor="middle" '
                f'class="val">{_fmt(cv)}</text>'
            )
        parts.append(
            f'  <text x="{gs+group_w/2:.1f}" y="243" text-anchor="middle" '
            f'class="lab">{SHORT.get(r["doc"], r["doc"][:4])}</text>'
        )

    # Micro reference lines.
    parts.append(f'  <line x1="{PL}" y1="{_y(cand_micro):.1f}" x2="{PR}" y2="{_y(cand_micro):.1f}" class="refC"/>')
    parts.append(f'  <line x1="{PL}" y1="{_y(doc_micro):.1f}" x2="{PR}" y2="{_y(doc_micro):.1f}" class="refD"/>')

    # Legend.
    parts.append('  <rect x="70" y="262" width="10" height="8" class="barD"/>')
    parts.append('  <text x="85" y="269" class="note">Docling section_header/title (terminal)</text>')
    parts.append('  <rect x="300" y="262" width="10" height="8" class="barC"/>')
    parts.append('  <text x="315" y="269" class="note">candidate layer (high-recall LLM input)</text>')
    parts.append(
        f'  <line x1="560" y1="266" x2="588" y2="266" class="refC"/>'
        f'<text x="593" y="269" class="note" font-weight="bold" style="fill:{BLUE}">'
        f'cand micro {cand_micro:.1f}%</text>'
    )
    parts.append(
        f'  <line x1="680" y1="266" x2="708" y2="266" class="refD"/>'
        f'<text x="713" y="269" class="note" font-weight="bold" style="fill:#555">'
        f'Docling micro {doc_micro:.1f}%</text>'
    )

    # Footer.
    parts.append('  <line x1="70" y1="286" x2="800" y2="286" stroke="#ccc" stroke-width=".7"/>')
    parts.append(
        f'  <text x="70" y="302" class="note">Title-only match (Levenshtein &#8805; 0.6, '
        f'symmetric); GT = all {gt} heading titles across 12 long PDFs. '
        f'Macro recall: Docling {doc_macro:.1f}% / candidate {cand_macro:.1f}%.</text>'
    )
    parts.append(
        '  <text x="70" y="316" class="note">Docling misses concentrate where its layout '
        "model reads structure as body text: appendix headings and deep numbered "
        "survey/textbook subsections (RL 92&#8594;25, LLM survey 115&#8594;40).</text>"
    )
    parts.append(
        '  <text x="70" y="330" class="note">Deterministic, no LLM calls &#8212; reproduce: '
        "python -m evaluation.docling_baseline &#183; source: "
        "experiment_results/docling_baseline.md. Colors colorblind-safe (Okabe&#8211;Ito).</text>"
    )

    parts.append("</svg>")
    return "\n".join(parts) + "\n", stats


def main() -> None:
    rows = json.loads(RAW.read_text(encoding="utf-8"))
    svg, stats = build_svg(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(rows)} documents, GT={stats['gt']})")
    print(
        "Docling recall  micro {docling_micro:.1f}%  macro {docling_macro:.1f}%".format(**stats)
    )
    print(
        "Candidate recall micro {cand_micro:.1f}%  macro {cand_macro:.1f}%".format(**stats)
    )


if __name__ == "__main__":
    main()
