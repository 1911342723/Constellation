# Round4 Parser Upgrade Report

Generated: 2026-06-08

Commit: `ebd0897` (dirty worktree; exact status captured in `round4_offline_raw.json`)

Model assumption for future full runs: `deepseek-chat`, temperature `0.1`, `num_runs=1`; final paper subset can be rerun with `num_runs=3`.

## What Changed

- Added Stage 2.5 deterministic `HeadingCandidate` generation for DOCX/PDF.
- Restricted Stage 3 LLM routing to candidate block IDs inside each window.
- Added resolver hierarchy priority: numbering > explicit style > LLM > font cross-validation.
- Added candidate-based inverse audit to promote missed headings.
- Upgraded PDF Stage 1 with page/bbox/line/column metadata, header/footer filtering, two-column ordering, and body-line merging.
- Upgraded DOCX Stage 1 with localized heading styles, style_id, outline level, list level, and numbering level metadata.
- Fixed parser cache key to hash full block JSON payload and pipeline config.
- Fixed evaluation hierarchy accuracy to use matched pairs and added block/Markdown coverage fields.
- Fixed pytest collection for CairoSVG and manual examples.

## Verification

| Command | Result |
|:--|:--|
| `pytest tests/test_round4_parser_upgrade.py -q` | 7 passed |
| `pytest tests/test_pdf_provider.py tests/test_round3_fixes.py -q` | 60 passed |
| `pytest tests -q` | 133 passed, 1 warning |
| `pytest -q` | 133 passed, 8 skipped, 1 warning |

Skipped tests are manual examples requiring `localhost:8001`, a local `test_document.docx`, optional LLM access, or Cairo runtime.

## Offline Experiments

These are Stage1/Stage2/candidate experiments only; no external LLM calls were made.

| Dataset | Docs | Avg Blocks | Avg Candidates | Avg Compression % | Avg Token Savings % |
|:--|--:|--:|--:|--:|--:|
| no_style_pdf | 4 | 16.2 | 7.5 | -0.7 | 0.2 |
| docx_benchmarks | 7 | 12.6 | 2.4 | -100.1 | -72.2 |
| paper_pdfs | 5 | 3285.2 | 1034.0 | 7.1 | 8.6 |

Raw data: `experiment_results/round4_offline_raw.json`.

## Failure Samples

Primary observed failure mode is negative compression on short or heavily meta-tagged documents. This is expected for Stage1/2-only measurements because candidate/layout metadata adds fixed overhead before LLM routing.

See `experiment_results/round4_failure_samples.md` for the full table.

## Pending Full LLM Experiments

Run after API budget is available:

| Ablation | Status |
|:--|:--|
| Full | pending full LLM run |
| No Candidate Router | script-ready via `ParserConfig(enable_heading_candidates=False)` |
| No PDF Layout | pending flag/scaffold |
| No Header/Footer Filter | pending flag/scaffold |
| No Font Cross-Val | script-ready via resolver config |
| Serial Only | script-ready via parser config |
| Rule-Only Candidate | pending flag/scaffold |

Recommended command shape:

```bash
python -m evaluation.paper_experiments --data-dir tests/data/papers --gt-dir tests/data/papers/ground_truth --num-runs 1 --output experiment_results/round4_full_papers_report.md
```
