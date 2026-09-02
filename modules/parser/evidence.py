"""Evidence fusion for heading candidates.

This module deliberately knows nothing about routing budgets or legacy scores.
It consumes typed :class:`EvidenceAtom` observations, collapses correlated
signals, and emits heuristic local probabilities.  The values are versioned
heuristics until a held-out calibration artifact is supplied; callers must not
report them as calibrated posteriors.
"""
from __future__ import annotations

import math
from collections.abc import Iterable

from modules.parser.schemas import EvidenceAtom, HeadingLabel, RegionRisk


CALIBRATION_VERSION = "heading-evidence.heuristic.v1"
PROBABILITY_QUALITY = "heuristic"

# Likelihood ratios express evidence strength without reviving the old
# additive raw-score model.  Reliability tempers each ratio exponentially.
_POSITIVE_LR: dict[str, float] = {
    "visible_numbering": 30.0,
    "effective_numbering": 18.0,
    "explicit_heading_style": 120.0,
    "outline_level": 90.0,
    "bold": 4.0,
    "alignment": 3.0,
    "standalone_line": 30.0,
    "semantic_title": 30.0,
    "toc_destination": 150.0,
    "run_in_pattern": 10.0,
}
_NEGATIVE_LR: dict[str, float] = {
    "caption_negative": 0.01,
    "list_prose_negative": 0.12,
    "table_region_negative": 0.18,
    "printed_toc_negative": 0.08,
    "margin_negative": 0.05,
}
_DEFAULT_LEVEL_PRIOR: dict[HeadingLabel, float] = {
    "L1": 0.42,
    "L2": 0.25,
    "L3": 0.14,
    "L4": 0.09,
    "L5": 0.06,
    "L6": 0.04,
}
_LEVEL_LABELS: tuple[HeadingLabel, ...] = (
    "L1", "L2", "L3", "L4", "L5", "L6",
)
_DETERMINISTIC_PROMOTION_KINDS = {
    "visible_numbering",
    "effective_numbering",
    "explicit_heading_style",
    "outline_level",
    "toc_destination",
    "font_ratio",
}


def _font_likelihood_ratio(atom: EvidenceAtom) -> float:
    observed = atom.observed_value
    ratio = observed.get("ratio", 1.0) if isinstance(observed, dict) else observed
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 1.0
    if value >= 1.5:
        return 35.0
    if value >= 1.25:
        return 25.0
    if value >= 1.10:
        return 4.0
    if value <= 0.75:
        return 0.65
    return 1.0


def evidence_likelihood_ratio(atom: EvidenceAtom) -> float:
    """Return the reliability-tempered likelihood ratio for one atom."""
    if atom.kind == "font_ratio":
        base = _font_likelihood_ratio(atom)
    elif atom.polarity > 0:
        base = _POSITIVE_LR.get(atom.kind, 1.0)
    else:
        base = _NEGATIVE_LR.get(atom.kind, 1.0)
    return base ** atom.reliability


def deduplicate_evidence(evidence: Iterable[EvidenceAtom]) -> list[EvidenceAtom]:
    """Keep one strongest observation per correlation group.

    Provider metadata often exposes the same fact through multiple surfaces
    (for example heading style *and* outline level, or visible numbering and
    effective numbering).  Keeping both atoms would double-count a single
    cause.  Ties are deterministic by evidence ID.
    """
    strongest: dict[str, tuple[float, EvidenceAtom]] = {}
    for atom in evidence:
        lr = evidence_likelihood_ratio(atom)
        impact = abs(math.log(max(lr, 1e-12)))
        previous = strongest.get(atom.correlation_group)
        if previous is None or (impact, atom.evidence_id) > (
            previous[0], previous[1].evidence_id,
        ):
            strongest[atom.correlation_group] = (impact, atom)
    return sorted(
        (entry[1] for entry in strongest.values()),
        key=lambda atom: (atom.anchor.order_key, atom.kind, atom.evidence_id),
    )


