# Round4 Failure-Chain Diagnosis (BERT / ResNet / ViT)

Diagnosis oracle: expert-known section structures of the three papers, matched with the same Levenshtein >= 0.6 rule the evaluator uses.

## 1. GT Layer (Stage 0)

| Doc | PDF TOC entries | GT headings | GT block_id==0 | Verdict |
|---|---:|---:|---:|---|
| bert | 0 | 30 | 0 | GT usable |
| resnet | 0 | 16 | 0 | GT usable |
| vit | 30 | 30 | 0 | GT usable |

## 2. Pipeline Layer (Stages 1 - 2.5)

| Doc | Blocks | Body font | Font range | Windows | Compression % | Candidates | Ref headings | Found in blocks | In candidates |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|
| bert | 863 | 10.9 | 3.2-20.0 | 4 | 40.0 | 89 | 30 | 30 | 29 |
| resnet | 872 | 10.0 | 3.3-20.0 | 4 | 45.3 | 47 | 16 | 16 | 16 |
| vit | 1920 | 10.0 | 3.9-20.0 | 8 | -62.9 | 129 | 31 | 31 | 30 |

## 3. Survival chain: bert

- Block-level recall upper bound: **100.0%**
- Candidate-level recall upper bound: **96.7%**

| Ref heading | Found (sim) | block_id | font/body | bold | head-style | In skeleton | In candidates | Miss reason |
|---|---|---:|---|---|---|---|---|---|
| Abstract | yes (1.00) | 5 | 1.101 | True | False | yes | YES (1.00) |  |
| 1 Introduction | yes (1.00) | 9 | 1.101 | True | False | yes | YES (1.00) |  |
| 2 Related Work | yes (1.00) | 19 | 1.101 | True | False | yes | YES (1.00) |  |
| 2.1 Unsupervised Feature-based Approaches | yes (1.00) | 22 | 1.0 | True | False | yes | YES (1.00) |  |
| 2.2 Unsupervised Fine-tuning Approaches | yes (1.00) | 29 | 1.0 | True | False | yes | YES (1.00) |  |
| 2.3 Transfer Learning from Supervised Data | yes (1.00) | 75 | 1.0 | True | False | yes | YES (1.00) |  |
| 3 BERT | yes (1.00) | 50 | 1.018 | False | False | yes | **NO** | score 0.45 < 0.55 (block-title-heuristic=0.45) |
| 3.1 Pre-training BERT | yes (1.00) | 126 | 1.0 | True | False | yes | YES (1.00) |  |
| 3.2 Fine-tuning BERT | yes (1.00) | 169 | 1.0 | True | False | yes | YES (1.00) |  |
| 4 Experiments | yes (1.00) | 195 | 1.101 | True | False | yes | YES (1.00) |  |
| 4.1 GLUE | yes (1.00) | 197 | 1.0 | True | False | yes | YES (1.00) |  |
| 4.2 SQuAD v1.1 | yes (1.00) | 241 | 1.0 | True | False | yes | YES (1.00) |  |
| 4.3 SQuAD v2.0 | yes (1.00) | 340 | 1.0 | True | False | yes | YES (1.00) |  |
| 4.4 SWAG | yes (1.00) | 362 | 1.0 | True | False | yes | YES (1.00) |  |
| 5 Ablation Studies | yes (1.00) | 364 | 1.101 | True | False | yes | YES (1.00) |  |
| 5.1 Effect of Pre-training Tasks | yes (1.00) | 382 | 1.0 | True | False | yes | YES (1.00) |  |
| 5.2 Effect of Model Size | yes (1.00) | 386 | 1.0 | True | False | yes | YES (1.00) |  |
| 5.3 Feature-based Approach with BERT | yes (1.00) | 389 | 1.0 | True | False | yes | YES (1.00) |  |
| 6 Conclusion | yes (1.00) | 447 | 1.101 | True | False | yes | YES (1.00) |  |
| References | yes (1.00) | 449 | 1.101 | True | False | yes | YES (1.00) |  |
| A Additional Details for BERT | yes (1.00) | 546 | 1.101 | True | False | yes | YES (1.00) |  |
| A.1 Illustration of the Pre-training Tasks | yes (1.00) | 547 | 1.0 | True | False | yes | YES (1.00) |  |
| A.2 Pre-training Procedure | yes (1.00) | 610 | 1.0 | True | False | yes | YES (1.00) |  |
| A.3 Fine-tuning Procedure | yes (1.00) | 639 | 1.0 | True | False | yes | YES (1.00) |  |
| A.4 Comparison of BERT, ELMo ,and OpenAI GPT | yes (0.75) | 646 | 1.0 | True | False | yes | YES (1.00) |  |
| B Detailed Experimental Setup | yes (1.00) | 657 | 1.101 | True | False | yes | YES (1.00) |  |
| B.1 Detailed Descriptions for the GLUE Benchm | yes (0.62) | 658 | 1.0 | True | False | yes | YES (1.00) |  |
| C Additional Ablation Studies | yes (1.00) | 809 | 1.101 | True | False | yes | YES (1.00) |  |
| C.1 Effect of Number of Training Steps | yes (1.00) | 810 | 1.0 | True | False | yes | YES (1.00) |  |
| C.2 Ablation for Different Masking Procedures | yes (0.76) | 818 | 1.0 | True | False | yes | YES (1.00) |  |

