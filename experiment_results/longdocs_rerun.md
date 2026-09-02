# Long-Document Rerun (post-fix, repaired GT)

> Generated 2026-06-11 | model: qwen-turbo | fixes in effect: small-caps join, inline merge, candidate tightening, out-of-candidate downgrade, repaired GT
> Baseline for comparison: FINAL_EXPERIMENT_REPORT 3.3 (avg F1=0.176, recall=0.569 on 12 docs with broken GT, DeepSeek)

| Document | Blocks | GT | Pred | P | R | **F1** | Hier.Acc | Time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| attention_is_all_you_need | 916 | 22 | 48 | 0.417 | 0.909 | **0.571** | 0.950 | 17.4s |
| **Average (1)** | | | | 0.417 | 0.909 | **0.571** | 0.950 | 17.4s |

## Missed headings (FN) per document

### attention_is_all_you_need (2 FN)
- Scaled Dot-Product Attention
- Multi-Head Attention
