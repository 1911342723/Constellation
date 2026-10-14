# 误报审计：作者抽查表

从论文统计的 98 条中按固定种子 1 抽取 20 条。
每一条：看“系统判成标题的行”和它前后的原文，判断它到底是不是章节标题，
然后把末尾 `author: ?` 的 `?` 改成 `agree`（同意 AI），或改成你自己的判定，后面可加备注。

三种判定：

- `genuine_heading`：真标题，只是参考答案没收录（含 Abstract、Acknowledgments、
  References、Appendix、行内编号小节）。
- `split_or_version_text`：属于参考答案里某个章节，但标题被拆成多行，或因 arXiv 版本不同而有出入。
- `error`：不是章节标题（图表文字、公式/算法行、列表项、页眉、参考文献条目；
  无编号的粗体段落标签、定理/证明标签也算 error）。

原文片段的格式：`>>` 标出被判成标题的那一行；方括号里是该行的排版（字号、是否粗体、
对齐方式），例如 `[9.0pt 粗体 右对齐]`。排版只是判断线索，不是判定。

## 1. 1509.08457 #0

- 系统判成标题的行：**ABSTRACT**（第 1 级，第 1 页）
- 原文页面：https://arxiv.org/pdf/1509.08457#page=1
- 参考答案附近的章节：1 INTRODUCTION；2 EXTERNAL FIELD DOMINANCE IN AQUAL
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [8.0pt 左对齐] 1Scottish Universities Physics Alliance, University of St Andrews, North Haugh, St Andrews, Fife, KY16 9SS, UK
   [8.0pt 左对齐] 7 April 2020
>> [9.0pt 粗体 右对齐] ABSTRACT
   [10.0pt 左对齐] The standard ΛCDM paradigm seems to describe cosmology and large scale
   [10.0pt 左对齐] structure formation very well. However, a number of puzzling observations remain on
```

author: agree

## 2. 1510.06794 #3

- 系统判成标题的行：**Acknowledgments**（第 1 级，第 2 页）
- 原文页面：https://arxiv.org/pdf/1510.06794#page=2
- 参考答案附近的章节：I. INTRODUCTION；I I. THEORY；II I. DISCUSSION
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [10.0pt 左对齐] its structure. It is now widely accepted that sequencebased proﬁle methods[10, 11] are the best method for template-based structure prediction. Noting that the 
   [10.0pt 左对齐] the present analysis help further clarify the meaning of this intricate relationship between protein sequences and structures.
>> [9.0pt 粗体 居中] Acknowledgments
   [10.0pt 左对齐] I thank Mr. Iseo Nose whose lecture on Goethe’s morphology motivated me to write this note.
   [9.0pt 左对齐] and Nucleic Acids (Cambridge Univ. Press, Cambridge, U. K., 1999).
```

author: agree

## 3. 1603.00232 #11

- 系统判成标题的行：**Value**（第 1 级，第 4 页）
- 原文页面：https://arxiv.org/pdf/1603.00232#page=4
- 参考答案附近的章节：II I. REQUIREMENTS AND MATCHED OPTICAL SOLUTIONS；A. Standard Guide Concept；B. Curved Guides；C. Multi-Channel Bender Options；IV. SUITE COST TOTALS
- **AI 判定：`error`（不是标题：图表文字）**

```
   [9.0pt 左对齐] TABLE I: 150 metre bender speciﬁcations for eﬃcient transmission of 1 ˚A neutrons.
   [9.0pt 粗体 左对齐] Parameter
>> [9.0pt 粗体 右对齐] Value
   [9.0pt 左对齐] 150 m bender width
   [9.0pt 右对齐] 4.0 cm
```

author: agree

## 4. 1605.05157 #2

- 系统判成标题的行：**77 Rue des Chantiers, 78000, Versailles, France**（第 1 级，第 1 页）
- 原文页面：https://arxiv.org/pdf/1605.05157#page=1
- 参考答案附近的章节：I. I NTRODU C T ION；II . ME THOD；A. Data preparation and generation
- **AI 判定：`error`（不是标题：题目页或页眉）**

```
   [11.0pt 居中] Email: ﬁrstname.name@mines-paristech.fr
   [11.0pt 居中] †Institut VEDECOM
>> [11.0pt 居中] 77 Rue des Chantiers, 78000, Versailles, France
   [11.0pt 居中] Email: ﬁrstname.name@vedecom.fr
   [20.0pt 左对齐] arXiv:1605.05157v2 [cs.RO] 16 Jun 2016
```

author: agree

## 5. 1608.02456 #8

- 系统判成标题的行：**H− beam**（第 2 级，第 2 页）
- 原文页面：https://arxiv.org/pdf/1608.02456#page=2
- 参考答案附近的章节：Abstract；1 Introduction；2 Why accelerate H− beams?；3 Beam loss mechanisms；3.1 Residual gas stripping；3.1.1 Gas stripping example
- **AI 判定：`error`（不是标题：图表文字）**

