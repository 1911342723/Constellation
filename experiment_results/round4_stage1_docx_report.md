# Constellation Experimental Results

## Table 1: Per-Document Performance

| Document | Blocks | Orig Chars | Skeleton Chars | Compress % | Candidates | Headings | F1 | Prec | Rec | Hier Acc | Block Cov | MD Char Cov | S1(ms) | S2(ms) | S3(ms) | S4(ms) | Total(ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ibm_equations | 26 | 1726 | 1777 | -3.0 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 44 | 0 | 0 | 0 | 0 |
| ibm_grouped_images | 7 | 53317 | 910 | 98.3 | 2 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 15 | 0 | 0 | 0 | 0 |
| ibm_headers | 22 | 300 | 1128 | -276.0 | 7 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 50 | 0 | 0 | 0 | 0 |
| ibm_lorem | 5 | 3478 | 826 | 76.3 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 10 | 0 | 0 | 0 | 0 |
| ms_equations | 5 | 180 | 744 | -313.3 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 12 | 0 | 0 | 0 | 0 |
| ms_test | 12 | 4537 | 1305 | 71.2 | 3 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 50 | 0 | 0 | 0 | 0 |
| semantic_only | 11 | 199 | 904 | -354.3 | 5 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 70 | 0 | 0 | 0 | 0 |
| **Average** | 13 | 9105 | 1085 | -100.1 | 2.4 | 0.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 36 | 0 | 0 | 0 | 0 |

## Table 2: Ablation Study

| Configuration | Avg F1 | Avg Prec | Avg Rec | Avg Hier Acc | Avg TED | Avg Block Cov | Avg MD Char Cov |
|---|---|---|---|---|---|---|---|

## Table 3: Token Efficiency

| Document | Full Tokens | Skeleton Tokens | Savings % | Windows | Chars | Skeleton Chars | Compress % |
|---|---|---|---|---|---|---|---|
| ibm_equations | 437 | 448 | -2.5 | 1 | 1726 | 1777 | -3.0 |
| ibm_grouped_images | 13330 | 229 | 98.3 | 1 | 53317 | 910 | 98.3 |
| ibm_headers | 80 | 282 | -252.5 | 1 | 300 | 1128 | -276.0 |
| ibm_lorem | 870 | 206 | 76.3 | 1 | 3478 | 826 | 76.3 |
| ms_equations | 46 | 186 | -304.3 | 1 | 180 | 744 | -313.3 |
| ms_test | 1137 | 335 | 70.5 | 1 | 4537 | 1305 | 71.2 |
| semantic_only | 91 | 265 | -191.2 | 1 | 199 | 904 | -354.3 |

## Table 4: Error Analysis

| Document | FP | FN | Level Errors | Heading Count | GT Headings |
|---|---|---|---|---|---|
| ibm_equations | 0 | 0 | 0 | 0 | 0 |
| ibm_grouped_images | 0 | 0 | 0 | 0 | 0 |
| ibm_headers | 0 | 0 | 0 | 0 | 0 |
| ibm_lorem | 0 | 0 | 0 | 0 | 0 |
| ms_equations | 0 | 0 | 0 | 0 | 0 |
| ms_test | 0 | 0 | 0 | 0 | 0 |
| semantic_only | 0 | 0 | 0 | 0 | 0 |

## LaTeX Table 1 (copy-paste)

```latex
\begin{table*}[t]
\centering
\caption{Per-document performance of Constellation.}
\label{tab:results}
\begin{tabular}{l|c|c|c|c|c|c|c|c}
\hline
Document & Blocks & Compress\% & Headings & F1 & Prec & Rec & Hier Acc & Time(ms) \\
\hline
ibm_equations & 26 & -3\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
ibm_grouped_images & 7 & 98\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
ibm_headers & 22 & -276\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
ibm_lorem & 5 & 76\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
ms_equations & 5 & -313\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
ms_test & 12 & 71\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
semantic_only & 11 & -354\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\textbf{Average} & 13 & -100\% & 0.0 & \textbf{0.000} & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\end{tabular}
\end{table*}
```