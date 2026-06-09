#!/usr/bin/env python3
"""
daily_metrics.py — Daily performance metrics for Passivbot on Hyperliquid.

Reads the local fill-event cache (caches/fill_events/hyperliquid/hyperliquid_live/)
and queries the HL API for current account value, then computes daily metrics:

  - Fill count, total notional volume
  - Maker vs taker fill breakdown
  - Gross PnL (closedPnl sum), fees paid, net PnL
  - Fee-to-gross ratio
  - Per-symbol breakdown of the above

Outputs:
  - logs/hl_daily_metrics.csv   (append-only, one row per day)
  - logs/hl_daily_metrics.json  (full history, overwritten each run)

Usage:
    python scripts/daily_metrics.py                # log yesterday's metrics
    python scripts/daily_metrics.py --date 2026-04-15  # log a specific date
    python scripts/daily_metrics.py --backfill     # process all cached fill days
    python scripts/daily_metrics.py --dry-run      # print metrics without writing

Read-only diagnostic — no trading, no config writes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
FILL_CACHE_DIR = REPO / "caches" / "fill_events" / "hyperliquid" / "hyperliquid_live"
LOG_DIR = REPO / "logs"
CSV_PATH = LOG_DIR / "hl_daily_metrics.csv"
JSON_PATH = LOG_DIR / "hl_daily_metrics.json"
API_KEYS_PATH = REPO / "api-keys.json"

DEFAULT_WALLET = "0x7336c944A995c6447bc87b856291FCE2c6205196"
INFO_URL = "https://api.hyperliquid.xyz/info"

CSV_COLUMNS = [
    "date",
    "account_value",
    "spot_usdc",
    "perp_account_value",
    "perp_unrealized_pnl",
    "withdrawable",
    "fill_count",
    "total_notional",
    "maker_count",
    "taker_count",
    "maker_pct",
    "gross_pnl",
    "fees_paid",
    "net_pnl",
    "fee_to_gross_ratio",
    "funding_paid",
    "symbols_traded",
    "symbol_breakdown",
    "funding_breakdown",
]


# ---------------------------------------------------------------------------
# Wallet resolution (same precedence as audit_hype_rebates.py)
# ---------------------------------------------------------------------------
def resolve_wallet() -> str:
    import os
    env = os.environ.get("HL_WALLET_ADDR")
    if env:
        return env
    if API_KEYS_PATH.exists():
        try:
            keys = json.loads(API_KEYS_PATH.read_text())
            addr = keys.get("hyperliquid_live", {}).get("wallet_address", "")
            if addr:
                return addr
        except Exception:
            pass
    return DEFAULT_WALLET


# ---------------------------------------------------------------------------
# HL API helpers
# ---------------------------------------------------------------------------
def hl_info(body: dict[str, Any], timeout: int = 15) -> Any:
    """POST to HL info endpoint and return parsed JSON."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(INFO_URL, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_account_breakdown(wallet: str) -> dict:
    """Fetch full account breakdown: spot USDC, perp accountValue, uPnL, withdrawable.

    Returns:
      {
        "spot_usdc":            float,  # spot USDC balance (incl. held portion)
        "spot_usdc_hold":       float,  # portion reserved as perp margin
        "perp_account_value":   float,  # perp sub-account net equity (incl. uPnL)
        "perp_unrealized_pnl":  float,  # sum of uPnL across open perp positions
        "withdrawable":         float,  # how much can be moved out right now (often $0)
        "account_value":        float,  # TRUE unified — matches HL UI
      }

    The account_value formula:
        spot_usdc + (perp_account_value - spot_usdc_hold)
    is the correct anti-double-count derivation. Prior version did
    ``spot_usdc + perp_account_value`` which double-counted ~$90 because
    spot.hold IS the perp margin reserve — adding both adds it twice.
    Canonical source: trading-bots/_scripts/v7.11_cutover_watchdog.py.
    Fixed 2026-05-27 alongside the openclaw-orchestrator monitor's
    matching fix (commit b19e3d5).
    """
    out = {
        "spot_usdc": 0.0,
        "spot_usdc_hold": 0.0,
        "perp_account_value": 0.0,
        "perp_unrealized_pnl": 0.0,
        "withdrawable": 0.0,
        "account_value": 0.0,
    }
    try:
        perp = hl_info({"type": "clearinghouseState", "user": wallet})
        out["perp_account_value"] = float(perp.get("marginSummary", {}).get("accountValue", 0))
        out["withdrawable"] = float(perp.get("withdrawable", 0))
        out["perp_unrealized_pnl"] = sum(
            float(p.get("position", {}).get("unrealizedPnl", 0))
            for p in perp.get("assetPositions", [])
        )
        time.sleep(0.25)

        spot = hl_info({"type": "spotClearinghouseState", "user": wallet})
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                out["spot_usdc"] = float(b.get("total", 0))
                out["spot_usdc_hold"] = float(b.get("hold", 0))
                break

        # CORRECT formula (matches HL UI, matches v7.11_cutover_watchdog).
        # spot_usdc.total already includes the held portion; perp_value
        # also includes that held margin (it's collateralising the perps).
        # Subtracting hold once removes the overlap.
        out["account_value"] = (
            out["spot_usdc"] + (out["perp_account_value"] - out["spot_usdc_hold"])
        )
    except Exception as e:
        print(f"WARN: Could not fetch account breakdown: {e}", file=sys.stderr)
    return out