```
   [10.9pt 粗体 右对齐] Possible for
   [10.9pt 粗体 右对齐] Possible for
>> [10.9pt 粗体 右对齐] H+ beam
   [10.9pt 粗体 右对齐] H− beam
   [10.9pt 左对齐] Residual gas stripping
```

author: agree

## 6. 1608.02456 #28

- 系统判成标题的行：**6 Beam loss mitigation**（第 1 级，第 16 页）
- 原文页面：https://arxiv.org/pdf/1608.02456#page=16
- 参考答案附近的章节：5 Errant beam capture at SNS
- **AI 判定：`split_or_version_text`（标题拆行或版本差异）**

```
   [10.0pt 居中] Fig. 16: Example of an errant beam event in the SNS linac [22,23]
   [10.9pt 左对齐] one of the warm linac RF cavities. Yet another example of an errant beam capture is shown in Fig. 18. A fast BCM in the MEBT, just downstream of the RFQ, shows 
>> [12.0pt 粗体 左对齐] 6 Beam loss mitigation
   [10.9pt 左对齐] We have discussed a variety of causes of beam loss in linacs. We will now turn our attention to methods of mitigating beam loss. Table 3 shows some of the more 
   [10.0pt 左对齐] Fig. 17: Example of an errant beam event in the SNS linac due to the sudden collapse of the RF ﬁeld in one of the warm linac RF cavities [22,23].
```

author: agree

## 7. 1703.04824 #3

- 系统判成标题的行：**(d) Reprintable output: same music expressed differently**（第 1 级，第 2 页）
- 原文页面：https://arxiv.org/pdf/1703.04824#page=2
- 参考答案附近的章节：I . I N TRODU CT I ON: WHAT D ATA S ET?；A. Contributions and outline；II . G ROU N D T RU T H O F M USICAL SYM B O LS；A. Interfaces in the OMR pipeline
- **AI 判定：`error`（不是标题：图表文字）**

```
   [8.0pt 左对齐] a piano roll.
   [8.0pt 居中] (c) Reprintable output: re-typesetting.
>> [8.0pt 居中] (d) Reprintable output: same music expressed differently
   [10.0pt 左对齐] Fig. 1: OMR for replayability and reprintability. The input (a) encodes the sequence of pitches, durations, and onsets (b), which can be expressed in different 
   [10.0pt 左对齐] truth, although deﬁning this approximation for the variety of representations of music is very much an open problem [2], [6], [7], [11], [18], [19]. In order to
```

author:agree

## 8. 1705.08550 #0

- 系统判成标题的行：**Abstract. Mammogram classiﬁcation is directly related to computer-aided di-**（第 1 级，第 1 页）
- 原文页面：https://arxiv.org/pdf/1705.08550#page=1
- 参考答案附近的章节：1 Introduction
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [9.0pt 居中] Dept. of Computer Science, University of California, Irvine
   [9.0pt 居中] {wentaoz1, xhx}@ics.uci.edu, {qlou, ysvang}@uci.edu
>> [9.0pt 居中] Abstract. Mammogram classiﬁcation is directly related to computer-aided di-
   [20.0pt 左对齐] arXiv:1705.08550v1 [cs.CV] 23 May 2017
   [9.0pt 居中] agnosis of breast cancer. Traditional methods rely on regions of interest (ROIs)
```

author: agree

## 9. 1709.08958 #16

- 系统判成标题的行：**4.1. The Fenchel-Nielsen twist**（第 2 级，第 7 页）
- 原文页面：https://arxiv.org/pdf/1709.08958#page=7
- 参考答案附近的章节：4. Fenchel-Nielsen twist deformation
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [10.0pt 居中] 4. Fenchel-Nielsen twist deformation
   [10.0pt 左对齐] Whilst make no claim as to the originality of the material in this section it is included to set up notation give an exposition of two results which we use in S
>> [10.0pt 左对齐] 4.1. The Fenchel-Nielsen twist. We choose a simple closed curve α ⊂ Σ. Fol-
   [10.0pt 左对齐] lowing [3], cut along this curve, and take the completion of the resulting surface with respect to the path metric to obtain a possibly disconnected surface wit
   [10.0pt 左对齐] then a (left) Fenchel-Nielsen twist along α allows one to construct a new surface Σt, homeomorphic to Σ by identifying the two boundary components with a left t
```

author: agree

## 10. 1710.03202 #0

- 系统判成标题的行：**Abstract**（第 1 级，第 1 页）
- 原文页面：https://arxiv.org/pdf/1710.03202#page=1
- 参考答案附近的章节：1 INTRODUCTION；2 MODEL IMPLEMENTATION；2.1 Unsteady RANS formulation of the governing equations
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [20.0pt 左对齐] arXiv:1710.03202v2 [physics.flu-dyn] 8 Oct 2018
   [7.0pt 左对齐] Department of Earth Science and Engineering,
