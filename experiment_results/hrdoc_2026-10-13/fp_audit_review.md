# False-positive audit: author spot-check

20 of 98 audited items (seed 1).
For each item, replace `?` on the `author:` line with `agree`, or with the verdict you
would give: `genuine_heading`, `split_or_version_text` or `error`. A note may follow.

- `genuine_heading`: a section title of the document that the reference omits
  (incl. Abstract, Acknowledgments, References, Appendix, run-in numbered subsections).
- `split_or_version_text`: part of a referenced section title (split across lines) or
  text that differs only because the fetched arXiv version differs.
- `error`: not a section title (unnumbered bold paragraph labels and theorem/proof
  labels count as errors).

## 1. 1509.08457 #0

- predicted: **ABSTRACT** (level 1, page 1) — https://arxiv.org/pdf/1509.08457#page=1
- AI verdict: `genuine_heading`
- nearby reference sections: 1 INTRODUCTION; 2 EXTERNAL FIELD DOMINANCE IN AQUAL

```
   [8.0pt left] 1Scottish Universities Physics Alliance, University of St Andrews, North Haugh, St Andrews, Fife, KY16 9SS, UK
   [8.0pt left] 7 April 2020
>> [9.0pt bold right] ABSTRACT
   [10.0pt left] The standard ΛCDM paradigm seems to describe cosmology and large scale
   [10.0pt left] structure formation very well. However, a number of puzzling observations remain on
```

author: ?

## 2. 1510.06794 #3

- predicted: **Acknowledgments** (level 1, page 2) — https://arxiv.org/pdf/1510.06794#page=2
- AI verdict: `genuine_heading`
- nearby reference sections: I. INTRODUCTION; I I. THEORY; II I. DISCUSSION

```
   [10.0pt left] its structure. It is now widely accepted that sequencebased proﬁle methods[10, 11] are the best method for template-based structure prediction. Noting that the 
   [10.0pt left] the present analysis help further clarify the meaning of this intricate relationship between protein sequences and structures.
>> [9.0pt bold center] Acknowledgments
   [10.0pt left] I thank Mr. Iseo Nose whose lecture on Goethe’s morphology motivated me to write this note.
   [9.0pt left] and Nucleic Acids (Cambridge Univ. Press, Cambridge, U. K., 1999).
```

author: ?

## 3. 1603.00232 #11

- predicted: **Value** (level 1, page 4) — https://arxiv.org/pdf/1603.00232#page=4
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: II I. REQUIREMENTS AND MATCHED OPTICAL SOLUTIONS; A. Standard Guide Concept; B. Curved Guides; C. Multi-Channel Bender Options; IV. SUITE COST TOTALS

```
   [9.0pt left] TABLE I: 150 metre bender speciﬁcations for eﬃcient transmission of 1 ˚A neutrons.
   [9.0pt bold left] Parameter
>> [9.0pt bold right] Value
   [9.0pt left] 150 m bender width
   [9.0pt right] 4.0 cm
```

author: ?

## 4. 1605.05157 #2

- predicted: **77 Rue des Chantiers, 78000, Versailles, France** (level 1, page 1) — https://arxiv.org/pdf/1605.05157#page=1
- AI verdict: `error` (title_page_or_running_header)
- nearby reference sections: I. I NTRODU C T ION; II . ME THOD; A. Data preparation and generation

```
   [11.0pt center] Email: ﬁrstname.name@mines-paristech.fr
   [11.0pt center] †Institut VEDECOM
>> [11.0pt center] 77 Rue des Chantiers, 78000, Versailles, France
   [11.0pt center] Email: ﬁrstname.name@vedecom.fr
   [20.0pt left] arXiv:1605.05157v2 [cs.RO] 16 Jun 2016
```

author: ?

## 5. 1608.02456 #8

- predicted: **H− beam** (level 2, page 2) — https://arxiv.org/pdf/1608.02456#page=2
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: Abstract; 1 Introduction; 2 Why accelerate H− beams?; 3 Beam loss mechanisms; 3.1 Residual gas stripping; 3.1.1 Gas stripping example

```
   [10.9pt bold right] Possible for
   [10.9pt bold right] Possible for
>> [10.9pt bold right] H+ beam
   [10.9pt bold right] H− beam
   [10.9pt left] Residual gas stripping
```

