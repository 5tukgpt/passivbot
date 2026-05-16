#!/usr/bin/env python3
"""
Compute per-generation hypervolume convergence for NSGA2 runs and persist
to config_optimizer.db.

How it works
------------
Passivbot's `src/optimize.py` writes one msgpack record per evaluation to
`all_results.bin` (no explicit generation index). We reconstruct generations
by chunking records into groups of `population_size` (read from the run's
optimize config block on the first record). For each cumulative generation,
we compute the non-dominated front over the evals seen so far and its
hypervolume against a per-run reference point (worst-seen + 1% margin per
objective).

DEAP's `tools._hypervolume.hv.hypervolume` does the heavy lifting (8D works
fine; it falls back to a Python implementation if the C extension is
missing). All objectives in `objectives_map["w_i"]` are already signed
so that lower-is-better — that matches DEAP's minimization convention.

Usage
-----
    ./compute_hypervolume.py --latest          # most recent run dir
    ./compute_hypervolume.py --all             # any run without HV rows
    ./compute_hypervolume.py <run_dir_path>

Idempotent: PRIMARY KEY (run_id, generation) — reruns are no-ops.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DAEMON_ROOT = Path(__file__).parent.resolve()
DB_PATH = DAEMON_ROOT / "config_optimizer.db"
OPTIMIZE_RESULTS_ROOT = Path("/Users/5tuktau/Projects/trading-bots/passivbot/optimize_results")

# Must run under passivbot/venv (deap + msgpack live there).
import msgpack  # noqa: E402
from deap.tools._hypervolume import hv as _deap_hv  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS pareto_hypervolume (
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    n_evals INTEGER NOT NULL,
    n_pareto INTEGER NOT NULL,
    hypervolume REAL NOT NULL,
    ref_point_json TEXT NOT NULL,
    PRIMARY KEY (run_id, generation),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_hv_run ON pareto_hypervolume(run_id);
"""


