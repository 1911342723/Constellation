# Constellation

A zero-loss document structure extraction engine via Control-Data Flow Decoupling.

[![Paper](https://img.shields.io/badge/Paper-Zenodo-blue)](https://zenodo.org/records/18917045)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)

[中文](README.md) | [📄 Paper (Zenodo)](https://zenodo.org/records/18917045)

---

## Overview

Constellation is a document structure parsing tool that converts unstructured Word documents into precise chapter trees through a four-stage pipeline. The core idea is to confine the LLM's role to "placing cursors" — it only marks chapter boundaries on a compressed skeleton, while deterministic algorithms handle character-level lossless reassembly.

This design stems from a key observation: LLMs excel at fuzzy semantic extraction but are fundamentally unreliable at character-level lossless reconstruction. Constellation separates these two responsibilities entirely.

## Core Architecture

Four-stage pipeline:

```
Stage 1: Physical Reduction     .docx --> List[Block]
Stage 2: Skeleton Compression   List[Block] --> Minimal skeleton text (90-95% compression)
Stage 3: AI Cursor Routing      Skeleton --> Chapter anchors [{block_id, title, level, snippet}]
Stage 4: Cursor Closure          Anchors + Raw Blocks --> Document tree --> Markdown
```

### Stage 1 — Physical Reduction

A hybrid XML engine (python-docx as the primary engine, lxml as the supplementary engine) converts .docx files into a standardized Block sequence. Each Block carries physical feature metadata (bold, font size, alignment, heading style). Supports paragraphs, tables, images, OMML formulas, and floating text boxes.

### Stage 2 — Skeleton Compression

I-frame / P-frame classification: structurally significant blocks (headings, multimedia, formatted blocks) are preserved as I-frames with injected Meta-Tags; body text blocks are treated as P-frames with head/tail truncation. Consecutive P-frames are folded via run-length encoding, but each folded paragraph retains a first-line summary (v2 degraded-visibility mechanism), ensuring no hidden headings are swallowed.

### Stage 3 — AI Cursor Routing

The LLM marks chapter boundaries on the skeleton text and outputs structured JSON. Each anchor includes a `snippet` field (first 30 characters of the heading text) for cross-validation in Stage 4. Prompt templates are externalized to `modules/parser/prompts/` and support hot-reloading.

### Stage 4 — Cursor Closure and Assembly

Three-phase post-processing:
1. Fuzzy anchor correction — cross-validates `block_id` against `snippet` using Levenshtein distance, automatically correcting drift within a sliding search radius
2. Hierarchy compliance repair — detects and fixes level jumps, with font-size physical features as auxiliary evidence
3. Forced closure — computes non-overlapping, gap-free intervals; stack-based algorithm builds the document tree

## Getting Started

### Requirements

- Python 3.10+
- An LLM API key compatible with the OpenAI protocol (DeepSeek / OpenAI / Claude, etc.)

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

Create a `.env` file with your API credentials:

```env
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

### Library Usage

```python
from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.parser import CaliperParser

# Stage 1: Extract blocks
provider = DocxProvider()
blocks = provider.parse("document.docx")

# Stage 2-4: Parse
parser = CaliperParser()
tree = parser.parse(blocks)

# Output
print(tree.to_markdown())
print(tree.to_json(indent=2))
```

Custom configuration (no dependency on global settings):

```python
from modules.parser.config import CompressorConfig, ResolverConfig

parser = CaliperParser(
    compressor_config=CompressorConfig(head_chars=60, rle_threshold=5),
    resolver_config=ResolverConfig(fuzzy_anchor_radius=8),
)
```

### API Service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 28001
```

Interactive API docs: `http://localhost:28001/docs`

Endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Health check |
| POST | `/api/v1/parse/docx` | Upload DOCX, return Block list (Stage 1 only) |
| POST | `/api/v1/parse/full` | Upload DOCX, full pipeline, return complete result |
| POST | `/api/v1/parse` | Accept Block list, execute Stage 2-4 |
| POST | `/api/v1/parse/paper` | Typesetting-system-specific output format |

## Project Structure

```
Constellation/
├── app/                          # Delivery layer
│   ├── main.py                   # FastAPI entry + global exception handlers
│   ├── api/
│   │   ├── routes.py             # API routes
│   │   └── schemas.py            # Request/response models
│   └── core/
│       ├── config/settings.py    # App configuration (pydantic-settings)
│       └── exceptions.py         # Exception hierarchy
├── modules/                      # Business layer
│   └── parser/
│       ├── parser.py             # Main parser (CaliperParser)
│       ├── compressor.py         # Stage 2: Skeleton compressor
│       ├── router.py             # Stage 3: LLM router
│       ├── resolver.py           # Stage 4: Interval resolver
│       ├── document_tree.py      # Document tree data structure
│       ├── schemas.py            # Internal data models
│       ├── config.py             # Decoupled configs (CompressorConfig, etc.)
│       └── prompts/              # LLM prompt templates
│           ├── router_system.txt
│           └── router_user.txt
├── infrastructure/               # Infrastructure layer
│   ├── ai/
│   │   └── llm_client.py        # LLM client (singleton + connection pool)
│   ├── models/
│   │   └── block.py             # Block atomic data model
│   └── providers/
│       └── docx_provider.py     # Stage 1: DOCX hybrid XML engine
├── tests/                        # Tests
├── examples/                     # Example scripts
├── docs/                         # Technical documentation
└── requirements.txt
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| I-frame / P-frame classification | Preserve structurally significant blocks in full; truncate body text to balance information completeness against token cost |
| P-frame degraded visibility | Folded paragraphs retain first-line summaries, eliminating blind spots where unconventional headings could be swallowed |
| Snippet cross-validation | LLM returns a heading text fragment; Levenshtein distance auto-corrects block_id drift |
| Short snippet exact matching | Snippets under 5 characters bypass fuzzy matching to avoid Levenshtein noise on short strings |
| Font-size-assisted hierarchy repair | Compares physical font size against same-level ancestors during level-jump repair, avoiding blind clamping |
| Config decoupling | Core modules accept Config objects via constructor injection, independent of global settings — usable as a standalone library |
| LLM client singleton | Shares the httpx connection pool, preventing port exhaustion under concurrent load |
| Global exception handling | No try/except boilerplate in routes; FastAPI global interceptors map domain exceptions to HTTP status codes |
| Externalized prompts | LLM prompts stored as template files with hot-reload support; tunable without code changes |

## Configuration Reference

All settings can be configured via `.env` file or environment variables:

```env
# LLM
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=4096

# Skeleton compression
SKELETON_HEAD_CHARS=40
SKELETON_TAIL_CHARS=30
SKELETON_ENABLE_RLE=true
SKELETON_RLE_THRESHOLD=3
SKELETON_MAX_RLE_GROUP=10

# Sliding window (long documents)
SLIDING_WINDOW_THRESHOLD=500
WINDOW_SIZE=300
WINDOW_OVERLAP=50

# Fuzzy anchoring
FUZZY_ANCHOR_RADIUS=5
FUZZY_MIN_SIMILARITY=0.4
```

## Tech Stack

- Python 3.10+
- FastAPI + Uvicorn
- Pydantic v2 + pydantic-settings
- OpenAI SDK (compatible with DeepSeek/Claude and other OpenAI-protocol providers)
- python-docx + lxml (hybrid XML engine)
- python-Levenshtein (fuzzy anchor correction)

## Citation

If Constellation is helpful to your research, please cite our paper:

> **Constellation: Lossless Document Structuring via Control-Data Flow Decoupling**
>
> 📄 [Zenodo Preprint](https://zenodo.org/records/18917045)

```bibtex
@article{constellation2025,
  title={Constellation: Lossless Document Structuring via Control-Data Flow Decoupling},
  year={2025},
  doi={10.5281/zenodo.18917045},
  publisher={Zenodo}
}
```

## Version

Current version: 1.0.0

## License

MIT License
