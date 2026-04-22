#!/usr/bin/env python3
"""
Reference Vault Tracker + Daily HL Metrics Logger

Compares our Passivbot vault's performance against the HL Reference Vault
(0x490af7d4a048a81db0f677517ed6373565b42349 — runs default Passivbot template
config live on Hyperliquid). Appends daily rows to two CSV files:

  1. logs/ref_vault_comparison.csv  — our PnL vs reference PnL + gap
  2. logs/hl_daily_metrics.csv      — daily account snapshot

Usage:
    python scripts/ref_vault_tracker.py              # one-shot log + compare
    python scripts/ref_vault_tracker.py --dry-run     # print without writing
    python scripts/ref_vault_tracker.py --history 7   # show last 7 comparison rows

Designed for launchd daily cron (9am local). Read-only HL info API — no trading.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent.parent  # passivbot/
LOGS_DIR = SCRIPT_DIR / "logs"
API_KEYS_PATH = SCRIPT_DIR / "api-keys.json"

COMPARISON_CSV = LOGS_DIR / "ref_vault_comparison.csv"
DAILY_METRICS_CSV = LOGS_DIR / "hl_daily_metrics.csv"
ALERTS_LOG = LOGS_DIR / "ref_vault_alerts.log"

# ── Constants ────────────────────────────────────────────────────────────
REFERENCE_VAULT = "0x490af7d4a048a81db0f677517ed6373565b42349"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
ALERT_GAP_THRESHOLD = 5.0   # alert if |gap_pct| > 5% for 3 consecutive days
ALERT_STREAK_DAYS = 3

# CSV headers
COMPARISON_HEADER = ["ts", "our_balance", "ref_balance", "our_pnl_7d", "ref_pnl_7d", "gap_pct"]
DAILY_METRICS_HEADER = [
    "date", "balance", "unrealized_pnl", "position_count",
    "fill_count_24h", "margin_used", "errors_24h",
]


def _hl_post(payload: dict, timeout: int = 15) -> dict:
    """POST to HL info API. Returns parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_INFO_URL, data, {"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def get_our_wallet() -> str:
    """Read wallet address from api-keys.json."""
    keys = json.loads(API_KEYS_PATH.read_text())
    addr = keys.get("hyperliquid_live", {}).get("wallet_address", "")
    if not addr:
        raise ValueError("No wallet_address in api-keys.json -> hyperliquid_live")
    return addr


def fetch_account_state(address: str) -> dict:
    """Fetch clearinghouse + spot state for an address. Read-only."""
    perp = _hl_post({"type": "clearinghouseState", "user": address})
    spot = _hl_post({"type": "spotClearinghouseState", "user": address})

    # Extract key numbers
    margin = perp.get("marginSummary", {})
    perp_value = float(margin.get("accountValue", 0))
    margin_used = float(margin.get("totalMarginUsed", 0))

    spot_usdc = 0.0
    for b in spot.get("balances", []):
        if b.get("coin") == "USDC":
            spot_usdc = float(b.get("total", 0))

    # Count open positions
    positions = perp.get("assetPositions", [])
    open_count = sum(
        1 for p in positions
        if float(p.get("position", {}).get("szi", 0)) != 0
    )

    unrealized = sum(
        float(p.get("position", {}).get("unrealizedPnl", 0))
        for p in positions
        if float(p.get("position", {}).get("szi", 0)) != 0
    )

    # Unified balance = spot USDC + unrealized PnL (if spot available)
    balance = (spot_usdc + unrealized) if spot_usdc > 0 else perp_value

    return {
        "balance": balance,
        "perp_value": perp_value,
        "spot_usdc": spot_usdc,
        "margin_used": margin_used,
        "unrealized_pnl": unrealized,
        "position_count": open_count,
    }


def fetch_fills_24h(address: str) -> int:
    """Count fills in last 24h for an address."""
    from datetime import timedelta

    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
    )
    try:
        fills = _hl_post({"type": "userFills", "user": address})
        return sum(1 for f in fills if f.get("time", 0) >= cutoff_ms)
    except Exception:
        return 0