def fetch_account_value(wallet: str) -> float:
    """Backward-compat wrapper: returns mark-to-market unified value."""
    return fetch_account_breakdown(wallet)["account_value"]


def fetch_funding_for_date(wallet: str, target: date) -> dict:
    """Fetch HL funding events for the given UTC day.

    Returns:
      {
        "funding_paid":      float,  # day total in USDC (negative = bot paid)
        "funding_breakdown": dict,   # {coin: {"events": int, "usdc": float}}
      }
    """
    start_ms = int(datetime(target.year, target.month, target.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86_400_000 - 1
    out = {"funding_paid": 0.0, "funding_breakdown": {}}
    try:
        events = hl_info({
            "type": "userFunding",
            "user": wallet,
            "startTime": start_ms,
            "endTime": end_ms,
        })
        if not isinstance(events, list):
            return out
        total = 0.0
        by_coin: dict[str, dict] = {}
        for ev in events:
            d = ev.get("delta") or {}
            coin = d.get("coin", "?")
            try:
                usdc = float(d.get("usdc", 0) or 0)
            except (TypeError, ValueError):
                usdc = 0.0
            total += usdc
            sd = by_coin.setdefault(coin, {"events": 0, "usdc": 0.0})
            sd["events"] += 1
            sd["usdc"] += usdc
        out["funding_paid"] = round(total, 6)
        out["funding_breakdown"] = {
            k: {"events": v["events"], "usdc": round(v["usdc"], 6)}
            for k, v in by_coin.items()
        }
    except Exception as e:
        print(f"WARN: Could not fetch funding for {target}: {e}", file=sys.stderr)
    return out


def fetch_fills_for_date(wallet: str, target: date) -> list[dict] | None:
    """Fetch HL fills for the given UTC day via the `userFillsByTime` endpoint.

    The local fill-event cache (FILL_CACHE_DIR) is written by the live bot, but
    it stopped updating at the 2026-05-22 v7.11 cutover (the v7.11 engine runs
    from a separate directory and never repopulated this repo's cache), so
    fill_count read 0 every day from 2026-05-23 onward. Fills are now pulled
    straight from the HL API — the same direct-API path that keeps funding
    working — and normalised into the cache's dict shape so the downstream
    extractors (classify_fill / extract_closed_pnl / extract_fee) are unchanged.

    Returns the list of fills (possibly empty), or None if the API call fails
    so the caller can fall back to the local cache for historical days.
    """
    start_ms = int(datetime(target.year, target.month, target.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86_400_000 - 1
    try:
        raw = hl_info({
            "type": "userFillsByTime",
            "user": wallet,
            "startTime": start_ms,
            "endTime": end_ms,
        })
    except Exception as e:
        print(f"WARN: Could not fetch fills for {target}: {e}", file=sys.stderr)
        return None
    if not isinstance(raw, list):
        return []

    fills = []
    for f in raw:
        try:
            px = float(f.get("px", 0) or 0)
            sz = float(f.get("sz", 0) or 0)
        except (TypeError, ValueError):
            continue
        try:
            fee = float(f.get("fee", 0) or 0)
        except (TypeError, ValueError):
            fee = 0.0
        # HL side "A" = ask/sell, "B" = bid/buy. qty is signed (negative = sell).
        is_sell = f.get("side") == "A"
        coin = f.get("coin", "?")
        closed_pnl = f.get("closedPnl", "0")
        fills.append({
            "symbol": f"{coin}/USDC:USDC",
            "side": "sell" if is_sell else "buy",
            "qty": -sz if is_sell else sz,
            "price": px,
            "pnl": float(closed_pnl or 0),
            "fees": {"currency": f.get("feeToken", "USDC"), "cost": fee},
            # `crossed` marks a taker fill (it crossed the spread).
            "raw": [{"data": {
                "takerOrMaker": "taker" if f.get("crossed") else "maker",
                "info": {"closedPnl": closed_pnl},
            }}],
        })
    return fills


# ---------------------------------------------------------------------------
# Fill cache reader
# ---------------------------------------------------------------------------
def load_fills_for_date(target: date) -> list[dict]:
    """Load fills from the local cache file for a given date."""
    cache_file = FILL_CACHE_DIR / f"{target.isoformat()}.json"
    if not cache_file.exists():
        return []
    try:
        data = json.loads(cache_file.read_text())
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARN: Error reading {cache_file}: {e}", file=sys.stderr)
        return []


def get_fills_for_date(wallet: str, target: date) -> list[dict]:
    """Resolve fills for a day, preferring the live HL API over the local cache.

    The API is authoritative going forward; the local cache is only used as a
    fallback for historical days the API can no longer reach (or if the API
    call fails).
    """
    api_fills = fetch_fills_for_date(wallet, target)
    if api_fills:
        return api_fills
    cached = load_fills_for_date(target)
    if cached:
        return cached
    return api_fills or []


def available_cache_dates() -> list[date]:
    """Return sorted list of dates that have fill cache files."""
    if not FILL_CACHE_DIR.exists():
        return []
    dates = []
    for f in FILL_CACHE_DIR.glob("*.json"):
        try:
            dates.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    dates.sort()
    return dates


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------
def classify_fill(fill: dict) -> str:
    """Return 'maker' or 'taker' for a fill."""
    raw = fill.get("raw") or []
    if raw and isinstance(raw[0], dict):
        data = raw[0].get("data", {})
        flag = data.get("takerOrMaker")
        if flag in ("maker", "taker"):
            return flag
    # Fallback: infer from fee rate
    price = fill.get("price") or 0
    qty = fill.get("qty") or 0
    fees = fill.get("fees") or {}
    cost = fees.get("cost") if isinstance(fees, dict) else None
    notional = price * qty
    if notional and cost is not None:
        rate = cost / notional
        return "maker" if rate < 0.00025 else "taker"
    return "maker"  # Passivbot is overwhelmingly maker


def extract_closed_pnl(fill: dict) -> float:
    """Extract closedPnl from the fill. Available in raw HL data."""
    raw = fill.get("raw") or []
    if raw and isinstance(raw[0], dict):
        info = raw[0].get("data", {}).get("info", {})
        cp = info.get("closedPnl")
        if cp is not None:
            return float(cp)
    # Fallback to top-level pnl
    return float(fill.get("pnl", 0))


def extract_fee(fill: dict) -> float:
    """Extract fee paid (positive = cost to us)."""
    fees = fill.get("fees") or {}
    if isinstance(fees, dict):
        return float(fees.get("cost", 0))
    return 0.0


def extract_symbol(fill: dict) -> str:
    """Extract the coin symbol (e.g. 'SOL' from 'SOL/USDC:USDC')."""
    sym = fill.get("symbol", "")
    return sym.split("/")[0] if "/" in sym else sym


def compute_metrics(fills: list[dict], target_date: date, balance: dict) -> dict:
    """Compute daily metrics from a list of fills.

    `balance` is the dict returned by fetch_account_breakdown(). For backward
    compatibility, a bare float is also accepted and treated as account_value.
    """
    if isinstance(balance, (int, float)):
        balance = {"account_value": float(balance), "spot_usdc": 0.0,
                   "perp_account_value": 0.0, "perp_unrealized_pnl": 0.0,
                   "withdrawable": 0.0}

    bal_fields = {
        "account_value": round(balance.get("account_value", 0), 2),
        "spot_usdc": round(balance.get("spot_usdc", 0), 2),
        "perp_account_value": round(balance.get("perp_account_value", 0), 2),
        "perp_unrealized_pnl": round(balance.get("perp_unrealized_pnl", 0), 2),
        "withdrawable": round(balance.get("withdrawable", 0), 2),
    }

    if not fills:
        return {
            "date": target_date.isoformat(),
            **bal_fields,
            "fill_count": 0,
            "total_notional": 0.0,
            "maker_count": 0,
            "taker_count": 0,
            "maker_pct": 0.0,
            "gross_pnl": 0.0,
            "fees_paid": 0.0,
            "net_pnl": 0.0,
            "fee_to_gross_ratio": 0.0,
            "funding_paid": 0.0,
            "symbols_traded": "",
            "symbol_breakdown": {},
            "funding_breakdown": {},
        }

    # Per-symbol accumulators
    sym_data: dict[str, dict] = {}

    total_maker = 0
    total_taker = 0
    total_notional = 0.0
    total_gross_pnl = 0.0
    total_fees = 0.0

    for fill in fills:
        sym = extract_symbol(fill)
        mt = classify_fill(fill)
        price = float(fill.get("price", 0))
        qty = float(fill.get("qty", 0))
        notional = abs(price * qty)
        closed_pnl = extract_closed_pnl(fill)
        fee = extract_fee(fill)

        if sym not in sym_data:
            sym_data[sym] = {
                "fill_count": 0,
                "notional": 0.0,
                "maker": 0,
                "taker": 0,
                "gross_pnl": 0.0,
                "fees": 0.0,
            }

        sd = sym_data[sym]
        sd["fill_count"] += 1
        sd["notional"] += notional
        sd["gross_pnl"] += closed_pnl
        sd["fees"] += fee
        if mt == "maker":
            sd["maker"] += 1
            total_maker += 1
        else:
            sd["taker"] += 1
            total_taker += 1

        total_notional += notional
        total_gross_pnl += closed_pnl
        total_fees += fee

    fill_count = total_maker + total_taker
    maker_pct = (total_maker / fill_count * 100) if fill_count else 0.0
    net_pnl = total_gross_pnl - total_fees
    fee_to_gross = (
        (total_fees / abs(total_gross_pnl) * 100)
        if total_gross_pnl != 0
        else 0.0
    )

    # Build per-symbol breakdown dict
    breakdown = {}
    for sym, sd in sorted(sym_data.items()):
        sc = sd["fill_count"]
        s_maker_pct = (sd["maker"] / sc * 100) if sc else 0.0
        s_net = sd["gross_pnl"] - sd["fees"]
        breakdown[sym] = {
            "fill_count": sc,
            "notional": round(sd["notional"], 2),
            "maker_count": sd["maker"],
            "taker_count": sd["taker"],
            "maker_pct": round(s_maker_pct, 1),
            "gross_pnl": round(sd["gross_pnl"], 6),
            "fees_paid": round(sd["fees"], 6),
            "net_pnl": round(s_net, 6),
        }

    return {
        "date": target_date.isoformat(),
        **bal_fields,
        "fill_count": fill_count,
        "total_notional": round(total_notional, 2),
        "maker_count": total_maker,
        "taker_count": total_taker,
        "maker_pct": round(maker_pct, 1),
        "gross_pnl": round(total_gross_pnl, 6),
        "fees_paid": round(total_fees, 6),
        "net_pnl": round(net_pnl, 6),
        "fee_to_gross_ratio": round(fee_to_gross, 2),
        "funding_paid": 0.0,
        "symbols_traded": "|".join(sorted(sym_data.keys())),
        "symbol_breakdown": breakdown,
        "funding_breakdown": {},
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def load_existing_csv() -> dict[str, dict]:
    """Load existing CSV rows keyed by date."""
    rows = {}
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows[row["date"]] = row
    return rows


def write_csv(all_metrics: list[dict]):
    """Write all metrics to CSV (sorted by date)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    all_metrics.sort(key=lambda m: m["date"])
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for m in all_metrics:
            row = dict(m)
            # Serialize nested dicts as JSON strings for CSV
            row["symbol_breakdown"] = json.dumps(m.get("symbol_breakdown", {}))
            row["funding_breakdown"] = json.dumps(m.get("funding_breakdown", {}))
            writer.writerow(row)


def write_json(all_metrics: list[dict]):
    """Write all metrics to JSON."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    all_metrics.sort(key=lambda m: m["date"])
    with open(JSON_PATH, "w") as f:
        json.dump(all_metrics, f, indent=2)
        f.write("\n")


def append_single_csv(metrics: dict):
    """Append a single day's metrics to CSV, creating if needed."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        row = dict(metrics)
        row["symbol_breakdown"] = json.dumps(metrics.get("symbol_breakdown", {}))
        row["funding_breakdown"] = json.dumps(metrics.get("funding_breakdown", {}))
        writer.writerow(row)


def update_json(metrics: dict):
    """Load existing JSON, upsert this day's entry, rewrite."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if JSON_PATH.exists():
        try:
            existing = json.loads(JSON_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Upsert by date
    by_date = {m["date"]: m for m in existing}
    by_date[metrics["date"]] = metrics
    all_metrics = sorted(by_date.values(), key=lambda m: m["date"])
    with open(JSON_PATH, "w") as f:
        json.dump(all_metrics, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def print_metrics(metrics: dict):
    """Pretty-print a day's metrics to stdout."""
    d = metrics
    print(f"\n{'='*60}")
    print(f"  Passivbot Daily Metrics — {d['date']}")
    print(f"{'='*60}")
    print(f"  Spot USDC (capital): ${d.get('spot_usdc', 0):>10,.2f}")
    print(f"  Perp accountValue:   ${d.get('perp_account_value', 0):>10,.2f}  (uPnL ${d.get('perp_unrealized_pnl', 0):+.2f}, withdrawable ${d.get('withdrawable', 0):,.2f})")
    print(f"  Mark-to-market sum:  ${d['account_value']:>10,.2f}")
    print(f"  Fills:             {d['fill_count']:>12d}")
    print(f"  Total Notional:    ${d['total_notional']:>12,.2f}")
    print(f"  Maker/Taker:       {d['maker_count']}/{d['taker_count']}  ({d['maker_pct']:.1f}% maker)")
    print(f"  Gross PnL:         ${d['gross_pnl']:>12.6f}")
    print(f"  Fees Paid:         ${d['fees_paid']:>12.6f}")
    print(f"  Net PnL:           ${d['net_pnl']:>12.6f}")
    print(f"  Fee/Gross Ratio:   {d['fee_to_gross_ratio']:>11.2f}%")
    print(f"  Funding Paid:      ${d.get('funding_paid', 0):>12.6f}  (negative = bot paid; positive = bot received)")

    breakdown = d.get("symbol_breakdown", {})
    if breakdown:
        print(f"\n  {'Symbol':<8} {'Fills':>6} {'Notional':>12} {'Maker%':>7} {'GrossPnL':>12} {'Fees':>10} {'NetPnL':>12}")
        print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*7} {'-'*12} {'-'*10} {'-'*12}")
        for sym, sd in sorted(breakdown.items()):
            print(
                f"  {sym:<8} {sd['fill_count']:>6d} ${sd['notional']:>11,.2f} "
                f"{sd['maker_pct']:>6.1f}% ${sd['gross_pnl']:>11.6f} "
                f"${sd['fees_paid']:>9.6f} ${sd['net_pnl']:>11.6f}"
            )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Daily metrics logger for Passivbot on Hyperliquid"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date to process (YYYY-MM-DD). Default: yesterday UTC.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process all available fill cache dates.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print metrics without writing to files.",
    )
    args = parser.parse_args()

    wallet = resolve_wallet()

    if args.backfill:
        # Cover a continuous range from the earliest cached day through
        # yesterday — the local cache stops at the v7.11 cutover (2026-05-22),
        # so a cache-only date list would miss every day since. Fills for the
        # gap days are pulled from the HL API by get_fills_for_date().
        cache_dates = available_cache_dates()
        yesterday = date.today() - timedelta(days=1)
        start = cache_dates[0] if cache_dates else yesterday
        span = (yesterday - start).days + 1
        dates = sorted(
            set(cache_dates) | {start + timedelta(days=i) for i in range(span)}
        )
        if not dates:
            print("No fill cache files found and no date range to backfill.", file=sys.stderr)
            sys.exit(1)

        print(f"Backfilling {len(dates)} days from {dates[0]} to {dates[-1]}...")

        # Fetch account breakdown once for the most recent day
        balance = fetch_account_breakdown(wallet)
        empty_balance = {"account_value": 0.0, "spot_usdc": 0.0,
                         "perp_account_value": 0.0, "perp_unrealized_pnl": 0.0,
                         "withdrawable": 0.0}

        all_metrics = []
        for i, d in enumerate(dates):
            fills = get_fills_for_date(wallet, d)
            # Only use live balance for today/yesterday; zero for historical
            bal = balance if d >= date.today() - timedelta(days=1) else empty_balance
            metrics = compute_metrics(fills, d, bal)
            funding = fetch_funding_for_date(wallet, d)
            metrics.update(funding)
            all_metrics.append(metrics)
            print_metrics(metrics)
            time.sleep(0.25)  # be polite to HL info endpoint during backfill

        if not args.dry_run:
            write_csv(all_metrics)
            write_json(all_metrics)
            print(f"Wrote {len(all_metrics)} days to:")
            print(f"  CSV:  {CSV_PATH}")
            print(f"  JSON: {JSON_PATH}")
        else:
            print("[dry-run] No files written.")
        return

    # Single-day mode
    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: Invalid date format: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        target = date.today() - timedelta(days=1)

    fills = get_fills_for_date(wallet, target)
    balance = fetch_account_breakdown(wallet)
    metrics = compute_metrics(fills, target, balance)
    funding = fetch_funding_for_date(wallet, target)
    metrics.update(funding)

    print_metrics(metrics)

    if not args.dry_run:
        # Check if this date already exists in CSV
        existing = load_existing_csv()
        if target.isoformat() in existing:
            print(f"Date {target} already in CSV — updating JSON only.")
            update_json(metrics)
        else:
            append_single_csv(metrics)
            update_json(metrics)
        print(f"Logged to: {CSV_PATH}")
        print(f"Updated:   {JSON_PATH}")
    else:
        print("[dry-run] No files written.")


if __name__ == "__main__":
    main()
