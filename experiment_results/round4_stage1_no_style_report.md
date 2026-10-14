# Constellation Experimental Results

## Table 1: Per-Document Performance

| Document | Blocks | Orig Chars | Skeleton Chars | Compress % | Candidates | Headings | F1 | Prec | Rec | Hier Acc | Block Cov | MD Char Cov | S1(ms) | S2(ms) | S3(ms) | S4(ms) | Total(ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01_numbered_uniform_font | 25 | 1542 | 1863 | -20.8 | 12 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 124 | 0 | 0 | 0 | 0 |
| 02_plain_text_no_numbering | 7 | 1378 | 1001 | 27.4 | 2 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 23 | 0 | 0 | 0 | 0 |
| 03_deep_hierarchy_uniform | 32 | 1205 | 1884 | -56.3 | 16 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 21 | 0 | 0 | 0 | 0 |
| 04_mixed_language_uniform | 1 | 1195 | 635 | 46.9 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 19 | 0 | 0 | 0 | 0 |
| **Average** | 16 | 1330 | 1346 | -0.7 | 7.5 | 0.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 46 | 0 | 0 | 0 | 0 |

## Table 2: Ablation Study

| Configuration | Avg F1 | Avg Prec | Avg Rec | Avg Hier Acc | Avg TED | Avg Block Cov | Avg MD Char Cov |
|---|---|---|---|---|---|---|---|

## Table 3: Token Efficiency

| Document | Full Tokens | Skeleton Tokens | Savings % | Windows | Chars | Skeleton Chars | Compress % |
|---|---|---|---|---|---|---|---|
| 01_numbered_uniform_font | 391 | 472 | -20.7 | 1 | 1542 | 1863 | -20.8 |
| 02_plain_text_no_numbering | 346 | 250 | 27.7 | 1 | 1378 | 1001 | 27.4 |
| 03_deep_hierarchy_uniform | 309 | 472 | -52.8 | 1 | 1205 | 1884 | -56.3 |
| 04_mixed_language_uniform | 298 | 159 | 46.6 | 1 | 1195 | 635 | 46.9 |

## Table 4: Error Analysis

| Document | FP | FN | Level Errors | Heading Count | GT Headings |
|---|---|---|---|---|---|
| 01_numbered_uniform_font | 0 | 0 | 0 | 0 | 0 |
| 02_plain_text_no_numbering | 0 | 0 | 0 | 0 | 0 |
| 03_deep_hierarchy_uniform | 0 | 0 | 0 | 0 | 0 |
| 04_mixed_language_uniform | 0 | 0 | 0 | 0 | 0 |

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
01_numbered_uniform_font & 25 & -21\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
02_plain_text_no_numbering & 7 & 27\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
03_deep_hierarchy_uniform & 32 & -56\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
04_mixed_language_uniform & 1 & 47\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\textbf{Average} & 16 & -1\% & 0.0 & \textbf{0.000} & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\end{tabular}
\end{table*}
```