#!/usr/bin/env python3
"""
Out-of-sample validation for daemon Pareto candidates.

For each top-N Pareto candidate from a given training run, re-runs the backtest
on a different (validation) window and records both the training metrics and
the OOS metrics side-by-side.

Persists to a new `oos_validations` table in config_optimizer.db, keyed on
(individual_hash, validation_window_start, validation_window_end). Idempotent.

Usage:
  ./oos_validate.py --training-run <run_id> --val-start 2026-04-10 --val-end 2026-05-10 [--top-n 5] [--metric sharpe_ratio_pnl]

Example: validate the 5 best-Sharpe Pareto candidates from the S3-era training run
against the recent Apr 10 → May 10 OOS window.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DAEMON_ROOT = Path(__file__).parent.resolve()
DB_PATH = DAEMON_ROOT / "config_optimizer.db"
PASSIVBOT_ROOT = Path("/Users/5tuktau/Projects/trading-bots/passivbot")
PYTHON = PASSIVBOT_ROOT / "venv/bin/python"
BACKTEST_SCRIPT = PASSIVBOT_ROOT / "src/backtest.py"
OPTIMIZE_RESULTS_ROOT = PASSIVBOT_ROOT / "optimize_results"
BACKTESTS_ROOT = PASSIVBOT_ROOT / "backtests"

OOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS oos_validations (
    individual_hash TEXT NOT NULL,
    training_run_id TEXT NOT NULL,
    val_window_start TEXT NOT NULL,
    val_window_end TEXT NOT NULL,
    val_run_at TEXT NOT NULL,
    train_metrics_json TEXT NOT NULL,
    val_metrics_json TEXT NOT NULL,
    n_coins_val INTEGER,
    coins_val_csv TEXT,
    notes TEXT,
    PRIMARY KEY (individual_hash, val_window_start, val_window_end),
    FOREIGN KEY (individual_hash) REFERENCES individuals(individual_hash)
);
CREATE INDEX IF NOT EXISTS idx_oos_training_run ON oos_validations(training_run_id);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(OOS_SCHEMA)
    conn.commit()
    return conn


def top_candidates(
    conn: sqlite3.Connection, training_run_id: str, metric: str, top_n: int, twe_max_cap: float = 0.8
) -> list[tuple[str, dict, dict]]:
    """Return [(individual_hash, full_config_json, train_metrics_json), ...]
    sorted by `metric` (stats.{metric}.mean) descending, filtered to twe_max <= cap.
    """
    rows = conn.execute(
        "SELECT individual_hash, config_json, metrics_json FROM individuals WHERE run_id = ?",
        (training_run_id,),
    ).fetchall()
    ranked = []
    for h, cfg_json, metrics_json in rows:
        m = json.loads(metrics_json)
        stats = m.get("stats", {})
        value = stats.get(metric, {}).get("mean", float("-inf"))
        twe_max = stats.get("total_wallet_exposure_max", {}).get("mean", 1.0)
        if twe_max > twe_max_cap:
            continue
        ranked.append((value, h, cfg_json, m))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [(h, json.loads(cfg), tm) for _, h, cfg, tm in ranked[:top_n]]


def find_latest_backtest_dir() -> Path:
    candidates = [d for d in (BACKTESTS_ROOT / "hyperliquid").iterdir() if d.is_dir()]
    if not candidates:
        raise FileNotFoundError("No backtest output dirs")
    return max(candidates, key=lambda d: d.stat().st_mtime)


def extract_metrics_from_backtest_dir(bt_dir: Path) -> dict | None:
    """Read backtest metrics summary. Passivbot writes results_summary.json or similar."""
    for candidate in ("analysis.json", "results_summary.json", "analyses_combined.json"):
        p = bt_dir / candidate
        if p.exists():
            with open(p) as f:
                return json.load(f)
    # Walk subdirs for stat files
    for p in bt_dir.rglob("analysis.json"):
        with open(p) as f:
            return json.load(f)
    for p in bt_dir.rglob("*.json"):
        if p.name in ("backtest_summary.json", "metrics.json"):
            with open(p) as f:
                return json.load(f)
    return None


def run_validation_backtest(cfg: dict, val_start: str, val_end: str) -> tuple[dict | None, Path]:
    """Run backtest with cfg patched to validation window. Return (metrics, backtest_dir)."""
    val_cfg = json.loads(json.dumps(cfg))  # deep copy
    val_cfg["backtest"]["start_date"] = val_start
    val_cfg["backtest"]["end_date"] = val_end
    # Strip output-only fields if present
    for k in ("results_dir", "results_filename", "metrics"):
        val_cfg.pop(k, None)
    # Suite must not be enabled with non-empty scenarios pointing at coins we lack
    if "suite" in val_cfg["backtest"]:
        val_cfg["backtest"]["suite"]["scenarios"] = []
        val_cfg["backtest"]["suite"]["enabled"] = False
        val_cfg["backtest"]["suite"]["include_base_scenario"] = True
    val_cfg["backtest"]["ohlcv_source_dir"] = "caches/ohlcv"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(val_cfg, f, indent=2)
        cfg_path = Path(f.name)

    try:
        result = subprocess.run(
            [str(PYTHON), str(BACKTEST_SCRIPT), str(cfg_path)],
            cwd=str(PASSIVBOT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        cfg_path.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"  backtest failed (exit {result.returncode}): {result.stderr[-500:]}", file=sys.stderr)
        return None, Path()

    bt_dir = find_latest_backtest_dir()
    metrics = extract_metrics_from_backtest_dir(bt_dir)
    return metrics, bt_dir


def main():
    parser = argparse.ArgumentParser(description="OOS validate top-N Pareto candidates")
    parser.add_argument("--training-run", required=True, help="run_id from runs table")
    parser.add_argument("--val-start", required=True, help="Validation window start (YYYY-MM-DD)")
    parser.add_argument("--val-end", required=True, help="Validation window end (YYYY-MM-DD)")
    parser.add_argument("--top-n", type=int, default=5, help="Number of candidates (default: 5)")
    parser.add_argument(
        "--metric",
        default="sharpe_ratio_pnl",
        help="Stat key to rank by (default: sharpe_ratio_pnl)",
    )
    parser.add_argument(
        "--twe-cap",
        type=float,
        default=0.8,
        help="Skip candidates with total_wallet_exposure_max > this (default: 0.8)",
    )
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    conn = init_db(Path(args.db))
    candidates = top_candidates(conn, args.training_run, args.metric, args.top_n, args.twe_cap)
    if not candidates:
        print(f"No candidates found in training run {args.training_run} (after twe_cap filter)")
        return 1

    print(f"Validating top {len(candidates)} Pareto candidates from {args.training_run}")
    print(f"  Ranking metric: {args.metric}")
    print(f"  TWE cap: {args.twe_cap}")
    print(f"  Validation window: {args.val_start} → {args.val_end}")
    print()

    run_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    for i, (ind_hash, cfg, train_metrics) in enumerate(candidates, 1):
        train_stats = train_metrics.get("stats", {})
        train_sharpe = train_stats.get("sharpe_ratio_pnl", {}).get("mean", float("nan"))
        train_gain = train_stats.get("gain_usd", {}).get("mean", float("nan"))
        train_twe = train_stats.get("total_wallet_exposure_max", {}).get("mean", float("nan"))
        print(f"[{i}/{len(candidates)}] {ind_hash[:12]}... "
              f"train: sharpe={train_sharpe:.3f}, gain={train_gain:.3f}, twe_max={train_twe:.3f}")

        val_metrics_full, bt_dir = run_validation_backtest(cfg, args.val_start, args.val_end)
        if val_metrics_full is None:
            print("  validation FAILED — skipping persist")
            continue

        # Look up the validation stats
        val_stats = val_metrics_full.get("stats", val_metrics_full)
        val_sharpe = (val_stats.get("sharpe_ratio_pnl", {}).get("mean")
                      if isinstance(val_stats.get("sharpe_ratio_pnl"), dict)
                      else val_stats.get("sharpe_ratio_pnl"))
        val_gain = (val_stats.get("gain_usd", {}).get("mean")
                    if isinstance(val_stats.get("gain_usd"), dict)
                    else val_stats.get("gain_usd"))
        val_twe = (val_stats.get("total_wallet_exposure_max", {}).get("mean")
                   if isinstance(val_stats.get("total_wallet_exposure_max"), dict)
                   else val_stats.get("total_wallet_exposure_max"))

        # Extract coin coverage from backtest dir name
        coins_csv = ""
        n_coins = 0
        if bt_dir.name:
            m = re.search(r"_(\d+)days_([A-Z_]+)_[a-f0-9]+$", bt_dir.name)
            if m:
                coins_csv = m.group(2).replace("_", ",")
                n_coins = len(coins_csv.split(","))

        cur.execute(
            "INSERT OR REPLACE INTO oos_validations "
            "(individual_hash, training_run_id, val_window_start, val_window_end, val_run_at, "
            "train_metrics_json, val_metrics_json, n_coins_val, coins_val_csv, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ind_hash,
                args.training_run,
                args.val_start,
                args.val_end,
                run_at,
                json.dumps(train_metrics),
                json.dumps(val_metrics_full),
                n_coins,
                coins_csv,
                f"bt_dir={bt_dir.name}",
            ),
        )
        conn.commit()
        sharpe_str = f"{val_sharpe:.3f}" if isinstance(val_sharpe, (int, float)) else str(val_sharpe)
        gain_str = f"{val_gain:.3f}" if isinstance(val_gain, (int, float)) else str(val_gain)
        twe_str = f"{val_twe:.3f}" if isinstance(val_twe, (int, float)) else str(val_twe)
        print(f"  val:   sharpe={sharpe_str}, gain={gain_str}, twe_max={twe_str}, "
              f"coins={n_coins} ({coins_csv})")

    print()
    print(f"OOS validation complete. {len(candidates)} candidates validated against "
          f"{args.val_start} → {args.val_end}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
