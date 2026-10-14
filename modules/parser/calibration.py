"""Held-out, domain-scoped isotonic calibration for confidence channels.

A fitted map is not a universal accuracy guarantee. Artifact application is
explicit, requires matching source/mode, and does not modify heading selection.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


def pipeline_fingerprint() -> str:
    import hashlib
    root = Path(__file__).resolve().parents[2]
    paths = ["modules/parser/global_inference.py", "modules/parser/heading_candidates.py",
             "modules/parser/evidence.py", "modules/parser/calibration.py",
             "modules/parser/typography.py", "modules/parser/anchor_alignment.py",
             "modules/parser/resolver.py", "modules/parser/config.py",
             "modules/parser/hierarchy.py",
             "modules/parser/router.py", "modules/parser/parser.py",
             "infrastructure/providers/pdf_provider.py", "infrastructure/providers/pdf_page_roles.py",
             "infrastructure/providers/pdf_content_relations.py", "infrastructure/providers/docx_provider.py"]
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def confidence_domain(block, has_votes: bool) -> str:
    meta = block.metadata or {}
    source = meta.get("source_format")
    if source is None:
        source = "pdf" if meta.get("page") is not None else "docx" if "style" in meta else "unknown"
    return f"{source}:{'model' if has_votes else 'deterministic'}"


@dataclass(frozen=True)
class CalibrationMap:
    upper_bounds: tuple[float, ...]
    probabilities: tuple[float, ...]

    @classmethod
    def fit(cls, scores: list[float], correct: list[bool]):
        if not scores or len(scores) != len(correct):
            raise ValueError("calibration requires nonempty paired samples")
        groups = {}
        for score, hit in zip(scores, correct, strict=True):
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("score must be finite and in [0, 1]")
            count, positives = groups.get(score, (0, 0))
            groups[score] = (count + 1, positives + int(hit))
        pools = []
        for score, (count, positives) in sorted(groups.items()):
            pools.append([score, count, positives])
            while len(pools) > 1 and pools[-2][2] / pools[-2][1] > pools[-1][2] / pools[-1][1]:
                right, left = pools.pop(), pools.pop()
                pools.append([right[0], left[1] + right[1], left[2] + right[2]])
        return cls(tuple(p[0] for p in pools), tuple(p[2] / p[1] for p in pools))

    def predict(self, score: float) -> float:
        from bisect import bisect_left
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and in [0, 1]")
        return self.probabilities[min(bisect_left(self.upper_bounds, score), len(self.probabilities) - 1)]


def load_calibration(path: str, parser_config=None) -> dict | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    train, validation = set(data["train_documents"]), set(data["validation_documents"])
    if data.get("version") != 1 or not train or not validation or train & validation:
        raise ValueError("calibration artifact requires disjoint train/validation documents")
    if not data.get("domain") or not data.get("pipeline_fingerprint"):
        raise ValueError("calibration artifact requires domain and pipeline provenance")
    if data["pipeline_fingerprint"] != pipeline_fingerprint():
        raise ValueError("calibration artifact is stale; refit for the current pipeline")
    if parser_config is not None and data.get("parser_config") != parser_config.model_dump(exclude={"confidence_calibration_path"}):
        raise ValueError("calibration artifact does not match parser configuration")
    for channel, curve in data["channels"].items():
        bounds, probs = curve["upper_bounds"], curve["probabilities"]
        if channel not in {"heading", "level", "alignment"} or not bounds or len(bounds) != len(probs):
            raise ValueError("invalid calibration channel")
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in bounds + probs):
            raise ValueError("invalid calibration probability")
        if any(a >= b for a, b in zip(bounds, bounds[1:], strict=False)) or probs != sorted(probs):
            raise ValueError("calibration map must be monotonic")
    return data


def calibrated_score(artifact: dict | None, channel: str, score: float, domain: str) -> dict:
    if artifact and artifact["domain"] == domain and channel in artifact["channels"]:
        curve = artifact["channels"][channel]
        probability = CalibrationMap(tuple(curve["upper_bounds"]), tuple(curve["probabilities"])).predict(score)
        return {"score": score, "probability": probability, "quality": "calibrated_on_holdout",
                "domain": domain, "artifact_id": artifact.get("artifact_id", "")}
    return {"score": score, "probability": None, "quality": "heuristic", "domain": domain}