### Stage 3 LLM dump: bert

- Windows routed: 4
- Raw LLM anchors: 41
- Dropped by filter: 0
- Final tree headings: 46

## 3. Survival chain: resnet

- Block-level recall upper bound: **100.0%**
- Candidate-level recall upper bound: **100.0%**

| Ref heading | Found (sim) | block_id | font/body | bold | head-style | In skeleton | In candidates | Miss reason |
|---|---|---:|---|---|---|---|---|---|
| Abstract | yes (1.00) | 7 | 1.2 | True | True | yes | YES (1.00) |  |
| 1. Introduction | yes (1.00) | 17 | 1.2 | True | True | yes | YES (1.00) |  |
| 2. Related Work | yes (1.00) | 63 | 1.2 | True | True | yes | YES (1.00) |  |
| 3. Deep Residual Learning | yes (1.00) | 67 | 1.2 | True | True | yes | YES (1.00) |  |
| 3.1. Residual Learning | yes (1.00) | 68 | 1.1 | True | False | yes | YES (1.00) |  |
| 3.2. Identity Mapping by Shortcuts | yes (1.00) | 75 | 1.1 | True | False | yes | YES (1.00) |  |
| 3.3. Network Architectures | yes (1.00) | 94 | 1.1 | True | False | yes | YES (1.00) |  |
| 3.4. Implementation | yes (1.00) | 220 | 1.1 | True | False | yes | YES (1.00) |  |
| 4. Experiments | yes (1.00) | 225 | 1.2 | True | True | yes | YES (1.00) |  |
| 4.1. ImageNet Classification | yes (0.93) | 226 | 1.1 | True | False | yes | YES (1.00) |  |
| 4.2. CIFAR-10 and Analysis | yes (1.00) | 538 | 1.1 | True | False | yes | YES (1.00) |  |
| 4.3. Object Detection on PASCAL and MS COCO | yes (1.00) | 707 | 1.1 | True | False | yes | YES (1.00) |  |
| References | yes (1.00) | 709 | 1.2 | True | True | yes | YES (1.00) |  |
| A. Object Detection Baselines | yes (1.00) | 728 | 1.2 | True | True | yes | YES (1.00) |  |
| B. Object Detection Improvements | yes (1.00) | 736 | 1.2 | True | True | yes | YES (1.00) |  |
| C. ImageNet Localization | yes (1.00) | 847 | 1.2 | True | True | yes | YES (1.00) |  |

### Stage 3 LLM dump: resnet

- Windows routed: 4
- Raw LLM anchors: 34
- Dropped by filter: 0
- Final tree headings: 38

## 3. Survival chain: vit

- Block-level recall upper bound: **100.0%**
- Candidate-level recall upper bound: **96.8%**

