# Comp-HRDoc results (test)

| System | Docs | P | R | F1 | Level acc. | Parent acc. | TEDS | Calls | Prompt tok. | Compl. tok. |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| det | 454 | 0.757 | 0.889 | 0.818 | 0.919 | 0.912 | 0.717 |  |  |  |
| det-nooutline | 454 | 0.733 | 0.825 | 0.776 | 0.947 | 0.902 | 0.644 |  |  |  |
| det-nooutline-layout | 454 | 0.934 | 0.898 | 0.916 | 0.930 | 0.908 | 0.810 |  |  |  |
| docling | 454 | 0.916 | 0.934 | 0.925 | 0.552 | 0.552 | 0.668 |  |  |  |
| docling-numlevels | 454 | 0.916 | 0.934 | 0.925 | 0.934 | 0.887 | 0.805 |  |  |  |
| model | 454 | 0.891 | 0.910 | 0.900 | 0.949 | 0.945 | 0.828 | 1481 | 5483854 | 266935 |
| model-nooutline | 454 | 0.886 | 0.876 | 0.881 | 0.957 | 0.936 | 0.791 | 1446 | 5369700 | 259319 |
| model-nooutline-layout | 454 | 0.950 | 0.924 | 0.937 | 0.958 | 0.941 | 0.865 | 1372 | 5017773 | 273262 |
| model-nooutline-nosilence | 454 | 0.696 | 0.888 | 0.780 | 0.954 | 0.900 | 0.656 | 1446 | 5369724 | 258062 |
| outline | 454 | 0.904 | 0.670 | 0.770 | 0.897 | 0.961 | 0.650 |  |  |  |
| det-nooutline-fidelity | 100 | 0.759 | 0.859 | 0.806 | 0.954 | 0.920 | 0.681 |  |  |  |
| llm_markdown | 100 | 0.883 | 0.921 | 0.902 | 0.915 | 0.908 | 0.840 |  | 1203037 | 1086792 |
| model-nooutline-rep2 | 100 | 0.868 | 0.905 | 0.886 | 0.967 | 0.952 | 0.811 | 321 | 1189315 | 57819 |
| model-nooutline-rep3 | 100 | 0.874 | 0.906 | 0.889 | 0.968 | 0.951 | 0.817 | 321 | 1189315 | 57442 |
| model-nooutline-rep4 | 100 | 0.875 | 0.906 | 0.890 | 0.968 | 0.951 | 0.818 | 321 | 1189295 | 57400 |

## Paired bootstrap (documents resampled, 95% interval)

- model-nooutline - det-nooutline (n=454): ΔF1 +0.105 [+0.083, +0.128], ΔTEDS +0.146 [+0.127, +0.166]
- model-nooutline - model-nooutline-nosilence (n=454): ΔF1 +0.101 [+0.082, +0.121], ΔTEDS +0.134 [+0.118, +0.150]
- model-nooutline - docling (n=454): ΔF1 -0.044 [-0.056, -0.032], ΔTEDS +0.123 [+0.104, +0.142]
- model-nooutline - docling-numlevels (n=454): ΔF1 -0.044 [-0.056, -0.032], ΔTEDS -0.014 [-0.031, +0.003]
- model-nooutline - outline (n=454): ΔF1 +0.112 [+0.086, +0.139], ΔTEDS +0.141 [+0.107, +0.176]
- model - det (n=454): ΔF1 +0.083 [+0.062, +0.106], ΔTEDS +0.111 [+0.094, +0.129]
- model - model-nooutline (n=454): ΔF1 +0.019 [+0.013, +0.026], ΔTEDS +0.037 [+0.025, +0.049]
- det - det-nooutline (n=454): ΔF1 +0.041 [+0.033, +0.050], ΔTEDS +0.072 [+0.060, +0.086]
- model-nooutline-layout - model-nooutline (n=454): ΔF1 +0.056 [+0.045, +0.066], ΔTEDS +0.074 [+0.061, +0.086]
- model-nooutline-layout - det-nooutline-layout (n=454): ΔF1 +0.021 [+0.016, +0.027], ΔTEDS +0.055 [+0.043, +0.067]
- model-nooutline-layout - docling-numlevels (n=454): ΔF1 +0.012 [+0.005, +0.019], ΔTEDS +0.060 [+0.046, +0.074]
- det-nooutline-layout - det-nooutline (n=454): ΔF1 +0.139 [+0.118, +0.162], ΔTEDS +0.165 [+0.148, +0.183]
- model-nooutline - llm_markdown (n=100): ΔF1 -0.014 [-0.056, +0.030], ΔTEDS -0.027 [-0.072, +0.019]
- model-nooutline-rep4 - llm_markdown (n=100): ΔF1 -0.012 [-0.052, +0.032], ΔTEDS -0.022 [-0.067, +0.023]

## Repeat runs of the full system (n=100 documents)

- model-nooutline: F1 0.888, TEDS 0.813
- model-nooutline-rep2: F1 0.886, TEDS 0.811
- model-nooutline-rep3: F1 0.889, TEDS 0.817
- model-nooutline-rep4: F1 0.890, TEDS 0.818
