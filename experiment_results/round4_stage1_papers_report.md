# Constellation Experimental Results

## Table 1: Per-Document Performance

| Document | Blocks | Orig Chars | Skeleton Chars | Compress % | Candidates | Headings | F1 | Prec | Rec | Hier Acc | Block Cov | MD Char Cov | S1(ms) | S2(ms) | S3(ms) | S4(ms) | Total(ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| attention_is_all_you_need | 1012 | 48828 | 63968 | -31.0 | 440 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 2204 | 5 | 0 | 0 | 0 |
| bert | 1135 | 64543 | 47725 | 26.1 | 391 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 3890 | 6 | 0 | 0 | 0 |
| gpt3 | 5151 | 232814 | 365206 | -56.9 | 2254 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 17214 | 21 | 0 | 0 | 0 |
| llm_survey | 8148 | 862189 | 378532 | 56.1 | 1603 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 34288 | 43 | 0 | 0 | 0 |
| resnet | 980 | 65931 | 38915 | 41.0 | 482 | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 4891 | 6 | 0 | 0 | 0 |
| **Average** | 3285 | 254861 | 178869 | 7.1 | 1034.0 | 0.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 12497 | 16 | 0 | 0 | 0 |

## Table 2: Ablation Study

| Configuration | Avg F1 | Avg Prec | Avg Rec | Avg Hier Acc | Avg TED | Avg Block Cov | Avg MD Char Cov |
|---|---|---|---|---|---|---|---|

## Table 3: Token Efficiency

| Document | Full Tokens | Skeleton Tokens | Savings % | Windows | Chars | Skeleton Chars | Compress % |
|---|---|---|---|---|---|---|---|
| attention_is_all_you_need | 12459 | 16002 | -28.4 | 4 | 48828 | 63968 | -31.0 |
| bert | 16419 | 11945 | 27.2 | 5 | 64543 | 47725 | 26.1 |
| gpt3 | 59491 | 91464 | -53.7 | 21 | 232814 | 365206 | -56.9 |
| llm_survey | 217584 | 95082 | 56.3 | 33 | 862189 | 378532 | 56.1 |
| resnet | 16727 | 9750 | 41.7 | 4 | 65931 | 38915 | 41.0 |

## Table 4: Error Analysis

| Document | FP | FN | Level Errors | Heading Count | GT Headings |
|---|---|---|---|---|---|
| attention_is_all_you_need | 0 | 0 | 0 | 0 | 0 |
| bert | 0 | 0 | 0 | 0 | 0 |
| gpt3 | 0 | 0 | 0 | 0 | 0 |
| llm_survey | 0 | 0 | 0 | 0 | 0 |
| resnet | 0 | 0 | 0 | 0 | 0 |

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
attention_is_all_you_need & 1012 & -31\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
bert & 1135 & 26\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
gpt3 & 5151 & -57\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
llm_survey & 8148 & 56\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
resnet & 980 & 41\% & 0 & 0.000 & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\textbf{Average} & 3285 & 7\% & 0.0 & \textbf{0.000} & 0.000 & 0.000 & 0.000 & 0 \\
\hline
\end{tabular}
\end{table*}
```