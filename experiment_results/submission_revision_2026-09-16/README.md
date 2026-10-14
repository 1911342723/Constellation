# Submission revision diagnostic run — 2026-09-16

**Status: diagnostic, not a validated new paper benchmark.** Existing reports were not overwritten and the original GT was not edited.

The current-worktree offline rerun reports **124/488 = 25.4%** exact-ID candidate membership, versus the older repository report's 474/488. It measures the combination of current extraction and stored reference IDs; it must not be presented as current heading-recovery accuracy before alignment is repaired and reviewed.

A candidate-independent diagnostic on Attention, BERT and Chain-of-Thought examined 89 stored headings. **61 have one normalized exact title match at a different block ID**, and 64 stored positions have title similarity below 0.6. These sets can overlap. This confirms substantial ID drift in the inspected subset; it does not establish that all misses are alignment errors or that accuracy will recover to an earlier value after repair.

All 25 DOCX interval-ownership checks passed. Those checks count source text through resolver intervals, not serialized Markdown/JSON, tables or image payloads. Estimated sparse-view token savings average 79.3%, and document-average route budget is 11.1%; these are properties of this diagnostic run, not updates to the submitted tables.

## Files

- `offline_paper_metrics.md` and `_raw.json`: separate current-worktree outputs.
- `gt_alignment_audit.json`: per-reference stored text and candidate-independent normalized exact-title suggestions for the three-document probe.
- `audit_gt_alignment.py`: the read-only alignment diagnostic; no candidate generator is used to suggest positions.
- `provenance.json`: observed source/GT/document hashes and installed package versions captured after the run. This is not a frozen source archive or proof that no concurrent edits occurred.

## Reproduction

From the repository root, using the recorded environment:

```python
import evaluation.offline_paper_metrics as m
m.OUT_MD = 'experiment_results/submission_revision_2026-09-16/offline_paper_metrics.md'
m.OUT_JSON = 'experiment_results/submission_revision_2026-09-16/offline_paper_metrics_raw.json'
m.main()
```

Then run `python experiment_results/submission_revision_2026-09-16/audit_gt_alignment.py`.

A future rerun should use a new dated directory to retain this evidence. Next: freeze a source archive, propose updated GT positions in a separate annotation snapshot, independently review ambiguity and exclusions, and only then evaluate TOC-on/off and final structure quality.