def heading_probability(
    evidence: Iterable[EvidenceAtom],
    *,
    prior: float = 0.05,
) -> float:
    """Fuse independent evidence families into a local heading probability."""
    atoms = deduplicate_evidence(evidence)
    prior = min(max(prior, 1e-6), 1.0 - 1e-6)
    log_odds = math.log(prior / (1.0 - prior))
    log_odds += math.fsum(
        math.log(max(evidence_likelihood_ratio(atom), 1e-12))
        for atom in atoms
    )
    if log_odds >= 0:
        probability = 1.0 / (1.0 + math.exp(-log_odds))
    else:
        exp_value = math.exp(log_odds)
        probability = exp_value / (1.0 + exp_value)
    return min(max(probability, 0.0), 1.0)


def level_probabilities(
    probability: float,
    evidence: Iterable[EvidenceAtom],
) -> dict[HeadingLabel, float]:
    """Build a normalized ``NONE/L1..L6`` local emission distribution."""
    atoms = deduplicate_evidence(evidence)
    log_scores = {
        label: math.log(_DEFAULT_LEVEL_PRIOR[label]) for label in _LEVEL_LABELS
    }
    for atom in atoms:
        if atom.polarity < 0 or not atom.level_likelihoods:
            continue
        for label in _LEVEL_LABELS:
            likelihood = atom.level_likelihoods.get(label, 1e-4)
            log_scores[label] += atom.reliability * math.log(max(likelihood, 1e-8))

    maximum = max(log_scores.values())
    weights = {label: math.exp(value - maximum) for label, value in log_scores.items()}
    total = math.fsum(weights.values())
    conditional = {label: weights[label] / total for label in _LEVEL_LABELS}

    result: dict[HeadingLabel, float] = {"NONE": 1.0 - probability}
    for label in _LEVEL_LABELS:
        result[label] = probability * conditional[label]
    # Eliminate the tiny floating drift rejected by the schema invariant.
    result["L6"] += 1.0 - math.fsum(result.values())
    return result


def promotion_probability(
    probability: float,
    evidence: Iterable[EvidenceAtom],
    region_risk: RegionRisk,
) -> float:
    """Estimate deterministic promotion support when no LLM vote exists.

    Promotion is a proposal emission, never a post-hoc insertion.  In
    particular, font scale and bold are independent physical causes: a short
    bold line that is only moderately larger than body text can be a genuine
    swallowed heading even when it has the same size as a confirmed sibling.
    The previous multiplicative projection drove that compound signal below
    0.5 and made the global decoder incapable of selecting it.
    """
    atoms = deduplicate_evidence(evidence)
    deterministic = [
        atom.reliability for atom in atoms
        if atom.polarity > 0 and atom.kind in _DETERMINISTIC_PROMOTION_KINDS
    ]
    support = max(deterministic, default=0.35)
    support_factor = 0.55 + 0.45 * support
    contamination_factor = 1.0 - 0.75 * region_risk.contamination_probability
    projected = probability * support_factor * contamination_factor

    # Structural floors are allowed only in the absence of explicit negative
    # evidence.  This prevents bold/large captions, TOC echoes and table or
    # margin artifacts from bypassing their typed rejection evidence.
    has_negative = any(atom.polarity < 0 for atom in atoms)
    if not has_negative:
        font_ratios = []
        positive_kinds = {
            atom.kind for atom in atoms if atom.polarity > 0
        }
        for atom in atoms:
            if atom.polarity <= 0 or atom.kind != "font_ratio":
                continue
            observed = atom.observed_value
            value = observed.get("ratio", 1.0) if isinstance(observed, dict) else observed
            try:
                font_ratios.append(float(value))
            except (TypeError, ValueError):
                continue
        largest_ratio = max(font_ratios, default=1.0)
        structural_floor = 0.0
        if largest_ratio >= 1.25:
            structural_floor = 0.70
        elif largest_ratio >= 1.10 and "bold" in positive_kinds:
            structural_floor = 0.64
        projected = max(
            projected,
            structural_floor * contamination_factor,
        )

    return min(max(projected, 0.0), 1.0)


def evidence_diversity(evidence: Iterable[EvidenceAtom]) -> int:
    """Count independent positive evidence families for deterministic ties."""
    return len({
        atom.correlation_group for atom in deduplicate_evidence(evidence)
        if atom.polarity > 0
    })
