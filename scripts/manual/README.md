# scripts/manual — one-shot tools

Diagnostics and ground-truth maintenance scripts. These are run on
demand; they are not part of the test suite or the evaluation pipeline.

| Script | Purpose | Run |
|---|---|---|
| `run_core_algorithm_benchmark.py` | Offline benchmark of Stages 2–4 (no LLM) | `python scripts/manual/run_core_algorithm_benchmark.py` |
| `run_coverage_audit.py` | Forced-closure lossless coverage audit — assembles from pseudo-anchors to verify zero content loss | `python scripts/manual/run_coverage_audit.py` |
| `run_pdf_pipeline.py` | Full pipeline on a single PDF (calls the LLM) | `python scripts/manual/run_pdf_pipeline.py <pdf>` |
| `run_zero_feature_demo.py` | Skeleton demo on a document with no typographic signal | `python scripts/manual/run_zero_feature_demo.py` |
| `create_no_style_docs.py` | Regenerate the zero-typography PDFs and their ground truth | `python scripts/manual/create_no_style_docs.py` |
| `diagnose_failure_chain.py` | Stage-by-stage failure chain for one document: blocks → candidates → alignment (no LLM) | `python scripts/manual/diagnose_failure_chain.py` |
| `diagnose_candidate_recall.py` | Which ground-truth headings the candidate layer missed, and why | `python scripts/manual/diagnose_candidate_recall.py` |
| `rebuild_long_docs_gt.py` | Rebuild ground truth for documents with no embedded TOC (reference structure + physical alignment + manual overrides) | `python scripts/manual/rebuild_long_docs_gt.py --write` |
| `realign_manual_gt.py` | Realign `block_id` for the manually annotated ground truth (`bert`, `resnet`). Mandatory after any `PdfProvider` change, alongside `evaluation/repair_gt.py`. Aborts without writing if any entry fails to match | `python scripts/manual/realign_manual_gt.py` |
| `plot_docling_baseline.py` | Render the Docling comparison chart from the recorded report | `python scripts/manual/plot_docling_baseline.py` |
| `repro_fuzzy_reorder.py` | Minimal reproduction: fuzzy correction reorders anchors, making interval splitting emit a block twice | `python scripts/manual/repro_fuzzy_reorder.py` |
| `add_abstract_to_gt.py` | ~~Insert Abstract entries into ground truth~~ **superseded** — the logic is now idempotent inside `evaluation/repair_gt.py`; kept for reference |

Output goes to `scripts/manual/output/` (git-ignored).

Ground-truth conventions are defined in `tests/data/GT_README.md`.