author: ?

## 6. 1608.02456 #28

- predicted: **6 Beam loss mitigation** (level 1, page 16) — https://arxiv.org/pdf/1608.02456#page=16
- AI verdict: `split_or_version_text`
- nearby reference sections: 5 Errant beam capture at SNS

```
   [10.0pt center] Fig. 16: Example of an errant beam event in the SNS linac [22,23]
   [10.9pt left] one of the warm linac RF cavities. Yet another example of an errant beam capture is shown in Fig. 18. A fast BCM in the MEBT, just downstream of the RFQ, shows 
>> [12.0pt bold left] 6 Beam loss mitigation
   [10.9pt left] We have discussed a variety of causes of beam loss in linacs. We will now turn our attention to methods of mitigating beam loss. Table 3 shows some of the more 
   [10.0pt left] Fig. 17: Example of an errant beam event in the SNS linac due to the sudden collapse of the RF ﬁeld in one of the warm linac RF cavities [22,23].
```

author: ?

## 7. 1703.04824 #3

- predicted: **(d) Reprintable output: same music expressed differently** (level 1, page 2) — https://arxiv.org/pdf/1703.04824#page=2
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: I . I N TRODU CT I ON: WHAT D ATA S ET?; A. Contributions and outline; II . G ROU N D T RU T H O F M USICAL SYM B O LS; A. Interfaces in the OMR pipeline

```
   [8.0pt left] a piano roll.
   [8.0pt center] (c) Reprintable output: re-typesetting.
>> [8.0pt center] (d) Reprintable output: same music expressed differently
   [10.0pt left] Fig. 1: OMR for replayability and reprintability. The input (a) encodes the sequence of pitches, durations, and onsets (b), which can be expressed in different 
   [10.0pt left] truth, although deﬁning this approximation for the variety of representations of music is very much an open problem [2], [6], [7], [11], [18], [19]. In order to
```

author: ?

## 8. 1705.08550 #0

- predicted: **Abstract. Mammogram classiﬁcation is directly related to computer-aided di-** (level 1, page 1) — https://arxiv.org/pdf/1705.08550#page=1
- AI verdict: `genuine_heading`
- nearby reference sections: 1 Introduction

```
   [9.0pt center] Dept. of Computer Science, University of California, Irvine
   [9.0pt center] {wentaoz1, xhx}@ics.uci.edu, {qlou, ysvang}@uci.edu
>> [9.0pt center] Abstract. Mammogram classiﬁcation is directly related to computer-aided di-
   [20.0pt left] arXiv:1705.08550v1 [cs.CV] 23 May 2017
   [9.0pt center] agnosis of breast cancer. Traditional methods rely on regions of interest (ROIs)
```

author: ?

## 9. 1709.08958 #16

- predicted: **4.1. The Fenchel-Nielsen twist** (level 2, page 7) — https://arxiv.org/pdf/1709.08958#page=7
- AI verdict: `genuine_heading`
- nearby reference sections: 4. Fenchel-Nielsen twist deformation

```
   [10.0pt center] 4. Fenchel-Nielsen twist deformation
   [10.0pt left] Whilst make no claim as to the originality of the material in this section it is included to set up notation give an exposition of two results which we use in S
>> [10.0pt left] 4.1. The Fenchel-Nielsen twist. We choose a simple closed curve α ⊂ Σ. Fol-
   [10.0pt left] lowing [3], cut along this curve, and take the completion of the resulting surface with respect to the path metric to obtain a possibly disconnected surface wit
   [10.0pt left] then a (left) Fenchel-Nielsen twist along α allows one to construct a new surface Σt, homeomorphic to Σ by identifying the two boundary components with a left t
```

author: ?

## 10. 1710.03202 #0

- predicted: **Abstract** (level 1, page 1) — https://arxiv.org/pdf/1710.03202#page=1
- AI verdict: `genuine_heading`
- nearby reference sections: 1 INTRODUCTION; 2 MODEL IMPLEMENTATION; 2.1 Unsteady RANS formulation of the governing equations

