"""_cluster_rects must reproduce the all-pairs padded-overlap components."""
import random

from infrastructure.providers.pdf_layout import _cluster_rects, _rects_overlap


def _reference(rects, pad_x, pad_y):
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _rects_overlap(rects[i], rects[j], pad_x, pad_y):
                parent[find(j)] = find(i)
    groups = {}
    for i, rect in enumerate(rects):
        groups.setdefault(find(i), []).append(rect)
    return list(groups.values())


def test_cluster_rects_matches_all_pairs_reference():
    rng = random.Random(3)
    for trial in range(300):
        n = rng.randint(1, 120)
        pad = rng.choice([0.5, 1.0, 3.0, 9.5])
        span = rng.choice([30, 100, 400])
        rects = []
        for _ in range(n):
            x, y = rng.uniform(0, span), rng.uniform(0, span)
            if rng.random() < 0.8:
                w, h = rng.uniform(0, 1.2 * pad), rng.uniform(0, 1.2 * pad)
            else:
                w, h = rng.uniform(0, span / 2), rng.uniform(0, span / 3)
            rects.append((x, y, x + w, y + h))
        assert _cluster_rects(rects, pad, pad) == _reference(rects, pad, pad), trial


def test_cluster_rects_dense_scatter_is_fast():
    import time
    rng = random.Random(0)
    rects = [(x, y, x + 1.5, y + 1.5) for x, y in
             ((rng.uniform(100, 300), rng.uniform(100, 300)) for _ in range(30000))]
    started = time.perf_counter()
    groups = _cluster_rects(rects, 10.0, 10.0)
    assert time.perf_counter() - started < 20
    assert sum(len(g) for g in groups) == len(rects)
