====================================================================================================
  Constellation Benchmark Data Collection
====================================================================================================

## Table: Extraction & Compression Metrics

| File | Blocks | Chars | Skeleton Chars | Compression | Extract(s) | Compress(s) | Windows |
|:-----|-------:|------:|---------------:|------------:|-----------:|------------:|--------:|
| chaotic_stress_test.docx | 1793 | 537,491 | 117,748 | 78.1% | 8.427 | 0.0103 | 7 |
| extreme_stress_test.docx | 11546 | 10,072,403 | 737,994 | 92.7% | 26.796 | 0.0484 | 46 |
| large_test.docx | 2043 | 1,335,672 | 140,329 | 89.5% | 88.290 | 0.0076 | 8 |
| stress_test_100w.docx | 3101 | 1,008,974 | 202,584 | 79.9% | 15.247 | 0.0135 | 13 |
| test_demo.docx | 93 | 9,570 | 4,976 | 48.0% | 0.228 | 0.0003 | 1 |
| ibm_equations.docx | 30 | 1,742 | 1,753 | -0.6% | 0.048 | 0.0001 | 1 |
| ibm_grouped_images.docx | 12 | 238 | 907 | -281.1% | 0.029 | 0.0000 | 1 |
| ibm_headers.docx | 23 | 322 | 1,087 | -237.6% | 0.060 | 0.0001 | 1 |
| ibm_lorem.docx | 6 | 3,500 | 737 | 78.9% | 0.010 | 0.0001 | 1 |
| ms_equations.docx | 8 | 198 | 680 | -243.4% | 0.010 | 0.0000 | 1 |
| ms_test.docx | 13 | 4,559 | 1,222 | 73.2% | 0.043 | 0.0000 | 1 |

## Table: Block Type Distribution

| File | Text | Table | Image | Formula | Headings | Coverage |
|:-----|-----:|------:|------:|--------:|---------:|---------:|
| chaotic_stress_test.docx | 1632 | 57 | 0 | 0 | 368 | 99.99% |
| extreme_stress_test.docx | 11256 | 92 | 0 | 0 | 0 | 100.23% |
| large_test.docx | 1998 | 45 | 0 | 0 | 311 | 100.30% |
| stress_test_100w.docx | 3101 | 0 | 0 | 0 | 600 | 100.61% |
| test_demo.docx | 84 | 5 | 4 | 0 | 18 | 100.52% |
| ibm_equations.docx | 12 | 0 | 0 | 18 | 0 | 96.97% |
| ibm_grouped_images.docx | 6 | 0 | 6 | 0 | 2 | 100.00% |
| ibm_headers.docx | 23 | 0 | 0 | 0 | 8 | 109.01% |
| ibm_lorem.docx | 6 | 0 | 0 | 0 | 0 | 78.83% |
| ms_equations.docx | 4 | 0 | 0 | 4 | 0 | 103.15% |
| ms_test.docx | 11 | 1 | 1 | 0 | 4 | 99.97% |

## Summary Statistics

- Documents: 11
- Total Blocks: 18,668
- Total Characters: 12,974,669
- Total Skeleton Characters: 1,210,017
- Average Compression Ratio: -20.2%
- Average Character Coverage: 99.05%
- Total Extract Time: 139.186s
- Total Compress Time: 0.0804s
- Extract Throughput: 93,217 chars/s
- Compress Throughput: 161,325,313 chars/s
