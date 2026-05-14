#!/usr/bin/env python3
"""
Config Optimizer Daemon — static HTML dashboard.

Reads daemon/config_optimizer.db and writes a single self-contained HTML
file at daemon/dashboards/index.html. No server; just open in a browser.

Panels:
  1. Daemon health card — last run, totals, time since last run
  2. Recent runs table — last 10 runs
  3. Pareto fronts — Sharpe vs Drawdown scatter for the 2 most recent runs
  4. OOS validation results — train vs val ADG for cross-regime checks

Run:
    cd ~/Projects/trading-bots/passivbot
    ./venv/bin/python daemon/dashboard_builder.py

To regenerate after each daemon run, wire into daemon_run.py as a final
step. For now it's manual / on-demand.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import pandas as pd
import plotly.express as px
import plotly.io as pio

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "daemon" / "config_optimizer.db"
OUT_PATH = REPO / "daemon" / "dashboards" / "index.html"


def fetch(con: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def safe_metric(row_json: str, key: str) -> float | None:
    try:
        m = json.loads(row_json) if row_json else {}
        v = m.get(key)
        return float(v) if v is not None else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def health_card(con: sqlite3.Connection) -> dict:
    runs = fetch(con, "SELECT * FROM runs ORDER BY started_at DESC")
    indivs = fetch(con, "SELECT COUNT(*) AS n FROM individuals")
    oos = fetch(con, "SELECT COUNT(*) AS n FROM oos_validations")
    last_run_at = runs.iloc[0]["started_at"] if not runs.empty else None
    age_str = "—"
    if last_run_at:
        try:
            t = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - t
            hours = delta.total_seconds() / 3600
            age_str = f"{hours:.1f}h ago" if hours < 48 else f"{hours/24:.1f}d ago"
        except (ValueError, AttributeError):
            age_str = last_run_at
    return {
        "n_runs": len(runs),
        "n_individuals": int(indivs.iloc[0]["n"]) if not indivs.empty else 0,
        "n_pareto": int(runs["n_pareto"].sum()) if not runs.empty else 0,
        "n_oos_validations": int(oos.iloc[0]["n"]) if not oos.empty else 0,
        "last_run_age": age_str,
        "last_run_id": runs.iloc[0]["run_id"] if not runs.empty else "—",
    }


def recent_runs_table(con: sqlite3.Connection, limit: int = 10) -> str:
    df = fetch(con, f"""
        SELECT started_at, run_id, exchange, n_days, n_pareto,
               substr(coins_csv, 1, 50) || (CASE WHEN length(coins_csv) > 50 THEN '...' ELSE '' END) AS coins
        FROM runs ORDER BY started_at DESC LIMIT {limit}
    """)
    if df.empty:
        return "<p>No runs recorded yet.</p>"
    return df.to_html(index=False, classes="datatable", escape=False, border=0)


def pareto_figures(con: sqlite3.Connection) -> list[tuple[str, str]]:
    runs = fetch(con, "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 2")
    figs: list[tuple[str, str]] = []
    for run_id in runs["run_id"].tolist():
        ind = fetch(con, """
            SELECT individual_hash, metrics_json FROM individuals
            WHERE run_id = ? AND in_pareto = 1
        """, (run_id,))
        if ind.empty:
            continue
        ind["sharpe"] = ind["metrics_json"].apply(lambda m: safe_metric(m, "sharpe_ratio_pnl"))
        ind["drawdown"] = ind["metrics_json"].apply(lambda m: safe_metric(m, "drawdown_worst_usd"))
        ind["adg_pnl"] = ind["metrics_json"].apply(lambda m: safe_metric(m, "adg_pnl"))
        plot_df = ind.dropna(subset=["sharpe", "drawdown"]).copy()
        if plot_df.empty:
            continue
        fig = px.scatter(
            plot_df, x="drawdown", y="sharpe", color="adg_pnl",
            hover_data=["individual_hash"],
            title=f"Pareto front — {run_id}",
            labels={"drawdown": "Drawdown (worst, USD)", "sharpe": "Sharpe ratio (PnL)", "adg_pnl": "ADG"},
            color_continuous_scale="Viridis",
        )
        fig.update_layout(height=400, margin=dict(l=40, r=20, t=50, b=40),
                          paper_bgcolor="#fff", plot_bgcolor="#f6f7f9")
        figs.append((run_id, pio.to_html(fig, include_plotlyjs=False, full_html=False)))
    return figs


def oos_summary(con: sqlite3.Connection) -> str:
    df = fetch(con, """
        SELECT training_run_id, val_window_start, val_window_end,
               train_metrics_json, val_metrics_json, n_coins_val
        FROM oos_validations ORDER BY val_run_at DESC LIMIT 20
    """)
    if df.empty:
        return "<p>No OOS validations recorded yet.</p>"
    df["train_adg"] = df["train_metrics_json"].apply(lambda m: safe_metric(m, "adg_pnl"))
    df["val_adg"] = df["val_metrics_json"].apply(lambda m: safe_metric(m, "adg_pnl"))
    df["train_dd"] = df["train_metrics_json"].apply(lambda m: safe_metric(m, "drawdown_worst_usd"))
    df["val_dd"] = df["val_metrics_json"].apply(lambda m: safe_metric(m, "drawdown_worst_usd"))
    out = df[["training_run_id", "val_window_start", "val_window_end",
              "n_coins_val", "train_adg", "val_adg", "train_dd", "val_dd"]].round(4)
    return out.to_html(index=False, classes="datatable", escape=False, border=0)


HTML_TMPL = Template("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Config Optimizer Daemon — Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #fafbfc; color: #111; }
  h1 { margin-top: 0; }
  h2 { margin-top: 32px; border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; }
  .health { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 14px; }
  .card .label { color: #6a737d; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
  .datatable { border-collapse: collapse; width: 100%; font-size: 13px; }
  .datatable th, .datatable td { border: 1px solid #e1e4e8; padding: 6px 10px; text-align: left; }
  .datatable th { background: #f6f8fa; }
  .footer { margin-top: 32px; color: #6a737d; font-size: 12px; }
  .fig-block { margin: 12px 0; }
</style></head>
<body>
<h1>Config Optimizer Daemon — Dashboard</h1>
<p style="color:#586069">Phase 1: NSGA2 sweeps × 2 substrates × cross-OOS validation, nightly @ 03:00 local.</p>

<div class="health">
  <div class="card"><div class="label">Total runs</div><div class="value">$n_runs</div></div>
  <div class="card"><div class="label">Individuals</div><div class="value">$n_individuals</div></div>
  <div class="card"><div class="label">Pareto candidates</div><div class="value">$n_pareto</div></div>
  <div class="card"><div class="label">OOS validations</div><div class="value">$n_oos_validations</div></div>
  <div class="card"><div class="label">Last run</div><div class="value">$last_run_age</div></div>
</div>

<h2>Recent runs</h2>
$recent_runs

<h2>Pareto fronts (2 most recent runs)</h2>
$pareto_block

<h2>Cross-regime OOS validations (last 20)</h2>
<p style="color:#586069">Promotion gate: TWE ≥ 0.10 and gain ≥ 0.95 on BOTH substrates. Survivors get a Telegram alert.</p>
$oos_table

<p class="footer">Generated $now_utc UTC · db: $db_path</p>
</body></html>
""")


def main() -> int:
    if not DB_PATH.exists():
        print(f"FATAL: {DB_PATH} not found", file=sys.stderr)
        return 1
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    try:
        health = health_card(con)
        recent_runs = recent_runs_table(con)
        figs = pareto_figures(con)
        pareto_block = "\n".join(
            f'<div class="fig-block">{html}</div>' for _, html in figs
        ) or "<p>No Pareto data to plot.</p>"
        oos_table = oos_summary(con)
    finally:
        con.close()

    html = HTML_TMPL.substitute(
        n_runs=health["n_runs"],
        n_individuals=health["n_individuals"],
        n_pareto=health["n_pareto"],
        n_oos_validations=health["n_oos_validations"],
        last_run_age=health["last_run_age"],
        recent_runs=recent_runs,
        pareto_block=pareto_block,
        oos_table=oos_table,
        now_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        db_path=str(DB_PATH),
    )
    OUT_PATH.write_text(html)
    print(f"Wrote {OUT_PATH}  (open in a browser)")
    print(f"  runs: {health['n_runs']}  individuals: {health['n_individuals']}  "
          f"pareto: {health['n_pareto']}  oos: {health['n_oos_validations']}  "
          f"last: {health['last_run_age']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
