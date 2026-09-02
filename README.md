# Constellation

**Deterministic document structure parsing with a bounded LLM control layer.**

Constellation converts DOCX and PDF documents into a logical section tree
(and lossless Markdown/JSON) by separating *semantic control* from
*content transport*. A rule-based pipeline owns every character of the
output; a language model is admitted only as a constrained observer that
picks among deterministically generated heading candidates and can never
touch content.

> **Research artifact.** This repository accompanies a manuscript
> currently under review. It contains the parsing algorithm, the
> evaluation harness, and the ground truth needed to reproduce the
> reported numbers. The hosted service this code was extracted from
> (HTTP API, MCP delivery layer, web console, authentication, metering,
> and enterprise document-source connectors) is not part of this release.

---

## Why the architecture looks like this

Rule-based parsers break when hierarchy is expressed through informal
typography — a slightly larger bold run, a numbering convention, a line
that is short and followed by a paragraph. End-to-end LLM extraction
handles that ambiguity, but regenerates every character on the way out,
which is where hallucination, silent rewriting, and omission come from.

Constellation splits the problem:

| Stage | Role | LLM involved |
|---|---|---|
| **1. Physical extraction** | DOCX/PDF → ordered `Block` stream with run-level provenance (font size, weight, alignment, numbering, spacing) | No |
| **2. Skeleton compression** | I/P-frame classification, run-length folding, and a candidate-aware sparse view of the document | No |
| **2.5. Candidate generation** | Typed positive/negative evidence fused into heading probabilities after correlation-group deduplication | No |
| **3. Routing** | The model observes only the sparse control view and returns anchors, which are monotonically aligned back to physical blocks | **Yes** |
| **4. Resolution & closure** | Fuzzy anchoring, unified hierarchy repair, reverse audit, and forced closure over the full block interval | No |

The consequence that matters: because Stage 4 assembles output by
*interval ownership over the original blocks* rather than by generation,
every source character is preserved by construction, and every block has
exactly one owner. The `partition` check in the evaluation harness
verifies this directly.

---

## What is reproducible here

The headline metrics are **deterministic and LLM-free** — no API key, no
sampling temperature, no model-version drift:

| Result | Value | Source |
|---|---|---|
| Candidate recall, 12 long PDFs (block-proximity discipline) | **471/488 = 96.5%** micro | `experiment_results/offline_paper_metrics.md` |
| Candidate recall vs. Docling 2.77.0 (title-only discipline) | **93.8%** micro vs. Docling **48.0%** | `experiment_results/docling_baseline.md` |
| Candidate recall, zero-typography synthetic PDFs | **47/47 = 100.0%** | `experiment_results/offline_paper_metrics.md` |
| Forced-closure character coverage (DOCX fixture) | **24,834/24,834 = 100.0%**, partition ok | `experiment_results/offline_paper_metrics.md` |
| Average control-view token savings | **58.6%**, at an **11.9%** decision budget | `experiment_results/offline_paper_metrics.md` |

Threshold sensitivity for candidate admission is in
`experiment_results/threshold_sensitivity.md`; recall is flat across the
swept range, so the numbers above are not a tuned operating point.

`offline_paper_metrics.md`, `threshold_sensitivity.md` and
`docling_baseline.md` regenerate **byte-identically** from a clean
install of `requirements.txt`.

> **The PyMuPDF pin is load-bearing.** Ground-truth `block_id` values are
> indices into the extracted block stream, and PyMuPDF changed its text
> extraction in 1.25 — `bert.pdf` yields 907 blocks on 1.24.14 and 727 on
> 1.25 or later. Installing a newer PyMuPDF silently misaligns every
> ground-truth file and drops the reported candidate recall from 96.5% to
> 17.6%. Install the pinned version, or realign the ground truth first
> with `python -m evaluation.repair_gt` and
> `python scripts/manual/realign_manual_gt.py`.

---

## Quick start

```bash
git clone https://github.com/1911342723/Constellation.git
cd Constellation
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Stages 1, 2, 2.5 and 4 run entirely offline. Stage 3 needs an
OpenAI-protocol endpoint; copy `.env.example` to `.env` and fill in
`LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` (DeepSeek, OpenAI, or any
compatible gateway).

```bash
python examples/minimal_parse.py path/to/document.pdf
```

Using it as a library:

```python
from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.parser import CaliperParser

blocks = PdfProvider().extract("paper.pdf")   # Stage 1
tree = CaliperParser().parse(blocks)          # Stages 2-4