```
   [20.0pt left] arXiv:1710.03202v2 [physics.flu-dyn] 8 Oct 2018
   [7.0pt left] Department of Earth Science and Engineering,
>> [10.0pt bold left] Abstract
   [7.0pt left] South Kensington Campus, Imperial College
   [7.0pt left] London, SW7 2AZ, UK
```

author: ?

## 11. 1712.03829 #29

- predicted: **sti ▷ ∆i ⊢ t : σi)i∈I** (level 1, page 26) — https://arxiv.org/pdf/1712.03829#page=26
- AI verdict: `error` (formula_or_algorithm_line)
- nearby reference sections: 5. Conclusion; Acknowledgements.; References

```
   [10.9pt left] form:
   [10.9pt left] (Πi
>> [10.9pt center] sti ▷ ∆i ⊢ t : σi)i∈I
   [10.9pt right] (m)
   [10.9pt center] Πst ▷ +i∈I∆i ⊢ t : [σi]i∈I
```

author: ?

## 12. 1801.01228 #16

- predicted: **(e) Step 5** (level 1, page 5) — https://arxiv.org/pdf/1801.01228#page=5
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: I V. RESU LT S; A. Comparison of POMDP to heuristic and random policy; B. Sample POMDP policy run in simulation and on drone; V. C O NC LUSIO NS A N D F U TU RE WORK; AC K NO WLE DGME N T

```
   [8.0pt right] (c) Step 3
   [8.0pt left] (d) Step 4
>> [8.0pt center] (e) Step 5
   [8.0pt right] (f) Step 6
   [8.0pt left] (g) Step 7
```

author: ?

## 13. 1801.10237 #9

- predicted: **23 S. Kimura, T. Takeuchi, K. Okunishi, M** (level 1, page 17) — https://arxiv.org/pdf/1801.10237#page=17
- AI verdict: `error` (reference_entry)
- nearby reference sections: (none)

```
   [10.9pt left] 1 L. Faddeev and L. Takhtajan, Physics Letters A 85, 375 (1981). 2 B. Lake, A. M. Tsvelik, S. Notbohm, D. Alan Tennant, T. G. Perring, M. Reehuis, C. Sekar, G. 
   [10.9pt left] 4 R. Coldea, D. A. Tennant, E. M. Wheeler, E. Wawrzynska, D. Prabhakaran, M. Telling, K. Habicht, P. Smeibidl, and K. Kiefer, Science 327, 177 (2010). 5 Z. Wang
>> [10.9pt left] 23 S. Kimura, T. Takeuchi, K. Okunishi, M. Hagiwara, Z. He, K. Kindo, T. Taniyama, and M. Itoh, Phys. Rev. Lett. 100, 057202 (2008). 24 B. Grenier, V. Simonet, 
```

author: ?

## 14. 1802.08375 #14

- predicted: **Conjecture. Let Ein** (level 2, page 5) — https://arxiv.org/pdf/1802.08375#page=5
- AI verdict: `error` (run_in_label_or_list_item)
- nearby reference sections: 6 Results; 6.1 CharCNN is biased towards surface form; 6.2 Tying weights bottom-up

```
   [10.9pt bold left] 6.2 Tying weights bottom-up
   [10.9pt left] From Table 2 one can notice that tying weights without tying subword embeddings (RW) always results in worse performance than the tying both weights and embeddi
>> [10.9pt bold left] Conjecture. Let Ein
   [10.9pt center] = Θin
   [8.0pt right] 0 , Θin
```

author: ?

## 15. 1803.00686 #12

- predicted: **(b) Medium details** (level 1, page 5) — https://arxiv.org/pdf/1803.00686#page=5
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: V. E X P ER IM ENTAL RE SU LT S; A. The Influence to Neural Style Transfer; B. The Strength of Weighting Factor; C. The Emphasis of Distance Transform Image; D. Logo Generation; E. Transferring Styles to Background; F. Font Generation; V I . C ONCL U S I ON; AC KNOWL EDG E M E NT

```
   [10.0pt left] Fig. 6 shows how the distance transform loss Ldistance inﬂuences neural style transfer. With different values of weighting factors α and β, different style tran
   [8.0pt left] (a) Large details
>> [8.0pt center] (b) Medium details
   [8.0pt right] (c) Small details
   [8.0pt left] Figure 9. Logo generation using different size and density style images.
```

