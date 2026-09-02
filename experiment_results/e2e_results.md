# End-to-End Evaluation Results (long-PDF tier)

> **Provenance / recovery note.** The original raw artifacts for this run —
> the per-configuration reports and the driver `evaluation/e2e_protocol.py` —
> were **lost**: they were never committed, and are absent from the working
> tree and from all git history. The numbers below were **recovered** from two
> surviving, mutually consistent stores used to prepare the manuscript, which
> agree to every digit. This file re-fixes those values so the end-to-end
> claims have an in-repo source of record again.
>
> It is a **recovered summary, not a fresh run.** Unlike every other report in
> this directory, it is *not* reproducible from this repository as it stands:
> regenerating the raw per-document, per-run JSON requires rebuilding
> `e2e_protocol.py` and re-running with LLM API access (see "To regenerate"
> below). The deterministic, LLM-free results are the ones this repository
> lets you verify directly.

## Protocol

- **Tier:** 12 long academic/technical PDFs (the heading-bearing corpus of `tab:recall`).
- **Metric:** micro over pooled matched pairs; a prediction matches GT by block proximity (|Δid| ≤ 3) **and** title similarity (same discipline as Sect. 4.1 / `compute_section_f1`). Hierarchy accuracy over matched pairs only.
- **Primary model:** `deepseek-v4-flash`, temperature 0.1, `num_runs = 3`; reported value = mean of 3 runs, run-to-run micro-F1 σ ≤ 0.02 (strict 0.006).
- **Token accounting:** real (mean total tokens per document).
- **External baselines** (no LLM): MarkItDown and Docling, their Markdown headings scored under the identical position-and-title metric.

## Table — nine configurations (deepseek-v4-flash, mean of 3 runs)

| Config | Docs | P | R | F1 | Tok | Hier.Acc | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| rule_only (no LLM) | 12 | 0.238 | 0.965 | 0.382 | 0 | — | candidates become anchors via closure |
| full | 12 | 0.599 | 0.971 | **0.741** | 179k | 0.865 | default routing |
| strict | 12 | 0.642 | 0.971 | **0.773** | 181k | — | out-of-candidate hard-dropped; σ=0.006; best |
| no_candidate | 12 | 0.544 | 0.969 | 0.697 | 134k | 0.968 | unconstrained; cheapest; hier.acc is a matched-pair selection effect |
| no_font_xval | 12 | 0.633 | 0.973 | 0.767 | 177k | 0.863 | net-neutral vs full |
| no_header_footer | 12 | 0.183 | 0.293 | 0.225 | 180k | — | Stage-1 ablation (collapse) |
| no_pdf_layout | 12 | 0.031 | 0.039 | 0.035 | 350k | — | Stage-1 ablation (collapse); llm_survey skeleton balloons to 1.97M tokens |
| MarkItDown | 12 | 0.000 | 0.000 | 0.000 | 0 | — | emits no PDF headings (plain-text PDF backend) |
| Docling | 11 | 0.735 | 0.603 | 0.663 | 0 | 0.554 | GPT-4 PDF PDFium error in this run (see note) |

**LLM margin:** full − rule_only = **+0.359** micro F1, recall essentially flat (0.965 → 0.971); the entire gain is precision (0.238 → 0.599).

**Docling note.** This e2e Docling run failed on the GPT-4 PDF (PDFium format error) and is scored on 11. The later OCR-disabled **Docling 2.77.0** deterministic baseline (`experiment_results/docling_baseline.md`, title-only recall) processes all 12 — so the GPT-4 gap is specific to this e2e run, not a Docling limitation.

## Cross-model replication

| Model | Scope | rule_only | full | strict | no_candidate | Margin (full−rule_only) |
|---|---|---:|---:|---:|---:|---:|
| deepseek-v4-flash | 12 docs | 0.382 | 0.741 | 0.773 | 0.697 | +0.359 |
| deepseek-chat | 12 docs | — | 0.742 | 0.752 | 0.656 | +0.360 (~8× lower latency) |
| qwen-turbo (Alibaba) | 10-doc subset¹ | 0.397 | 0.585 | 0.671 | 0.273 | +0.189 |

¹ `reinforcement_learning` and `diffusion_models` excluded (qwen-turbo timeouts). On the same 10 docs deepseek-chat margin is +0.327. The three qualitative findings (positive LLM margin; candidate constraint beating no_candidate; strict > full) reproduce across both vendors.

## Seven findings

1. **LLM contributes a large, measurable margin:** +0.359 micro F1 over the deterministic floor, almost entirely precision; robust at σ ≤ 0.02.
2. **Candidate constraint helps:** full 0.741 / strict 0.773 both beat no_candidate 0.697 (the last also cheapest, 134k tok). no_candidate's higher matched-pair hier.acc (0.968 vs 0.865) is a selection effect of the matched-pair metric, not better leveling.
3. **Downgrade channel does not pay for itself here (negative result):** strict 0.773 > full 0.741 at identical recall, tightest variance (σ=0.006); repositioned as a recall-critical opt-in.
4. **Stage-1 normalization is load-bearing (decisive ablation):** no_pdf_layout collapses F1 to 0.035, no_header_footer to 0.225 — an order of magnitude below any routing ablation.
5. **Font cross-validation is net-neutral here:** no_font_xval 0.767 leaves micro F1 (full 0.741) and hier.acc (0.863 vs 0.865) essentially unchanged (nudges diffusion leveling 0.974 → 1.000).
6. **Baselines and the GPT-4 outlier:** MarkItDown 0.000 (no PDF headings); Docling 0.663 on 11 docs with far weaker hier.acc (0.554 vs full 0.865). GPT-4 stays the precision outlier (full precision 0.209); ordinal-suffix numeric gate closed (gpt-4 rule_only candidates 676 → 537).
7. **Not specific to one model:** see cross-model replication above.

## To regenerate (raw artifacts)

The original driver `evaluation/e2e_protocol.py` is lost. To rebuild reproducible raw JSON:

1. Re-implement an e2e runner over `tests/data/long_docs/*.pdf` driving `CaliperParser` under nine configs:
   - `full` = defaults; `strict` = `ParserConfig(enable_anchor_downgrade=False)`; `no_candidate` = `ParserConfig(enable_heading_candidates=False)`; `no_font_xval` = `ResolverConfig(level_jump_font_size_tolerance=0.0)`; `rule_only` = candidates → anchors with no LLM call;
   - `no_pdf_layout` / `no_header_footer` require PdfProvider flags (not yet scaffolded — see `ROUND4_EXPERIMENT_REPORT.md` "pending flag/scaffold");
   - `MarkItDown` / `Docling` baselines export Markdown headings, scored with `compute_section_f1`.
2. Score with `evaluation/metrics.compute_section_f1` (|Δid| ≤ 3 + title), `num_runs=3`, models `deepseek-v4-flash` / `deepseek-chat` / `qwen-turbo` (needs API keys; non-trivial cost).