def count_errors_24h() -> int:
    """Count 429 rate-limit events in the last 24h from rate_limits.jsonl."""
    from datetime import timedelta

    log_path = LOGS_DIR / "rate_limits.jsonl"
    if not log_path.exists():
        return 0
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
    count = 0
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("ts_ms", 0) >= cutoff_ms:
                    count += 1
            except (json.JSONDecodeError, KeyError):
                continue
    return count


def compute_pnl_7d(csv_path: Path, balance_col: str = "our_balance") -> float | None:
    """Compute 7-day PnL % from the comparison CSV.

    Returns percentage change or None if < 7 days of data.
    """
    if not csv_path.exists():
        return None
    rows = _read_csv(csv_path)
    if len(rows) < 7:
        return None
    try:
        current = float(rows[-1][balance_col])
        past = float(rows[-7][balance_col])
        if past == 0:
            return None
        return ((current - past) / past) * 100
    except (KeyError, ValueError, IndexError):
        return None


def compute_gap_pct(our_pnl: float | None, ref_pnl: float | None) -> float:
    """Compute performance gap. Positive = we're ahead."""
    if our_pnl is None or ref_pnl is None:
        return 0.0
    return our_pnl - ref_pnl


def check_gap_streak(csv_path: Path, threshold: float = ALERT_GAP_THRESHOLD,
                     streak: int = ALERT_STREAK_DAYS) -> bool:
    """Check if |gap_pct| > threshold for N consecutive most-recent rows.

    Returns True if alert condition is met.
    """
    if not csv_path.exists():
        return False
    rows = _read_csv(csv_path)
    if len(rows) < streak:
        return False
    recent = rows[-streak:]
    try:
        return all(abs(float(r["gap_pct"])) > threshold for r in recent)
    except (KeyError, ValueError):
        return False


