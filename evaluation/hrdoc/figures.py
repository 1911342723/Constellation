"""Figures for the Comp-HRDoc section of the paper (vector PDF, embedded fonts).

python -m evaluation.hrdoc.figures --runs <runs dir> --split test --out build/figures
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from evaluation.hrdoc.evaluate import summarize  # noqa: E402

plt.rcParams.update({
    "pdf.fonttype": 42, "ps.fonttype": 42, "font.family": "DejaVu Sans", "font.size": 7.5,
    "axes.edgecolor": "#8a8984", "axes.linewidth": 0.6, "axes.labelcolor": "#52514e",
    "xtick.color": "#52514e", "ytick.color": "#52514e", "xtick.major.width": 0.6, "ytick.major.width": 0,
    "axes.spines.top": False, "axes.spines.right": False,
})
BLUE, ORANGE, GRAY, INK, MUTED = "#2a78d6", "#eb6834", "#b4b2a9", "#0b0b0b", "#52514e"

ROWS = [("outline", "Embedded bookmarks"), ("docling", "Docling 2.138"),
        ("det-nooutline", "Constellation, deterministic"),
        ("model-nooutline-nosilence", "Constellation, w/o silence"),
        ("model-nooutline", "Constellation, full")]


def load(run: Path) -> dict[str, dict]:
    out = {}
    for path in run.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "metrics" in record:
            out[record["doc"]] = record
    return out


def doc_f1(metrics: dict) -> float:
    p = metrics["tp"] / max(metrics["pred"], 1)
    r = metrics["tp"] / max(metrics["gt"], 1)
    return 2 * p * r / max(p + r, 1e-12)


# (run, legend label, colour, line style); the full system is the only saturated series.
DIST = [("model-nooutline", "Constellation, full", BLUE, "-"),
        ("model-nooutline-layout", "Constellation + layout evidence", BLUE, (0, (4, 1.5))),
        ("det-nooutline", "Constellation, deterministic", "#8a8984", "-"),
        ("docling-numlevels", "Docling + numbering", ORANGE, "-"),
        ("outline", "Embedded bookmarks", INK, (0, (3, 2)))]


def fig_main(runs: Path, split: str, out: Path) -> None:
    """Per-document score distributions: share of documents reaching at least x."""
    data = {key: load(runs / f"{split}-{key}") for key, *_ in DIST}
    docs = sorted(set.intersection(*(set(v) for v in data.values() if v)))
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.1), sharey=True, gridspec_kw={"wspace": 0.08})
    panels = [(lambda m: doc_f1(m), "Per-document heading F1"), (lambda m: m["teds"], "Per-document TEDS")]
    for ax, (value, title) in zip(axes, panels):
        for key, label, color, style in DIST:
            if not data[key]:
                continue
            scores = [value(data[key][d]["metrics"]) for d in docs]
            # Survival curve y(x) = share of documents with score >= x (left-continuous, hence "pre").
            levels = sorted(set(scores))
            px = [0.0] + levels + ([1.0] if levels[-1] < 1.0 else [])
            py = [1.0] + [sum(s >= v for s in scores) / len(scores) for v in levels] + ([0.0] if levels[-1] < 1.0 else [])
            ax.step(px, py, where="pre",
                    color=color, linestyle=style, linewidth=1.6 if key.startswith("model") else 1.1,
                    label=label, zorder=3 if key.startswith("model") else 2)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1.02)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_xticklabels(["0", "0.25", "0.5", "0.75", "1"])
        ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_yticklabels(["0", "25%", "50%", "75%", "100%"])
        ax.set_title(title, fontsize=7.5, color=INK, loc="left", pad=3)
        ax.set_xlabel("score threshold $x$")
        ax.grid(color="#e7e6e2", linewidth=0.5)
        ax.set_axisbelow(True)
    axes[0].set_ylabel(f"documents with score $\\geq x$\n(n = {len(docs)})")
    axes[0].legend(frameon=False, loc="lower left", fontsize=6.8, handlelength=2.2)
    fig.savefig(out / "fig_main_results.pdf", bbox_inches="tight")
    plt.close(fig)


def fig_fidelity(runs: Path, out: Path) -> None:
    gen = load(runs / "gen-llm_markdown")
    full = load(runs / "gen-model-nooutline-rep4")  # run with the shared word-level measure
    docs = sorted(d for d in gen if d in full and "error" not in gen[d] and "error" not in full[d])
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.0), gridspec_kw={"wspace": 0.28})
    ax = axes[0]
    x_gen = [gen[d]["info"]["usage"]["prompt_tokens"] / 1000 for d in docs]
    y_gen = [gen[d]["info"]["word_preservation"]["recall"] for d in docs]
    y_full = [full[d]["info"]["word_preservation"]["recall"] for d in docs]
    truncated = [gen[d]["info"]["finish_reason"] == "length" for d in docs]
    ax.scatter(x_gen, y_full, s=12, color=BLUE, edgecolor="white", linewidth=0.6, label="Constellation", zorder=3)
    ax.scatter([x for x, t in zip(x_gen, truncated) if not t], [y for y, t in zip(y_gen, truncated) if not t],
               s=12, color=ORANGE, edgecolor="white", linewidth=0.6, label="Generative LLM", zorder=2)
    if any(truncated):
        ax.scatter([x for x, t in zip(x_gen, truncated) if t], [y for y, t in zip(y_gen, truncated) if t],
                   s=14, marker="x", color=ORANGE, linewidth=1.0, label="Generative, truncated", zorder=2)
    ax.set_xlabel("Source length (thousand input tokens)")
    ax.set_ylabel("Share of source words preserved")
    ax.set_ylim(min(y_gen + y_full + [0.9]) - 0.03, 1.02)
    ax.grid(color="#e7e6e2", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="lower left", fontsize=6.8, handletextpad=0.3)
    ax.set_title("Content preservation per document", fontsize=7.5, color=INK, loc="left", pad=3)

    ax = axes[1]
    out_gen = [gen[d]["info"]["usage"]["completion_tokens"] / 1000 for d in docs]
    out_full = [full[d]["info"]["usage"]["completion_tokens"] / 1000 for d in docs]
    ax.scatter(x_gen, out_full, s=12, color=BLUE, edgecolor="white", linewidth=0.6, label="Constellation", zorder=3)
    ax.scatter(x_gen, out_gen, s=12, color=ORANGE, edgecolor="white", linewidth=0.6, label="Generative LLM", zorder=2)
    ax.set_xlabel("Source length (thousand input tokens)")
    ax.set_ylabel("Output tokens (thousands)")
    ax.grid(color="#e7e6e2", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=6.8, handletextpad=0.3)
    ax.set_title("Model output per document", fontsize=7.5, color=INK, loc="left", pad=3)
    fig.savefig(out / "fig_fidelity_cost.pdf", bbox_inches="tight")
    plt.close(fig)


def fig_example(runs: Path, split: str, doc_id: str, page: int, out: Path, toc_dir: Path, pdf_dir: Path,
                crop: tuple[float, float] = (0.06, 0.52)) -> None:
    """One page: deterministic proposals (left) vs the full system (right)."""
    import fitz
    from matplotlib.patches import Rectangle

    from evaluation.hrdoc.evaluate import Pred, _blocks, load_gt, match, norm

    data = (pdf_dir / f"{doc_id}.pdf").read_bytes()
    gt = load_gt(toc_dir / f"{doc_id}.json")
    blocks = [b for b in _blocks(data, False) if b.type == "text" and b.text]

    used: set[int] = set()

    def locate(text: str, page_index: int):
        """Bounding box of the first unused block on the page whose text starts with ``text``."""
        key = norm(text)[:40]
        for number, block in enumerate(blocks):
            meta = block.metadata or {}
            if (number not in used and int(meta.get("page", 0)) - 1 == page_index and key
                    and norm(block.text).startswith(key)):
                used.add(number)
                return meta["bbox"]
        return None

    doc = fitz.open(stream=data, filetype="pdf")
    dpi = 220
    pix = doc[page].get_pixmap(dpi=dpi)
    scale = dpi / 72
    image = plt.imread(__import__("io").BytesIO(pix.tobytes("png")))
    panel_height = 3.45 * (crop[1] - crop[0]) * image.shape[0] / image.shape[1]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, panel_height + 0.2), gridspec_kw={"wspace": 0.03})
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1 - 0.2 / (panel_height + 0.2))
    for ax, (system, title) in zip(axes, [("det-nooutline", "(a) Deterministic decoding"),
                                         ("model-nooutline", "(b) Full system (LLM votes and silences)")]):
        record = json.loads((runs / f"{split}-{system}" / f"{doc_id}.json").read_text(encoding="utf-8"))
        used.clear()
        preds = [Pred(p["text"], p["page"], p["level"], p["parent"]) for p in record["predictions"]]
        mapping = match(gt, preds)
        matched_preds = set(mapping.values())
        ax.imshow(image)
        ax.set_ylim(image.shape[0] * crop[1], image.shape[0] * crop[0])
        ax.set_axis_off()
        ax.set_title(title, fontsize=7.5, color=INK, loc="left", pad=3)
        for index, pred in enumerate(preds):
            if pred.page != page:
                continue
            bbox = locate(pred.text, page)
            if bbox is None:
                continue
            ok = index in matched_preds
            color = BLUE if ok else "#e34948"
            ax.add_patch(Rectangle((bbox[0] * scale - 4, bbox[1] * scale - 3), (bbox[2] - bbox[0]) * scale + 8,
                                   (bbox[3] - bbox[1]) * scale + 6, fill=False, edgecolor=color, linewidth=1.1))
            if ok:  # depth is only meaningful for matched headings
                ax.text(bbox[0] * scale - 10, (bbox[1] + bbox[3]) / 2 * scale, f"L{pred.level}", ha="right",
                        va="center", fontsize=6, color=color, fontweight="bold")
        for gi, section in enumerate(gt):
            if section.page != page or gi in mapping:
                continue
            bbox = locate(section.text, page)
            if bbox is not None:
                ax.add_patch(Rectangle((bbox[0] * scale - 4, bbox[1] * scale - 3), (bbox[2] - bbox[0]) * scale + 8,
                                       (bbox[3] - bbox[1]) * scale + 6, fill=False, edgecolor=MUTED,
                                       linewidth=1.0, linestyle=(0, (2, 1.5))))
    handles = [Rectangle((0, 0), 1, 1, fill=False, edgecolor=BLUE, linewidth=1.1),
               Rectangle((0, 0), 1, 1, fill=False, edgecolor="#e34948", linewidth=1.1),
               Rectangle((0, 0), 1, 1, fill=False, edgecolor=MUTED, linewidth=1.0, linestyle=(0, (2, 1.5)))]
    fig.legend(handles, ["matches a reference section", "not in the reference", "missed reference section"],
               loc="upper center", ncol=3, frameon=False, fontsize=7, bbox_to_anchor=(0.5, 0.0))
    fig.savefig(out / "fig_example.pdf", bbox_inches="tight", dpi=220)
    plt.close(fig)


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runs", type=Path, required=True)
    cli.add_argument("--split", default="test")
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--example", help="dev document and 0-based page, e.g. 1504.08093:0")
    cli.add_argument("--toc-dir", type=Path)
    cli.add_argument("--pdf-dir", type=Path)
    args = cli.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    fig_main(args.runs, args.split, args.out)
    if (args.runs / "gen-llm_markdown").exists():
        fig_fidelity(args.runs, args.out)
    if args.example:
        doc_id, page = args.example.split(":")
        fig_example(args.runs, "dev", doc_id, int(page), args.out, args.toc_dir, args.pdf_dir)


if __name__ == "__main__":
    main()