>> [10.0pt 粗体 左对齐] Abstract
   [7.0pt 左对齐] South Kensington Campus, Imperial College
   [7.0pt 左对齐] London, SW7 2AZ, UK
```

author: agree

## 11. 1712.03829 #29

- 系统判成标题的行：**sti ▷ ∆i ⊢ t : σi)i∈I**（第 1 级，第 26 页）
- 原文页面：https://arxiv.org/pdf/1712.03829#page=26
- 参考答案附近的章节：5. Conclusion；Acknowledgements.；References
- **AI 判定：`error`（不是标题：公式或算法行）**

```
   [10.9pt 左对齐] form:
   [10.9pt 左对齐] (Πi
>> [10.9pt 居中] sti ▷ ∆i ⊢ t : σi)i∈I
   [10.9pt 右对齐] (m)
   [10.9pt 居中] Πst ▷ +i∈I∆i ⊢ t : [σi]i∈I
```

author: agree

## 12. 1801.01228 #16

- 系统判成标题的行：**(e) Step 5**（第 1 级，第 5 页）
- 原文页面：https://arxiv.org/pdf/1801.01228#page=5
- 参考答案附近的章节：I V. RESU LT S；A. Comparison of POMDP to heuristic and random policy；B. Sample POMDP policy run in simulation and on drone；V. C O NC LUSIO NS A N D F U TU RE WORK；AC K NO WLE DGME N T
- **AI 判定：`error`（不是标题：图表文字）**

```
   [8.0pt 右对齐] (c) Step 3
   [8.0pt 左对齐] (d) Step 4
>> [8.0pt 居中] (e) Step 5
   [8.0pt 右对齐] (f) Step 6
   [8.0pt 左对齐] (g) Step 7
```

author: agree

## 13. 1801.10237 #9

- 系统判成标题的行：**23 S. Kimura, T. Takeuchi, K. Okunishi, M**（第 1 级，第 17 页）
- 原文页面：https://arxiv.org/pdf/1801.10237#page=17
- 参考答案附近的章节：（无）
- **AI 判定：`error`（不是标题：参考文献条目）**

```
   [10.9pt 左对齐] 1 L. Faddeev and L. Takhtajan, Physics Letters A 85, 375 (1981). 2 B. Lake, A. M. Tsvelik, S. Notbohm, D. Alan Tennant, T. G. Perring, M. Reehuis, C. Sekar, G. 
   [10.9pt 左对齐] 4 R. Coldea, D. A. Tennant, E. M. Wheeler, E. Wawrzynska, D. Prabhakaran, M. Telling, K. Habicht, P. Smeibidl, and K. Kiefer, Science 327, 177 (2010). 5 Z. Wang
>> [10.9pt 左对齐] 23 S. Kimura, T. Takeuchi, K. Okunishi, M. Hagiwara, Z. He, K. Kindo, T. Taniyama, and M. Itoh, Phys. Rev. Lett. 100, 057202 (2008). 24 B. Grenier, V. Simonet, 
```

author: agree

## 14. 1802.08375 #14

- 系统判成标题的行：**Conjecture. Let Ein**（第 2 级，第 5 页）
- 原文页面：https://arxiv.org/pdf/1802.08375#page=5
- 参考答案附近的章节：6 Results；6.1 CharCNN is biased towards surface form；6.2 Tying weights bottom-up
- **AI 判定：`error`（不是标题：行内标签或列表项）**

```
   [10.9pt 粗体 左对齐] 6.2 Tying weights bottom-up
   [10.9pt 左对齐] From Table 2 one can notice that tying weights without tying subword embeddings (RW) always results in worse performance than the tying both weights and embeddi
>> [10.9pt 粗体 左对齐] Conjecture. Let Ein
   [10.9pt 居中] = Θin
   [8.0pt 右对齐] 0 , Θin
```

author: agree

## 15. 1803.00686 #12

- 系统判成标题的行：**(b) Medium details**（第 1 级，第 5 页）
- 原文页面：https://arxiv.org/pdf/1803.00686#page=5
- 参考答案附近的章节：V. E X P ER IM ENTAL RE SU LT S；A. The Influence to Neural Style Transfer；B. The Strength of Weighting Factor；C. The Emphasis of Distance Transform Image；D. Logo Generation；E. Transferring Styles to Background；F. Font Generation；V I . C ONCL U S I ON；AC KNOWL EDG E M E NT
- **AI 判定：`error`（不是标题：图表文字）**

```
   [10.0pt 左对齐] Fig. 6 shows how the distance transform loss Ldistance inﬂuences neural style transfer. With different values of weighting factors α and β, different style tran
   [8.0pt 左对齐] (a) Large details
