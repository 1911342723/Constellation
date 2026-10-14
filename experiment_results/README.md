# Experiment results

Committed reports and per-document records that back the numbers
reported in the manuscript.

## Comp-HRDoc (main evaluation)

| Path | Contents |
|---|---|
| `hrdoc_2026-10-13/admission.json`, `split.json`, `dev_ids.json`, `test_ids.json` | arXiv version admission and the frozen development/test split |
| `hrdoc_2026-10-13/pdf_manifest.json` | URL, size and SHA-256 of every fetched PDF |
| `hrdoc_2026-10-13/runs/<split>-<system>/<id>.json` | One record per document and system: predictions, scores under each convention, timings, token counts |
| `hrdoc_2026-10-13/report/` | Summary tables, paired bootstrap intervals, repeat-run spread, sensitivity (`python -m evaluation.hrdoc.report`) |
| `hrdoc_2026-10-13/retries.json` | Documents whose failed run was repeated once |
| `hrdoc_2026-10-13/fp_audit.json` | Error audit of a sample of false-positive headings |
| `hrdoc_2026-10-13/superseded/` | Runs replaced during the revision, kept for transparency |
| `hrdoc_2026-10-13/{code,perf,layout}_commit.txt` | Development-repository commit ids; they correspond to the tags `ijdar-frozen`, `ijdar-perf` and `ijdar-layout` |
| `paper_2026-10-14/` | Controlled tiers (mis-styled DOCX, unstyled PDF, long PDFs) re-run on the frozen code |

## Earlier development reports

Committed reports that back the numbers reported in the manuscript. Each
Markdown report names the script that produced it and ships the raw
measurements alongside as `*_raw.json`.

## Deterministic, LLM-free (reproducible without an API key)

| Report | Produced by | Contents |
|---|---|---|
| `offline_paper_metrics.md` | `python -m evaluation.offline_paper_metrics` | Control-view savings, candidate count and budget, candidate recall on 12 long PDFs and 4 zero-typography PDFs, forced-closure coverage with the partition check |
| `docling_baseline.md` | `python -m evaluation.docling_baseline` | Heading-detection recall of the candidate layer against Docling 2.77.0 under a title-only matching discipline, with the full list of missed ground-truth headings |
| `threshold_sensitivity.md` | `python -m evaluation.threshold_sensitivity` | One-at-a-time sweep of the three candidate-admission thresholds around the production defaults |
| `gt_repair_report.md` | `python -m evaluation.repair_gt` | Ground-truth realignment run: per-document location rate and the remaining manual to-do list |
| `f1_zero_diagnosis.md` | `python -m evaluation.diagnose_f1_zero` | Stage-by-stage attribution for the three documents that scored F1 = 0, plus a health audit of all 12 ground-truth files |

`round4_*` files are the corresponding Stage 1 reports for the DOCX,
long-PDF and zero-typography subsets, and the raw offline summary.

## Reading the numbers

Results are reported per subset — short DOCX, long PDF, zero-typography
— and never merged into a single headline figure. The subsets have very
different difficulty profiles, and averaging them lets the easiest one
speak for the whole system.

Ground-truth `block_id` values are coupled to the exact PDF byte stream
and to the extractor version. Any change to `PdfProvider` behaviour (line
merging, header filtering, span joining) shifts block ids and invalidates
every ground-truth file until `python -m evaluation.repair_gt` is re-run.
Reports regenerated against realigned ground truth may therefore differ
slightly from the committed ones.

The `bert` and `resnet` ground-truth files are AI-drafted from public
knowledge and physically verified against blocks, but not independently
double annotated. This is disclosed in the manuscript and should be
spot-checked before being relied on.