def _dominates(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """True iff `a` dominates `b` (all <=, at least one <). Minimization."""
    le = True
    strict = False
    for av, bv in zip(a, b):
        if av > bv:
            le = False
            break
        if av < bv:
            strict = True
    return le and strict


def _pareto_front(points: list[tuple[float, ...]]) -> list[tuple[float, ...]]:
    """O(n^2) non-dominated filter. Fine for population_size up to a few hundred."""
    front: list[tuple[float, ...]] = []
    for p in points:
        if any(_dominates(q, p) for q in front):
            continue
        front = [q for q in front if not _dominates(p, q)]
        front.append(p)
    return front


def load_records(run_dir: Path) -> tuple[list[tuple[float, ...]], int]:
    """Return (objective_vectors_in_order, population_size_for_this_run).

    Passivbot occasionally drops a w_i key when its underlying metric is None
    (constraint violation, divide-by-zero on flat curves). We fill those slots
    with the worst seen value for that axis +1% so the candidate is dominated
    by anything well-defined, instead of corrupting the run dimension count.
    """
    bin_path = run_dir / "all_results.bin"
    if not bin_path.exists():
        raise FileNotFoundError(f"{bin_path} not found")
    raw: list[dict] = []
    pop_size: int | None = None
    n_objs: int | None = None
    with open(bin_path, "rb") as f:
        for rec in msgpack.Unpacker(f, raw=False):
            if pop_size is None:
                pop_size = int(rec["optimize"]["population_size"])
            if n_objs is None:
                # Authoritative dimensionality comes from the scoring config.
                n_objs = len(rec["optimize"].get("scoring") or [])
            raw.append(rec["metrics"]["objectives"])
    if pop_size is None or not raw:
        raise ValueError("No records in all_results.bin")
    if not n_objs:
        # Fallback: pick the max key index seen + 1.
        n_objs = 1 + max(int(k.split("_")[1]) for r in raw for k in r)

    # Per-axis worst (max) over records that have the key.
    worst = [float("-inf")] * n_objs
    for r in raw:
        for k, v in r.items():
            if v is None:
                continue
            i = int(k.split("_")[1])
            if i < n_objs and v > worst[i]:
                worst[i] = float(v)
    fill = [(w + abs(w) * 0.01 + 1e-9) if w > float("-inf") else 1.0 for w in worst]

    vecs: list[tuple[float, ...]] = []
    for r in raw:
        row = list(fill)
        for k, v in r.items():
            if v is None:
                continue
            i = int(k.split("_")[1])
            if i < n_objs:
                row[i] = float(v)
        vecs.append(tuple(row))
    return vecs, pop_size


def reference_point(vecs: list[tuple[float, ...]]) -> tuple[float, ...]:
    """Per-objective worst value across the run + 1% margin.

    DEAP's hypervolume needs a reference point that is strictly worse than
    every Pareto point on every axis. We take max(obj) and pad by 1% of the
    range. If an axis has zero range, pad by 1.0 to avoid degeneracy.
    """
    n = len(vecs[0])
    ref = []
    for j in range(n):
        col = [v[j] for v in vecs]
        lo = min(col)
        hi = max(col)
        spread = hi - lo if hi > lo else 1.0
        ref.append(hi + 0.01 * spread)
    return tuple(ref)


def compute_run_hypervolumes(run_dir: Path) -> list[dict]:
    """Return one row dict per generation."""
    vecs, pop_size = load_records(run_dir)
    if pop_size <= 0:
        raise ValueError(f"Invalid population_size: {pop_size}")
    ref = reference_point(vecs)
    import math
    n_gens = max(1, math.ceil(len(vecs) / pop_size))

    rows: list[dict] = []
    seen: list[tuple[float, ...]] = []
    for g in range(n_gens):
        start = g * pop_size
        end = min(start + pop_size, len(vecs))
        seen.extend(vecs[start:end])
        front = _pareto_front(seen)
        hv_value = float(_deap_hv.hypervolume([list(p) for p in front], list(ref))) if front else 0.0
        rows.append({
            "generation": g,
            "n_evals": len(seen),
            "n_pareto": len(front),
            "hypervolume": hv_value,
            "ref_point": list(ref),
        })
    return rows


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def persist_run(conn: sqlite3.Connection, run_id: str, rows: list[dict]) -> int:
    import json
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        cur.execute(
            "INSERT OR REPLACE INTO pareto_hypervolume "
            "(run_id, generation, n_evals, n_pareto, hypervolume, ref_point_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, r["generation"], r["n_evals"], r["n_pareto"],
             r["hypervolume"], json.dumps(r["ref_point"])),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def find_latest_run_dir() -> Path:
    from persist_results import RUN_DIR_PATTERN
    candidates = [
        d for d in OPTIMIZE_RESULTS_ROOT.iterdir()
        if d.is_dir() and RUN_DIR_PATTERN.match(d.name)
    ]
    if not candidates:
        raise FileNotFoundError(f"No NSGA2 run dirs in {OPTIMIZE_RESULTS_ROOT}")
    return max(candidates, key=lambda d: d.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute per-generation hypervolume")
    parser.add_argument("run_dir", nargs="?", help="Path to optimize_results/{run_dir}")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="Process every run_id in DB that has no HV rows yet")
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.all:
        from persist_results import RUN_DIR_PATTERN
        existing = {row[0] for row in conn.execute(
            "SELECT DISTINCT run_id FROM pareto_hypervolume")}
        all_runs = [(row[0], Path(row[1])) for row in conn.execute(
            "SELECT run_id, output_dir FROM runs")]
        targets = [(rid, p) for rid, p in all_runs if rid not in existing and p.is_dir()]
        if not targets:
            print("No runs to process.")
            return 0
        for rid, run_dir in targets:
            try:
                rows = compute_run_hypervolumes(run_dir)
                n = persist_run(conn, rid, rows)
                print(f"hv {rid}: {n} generations, last_hv={rows[-1]['hypervolume']:.4g}")
            except Exception as exc:
                print(f"FAILED {rid}: {exc}", file=sys.stderr)
        return 0

    if args.latest:
        run_dir = find_latest_run_dir()
    elif args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        parser.error("Provide a run_dir, --latest, or --all")

    rows = compute_run_hypervolumes(run_dir)
    n = persist_run(conn, run_dir.name, rows)
    print(f"hv {run_dir.name}: {n} generations persisted")
    for r in rows:
        print(f"  gen {r['generation']:3d}: evals={r['n_evals']:4d} pareto={r['n_pareto']:3d} hv={r['hypervolume']:.4g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