| Ref heading | Found (sim) | block_id | font/body | bold | head-style | In skeleton | In candidates | Miss reason |
|---|---|---:|---|---|---|---|---|---|
| Abstract | yes (1.00) | 9 | 1.2 | False | True | yes | YES (1.00) |  |
| 1 Introduction | yes (1.00) | 21 | 1.2 | False | True | yes | YES (1.00) |  |
| 2 Related Work | yes (1.00) | 57 | 1.2 | False | True | yes | YES (1.00) |  |
| 3 Method | yes (1.00) | 123 | 1.2 | False | True | yes | YES (1.00) |  |
| 3.1 Vision Transformer (ViT) | yes (1.00) | 98 | 0.78 | False | False | yes | **NO** | score 0.00 < 0.55 (no signals) |
| 3.2 Fine-tuning and Higher Resolution | yes (1.00) | 184 | 1.0 | False | False | yes | YES (1.00) |  |
| 4 Experiments | yes (1.00) | 195 | 1.2 | False | True | yes | YES (1.00) |  |
| 4.1 Setup | yes (1.00) | 202 | 1.0 | False | False | yes | YES (1.00) |  |
| 4.2 Comparison to State of the Art | yes (1.00) | 264 | 1.0 | False | False | yes | YES (1.00) |  |
| 4.3 Pre-training Data Requirements | yes (1.00) | 343 | 1.0 | False | False | yes | YES (1.00) |  |
| 4.4 Scaling Study | yes (1.00) | 445 | 1.0 | False | False | yes | YES (1.00) |  |
| 4.5 Inspecting Vision Transformer | yes (1.00) | 463 | 1.0 | False | False | yes | YES (1.00) |  |
| 4.6 Self-supervision | yes (1.00) | 472 | 1.0 | False | False | yes | YES (1.00) |  |
| 5 Conclusion | yes (1.00) | 538 | 1.2 | False | True | yes | YES (1.00) |  |
| A Multihead Self-attention | yes (1.00) | 763 | 1.2 | False | True | yes | YES (1.00) |  |
| B Experiment details | yes (1.00) | 780 | 1.2 | False | True | yes | YES (1.00) |  |
| B.1 Training | yes (1.00) | 781 | 1.0 | False | False | yes | YES (1.00) |  |
| B.1.1 Fine-tuning | yes (1.00) | 787 | 1.0 | False | False | yes | YES (1.00) |  |
| B.1.2 Self-supervision | yes (1.00) | 869 | 1.0 | False | False | yes | YES (1.00) |  |
| C Additional Results | yes (1.00) | 890 | 1.2 | False | True | yes | YES (1.00) |  |
| D Additional Analyses | yes (1.00) | 1028 | 1.2 | False | True | yes | YES (1.00) |  |
| D.1 SGD vs. Adam for ResNets | yes (1.00) | 1029 | 1.0 | False | False | yes | YES (1.00) |  |
| D.2 Transformer shape | yes (1.00) | 1173 | 1.0 | False | False | yes | YES (1.00) |  |
| D.3 Head Type and class token | yes (1.00) | 1185 | 0.8 | False | False | yes | YES (1.00) |  |
| D.4 Positional Embedding | yes (1.00) | 1245 | 1.0 | False | False | yes | YES (1.00) |  |
| D.5 Empirical Computational Costs | yes (1.00) | 1363 | 1.0 | False | False | yes | YES (1.00) |  |
| D.6 Axial Attention | yes (1.00) | 1417 | 1.0 | False | False | yes | YES (1.00) |  |
| D.7 Attention Distance | yes (1.00) | 1482 | 1.0 | False | False | yes | YES (1.00) |  |
| D.8 Attention Maps | yes (1.00) | 1489 | 1.0 | False | False | yes | YES (1.00) |  |
| D.9 ObjectNet Results | yes (1.00) | 1494 | 1.0 | False | False | yes | YES (1.00) |  |
| D.10 VTAB Breakdown | yes (1.00) | 1497 | 1.0 | False | False | yes | YES (1.00) |  |

### Stage 3 LLM dump: vit

- Windows routed: 8
- Raw LLM anchors: 64
- Dropped by filter: 0
- Final tree headings: 65
