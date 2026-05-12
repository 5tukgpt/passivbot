#!/usr/bin/env python3
"""
Persist NSGA2 results from `optimize_results/{run_dir}/` into config_optimizer.db.

Reads pareto/*.json (one file per Pareto-optimal individual) and writes:
  - one row per run into `runs`
  - one row per Pareto individual into `individuals`

Idempotent: run hash + individual hash are primary keys. Re-running on the same
directory is a no-op (INSERT OR IGNORE).

Usage:
  ./persist_results.py <optimize_results/{run_dir}>
  ./persist_results.py --latest      # auto-pick the most recent run dir
  ./persist_results.py --all         # ingest every run dir not already in DB
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DAEMON_ROOT = Path(__file__).parent.resolve()
DB_PATH = DAEMON_ROOT / "config_optimizer.db"
OPTIMIZE_RESULTS_ROOT = Path("/Users/5tuktau/Projects/trading-bots/passivbot/optimize_results")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    exchange TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    n_days INTEGER NOT NULL,
    coins_csv TEXT NOT NULL,
    n_pareto INTEGER NOT NULL,
    output_dir TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS individuals (
    individual_hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    in_pareto INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    bot_long_json TEXT NOT NULL,
    bot_short_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_individuals_run ON individuals(run_id);
CREATE INDEX IF NOT EXISTS idx_individuals_pareto ON individuals(in_pareto);
"""

RUN_DIR_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2})_(?P<exchange>[a-z]+)_(?P<ndays>\d+)days_(?P<coins>[A-Za-z0-9_]+)_(?P<hash>[a-f0-9]+)$"
)


def parse_run_dir_name(name: str) -> dict | None:
    m = RUN_DIR_PATTERN.match(name)
    if not m:
        return None
    parts = m.groupdict()
    ts_iso = parts["ts"].replace("_", ":", 2).replace("T", "T", 1)
    iso = parts["ts"][:10] + "T" + parts["ts"][11:].replace("_", ":") + "+00:00"
    return {
        "run_id": name,
        "started_at_iso": iso,
        "exchange": parts["exchange"],
        "n_days": int(parts["ndays"]),
        "coins_csv": parts["coins"].replace("_", ","),
        "hash": parts["hash"],
    }


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def ingest_run(conn: sqlite3.Connection, run_dir: Path) -> tuple[int, int]:
    """Return (inserted_run, inserted_individuals). Idempotent."""
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Not a directory: {run_dir}")
    parsed = parse_run_dir_name(run_dir.name)
    if not parsed:
        raise ValueError(f"Run dir name doesn't match expected pattern: {run_dir.name}")

    pareto_dir = run_dir / "pareto"
    if not pareto_dir.is_dir():
        raise FileNotFoundError(f"No pareto/ subdir in {run_dir}")
    pareto_files = sorted(pareto_dir.glob("*.json"))

    # Probe the first pareto file for window_start/end
    if not pareto_files:
        raise ValueError(f"No Pareto JSONs in {pareto_dir}")
    with open(pareto_files[0]) as f:
        sample = json.load(f)
    window_start = sample.get("backtest", {}).get("start_date", "")
    window_end = sample.get("backtest", {}).get("end_date", "")

    ingested_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    cur.execute(
        "INSERT OR IGNORE INTO runs "
        "(run_id, started_at, ingested_at, exchange, window_start, window_end, n_days, coins_csv, n_pareto, output_dir) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            parsed["run_id"],
            parsed["started_at_iso"],
            ingested_at,
            parsed["exchange"],
            window_start,
            window_end,
            parsed["n_days"],
            parsed["coins_csv"],
            len(pareto_files),
            str(run_dir),
        ),
    )
    inserted_run = cur.rowcount

    inserted_individuals = 0
    for fp in pareto_files:
        individual_hash = fp.stem
        with open(fp) as f:
            data = json.load(f)
        metrics = data.get("metrics", {})
        bot_long = data.get("bot", {}).get("long", {})
        bot_short = data.get("bot", {}).get("short", {})
        cur.execute(
            "INSERT OR IGNORE INTO individuals "
            "(individual_hash, run_id, in_pareto, config_json, metrics_json, bot_long_json, bot_short_json) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (
                individual_hash,
                parsed["run_id"],
                json.dumps(data),
                json.dumps(metrics),
                json.dumps(bot_long),
                json.dumps(bot_short),
            ),
        )
        inserted_individuals += cur.rowcount

    conn.commit()
    return inserted_run, inserted_individuals


def find_latest_run_dir() -> Path:
    candidates = [d for d in OPTIMIZE_RESULTS_ROOT.iterdir() if d.is_dir() and RUN_DIR_PATTERN.match(d.name)]
    if not candidates:
        raise FileNotFoundError(f"No NSGA2 run dirs in {OPTIMIZE_RESULTS_ROOT}")
    return max(candidates, key=lambda d: d.stat().st_mtime)


def main():
    parser = argparse.ArgumentParser(description="Persist NSGA2 results into SQLite")
    parser.add_argument("run_dir", nargs="?", help="Path to optimize_results/{run_dir}")
    parser.add_argument("--latest", action="store_true", help="Auto-pick the most recent run dir")
    parser.add_argument("--all", action="store_true", help="Ingest every run dir not already in DB")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="DB path (default: %(default)s)")
    args = parser.parse_args()

    conn = init_db(Path(args.db))

    if args.all:
        existing = {row[0] for row in conn.execute("SELECT run_id FROM runs")}
        candidates = sorted(
            d for d in OPTIMIZE_RESULTS_ROOT.iterdir()
            if d.is_dir() and RUN_DIR_PATTERN.match(d.name) and d.name not in existing
        )
        if not candidates:
            print("No new run dirs to ingest.")
            return 0
        total_runs = total_individuals = 0
        for run_dir in candidates:
            try:
                runs_added, indiv_added = ingest_run(conn, run_dir)
                total_runs += runs_added
                total_individuals += indiv_added
                print(f"ingested {run_dir.name}: +{runs_added} run, +{indiv_added} individuals")
            except Exception as exc:
                print(f"FAILED {run_dir.name}: {exc}", file=sys.stderr)
        print(f"Total: {total_runs} runs, {total_individuals} individuals")
        return 0

    if args.latest:
        run_dir = find_latest_run_dir()
    elif args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        parser.error("Provide a run_dir path, --latest, or --all")

    runs_added, indiv_added = ingest_run(conn, run_dir)
    print(f"ingested {run_dir.name}: +{runs_added} run, +{indiv_added} individuals")

    # Quick summary
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    n_individuals = conn.execute("SELECT COUNT(*) FROM individuals").fetchone()[0]
    print(f"DB total: {n_runs} runs, {n_individuals} individuals at {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
