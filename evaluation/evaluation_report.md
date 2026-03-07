# Constellation Evaluation Report

Data directory: `tests\data`
Ground truth directory: `evaluation\ground_truth`
Runs per document: 1

### ibm_lorem.docx

| Metric | Value |
|:-------|------:|
| Precision | 0.0000 |
| Recall | 0.0000 |
| **F1** | **0.0000** |
| TP / FP / FN | 0 / 1 / 0 |
| Hierarchy Accuracy | 0.0000 |
| Tree Edit Distance | 1.0 |
| Character Recall | 1.0029 |

**False positives (FP):**
- [ID=0] L1: Document

### ms_test.docx

| Metric | Value |
|:-------|------:|
| Precision | 0.6667 |
| Recall | 1.0000 |
| **F1** | **0.8000** |
| TP / FP / FN | 2 / 1 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 1.0 |
| Character Recall | 0.9982 |

**False positives (FP):**
- [ID=6] L2: d666f1f7-46cb-42bd-9a39-9a39cf2a509f

### 1_nested_hell.docx

| Metric | Value |
|:-------|------:|
| Precision | 1.0000 |
| Recall | 1.0000 |
| **F1** | **1.0000** |
| TP / FP / FN | 1 / 0 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 0.0 |
| Character Recall | 0.8950 |

### 2_math_fidelity.docx

| Metric | Value |
|:-------|------:|
| Precision | 1.0000 |
| Recall | 1.0000 |
| **F1** | **1.0000** |
| TP / FP / FN | 1 / 0 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 0.0 |
| Character Recall | 0.8984 |

### 3_multilevel_list.docx

| Metric | Value |
|:-------|------:|
| Precision | 1.0000 |
| Recall | 1.0000 |
| **F1** | **1.0000** |
| TP / FP / FN | 1 / 0 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 0.0 |
| Character Recall | 0.9111 |

### 4_floating_objects.docx

| Metric | Value |
|:-------|------:|
| Precision | 1.0000 |
| Recall | 1.0000 |
| **F1** | **1.0000** |
| TP / FP / FN | 1 / 0 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 0.0 |
| Character Recall | 0.9174 |

### test_demo.docx

| Metric | Value |
|:-------|------:|
| Precision | 0.9412 |
| Recall | 1.0000 |
| **F1** | **0.9697** |
| TP / FP / FN | 16 / 1 / 0 |
| Hierarchy Accuracy | 1.0000 |
| Tree Edit Distance | 1.0 |
| Character Recall | 0.9691 |

**False positives (FP):**
- [ID=31] L3: D

---
## Summary

| File | F1 | Precision | Recall | Hier.Acc | TED | CharRecall | Time(s) |
|:-----|---:|----------:|-------:|---------:|----:|-----------:|--------:|
| ibm_lorem.docx | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 1.0 | 1.0029 | 0.75 |
| ms_test.docx | 0.8000 | 0.6667 | 1.0000 | 1.0000 | 1.0 | 0.9982 | 1.84 |
| 1_nested_hell.docx | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0 | 0.8950 | 0.69 |
| 2_math_fidelity.docx | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0 | 0.8984 | 0.72 |
| 3_multilevel_list.docx | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0 | 0.9111 | 0.87 |
| 4_floating_objects.docx | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0 | 0.9174 | 0.84 |
| test_demo.docx | 0.9697 | 0.9412 | 1.0000 | 1.0000 | 1.0 | 0.9691 | 4.33 |

**Average F1**: 0.8242
**Average Hierarchy Accuracy**: 0.8571