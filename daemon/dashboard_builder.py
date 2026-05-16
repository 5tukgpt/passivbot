#!/usr/bin/env python3
"""
Config Optimizer Daemon — static HTML dashboard.

Reads daemon/config_optimizer.db and writes a single self-contained HTML
file at daemon/dashboards/index.html. No server; just open in a browser.

Panels:
  1. Daemon health: last run, totals, time-since-last, error rate
  2. Recent runs table (NSGA2 sweeps)
  3. Pareto front scatter — Sharpe vs Drawdown (2 most recent runs)
  4. Hypervolume convergence — HV vs generation (recent runs)
  5. Vol-filter-biting indicator — does the candidate's
     (volume_drop, n_positions) combo leave any slack for the vol filter
     to actually select? Computed per-candidate using the run's coin pool
     and the candidate's own n_positions, not a hardcoded threshold.
  6. Live config vs best Pareto candidate — per-parameter delta
  7. Cross-regime OOS validation results

Run:
    cd ~/Projects/trading-bots/passivbot
    ./venv/bin/python daemon/dashboard_builder.py

Hooked from daemon_run.py — regenerates after each nightly cycle.
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
import plotly.graph_objects as go
import plotly.io as pio

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "daemon" / "config_optimizer.db"
OUT_PATH = REPO / "daemon" / "dashboards" / "index.html"
LIVE_CONFIG_PATH = REPO / "configs" / "live" / "optimized.json"

# Volatility filter "biting" math, from Target 2 Step 0 profile
# (research/target2-vol-filter-profile.md).
#
# Passivbot's selector (passivbot-rust/src/coin_selection.rs):
#   keep_v = min(max(round(n_cand * (1 - volume_drop)), n_pos), n_cand)
#   keep_t = min(max(round(keep_v   * (1 - vol_drop)),   n_pos), keep_v)
#
# The volatility stage only drops coins when keep_t < keep_v, which in turn
# requires keep_v > n_pos (volume filter must leave slack) AND vol_drop > 0.
#
# So the right "biting" question is per-candidate, depending on the run's
# coin-pool size and the candidate's own n_positions — not a hardcoded
# volume_drop threshold. The dashboard now computes both keep_v and keep_t
# and flags the candidate as biting iff keep_t < keep_v.


def _coins_count_from_csv(coins_csv: str | None) -> int | None:
    """`runs.coins_csv` is either a comma-joined ticker list ("BTC,ETH,SOL")
    or a synthetic label whose first token is a count ("14,coins"). Return
    the inferred candidate-pool size, or None if unparseable.
    """
    if not coins_csv:
        return None
    parts = [p.strip() for p in coins_csv.split(",") if p.strip()]
    if not parts:
        return None
    if parts[0].isdigit():
        return int(parts[0])
    return len(parts)


def _selector_keep(n_cand: int, n_pos: int, drop_pct: float) -> int:
    """Mirror Passivbot's selector clamp: keep = min(max(round(n*(1-drop)), n_pos), n)."""
    if n_cand <= 0 or n_pos <= 0:
        return 0
    keep = round(n_cand * (1.0 - max(0.0, min(1.0, drop_pct))))
    if keep == 0:
        keep = 1
    keep = max(keep, n_pos)
    keep = min(keep, n_cand)
    return keep


def biting_metrics(n_cand: int | None, n_pos: float | int | None,
                   volume_drop: float | None,
                   vol_drop: float | None) -> dict:
    """Return {keep_v, keep_t, biting} or {keep_v: None, ...} on bad inputs."""
    out = {"keep_v": None, "keep_t": None, "biting": None}
    if n_cand is None or n_pos is None or volume_drop is None:
        return out
    try:
        n_pos_i = max(1, int(round(float(n_pos))))
    except (TypeError, ValueError):
        return out
    keep_v = _selector_keep(n_cand, n_pos_i, float(volume_drop))
    out["keep_v"] = keep_v
    if vol_drop is None:
        out["biting"] = False
        return out
    keep_t = _selector_keep(keep_v, n_pos_i, float(vol_drop))
    out["keep_t"] = keep_t
    out["biting"] = keep_t < keep_v
    return out