>> [8.0pt 居中] (b) Medium details
   [8.0pt 右对齐] (c) Small details
   [8.0pt 左对齐] Figure 9. Logo generation using different size and density style images.
```

author: agree

## 16. 1803.07733 #9

- 系统判成标题的行：**5.3 Solv**（第 2 级，第 10 页）
- 原文页面：https://arxiv.org/pdf/1803.07733#page=10
- 参考答案附近的章节：5 Bach flow on locally homogeneous 1 × 3 products；Theorem 5.1.；Proof .；Theorem 5.2.；Theorem 5.3.
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [10.9pt 左对齐] Theorem 5.2. Let M be a compact quotient of R×Nil and let p ∈ M. Let g solve equation (2.1)
   [10.9pt 左对齐] where h is locally homogeneous. Then (M, g, p) collapses to a ﬂat surface in the pointed Gromov– Hausdorﬀtopology.
>> [12.0pt 粗体 左对齐] 5.3 Solv
   [10.9pt 左对齐] For this manifold the matrix used to determine the structure constants in Theorem 3.1 is
   [10.9pt 左对齐] E = diag(−1, 1, 0). For any metric g, using a diagonalizing basis, the Ricci tensor is diagonal with
```

author: agree

## 17. 1803.10983 #17

- 系统判成标题的行：**8: G1 ← G/wy, G2 ← G/yz, G3 ← G − ewz**（第 1 级，第 6 页）
- 原文页面：https://arxiv.org/pdf/1803.10983#page=6
- 参考答案附近的章节：3.2 The Max-Cut Algorithm；3.3 Correctness
- **AI 判定：`error`（不是标题：公式或算法行）**

```
   [9.0pt 左对齐] 6: S2 ← MaxCut(G/yz, Update(X, y, z))
   [9.0pt 左对齐] 7: S3 ← MaxCut(G − ewz, X \ {χ})
>> [9.0pt 左对齐] 8: G1 ← G/wy, G2 ← G/yz, G3 ← G − ewz
   [9.0pt 左对齐] 9: j ← argmax c(δ(Si, Gi))
   [6.0pt 左对齐] 1≤i≤3
```

author: agree

## 18. 1806.05266 #0

- 系统判成标题的行：**Abstract**（第 1 级，第 1 页）
- 原文页面：https://arxiv.org/pdf/1806.05266#page=1
- 参考答案附近的章节：1. Introduction；2. Examples of null hypothesis tests using differences of two Poisson distributi
- **AI 判定：`genuine_heading`（真标题，参考答案漏收）**

```
   [10.0pt 居中] Walter Toki*, Thomas W. Campbell, Erez Reinherz-Aronis
   [8.0pt 居中] Department of Physics, Colorado State University, Fort Collins, CO. 80523, USA
>> [10.0pt 粗体 左对齐] Abstract
   [10.0pt 左对齐] The p-value or statistical signiﬁcance of a CP conservation null hypothesis test is determined from counting electron
   [10.0pt 左对齐] neutrino and antineutrino appearance oscillation events. The statistical estimates include cases with background
```

author: agree

## 19. 1807.08233 #24

- 系统判成标题的行：**Raspberry Pi 3**（第 1 级，第 7 页）
- 原文页面：https://arxiv.org/pdf/1807.08233#page=7
- 参考答案附近的章节：V. V EHICL E B EHAV I OR；A. Line Detection；B. Target Region using Lines；C. Target Region Without using Lines；VI. CO N C L USIO N；A. Appendix；RE F E R ENCES
- **AI 判定：`error`（不是标题：图表文字）**

```
   [8.0pt 居中] ECX 1/10 Chassis
   [8.0pt 居中] Laptop
>> [8.0pt 居中] Raspberry Pi 3
   [8.0pt 居中] Arduino UNO R3
   [8.0pt 居中] 3D Printed Parts
```

author: agree

## 20. 1808.07823 #11

- 系统判成标题的行：**15. Ronneberger, O., Fischer, P., Brox, T.**（第 1 级，第 9 页）
- 原文页面：https://arxiv.org/pdf/1808.07823#page=9
- 参考答案附近的章节：5 Acknowledgements；References
- **AI 判定：`error`（不是标题：参考文献条目）**

```
   [9.0pt 左对齐] encoder-decoder networks with symmetric skip connections. In: Advances in neural
   [9.0pt 左对齐] information processing systems. (2016) 2802–2810
>> [9.0pt 左对齐] 15. Ronneberger, O., Fischer, P., Brox, T.: U-net: Convolutional networks for biomedi-
   [9.0pt 左对齐] cal image segmentation. In: International Conference on Medical image computing
   [9.0pt 左对齐] and computer-assisted intervention, Springer (2015) 234–241
```

author: agree
