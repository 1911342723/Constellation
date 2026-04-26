"""Generate ablation study figure for the thesis (毕业论文.md).

Produces a 2x2 panel figure summarizing:
  (a) Token/skeleton reduction across 8 benchmark documents (compression ON vs OFF)
  (b) Component ablation on test_demo.docx (F1 impact of removing each component)
  (c) Skeleton size vs RLE dynamic prefix length (on test_demo.docx)
  (d) Parse latency by Phantom Projection strategy (averaged across evaluated docs)

Output: docs/arxiv_submission/figures/fig_ablation.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "arxiv_submission" / "figures" / "fig_ablation.png"

# -----------------------------------------------------------------------------
# Style: scientific, publication-grade
# -----------------------------------------------------------------------------
mpl.rcParams.update({
    "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
    "axes.unicode_minus": False,
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "figure.dpi": 140,
    "savefig.dpi": 220,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.linestyle": "--",
    "grid.alpha": 0.35,
})

CB = {
    "blue":   "#2E5BFF",
    "orange": "#FF8F1C",
    "green":  "#2CA02C",
    "red":    "#D62728",
    "gray":   "#7F7F7F",
    "teal":   "#17BECF",
}

# -----------------------------------------------------------------------------
# Data (from evaluation.run_ablation_data and evaluation.run_ablation)
# -----------------------------------------------------------------------------

# Panel (a): compression ablation across 8 docs
compression_docs = [
    "extreme_stress\n(10M)",
    "large_test\n(1.3M)",
    "stress_100w\n(1M)",
    "chaotic_stress\n(537K)",
    "test_demo\n(9.5K)",
    "ms_test\n(4.5K)",
    "ibm_lorem\n(3.5K)",
    "ms_equations\n(198)",
]
with_rle   = [737_948, 140_321, 202_571, 117_741, 4_975, 1_211,   736,   679]
without_rle = [2_216_519, 373_672, 545_076, 305_180, 7_337, 1_316, 1_076,  282]
reduction  = [66.7, 62.4, 62.8, 61.4, 32.2, 8.0, 31.6, -140.8]

# Panel (b): component ablation on test_demo.docx
# 全量 Constellation 为对照组；依次剔除单个组件
components = [
    "Full\nConstellation",
    "− RLE\ncompression",
    "− Phantom\n(parallel)",
    "Fuzzy r=0\n(radius off)",
]
comp_f1   = [0.9697, 0.9412, 0.9697, 0.9697]
comp_time = [4.14,   4.97,   4.81,   4.14]  # seconds

# Panel (c): skeleton size vs RLE prefix length on test_demo
prefix_len     = [15, 25, 35, 50, 75]
skeleton_chars = [4835, 4835, 4975, 5155, 5440]

# Panel (d): phantom projection strategy latency (avg over test_demo, 1_nested_hell, ms_test)
strategies   = ["Parallel\n(no phantom)", "Speculative\n(tol=1)", "Serial\n(full phantom)"]
lat_demo     = [4.81, 4.36, 4.57]
lat_nested   = [1.31, 1.20, 1.20]
lat_ms       = [2.32, 2.24, 2.08]
lat_mean     = [np.mean(x) for x in zip(lat_demo, lat_nested, lat_ms)]
lat_std      = [np.std(x)  for x in zip(lat_demo, lat_nested, lat_ms)]

# -----------------------------------------------------------------------------
# Build figure
# -----------------------------------------------------------------------------
fig, axes = plt.subplots(2, 2, figsize=(11.8, 8.0))
ax_a, ax_b = axes[0]
ax_c, ax_d = axes[1]

# --- (a) grouped bar: skeleton chars w/ vs w/o RLE + reduction overlay ---
x = np.arange(len(compression_docs))
width = 0.38
ax_a.bar(x - width/2, np.array(without_rle) / 1e3, width,
         label="w/o RLE", color=CB["gray"], edgecolor="black", linewidth=0.5)
ax_a.bar(x + width/2, np.array(with_rle) / 1e3,    width,
         label="w/ RLE",  color=CB["blue"], edgecolor="black", linewidth=0.5)
ax_a.set_yscale("log")
ax_a.set_ylabel("Skeleton Size (×10³ chars, log)")
ax_a.set_xticks(x)
ax_a.set_xticklabels(compression_docs, rotation=0, fontsize=8)
ax_a.set_title("(a) 骨架压缩对比：w/ vs w/o RLE")
ax_a.legend(loc="upper right", frameon=True)

ax_a2 = ax_a.twinx()
ax_a2.plot(x, reduction, marker="o", color=CB["red"], linewidth=1.8,
           markersize=6, label="Token Reduction (%)")
ax_a2.axhline(0, color="black", linewidth=0.6, linestyle=":")
ax_a2.set_ylabel("Reduction (%)", color=CB["red"])
ax_a2.tick_params(axis="y", labelcolor=CB["red"])
ax_a2.set_ylim(-160, 100)
ax_a2.grid(False)

# annotate reduction values for large docs
for xi, r in zip(x[:4], reduction[:4]):
    ax_a2.annotate(f"{r:.1f}%", (xi, r), textcoords="offset points",
                   xytext=(0, 8), ha="center", fontsize=8, color=CB["red"])

# --- (b) component ablation: F1 (bar) + Time (line) ---
xb = np.arange(len(components))
bars = ax_b.bar(xb, comp_f1, color=[CB["blue"], CB["red"], CB["orange"], CB["green"]],
                edgecolor="black", linewidth=0.5, width=0.55)
ax_b.set_ylim(0.90, 1.00)
ax_b.set_ylabel("Section F1")
ax_b.set_xticks(xb)
ax_b.set_xticklabels(components, fontsize=9)
ax_b.set_title("(b) 组件消融：F1 与耗时（test_demo.docx）")
for bar, v in zip(bars, comp_f1):
    ax_b.annotate(f"{v:.4f}", xy=(bar.get_x() + bar.get_width()/2, v),
                  xytext=(0, 3), textcoords="offset points",
                  ha="center", fontsize=8.5)

ax_b2 = ax_b.twinx()
ax_b2.plot(xb, comp_time, marker="s", color=CB["gray"], linewidth=1.6,
           markersize=7, label="Time (s)")
ax_b2.set_ylabel("Parse Time (s)", color=CB["gray"])
ax_b2.tick_params(axis="y", labelcolor=CB["gray"])
ax_b2.set_ylim(3.5, 5.5)
ax_b2.grid(False)

# annotate F1 delta vs full
full_f1 = comp_f1[0]
for i, v in enumerate(comp_f1):
    if i == 0:
        continue
    d = (v - full_f1) * 100
    if abs(d) > 0.01:
        ax_b.annotate(f"Δ={d:+.2f}pp", xy=(xb[i], v), xytext=(0, -18),
                      textcoords="offset points", ha="center",
                      fontsize=8, color=CB["red"])

# --- (c) skeleton size vs RLE prefix length ---
ax_c.plot(prefix_len, skeleton_chars, marker="o", linewidth=2.0,
          markersize=8, color=CB["teal"])
ax_c.fill_between(prefix_len, skeleton_chars, alpha=0.12, color=CB["teal"])
ax_c.set_xlabel("RLE Dynamic Prefix Length")
ax_c.set_ylabel("Skeleton Size (chars)")
ax_c.set_title("(c) RLE 前缀长度 vs 骨架规模（test_demo.docx）")
for xi, yi in zip(prefix_len, skeleton_chars):
    ax_c.annotate(f"{yi}", (xi, yi), xytext=(0, 8),
                  textcoords="offset points", ha="center", fontsize=8.5)
ax_c.axvline(35, color=CB["red"], linestyle="--", linewidth=1.2, alpha=0.7)
ax_c.annotate("默认 35", xy=(35, skeleton_chars[2]), xytext=(8, -4),
              textcoords="offset points", fontsize=9, color=CB["red"])

# --- (d) phantom projection strategy: mean latency across docs ---
xd = np.arange(len(strategies))
ax_d.bar(xd, lat_mean, yerr=lat_std, width=0.52,
         color=[CB["gray"], CB["orange"], CB["blue"]],
         edgecolor="black", linewidth=0.5, capsize=6, error_kw={"linewidth": 1.0})
ax_d.set_xticks(xd)
ax_d.set_xticklabels(strategies, fontsize=9)
ax_d.set_ylabel("Mean Parse Time (s)  — 3 docs")
ax_d.set_title("(d) 状态幻影投影策略：延迟对比")
for i, (m, s) in enumerate(zip(lat_mean, lat_std)):
    ax_d.annotate(f"{m:.2f}±{s:.2f}s", xy=(xd[i], m + s),
                  xytext=(0, 4), textcoords="offset points",
                  ha="center", fontsize=8.5)

# overlay scatter of individual docs
for i, docvals in enumerate(zip(lat_demo, lat_nested, lat_ms)):
    ax_d.scatter([xd[0] + i*0]*0, [], label=None)  # placeholder
markers = ["o", "s", "^"]
colors  = ["#444444", "#444444", "#444444"]
labels  = ["test_demo", "1_nested_hell", "ms_test"]
for j, (doc_lats, mk, lb) in enumerate(zip([lat_demo, lat_nested, lat_ms], markers, labels)):
    ax_d.scatter(xd, doc_lats, marker=mk, s=36, facecolors="white",
                 edgecolors=colors[j], linewidths=1.1, zorder=5, label=lb)
ax_d.legend(loc="upper right", frameon=True, fontsize=8)

plt.suptitle("图 3-5  消融实验结果：压缩、组件、RLE 前缀与状态幻影策略",
             fontsize=12.5, y=0.995)
plt.tight_layout(rect=(0, 0, 1, 0.965))

OUT.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT, bbox_inches="tight")
print(f"[OK] Saved: {OUT}  ({OUT.stat().st_size/1024:.1f} KB)")