def fetch(con: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def stat(metrics_json: str, key: str) -> float | None:
    """Pull a value from individuals.metrics_json. The JSON is nested:
        { "objectives": {...}, "stats": {...}, "constraint_violation": ... }
    so most metrics live under "stats". Suite-aggregated metrics come back
    as dicts like {"mean": ..., "median": ...}; we take "mean" in that case.
    """
    try:
        m = json.loads(metrics_json) if metrics_json else {}
    except (TypeError, json.JSONDecodeError):
        return None

    def _flatten(v):
        if isinstance(v, dict):
            v = v.get("mean") if "mean" in v else next(iter(v.values()), None)
        return float(v) if isinstance(v, (int, float)) else None

    if isinstance(m, dict):
        s = m.get("stats")
        if isinstance(s, dict) and key in s:
            return _flatten(s.get(key))
        if key in m:
            return _flatten(m.get(key))
    return None


def health_card(con: sqlite3.Connection) -> dict:
    runs = fetch(con, "SELECT * FROM runs ORDER BY started_at DESC")
    indivs = fetch(con, "SELECT COUNT(*) AS n FROM individuals")
    oos = fetch(con, "SELECT COUNT(*) AS n FROM oos_validations")

    # Daemon runs (success/fail tracking, if available).
    try:
        dr = fetch(con,
            "SELECT status, started_at, duration_s, error "
            "FROM daemon_runs ORDER BY started_at DESC LIMIT 30")
    except Exception:
        dr = pd.DataFrame(columns=["status", "started_at", "duration_s", "error"])

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

    n_daemon = len(dr)
    n_failed = int((dr["status"] == "failed").sum()) if n_daemon else 0
    err_rate = f"{(n_failed/n_daemon*100):.0f}%" if n_daemon else "—"
    last_daemon_status = dr.iloc[0]["status"] if n_daemon else "—"

    return {
        "n_runs": len(runs),
        "n_individuals": int(indivs.iloc[0]["n"]) if not indivs.empty else 0,
        "n_pareto": int(runs["n_pareto"].sum()) if not runs.empty else 0,
        "n_oos_validations": int(oos.iloc[0]["n"]) if not oos.empty else 0,
        "last_run_age": age_str,
        "last_run_id": runs.iloc[0]["run_id"] if not runs.empty else "—",
        "n_daemon_runs": n_daemon,
        "err_rate": err_rate,
        "last_daemon_status": last_daemon_status,
    }


def recent_runs_table(con: sqlite3.Connection, limit: int = 10) -> str:
    df = fetch(con, f"""
        SELECT started_at, run_id, exchange, n_days, n_pareto,
               substr(coins_csv, 1, 50) || (CASE WHEN length(coins_csv) > 50 THEN '...' ELSE '' END) AS coins
        FROM runs ORDER BY started_at DESC LIMIT {limit}
    """)
    if df.empty:
        return "<p>No runs recorded yet.</p>"
    df = df.copy()
    df["run_id"] = df["run_id"].str.slice(0, 35) + "…"
    return df.to_html(index=False, classes="datatable", escape=False, border=0)


def pareto_figures(con: sqlite3.Connection) -> list[tuple[str, str]]:
    runs = fetch(con, "SELECT run_id, coins_csv FROM runs ORDER BY started_at DESC LIMIT 2")
    figs: list[tuple[str, str]] = []
    for _, run_row in runs.iterrows():
        run_id = run_row["run_id"]
        n_cand = _coins_count_from_csv(run_row["coins_csv"])
        ind = fetch(con, """
            SELECT individual_hash, bot_long_json, metrics_json FROM individuals
            WHERE run_id = ? AND in_pareto = 1
        """, (run_id,))
        if ind.empty:
            continue

        def _gv(bot_json: str, key: str) -> float | None:
            try:
                b = json.loads(bot_json) if bot_json else {}
                v = b.get(key)
                return float(v) if v is not None else None
            except (TypeError, json.JSONDecodeError):
                return None

        ind["sharpe"] = ind["metrics_json"].apply(lambda m: stat(m, "sharpe_ratio_pnl"))
        ind["drawdown"] = ind["metrics_json"].apply(lambda m: stat(m, "drawdown_worst_usd"))
        ind["adg_pnl"] = ind["metrics_json"].apply(lambda m: stat(m, "adg_pnl"))
        ind["volume_drop"] = ind["bot_long_json"].apply(lambda b: _gv(b, "filter_volume_drop_pct"))
        ind["vol_drop"] = ind["bot_long_json"].apply(lambda b: _gv(b, "filter_volatility_drop_pct"))
        ind["n_pos"] = ind["bot_long_json"].apply(lambda b: _gv(b, "n_positions"))
        bm = ind.apply(
            lambda r: biting_metrics(n_cand, r["n_pos"], r["volume_drop"], r["vol_drop"]),
            axis=1,
        )
        ind["keep_v"] = bm.apply(lambda d: d["keep_v"])
        ind["biting"] = bm.apply(
            lambda d: "biting" if d["biting"] is True else (
                "phantom" if d["biting"] is False else "unknown"
            )
        )
        plot_df = ind.dropna(subset=["sharpe", "drawdown"]).copy()
        if plot_df.empty:
            continue
        fig = px.scatter(
            plot_df, x="drawdown", y="sharpe",
            color="biting",
            symbol="biting",
            hover_data=["individual_hash", "adg_pnl", "volume_drop", "vol_drop", "keep_v"],
            title=f"Pareto front — {run_id[:35]}…",
            labels={"drawdown": "Drawdown (worst, fraction)",
                    "sharpe": "Sharpe ratio (PnL)",
                    "biting": "Vol filter"},
            color_discrete_map={"biting": "#28a745", "phantom": "#6a737d", "unknown": "#cccccc"},
            category_orders={"biting": ["biting", "phantom", "unknown"]},
        )
        fig.update_layout(height=380, margin=dict(l=40, r=20, t=50, b=40),
                          paper_bgcolor="#fff", plot_bgcolor="#f6f7f9",
                          legend=dict(orientation="h", y=-0.2))
        figs.append((run_id, pio.to_html(fig, include_plotlyjs=False, full_html=False)))
    return figs


def hypervolume_figure(con: sqlite3.Connection) -> str:
    """Plot HV-vs-generation for the 4 most recent runs, on a shared chart.
    Each run is normalized to its own HV-max so they can be visually compared
    on convergence shape (different substrates can have very different
    absolute HV scales).
    """
    try:
        df = fetch(con, """
            SELECT h.run_id, h.generation, h.hypervolume, h.n_pareto, h.n_evals,
                   r.started_at, r.n_days
            FROM pareto_hypervolume h
            JOIN runs r ON r.run_id = h.run_id
            WHERE h.run_id IN (
                SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 4
            )
            ORDER BY h.run_id, h.generation
        """)
    except Exception:
        return "<p>Hypervolume table not initialized. Run <code>compute_hypervolume.py --all</code>.</p>"
    if df.empty:
        return "<p>No hypervolume data yet.</p>"
    # Short label per run: window-days + last 4 chars of hash.
    df["label"] = df["run_id"].str.extract(r"^(\d{4}-\d{2}-\d{2})")[0] + \
                  " · " + df["n_days"].astype(str) + "d · " + df["run_id"].str[-6:]
    # Normalize within run so curves are comparable.
    df["hv_normalized"] = df.groupby("run_id")["hypervolume"].transform(
        lambda s: s / s.max() if s.max() > 0 else s
    )

    fig = go.Figure()
    for label, sub in df.groupby("label"):
        fig.add_trace(go.Scatter(
            x=sub["generation"], y=sub["hv_normalized"],
            mode="lines+markers", name=label,
            hovertemplate="gen %{x}<br>hv_norm=%{y:.3f}<br>%{customdata[0]} evals<br>%{customdata[1]} pareto",
            customdata=sub[["n_evals", "n_pareto"]].values,
        ))
    fig.update_layout(
        title="Hypervolume convergence (normalized within run)",
        xaxis_title="Generation",
        yaxis_title="HV (run-relative)",
        height=380, margin=dict(l=40, r=20, t=50, b=40),
        paper_bgcolor="#fff", plot_bgcolor="#f6f7f9",
        legend=dict(orientation="h", y=-0.2),
    )
    return pio.to_html(fig, include_plotlyjs=False, full_html=False)


def vol_filter_biting_panel(con: sqlite3.Connection) -> tuple[str, str]:
    """For the most recent run, return (status_html, table_html).

    Computes biting per-candidate using (n_cand from runs.coins_csv,
    n_positions from the candidate's own bot_long_json). 'Biting' means
    `keep_t < keep_v` — the volatility stage drops at least one coin that
    the volume stage left in.
    """
    runs = fetch(con, "SELECT run_id, coins_csv FROM runs ORDER BY started_at DESC LIMIT 1")
    if runs.empty:
        return ("<p>No runs.</p>", "")
    run_id = runs.iloc[0]["run_id"]
    n_cand = _coins_count_from_csv(runs.iloc[0]["coins_csv"])
    ind = fetch(con,
        "SELECT individual_hash, bot_long_json, metrics_json FROM individuals "
        "WHERE run_id = ? AND in_pareto = 1", (run_id,))
    if ind.empty:
        return ("<p>No Pareto data for latest run.</p>", "")

    def _gv(bot_json: str, key: str) -> float | None:
        try:
            b = json.loads(bot_json) if bot_json else {}
            v = b.get(key)
            return float(v) if v is not None else None
        except (TypeError, json.JSONDecodeError):
            return None

    ind["volume_drop"] = ind["bot_long_json"].apply(lambda b: _gv(b, "filter_volume_drop_pct"))
    ind["volatility_drop"] = ind["bot_long_json"].apply(lambda b: _gv(b, "filter_volatility_drop_pct"))
    ind["n_pos"] = ind["bot_long_json"].apply(lambda b: _gv(b, "n_positions"))
    ind["adg_pnl"] = ind["metrics_json"].apply(lambda m: stat(m, "adg_pnl"))
    ind["sharpe"] = ind["metrics_json"].apply(lambda m: stat(m, "sharpe_ratio_pnl"))
    ind["drawdown"] = ind["metrics_json"].apply(lambda m: stat(m, "drawdown_worst_usd"))

    bm = ind.apply(
        lambda r: biting_metrics(n_cand, r["n_pos"], r["volume_drop"], r["volatility_drop"]),
        axis=1,
    )
    ind["keep_v"] = bm.apply(lambda d: d["keep_v"])
    ind["keep_t"] = bm.apply(lambda d: d["keep_t"])
    ind["biting_flag"] = bm.apply(lambda d: d["biting"])
    ind["biting"] = ind["biting_flag"].apply(
        lambda v: "✓ biting" if v is True else ("— not biting" if v is False else "—")
    )

    n_total = int(ind["biting_flag"].notna().sum())
    n_biting = int((ind["biting_flag"] == True).sum())  # noqa: E712
    pct = (n_biting / n_total * 100) if n_total else 0
    biting_color = "#28a745" if n_biting > 0 else "#d73a49"
    n_cand_str = str(n_cand) if n_cand else "?"
    status_html = (
        f'<div class="card" style="border-left: 4px solid {biting_color}">'
        f'<div class="label">Vol-filter biting?</div>'
        f'<div class="value">{n_biting}/{n_total} ({pct:.0f}%)</div>'
        f'<div style="font-size:11px;color:#586069;margin-top:4px">'
        f'Pareto candidates where keep_t &lt; keep_v · run pool = {n_cand_str} coins</div>'
        f'</div>'
    )

    show = ind.dropna(subset=["volume_drop"]).copy()
    if show.empty:
        return (status_html, "<p>No volume_drop values to display.</p>")
    show["hash"] = show["individual_hash"].str.slice(0, 12)
    show["n_pos"] = show["n_pos"].apply(lambda v: f"{int(v)}" if pd.notna(v) else "—")
    show = show[["hash", "n_pos", "volume_drop", "keep_v", "volatility_drop", "keep_t",
                 "biting", "adg_pnl", "sharpe", "drawdown"]].copy()
    for col in ["volume_drop", "volatility_drop", "adg_pnl", "sharpe", "drawdown"]:
        show[col] = pd.to_numeric(show[col], errors="coerce").round(4)
    show = show.sort_values("volume_drop").head(20)
    return (status_html, show.to_html(index=False, classes="datatable", escape=False, border=0))


def live_vs_pareto_panel(con: sqlite3.Connection) -> str:
    """Compare current live config (configs/live/optimized.json long block) to
    the best Pareto candidate (highest sharpe_ratio_pnl on the most recent
    run). Show per-parameter delta for the parameters in the optimizer bounds.
    """
    if not LIVE_CONFIG_PATH.exists():
        return f"<p>Live config not found at {LIVE_CONFIG_PATH}.</p>"
    try:
        with open(LIVE_CONFIG_PATH) as f:
            live = json.load(f)
    except Exception as exc:
        return f"<p>Failed to read live config: {exc}</p>"
    live_long = live.get("bot", {}).get("long", {})
    if not live_long:
        return "<p>Live config has no bot.long section.</p>"

    runs = fetch(con, "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1")
    if runs.empty:
        return "<p>No runs to compare.</p>"
    run_id = runs.iloc[0]["run_id"]
    ind = fetch(con,
        "SELECT individual_hash, bot_long_json, metrics_json FROM individuals "
        "WHERE run_id = ? AND in_pareto = 1", (run_id,))
    if ind.empty:
        return "<p>No Pareto candidates for latest run.</p>"
    ind["sharpe"] = ind["metrics_json"].apply(lambda m: stat(m, "sharpe_ratio_pnl"))
    ind = ind.dropna(subset=["sharpe"]).sort_values("sharpe", ascending=False)
    if ind.empty:
        return "<p>No scoreable candidates for latest run.</p>"
    best = ind.iloc[0]
    best_long = json.loads(best["bot_long_json"])
    best_hash = best["individual_hash"][:12]
    best_sharpe = best["sharpe"]

    keys = sorted(set(live_long.keys()) | set(best_long.keys()))
    rows = []
    for k in keys:
        lv = live_long.get(k)
        bv = best_long.get(k)
        if not isinstance(lv, (int, float)) or not isinstance(bv, (int, float)):
            continue
        if lv == bv:
            delta_str = "—"
            pct_str = "—"
        else:
            delta = bv - lv
            pct = (delta / lv * 100) if lv else float("inf")
            delta_str = f"{delta:+.4g}"
            pct_str = f"{pct:+.1f}%" if abs(pct) < 1e6 else "n/a"
        rows.append({"parameter": k, "live": lv, "best_pareto": bv,
                     "delta": delta_str, "pct": pct_str})
    df = pd.DataFrame(rows)
    if df.empty:
        return "<p>No comparable numeric parameters.</p>"
    table = df.to_html(index=False, classes="datatable", escape=False, border=0)
    header = (
        f'<p>Best Pareto candidate from <code>{run_id[:35]}…</code> · '
        f'hash <code>{best_hash}</code> · '
        f'<strong>Sharpe = {best_sharpe:.3f}</strong>. '
        f'Live config: <code>{LIVE_CONFIG_PATH.name}</code>.</p>'
    )
    return header + table


def oos_summary(con: sqlite3.Connection) -> str:
    df = fetch(con, """
        SELECT individual_hash, training_run_id, val_window_start, val_window_end,
               train_metrics_json, val_metrics_json, n_coins_val
        FROM oos_validations ORDER BY val_run_at DESC LIMIT 20
    """)
    if df.empty:
        return "<p>No OOS validations recorded yet.</p>"

    def _nested(s: str, key: str) -> float | None:
        try:
            m = json.loads(s) if s else {}
            inner = m.get("stats") if isinstance(m, dict) else None
            src = inner if isinstance(inner, dict) else m
            v = src.get(key) if isinstance(src, dict) else None
            return float(v) if v is not None else None
        except (TypeError, json.JSONDecodeError):
            return None

    df["hash"] = df["individual_hash"].str.slice(0, 12)
    df["train_sharpe"] = df["train_metrics_json"].apply(lambda m: _nested(m, "sharpe_ratio_pnl"))
    df["val_sharpe"] = df["val_metrics_json"].apply(lambda m: _nested(m, "sharpe_ratio_pnl"))
    df["train_twe"] = df["train_metrics_json"].apply(lambda m: _nested(m, "total_wallet_exposure_max"))
    df["val_twe"] = df["val_metrics_json"].apply(lambda m: _nested(m, "total_wallet_exposure_max"))
    df["train_gain"] = df["train_metrics_json"].apply(lambda m: _nested(m, "gain_usd"))
    df["val_gain"] = df["val_metrics_json"].apply(lambda m: _nested(m, "gain_usd"))
    out = df[["hash", "val_window_start", "val_window_end", "n_coins_val",
              "train_sharpe", "val_sharpe", "train_twe", "val_twe",
              "train_gain", "val_gain"]].round(4)
    return out.to_html(index=False, classes="datatable", escape=False, border=0)


HTML_TMPL = Template("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Config Optimizer Daemon — Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #fafbfc; color: #111; max-width: 1400px; }
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
  code { background: #f6f8fa; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 1000px) { .two-col { grid-template-columns: 1fr; } }
</style></head>
<body>
<h1>Config Optimizer Daemon — Dashboard</h1>
<p style="color:#586069">Phase 1: NSGA2 sweeps × 2 substrates × cross-OOS validation, nightly @ 03:00 local.</p>

<div class="health">
  <div class="card"><div class="label">NSGA2 runs</div><div class="value">$n_runs</div></div>
  <div class="card"><div class="label">Individuals</div><div class="value">$n_individuals</div></div>
  <div class="card"><div class="label">Pareto candidates</div><div class="value">$n_pareto</div></div>
  <div class="card"><div class="label">OOS validations</div><div class="value">$n_oos_validations</div></div>
  <div class="card"><div class="label">Daemon cycles</div><div class="value">$n_daemon_runs</div></div>
  <div class="card"><div class="label">Error rate</div><div class="value">$err_rate</div></div>
  <div class="card"><div class="label">Last cycle</div><div class="value">$last_daemon_status</div></div>
  <div class="card"><div class="label">Last NSGA2</div><div class="value">$last_run_age</div></div>
  $vol_filter_card
</div>

<h2>Recent runs</h2>
$recent_runs

<h2>Pareto fronts (2 most recent runs)</h2>
<div class="two-col">$pareto_block</div>

<h2>Hypervolume convergence</h2>
<p style="color:#586069">Per-generation hypervolume (normalized within run). Monotonic = good — NSGA2 should always expand the front.</p>
$hypervolume_block

<h2>Vol-filter biting? (latest run, long side)</h2>
<p style="color:#586069">Target 2 finding: <code>filter_volatility_drop_pct</code> only changes selection when the volume stage leaves slack (<code>keep_v &gt; n_positions</code>). The table shows per-candidate <code>keep_v</code> (after volume stage) and <code>keep_t</code> (after vol stage) — biting iff <code>keep_t &lt; keep_v</code>. Live config (<code>volume_drop=0.884, n_pos=5</code>, 15 coins) sits at <code>keep_v = 5 = keep_t</code> → not biting.</p>
$vol_filter_table

<h2>Live config vs best Pareto candidate (long side)</h2>
$live_vs_pareto

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
        hv_html = hypervolume_figure(con)
        vol_card_html, vol_table_html = vol_filter_biting_panel(con)
        live_vs_pareto_html = live_vs_pareto_panel(con)
        oos_table = oos_summary(con)
    finally:
        con.close()

    html = HTML_TMPL.substitute(
        n_runs=health["n_runs"],
        n_individuals=health["n_individuals"],
        n_pareto=health["n_pareto"],
        n_oos_validations=health["n_oos_validations"],
        n_daemon_runs=health["n_daemon_runs"],
        err_rate=health["err_rate"],
        last_daemon_status=health["last_daemon_status"],
        last_run_age=health["last_run_age"],
        recent_runs=recent_runs,
        pareto_block=pareto_block,
        hypervolume_block=hv_html,
        vol_filter_card=vol_card_html,
        vol_filter_table=vol_table_html,
        live_vs_pareto=live_vs_pareto_html,
        oos_table=oos_table,
        now_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        db_path=str(DB_PATH),
    )
    OUT_PATH.write_text(html)
    print(f"Wrote {OUT_PATH}  (open in a browser)")
    print(f"  runs: {health['n_runs']}  individuals: {health['n_individuals']}  "
          f"pareto: {health['n_pareto']}  oos: {health['n_oos_validations']}  "
          f"daemon_runs: {health['n_daemon_runs']}  err_rate: {health['err_rate']}  "
          f"last: {health['last_run_age']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