author: ?

## 16. 1803.07733 #9

- predicted: **5.3 Solv** (level 2, page 10) — https://arxiv.org/pdf/1803.07733#page=10
- AI verdict: `genuine_heading`
- nearby reference sections: 5 Bach flow on locally homogeneous 1 × 3 products; Theorem 5.1.; Proof .; Theorem 5.2.; Theorem 5.3.

```
   [10.9pt left] Theorem 5.2. Let M be a compact quotient of R×Nil and let p ∈ M. Let g solve equation (2.1)
   [10.9pt left] where h is locally homogeneous. Then (M, g, p) collapses to a ﬂat surface in the pointed Gromov– Hausdorﬀtopology.
>> [12.0pt bold left] 5.3 Solv
   [10.9pt left] For this manifold the matrix used to determine the structure constants in Theorem 3.1 is
   [10.9pt left] E = diag(−1, 1, 0). For any metric g, using a diagonalizing basis, the Ricci tensor is diagonal with
```

author: ?

## 17. 1803.10983 #17

- predicted: **8: G1 ← G/wy, G2 ← G/yz, G3 ← G − ewz** (level 1, page 6) — https://arxiv.org/pdf/1803.10983#page=6
- AI verdict: `error` (formula_or_algorithm_line)
- nearby reference sections: 3.2 The Max-Cut Algorithm; 3.3 Correctness

```
   [9.0pt left] 6: S2 ← MaxCut(G/yz, Update(X, y, z))
   [9.0pt left] 7: S3 ← MaxCut(G − ewz, X \ {χ})
>> [9.0pt left] 8: G1 ← G/wy, G2 ← G/yz, G3 ← G − ewz
   [9.0pt left] 9: j ← argmax c(δ(Si, Gi))
   [6.0pt left] 1≤i≤3
```

author: ?

## 18. 1806.05266 #0

- predicted: **Abstract** (level 1, page 1) — https://arxiv.org/pdf/1806.05266#page=1
- AI verdict: `genuine_heading`
- nearby reference sections: 1. Introduction; 2. Examples of null hypothesis tests using differences of two Poisson distributi

```
   [10.0pt center] Walter Toki*, Thomas W. Campbell, Erez Reinherz-Aronis
   [8.0pt center] Department of Physics, Colorado State University, Fort Collins, CO. 80523, USA
>> [10.0pt bold left] Abstract
   [10.0pt left] The p-value or statistical signiﬁcance of a CP conservation null hypothesis test is determined from counting electron
   [10.0pt left] neutrino and antineutrino appearance oscillation events. The statistical estimates include cases with background
```

author: ?

## 19. 1807.08233 #24

- predicted: **Raspberry Pi 3** (level 1, page 7) — https://arxiv.org/pdf/1807.08233#page=7
- AI verdict: `error` (figure_or_table_text)
- nearby reference sections: V. V EHICL E B EHAV I OR; A. Line Detection; B. Target Region using Lines; C. Target Region Without using Lines; VI. CO N C L USIO N; A. Appendix; RE F E R ENCES

```
   [8.0pt center] ECX 1/10 Chassis
   [8.0pt center] Laptop
>> [8.0pt center] Raspberry Pi 3
   [8.0pt center] Arduino UNO R3
   [8.0pt center] 3D Printed Parts
```

author: ?

## 20. 1808.07823 #11

- predicted: **15. Ronneberger, O., Fischer, P., Brox, T.** (level 1, page 9) — https://arxiv.org/pdf/1808.07823#page=9
- AI verdict: `error` (reference_entry)
- nearby reference sections: 5 Acknowledgements; References

```
   [9.0pt left] encoder-decoder networks with symmetric skip connections. In: Advances in neural
   [9.0pt left] information processing systems. (2016) 2802–2810
>> [9.0pt left] 15. Ronneberger, O., Fischer, P., Brox, T.: U-net: Convolutional networks for biomedi-
   [9.0pt left] cal image segmentation. In: International Conference on Medical image computing
   [9.0pt left] and computer-assisted intervention, Springer (2015) 234–241
```

author: ?
