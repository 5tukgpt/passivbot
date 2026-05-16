"""Smoke tests for daemon/compute_hypervolume.py.

Run:
    cd ~/Projects/trading-bots/passivbot
    ./venv/bin/python -m pytest daemon/tests/test_compute_hypervolume.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))

import compute_hypervolume as ch  # noqa: E402


def test_pareto_front_basic():
    pts = [(0.0, 1.0), (1.0, 0.0), (0.5, 0.5), (2.0, 2.0)]
    front = ch._pareto_front(pts)
    # (2.0, 2.0) is dominated by everything else.
    assert (2.0, 2.0) not in front
    # The other three points are mutually non-dominated.
    assert {(0.0, 1.0), (1.0, 0.0), (0.5, 0.5)} == set(front)


def test_pareto_front_dedups_dominated():
    pts = [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5)]
    front = ch._pareto_front(pts)
    assert front == [(0.0, 0.0)]


def test_dominates_strict_inequality_required():
    # Equal point should not dominate itself.
    assert not ch._dominates((1.0, 1.0), (1.0, 1.0))
    # Lower on all axes does dominate.
    assert ch._dominates((0.5, 0.5), (1.0, 1.0))
    # Lower on one axis only does dominate if not worse on others.
    assert ch._dominates((0.5, 1.0), (1.0, 1.0))
    # Mixed is not domination.
    assert not ch._dominates((0.5, 2.0), (1.0, 1.0))


def test_reference_point_strictly_above_max():
    vecs = [(1.0, 2.0), (3.0, 1.0), (2.0, 3.0)]
    ref = ch.reference_point(vecs)
    # Reference must be strictly greater than max on every axis.
    assert ref[0] > 3.0
    assert ref[1] > 3.0


def test_reference_point_degenerate_axis():
    # All same value on one axis — fallback should still produce a valid ref.
    vecs = [(1.0, 5.0), (1.0, 5.0)]
    ref = ch.reference_point(vecs)
    assert ref[0] > 1.0
    assert ref[1] > 5.0


def test_compute_run_hypervolumes_synthetic(tmp_path):
    """End-to-end with a hand-rolled all_results.bin (msgpack records).

    Verifies hypervolume is monotonically non-decreasing across generations,
    which is the convergence property NSGA2 maintains by construction.
    """
    import msgpack

    pop_size = 4
    n_gens = 3
    records = []
    # Generation 0 — wide spread, large dominated area
    gen0 = [(0.8, 0.9), (0.7, 0.95), (0.95, 0.7), (0.85, 0.85)]
    # Generation 1 — slightly improved
    gen1 = [(0.6, 0.7), (0.5, 0.85), (0.8, 0.5), (0.7, 0.6)]
    # Generation 2 — further improved
    gen2 = [(0.4, 0.5), (0.3, 0.7), (0.6, 0.3), (0.5, 0.4)]
    for vecs in [gen0, gen1, gen2]:
        for v in vecs:
            records.append({
                "optimize": {
                    "population_size": pop_size,
                    "scoring": ["a", "b"],
                },
                "metrics": {
                    "objectives": {"w_0": v[0], "w_1": v[1]},
                },
            })
    run_dir = tmp_path / "synthetic_run"
    run_dir.mkdir()
    with open(run_dir / "all_results.bin", "wb") as f:
        for r in records:
            f.write(msgpack.packb(r))

    rows = ch.compute_run_hypervolumes(run_dir)
    assert len(rows) == n_gens
    hvs = [r["hypervolume"] for r in rows]
    # Strictly increasing — each generation found better solutions.
    assert hvs[0] < hvs[1] < hvs[2], f"HV did not increase monotonically: {hvs}"
    # Pareto size shouldn't shrink as new dominating points show up — but
    # may stay the same if generation introduces non-dominating offspring.
    sizes = [r["n_pareto"] for r in rows]
    assert all(s >= 1 for s in sizes)


def test_compute_run_hypervolumes_handles_missing_objectives(tmp_path):
    """A record with a missing w_i (e.g. constraint violation made one metric
    None) must not corrupt the dimensionality of the whole run.
    """
    import msgpack
    records = [
        {"optimize": {"population_size": 2, "scoring": ["a", "b", "c"]},
         "metrics": {"objectives": {"w_0": 0.5, "w_1": 0.5, "w_2": 0.5}}},
        {"optimize": {"population_size": 2, "scoring": ["a", "b", "c"]},
         "metrics": {"objectives": {"w_0": 0.3, "w_2": 0.6}}},  # w_1 missing
    ]
    run_dir = tmp_path / "missing_obj_run"
    run_dir.mkdir()
    with open(run_dir / "all_results.bin", "wb") as f:
        for r in records:
            f.write(msgpack.packb(r))
    vecs, pop = ch.load_records(run_dir)
    assert pop == 2
    # All vectors must have the same dimension (3, from scoring).
    assert all(len(v) == 3 for v in vecs)
    # The missing slot got filled with the per-axis fill — strictly worse than
    # the well-defined value 0.5.
    assert vecs[1][1] > 0.5
