# Algorithm acceptance — 2026-10-03

Mode: **deterministic_only**. No GT files changed.

Stored-ID scores and exact-title diagnostic snapshot scores are separate in results.json.
Unresolved/run-in GT remains in the all-GT recall denominator. Coverage applies only to extracted blocks.

| Tier | Documents | Diagnostic P | Diagnostic R | Diagnostic F1 | Unresolved GT |
|---|---:|---:|---:|---:|---:|
| long_docs | 12 | 0.256 | 0.984 | 0.406 | 27 |
| mis_styled | 24 | 1.000 | 1.000 | 1.000 | 0 |
| no_style | 4 | 1.000 | 0.745 | 0.854 | 0 |

Failed documents: 0. See per-document diagnostics and confidence channels in results.json.
