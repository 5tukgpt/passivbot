#!/usr/bin/env python3
"""
fee_analysis.py — Maker/taker + per-symbol fee breakdown on the live fill cache.

Reads: passivbot/caches/fill_events/hyperliquid/hyperliquid_live/*.json
Writes: passivbot/reports/fee_analysis_<today>.md

Safe to run anytime. Pure post-processor — does not touch the bot process,
does not hit any exchange API.

Uses the authoritative maker/taker flag from ccxt's raw data when present;
falls back to implied-fee-rate inference (HL maker ~0.015%, taker ~0.035%)
only if the flag is missing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # passivbot/
CACHE_DIR = REPO / "caches" / "fill_events" / "hyperliquid" / "hyperliquid_live"
REPORT_DIR = REPO / "reports"

# ---------------------------------------------------------------------------
# HL fee-rate thresholds for the fallback path only
# ---------------------------------------------------------------------------
MAKER_MAX = 0.00020  # 0.020% — tolerant upper bound on maker
TAKER_MIN = 0.00030  # 0.030% — tolerant lower bound on taker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_fill(fill: dict) -> str:
    """Return 'maker', 'taker', or 'unknown' for a fill dict."""
    # 1. Prefer the explicit ccxt flag from raw data.
    raw = fill.get("raw") or []
    if raw:
        data = raw[0].get("data", {}) if isinstance(raw[0], dict) else {}
        flag = data.get("takerOrMaker")
        if flag in ("maker", "taker"):
            return flag

    # 2. Fall back to implied fee rate.
    price = fill.get("price") or 0
    qty = fill.get("qty") or 0
    fees = fill.get("fees") or {}
    cost = fees.get("cost") if isinstance(fees, dict) else None
    notional = price * qty
    if not notional or cost is None:
        return "unknown"
    rate = cost / notional
    if rate <= MAKER_MAX:
        return "maker"
    if rate >= TAKER_MIN:
        return "taker"
    return "unknown"


def coin_from_symbol(symbol: str) -> str:
    # "SOL/USDC:USDC" -> "SOL"
    return symbol.split("/")[0] if "/" in symbol else symbol


def load_fills():
    for f in sorted(CACHE_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {e}")
            continue
        # Some days may have dict wrappers with status=api_error (once that
        # feature lands). Only iterate list-shaped contents.
        if not isinstance(data, list):
            continue
        day = f.stem  # YYYY-MM-DD
        for fill in data:
            yield day, fill


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def analyse():
    # Per-symbol stats
    per_symbol = defaultdict(lambda: {
        "fills": 0, "maker": 0, "taker": 0, "unknown": 0,
        "gross": 0.0, "fees": 0.0,
    })
    # Per-day stats (for timeseries)
    per_day = defaultdict(lambda: {
        "fills": 0, "maker": 0, "taker": 0, "unknown": 0,
        "gross": 0.0, "fees": 0.0,
    })
    # Per-order-type (entry_initial_*, close_grid_*, close_trailing_*, etc.)
    per_type = defaultdict(lambda: {
        "fills": 0, "maker": 0, "taker": 0, "unknown": 0,
        "gross": 0.0, "fees": 0.0,
    })
    # Top fee offenders
    offenders = []

    total = {"fills": 0, "maker": 0, "taker": 0, "unknown": 0,
             "gross": 0.0, "fees": 0.0, "notional": 0.0}

    for day, fill in load_fills():
        bucket = classify_fill(fill)
        coin = coin_from_symbol(fill.get("symbol", "?"))
        ot = fill.get("pb_order_type", "unknown")
        qty = fill.get("qty") or 0
        price = fill.get("price") or 0
        pnl = fill.get("pnl") or 0
        fee = (fill.get("fees") or {}).get("cost") or 0
        notional = abs(qty * price)  # short fills have negative qty

        for agg in (per_symbol[coin], per_day[day], per_type[ot], total):
            agg["fills"] += 1
            agg[bucket] += 1
            agg["gross"] += pnl
            agg["fees"] += fee
        total["notional"] += notional

        offenders.append((fee, day, coin, ot, bucket, notional, pnl))

    offenders.sort(reverse=True)
    return total, per_symbol, per_day, per_type, offenders[:15]


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "—"


def usd(n):
    return f"${n:+,.2f}" if n else "$0.00"


def render(total, per_symbol, per_day, per_type, offenders) -> str:
    out = []
    today = datetime.now(timezone.utc).date().isoformat()
    out.append(f"# Passivbot Fee Analysis — {today}\n")
    out.append("Source: `caches/fill_events/hyperliquid/hyperliquid_live/*.json`")
    out.append("Maker/taker flag from ccxt `raw.data.takerOrMaker` when present; "
               "fee-rate inference (≤0.020% maker / ≥0.030% taker) otherwise.\n")

    # -- Overall --------------------------------------------------------------
    out.append("## Overall\n")
    fills = total["fills"]
    net = total["gross"] - total["fees"]
    fee_drag = (total["fees"] / total["gross"] * 100.0) if total["gross"] else 0.0
    out.append(f"- Fills: **{fills:,}**")
    out.append(f"- Notional: **${total['notional']:,.0f}**")
    out.append(f"- Maker: **{total['maker']} ({pct(total['maker'], fills)})**")
    out.append(f"- Taker: **{total['taker']} ({pct(total['taker'], fills)})**")
    if total["unknown"]:
        out.append(f"- Unknown: {total['unknown']} ({pct(total['unknown'], fills)})")
    out.append(f"- Gross PnL: **{usd(total['gross'])}**")
    out.append(f"- Fees: **{usd(-total['fees'])}**")
    out.append(f"- Net PnL: **{usd(net)}**")
    out.append(f"- Fee / gross ratio: **{fee_drag:.1f}%**")
    if total["fees"] and total["notional"]:
        blended = total["fees"] / total["notional"] * 10000  # bps
        out.append(f"- Blended effective fee rate: **{blended:.2f} bps** of notional")
    out.append("")

    # -- Per symbol -----------------------------------------------------------
    out.append("## Per Symbol\n")
    out.append("| Coin | Fills | Maker% | Taker% | Gross | Fees | Net | Fee/Gross |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    rows = sorted(per_symbol.items(), key=lambda kv: -kv[1]["fills"])
    for coin, s in rows:
        net_s = s["gross"] - s["fees"]
        fg = (s["fees"] / s["gross"] * 100.0) if s["gross"] > 0 else float("inf") if s["fees"] else 0.0
        fg_disp = f"{fg:.0f}%" if fg != float("inf") else "∞"
        out.append(
            f"| {coin} | {s['fills']} | {pct(s['maker'], s['fills'])} | "
            f"{pct(s['taker'], s['fills'])} | {usd(s['gross'])} | "
            f"{usd(-s['fees'])} | {usd(net_s)} | {fg_disp} |"
        )
    out.append("")

    # -- Per order type -------------------------------------------------------
    out.append("## Per Order Type\n")
    out.append("| Order type | Fills | Maker% | Taker% | Gross | Fees |")
    out.append("|---|---:|---:|---:|---:|---:|")
    rows = sorted(per_type.items(), key=lambda kv: -kv[1]["fills"])
    for ot, s in rows:
        out.append(
            f"| `{ot}` | {s['fills']} | {pct(s['maker'], s['fills'])} | "
            f"{pct(s['taker'], s['fills'])} | {usd(s['gross'])} | "
            f"{usd(-s['fees'])} |"
        )
    out.append("")

    # -- Daily timeseries -----------------------------------------------------
    out.append("## Daily Timeseries\n")
    out.append("| Day | Fills | Maker% | Taker% | Gross | Fees | Net |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for day, s in sorted(per_day.items()):
        net_d = s["gross"] - s["fees"]
        out.append(
            f"| {day} | {s['fills']} | {pct(s['maker'], s['fills'])} | "
            f"{pct(s['taker'], s['fills'])} | {usd(s['gross'])} | "
            f"{usd(-s['fees'])} | {usd(net_d)} |"
        )
    out.append("")

    # -- Offenders ------------------------------------------------------------
    out.append("## Top 15 Fee-Paying Fills\n")
    out.append("| Fee | Day | Coin | Order type | M/T | Notional | PnL |")
    out.append("|---:|---|---|---|---|---:|---:|")
    for fee, day, coin, ot, bucket, notional, pnl in offenders:
        out.append(
            f"| {usd(-fee)} | {day} | {coin} | `{ot}` | {bucket} | "
            f"${notional:,.0f} | {usd(pnl)} |"
        )
    out.append("")

    return "\n".join(out)


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    total, per_symbol, per_day, per_type, offenders = analyse()
    report = render(total, per_symbol, per_day, per_type, offenders)
    today = datetime.now(timezone.utc).date().isoformat()
    out_path = REPORT_DIR / f"fee_analysis_{today}.md"
    out_path.write_text(report)
    print(f"wrote {out_path}")
    # Also echo headline stats to stdout for a quick eyeball.
    fills = total["fills"]
    net = total["gross"] - total["fees"]
    print(f"fills={fills}  maker%={pct(total['maker'], fills)}  "
          f"taker%={pct(total['taker'], fills)}  "
          f"gross={usd(total['gross'])}  fees={usd(-total['fees'])}  net={usd(net)}")


if __name__ == "__main__":
    main()