print(tree.get_stats())
open("paper.md", "w", encoding="utf-8").write(tree.to_markdown())
```

---

## Reproducing the reported results

### 1. Fetch the evaluation set

The twelve long PDFs are third-party publications (eleven arXiv
preprints and one NIST publication). They are referenced by source
rather than redistributed:

```bash
python -m evaluation.fetch_dataset
```

This downloads them into `tests/data/long_docs/` and verifies each
against the SHA-256 of the copy the reported numbers were computed on.
Provenance, identifiers and checksums live in
`evaluation/datasets/long_docs_manifest.json`.

> **If a checksum differs**, the publisher has issued a newer revision.
> Ground-truth `block_id` values are coupled to the exact byte stream, so
> realign before evaluating: `python -m evaluation.repair_gt` for the
> TOC-derived documents, and `python scripts/manual/realign_manual_gt.py`
> for the two with manually reconstructed ground truth (`bert`, `resnet`).

The zero-typography PDFs in `tests/data/no_style/` are synthetic and
ship with the repository, as does the DOCX closure fixture.

### 2. Run the offline suites (no API key)

```bash
python -m evaluation.offline_paper_metrics     # candidate recall, budget, closure
python -m evaluation.threshold_sensitivity     # admission threshold sweep
python -m evaluation.docling_baseline          # baseline comparison
```

Each writes a Markdown report plus a `_raw.json` next to the committed
ones in `experiment_results/`. Re-running the *recorded* Docling column
requires the optional pinned dependency:

```bash
pip install -r requirements-docling.txt        # docling==2.77.0
```

### 3. End-to-end evaluation (needs an API key)

```bash
python -m evaluation.run_longdocs_eval                     # long-PDF set
python -m evaluation.run_evaluation --data-dir tests/data  # the DOCX fixture
```

End-to-end scores depend on which model confirmed how much of the
deterministic ceiling, so they are a property of the model vendor and
version rather than of the architecture. The offline suites above are
the numbers the manuscript reports.

`evaluation/paper_experiments.py` drives a DOCX benchmark corpus that is
**not distributed** — those were internal documents. Point `--data-dir`
at your own corpus, with matching ground truth under `--gt-dir` in the
schema described by `evaluation/ground_truth/_schema.json`. For the same
reason, `evaluation/ground_truth/` contains annotations whose source DOCX
files are absent; `test_demo.json` is the one that pairs with a
distributed document.

---

## Ground truth

Ground-truth construction is documented in `tests/data/GT_README.md`.
Two rules govern it:

- **Ground truth is never derived from the system under test.** TOC
  entries, author-provided reference structure, and manual annotation are
  admissible; the candidate generator is not, because that would be
  circular.
- **`block_id = -1`, never `0`, marks an unlocatable heading.** Zero is a
  valid block id, and using it as a failure sentinel silently
  manufactured F1 = 0.000 scores before this was caught.

Each long-document ground-truth file carries a `gt_source` field
recording which of the three provenance classes it belongs to. The two
`manual_expert_reference` documents (`bert`, `resnet`) have no embedded
TOC; their section structure was AI-drafted from public knowledge and
physically verified against blocks, but not independently double
annotated — this is disclosed rather than smoothed over.

---

## Repository layout

```
modules/parser/            Stages 2-4: the core algorithm
  compressor.py              Stage 2   I/P frames, RLE folding, sparse view
  heading_candidates.py      Stage 2.5 deterministic candidate generation
  evidence.py                          typed positive/negative evidence
  router.py                  Stage 3   LLM candidate confirmation
  anchor_alignment.py                  monotonic anchor alignment
  global_inference.py                  document-wide NONE/L1..L6 decoding
  resolver.py                Stage 4   fuzzy anchoring, audit, forced closure
  hierarchy.py                         the pipeline's single repair point
  document_tree.py                     tree model, Markdown/JSON rendering
  prompts/                             prompt templates (hot-reloaded)

infrastructure/
  models/block.py            Block atom, meta-tags, Markdown rendering
  providers/                 Stage 1 extractors (DOCX, PDF, CSV/XLSX, MD, text)
  ai/llm_client.py           OpenAI-protocol client, pooling, token budgeting

app/core/                    Shared exception hierarchy and pipeline settings

evaluation/                  Metrics, experiment drivers, GT tooling
experiment_results/          Committed reports backing the reported numbers
tests/                       Regression suite for the algorithm
scripts/manual/              One-shot diagnostics and GT maintenance tools
examples/minimal_parse.py    Smallest end-to-end run
```

`app/core/` retains only the exception hierarchy and the parser
configuration object that the pipeline imports; the delivery layer that
used to sit above it is not published.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Run from the repository root — the suite resolves imports against the
working directory. Every test is deterministic and offline; LLM calls are
mocked throughout.

---

## Citation

A citation entry will be added once the manuscript is accepted. Until
then, please cite this repository by URL.

---

## License

[Apache License 2.0](LICENSE).

The evaluation documents downloaded by `evaluation/fetch_dataset.py` are
**not** covered by this license. They remain under the terms set by their
respective publishers and are referenced by source rather than
redistributed here.