def _read_csv(path: Path) -> list[dict]:
    """Read CSV file into list of dicts."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _append_csv(path: Path, header: list[str], row: dict):
    """Append a row to a CSV file, creating with header if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def run(dry_run: bool = False) -> dict:
    """Main entry point. Fetches data, computes metrics, writes CSVs.

    Returns summary dict for testing/display.
    """
    now = datetime.now(timezone.utc)
    our_wallet = get_our_wallet()

    # Fetch account states (read-only API)
    our_state = fetch_account_state(our_wallet)
    ref_state = fetch_account_state(REFERENCE_VAULT)

    # Fetch fill counts
    our_fills = fetch_fills_24h(our_wallet)
    errors_24h = count_errors_24h()

    # Build daily metrics row
    daily_row = {
        "date": now.strftime("%Y-%m-%d"),
        "balance": f"{our_state['balance']:.2f}",
        "unrealized_pnl": f"{our_state['unrealized_pnl']:.4f}",
        "position_count": our_state["position_count"],
        "fill_count_24h": our_fills,
        "margin_used": f"{our_state['margin_used']:.2f}",
        "errors_24h": errors_24h,
    }

    # Compute 7-day PnL from existing comparison CSV history
    # For reference vault, we'd need its historical balances too.
    # On first runs, PnL will be None until 7 rows accumulate.
    our_pnl_7d = compute_pnl_7d(COMPARISON_CSV, "our_balance")
    ref_pnl_7d = compute_pnl_7d(COMPARISON_CSV, "ref_balance")
    gap_pct = compute_gap_pct(our_pnl_7d, ref_pnl_7d)

    comparison_row = {
        "ts": now.isoformat(),
        "our_balance": f"{our_state['balance']:.2f}",
        "ref_balance": f"{ref_state['balance']:.2f}",
        "our_pnl_7d": f"{our_pnl_7d:.2f}" if our_pnl_7d is not None else "",
        "ref_pnl_7d": f"{ref_pnl_7d:.2f}" if ref_pnl_7d is not None else "",
        "gap_pct": f"{gap_pct:.2f}",
    }

    summary = {
        "our_balance": our_state["balance"],
        "ref_balance": ref_state["balance"],
        "our_pnl_7d": our_pnl_7d,
        "ref_pnl_7d": ref_pnl_7d,
        "gap_pct": gap_pct,
        "fills_24h": our_fills,
        "errors_24h": errors_24h,
        "positions": our_state["position_count"],
    }

    if dry_run:
        print("=== DRY RUN — no files written ===")
        print(f"\nOur balance:     ${our_state['balance']:.2f}")
        print(f"Ref balance:     ${ref_state['balance']:.2f}")
        print(f"Our 7d PnL:      {our_pnl_7d:.2f}%" if our_pnl_7d is not None else "Our 7d PnL:      (need 7 days of data)")
        print(f"Ref 7d PnL:      {ref_pnl_7d:.2f}%" if ref_pnl_7d is not None else "Ref 7d PnL:      (need 7 days of data)")
        print(f"Gap:             {gap_pct:+.2f}%")
        print(f"Fills 24h:       {our_fills}")
        print(f"Errors 24h:      {errors_24h}")
        print(f"Positions:       {our_state['position_count']}")
        return summary

    # Write CSVs
    _append_csv(DAILY_METRICS_CSV, DAILY_METRICS_HEADER, daily_row)
    _append_csv(COMPARISON_CSV, COMPARISON_HEADER, comparison_row)

    print(f"Our balance:  ${our_state['balance']:.2f}  |  Ref balance: ${ref_state['balance']:.2f}")
    if our_pnl_7d is not None:
        print(f"7d PnL:  ours={our_pnl_7d:+.2f}%  ref={ref_pnl_7d:+.2f}%  gap={gap_pct:+.2f}%")
    else:
        print("7d PnL: accumulating data (need 7 daily rows)")
    print(f"Logged to: {DAILY_METRICS_CSV}")
    print(f"Comparison: {COMPARISON_CSV}")

    # Check alert condition
    if check_gap_streak(COMPARISON_CSV):
        alert_msg = (
            f"[{now.isoformat()}] ALERT: |gap_pct| > {ALERT_GAP_THRESHOLD}% "
            f"for {ALERT_STREAK_DAYS} consecutive days. "
            f"Current gap: {gap_pct:+.2f}%\n"
        )
        with open(ALERTS_LOG, "a") as f:
            f.write(alert_msg)
        print(f"⚠ Alert written to {ALERTS_LOG}")

    return summary


def show_history(n: int):
    """Show last N rows from comparison CSV."""
    if not COMPARISON_CSV.exists():
        print("No comparison history yet.")
        return
    rows = _read_csv(COMPARISON_CSV)
    if not rows:
        print("Comparison CSV is empty.")
        return
    # Print header
    print(f"{'Date':>25}  {'Ours':>10}  {'Ref':>10}  {'Our7d%':>8}  {'Ref7d%':>8}  {'Gap%':>8}")
    print("-" * 75)
    for row in rows[-n:]:
        ts = row.get("ts", "")[:10]
        print(
            f"{ts:>25}  "
            f"${float(row.get('our_balance', 0)):>9.2f}  "
            f"${float(row.get('ref_balance', 0)):>9.2f}  "
            f"{row.get('our_pnl_7d', 'n/a'):>8}  "
            f"{row.get('ref_pnl_7d', 'n/a'):>8}  "
            f"{row.get('gap_pct', 'n/a'):>8}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Reference Vault Tracker + Daily HL Metrics Logger"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print metrics without writing files")
    parser.add_argument("--history", type=int, metavar="N",
                        help="Show last N comparison rows")
    args = parser.parse_args()

    if args.history:
        show_history(args.history)
        return

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
