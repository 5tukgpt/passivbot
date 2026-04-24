#!/usr/bin/env python3
"""
Hyperliquid HYPE fee-rebate audit.

Purpose
-------
HL shipped a gasless / HYPE fee-rebate program in March 2026. Passivbot's grid
strategy is maker-heavy (every entry is `post_only`), so any fraction of our
maker fees that is rebated in HYPE has been accumulating silently since the
Passivbot live-cutover on Mar 22, 2026.

This script is a read-only diagnostic — it does NOT submit orders, trades, or
staking transactions. Output is a console summary plus an optional markdown
audit report suitable for openclaw-orchestrator/reports/.

Data sources (all POST https://api.hyperliquid.xyz/info):
  - `clearinghouseState` — perp account value, positions (context)
  - `spotClearinghouseState` — spot balances including HYPE
  - `userFees` — builder/referral/maker/taker breakdown + cumulative fees
  - `userFillsByTime` — paginated fill history; `fee` < 0 == rebate

Usage
-----
    python scripts/audit_hype_rebates.py                   # full audit since Mar 22
    python scripts/audit_hype_rebates.py --lookback-days 7 # last 7 days only
    python scripts/audit_hype_rebates.py --wallet 0x...    # override wallet
    python scripts/audit_hype_rebates.py --report-to path/to/report.md
    python scripts/audit_hype_rebates.py --json-out path/to/audit.json
    python scripts/audit_hype_rebates.py --dry-run         # print what would be queried, no API calls

Research source: research-20260424-1825.md#1 (hl-hype-rebate-audit, P2).
Tracker id: hl-hype-rebate-audit.

Rate-limit budget
-----------------
`info` endpoint costs weight-1 (HL public info limit is typically 1200 weight / min).
This script's worst case (33-day lookback) is:
  3 point queries (state / spot / fees)  +  ~17 paginated userFillsByTime calls
  ≈ 20 requests spaced with a 0.25s sleep → ~5s wall time, ≤2% of 429 budget.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent.parent
API_KEYS_PATH = SCRIPT_DIR / "api-keys.json"

# Passivbot LIVE cutover date on Hyperliquid (see CLAUDE.md "Current State").
PASSIVBOT_LIVE_START_MS = int(datetime(2026, 3, 22, tzinfo=timezone.utc).timestamp() * 1000)

# Known wallet — also present in CLAUDE.md and plist. Used only as fallback
# after --wallet / env var / api-keys.json are checked.
DEFAULT_WALLET = "0x7336c944A995c6447bc87b856291FCE2c6205196"

INFO_URL = "https://api.hyperliquid.xyz/info"
REQUEST_SLEEP_S = 0.25  # gentle pacing to stay well under HL 429 budget
FILL_PAGE_LIMIT = 2000   # HL server-side cap
MATERIAL_REBATE_USD = 1.0  # threshold for "add a daily metric" decision


def resolve_wallet(cli_wallet: str | None) -> str:
    """Resolve wallet address with precedence: --wallet > env > api-keys.json > default."""
    if cli_wallet:
        return cli_wallet
    env_addr = os.environ.get("HL_WALLET_ADDR")
    if env_addr:
        return env_addr
    if API_KEYS_PATH.exists():
        try:
            keys = json.loads(API_KEYS_PATH.read_text())
            addr = keys.get("hyperliquid_live", {}).get("wallet_address", "")
            if addr:
                return addr
        except Exception:
            pass
    return DEFAULT_WALLET


def hl_info(body: dict[str, Any], timeout: int = 15) -> Any:
    """POST to HL info endpoint and return parsed JSON."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(INFO_URL, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_hype_balance(wallet: str) -> dict[str, float]:
    """Return {'hype_balance': float, 'usdc_spot': float, 'perp_account_value': float}."""
    out = {"hype_balance": 0.0, "usdc_spot": 0.0, "perp_account_value": 0.0}

    spot = hl_info({"type": "spotClearinghouseState", "user": wallet})
    for b in spot.get("balances", []):
        coin = b.get("coin", "")
        if coin == "HYPE":
            out["hype_balance"] = float(b.get("total", 0))
        elif coin == "USDC":
            out["usdc_spot"] = float(b.get("total", 0))
    time.sleep(REQUEST_SLEEP_S)

    perp = hl_info({"type": "clearinghouseState", "user": wallet})
    ms = perp.get("marginSummary", {})
    out["perp_account_value"] = float(ms.get("accountValue", 0))
    time.sleep(REQUEST_SLEEP_S)

    return out


def fetch_user_fees(wallet: str) -> dict[str, Any]:
    """`userFees` endpoint — may return cumulative maker/taker + rebate breakdown."""
    try:
        data = hl_info({"type": "userFees", "user": wallet})
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return {"_unsupported": True}
        raise
    finally:
        time.sleep(REQUEST_SLEEP_S)
    return data


def fetch_fills_paginated(
    wallet: str,
    start_ms: int,
    end_ms: int | None = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Paginate `userFillsByTime` until we exhaust the window.
    Returns fills in chronological order (oldest first).
    """
    fills: list[dict] = []
    cursor_ms = start_ms
    end_ms = end_ms or int(time.time() * 1000)
    page = 0

    while cursor_ms < end_ms:
        body = {
            "type": "userFillsByTime",
            "user": wallet,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "aggregateByTime": False,
        }
        try:
            batch = hl_info(body)
        except urllib.error.HTTPError as e:
            print(f"WARN: userFillsByTime page {page} HTTP {e.code}", file=sys.stderr)
            break
        if not batch:
            break

        fills.extend(batch)
        page += 1
        if verbose:
            print(f"  page {page}: {len(batch)} fills (cumulative {len(fills)})", file=sys.stderr)

        if len(batch) < FILL_PAGE_LIMIT:
            break  # last page (server returned fewer than the cap)
        # advance cursor past the newest fill's timestamp to avoid dup
        newest_ms = max(int(f.get("time", 0)) for f in batch)
        if newest_ms <= cursor_ms:
            break  # guard against stuck cursor
        cursor_ms = newest_ms + 1
        time.sleep(REQUEST_SLEEP_S)

    fills.sort(key=lambda f: int(f.get("time", 0)))
    return fills


def summarize_fills(fills: list[dict]) -> dict[str, Any]:
    """Aggregate fee spend by feeToken and by side. HL reports fees as positive
    strings when paid, negative when rebated."""
    totals = {
        "n_fills": len(fills),
        "gross_fee_paid_usdc": 0.0,
        "gross_rebate_usdc": 0.0,
        "net_fee_usdc": 0.0,
        "fee_by_token": {},   # token -> total (positive = paid, negative = rebated)
        "maker_volume_usd": 0.0,
        "taker_volume_usd": 0.0,
        "first_fill_ts": None,
        "last_fill_ts": None,
    }
    for f in fills:
        fee = float(f.get("fee", "0"))
        token = f.get("feeToken", "USDC") or "USDC"
        px = float(f.get("px", "0"))
        sz = float(f.get("sz", "0"))
        notional = abs(px * sz)

        if fee >= 0:
            totals["gross_fee_paid_usdc"] += fee  # assume USDC-denom if token=USDC
        else:
            totals["gross_rebate_usdc"] += (-fee)

        totals["net_fee_usdc"] += fee
        totals["fee_by_token"][token] = totals["fee_by_token"].get(token, 0.0) + fee

        # Maker fills on HL have the "crossed" flag false
        crossed = f.get("crossed", False)
        if crossed:
            totals["taker_volume_usd"] += notional
        else:
            totals["maker_volume_usd"] += notional

        ts = int(f.get("time", 0))
        if totals["first_fill_ts"] is None or ts < totals["first_fill_ts"]:
            totals["first_fill_ts"] = ts
        if totals["last_fill_ts"] is None or ts > totals["last_fill_ts"]:
            totals["last_fill_ts"] = ts

    return totals


def decide(totals: dict[str, Any], hype_bal: float, days_covered: float) -> dict[str, str]:
    """Return human-readable decisions."""
    dec = {}
    rebate = totals["gross_rebate_usdc"]
    per_day = rebate / max(days_covered, 1e-9)

    if rebate >= MATERIAL_REBATE_USD:
        dec["metric"] = (
            f"ADD `hype_rebates_24h` metric to passivbot_monitor.py "
            f"(pattern: rate_limit_429_24h). Observed rate: ${per_day:.4f}/day."
        )
    else:
        dec["metric"] = (
            f"SKIP metric addition — observed rebate ${rebate:.4f} over {days_covered:.1f} days "
            f"(~${per_day:.4f}/day) is below the ${MATERIAL_REBATE_USD:.2f} threshold. "
            "Document in _meta/lessons-learned.md so future runs don't re-audit."
        )

    if hype_bal * 1.0 >= 10:  # stake threshold from research spec
        dec["stake"] = (
            f"CANDIDATE for staking — HYPE balance {hype_bal:.4f} >=10. "
            "2.37% APR + 5–40% fee discount, 7-day unbond. Staking is a James-approval action (Tier 3)."
        )
    else:
        dec["stake"] = (
            f"DO NOT STAKE — HYPE balance {hype_bal:.4f} below $10 staking threshold."
        )

    return dec


def fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_markdown(
    wallet: str,
    window_start_ms: int,
    window_end_ms: int,
    balances: dict,
    fees_endpoint: dict,
    totals: dict,
    decisions: dict,
) -> str:
    days = (window_end_ms - window_start_ms) / 86_400_000
    per_day_rebate = totals["gross_rebate_usdc"] / max(days, 1e-9)
    per_day_net = totals["net_fee_usdc"] / max(days, 1e-9)

    lines = []
    lines.append(f"# HYPE Fee-Rebate Audit — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"Tracker: `hl-hype-rebate-audit` · Research source: `research-20260424-1825.md#1`")
    lines.append("")
    lines.append("## Wallet & Window")
    lines.append("")
    lines.append(f"- Wallet: `{wallet}`")
    lines.append(f"- Window start: {fmt_ms(window_start_ms)}")
    lines.append(f"- Window end:   {fmt_ms(window_end_ms)}")
    lines.append(f"- Days covered: **{days:.2f}**")
    lines.append("")
    lines.append("## Balances")
    lines.append("")
    lines.append(f"- HYPE spot balance:   **{balances['hype_balance']:.6f}**")
    lines.append(f"- USDC spot balance:   ${balances['usdc_spot']:.2f}")
    lines.append(f"- Perp account value:  ${balances['perp_account_value']:.2f}")
    lines.append("")
    lines.append("## Fee Activity")
    lines.append("")
    lines.append(f"- Fills observed:      **{totals['n_fills']}**")
    lines.append(f"- Maker volume:        ${totals['maker_volume_usd']:,.2f}")
    lines.append(f"- Taker volume:        ${totals['taker_volume_usd']:,.2f}")
    lines.append(f"- Gross fees paid:     ${totals['gross_fee_paid_usdc']:.4f}")
    lines.append(f"- Gross rebates:       ${totals['gross_rebate_usdc']:.4f}")
    lines.append(f"- **Net fee spend:**   **${totals['net_fee_usdc']:.4f}** ({per_day_net:+.4f}/day)")
    lines.append(f"- **Rebate rate:**     **${per_day_rebate:.4f}/day**")
    if totals["fee_by_token"]:
        lines.append("")
        lines.append("### Fee by token")
        lines.append("")
        for tok, amt in sorted(totals["fee_by_token"].items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"- `{tok}`: {amt:+.6f}")
    lines.append("")
    lines.append("## `userFees` endpoint snapshot")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(fees_endpoint, indent=2, default=str)[:2000])
    lines.append("```")
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    lines.append(f"- **Metric:** {decisions['metric']}")
    lines.append(f"- **Stake:** {decisions['stake']}")
    lines.append("")
    lines.append("## Risk / Safety")
    lines.append("")
    lines.append("- Read-only audit. No orders, no transfers, no staking txns. Rate-budget ≤2% of HL `info` 1200-weight/min cap.")
    lines.append("- Results should be spot-checked against HL app UI (spot balances / trade history) before acting.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="HL HYPE fee-rebate audit (read-only)")
    parser.add_argument("--wallet", help="Override wallet address (else: env HL_WALLET_ADDR, then api-keys.json, then default)")
    parser.add_argument("--lookback-days", type=float, default=None,
                        help="Audit window in days from now. Default = since Passivbot live (Mar 22 2026).")
    parser.add_argument("--report-to", type=Path,
                        help="Write markdown audit report to this path.")
    parser.add_argument("--json-out", type=Path,
                        help="Write machine-readable audit JSON to this path.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be queried; make no HTTP calls.")
    parser.add_argument("--verbose", action="store_true", help="Verbose pagination progress.")
    args = parser.parse_args()

    wallet = resolve_wallet(args.wallet)
    now_ms = int(time.time() * 1000)
    if args.lookback_days is not None:
        start_ms = now_ms - int(args.lookback_days * 86_400_000)
    else:
        start_ms = PASSIVBOT_LIVE_START_MS

    print(f"Wallet:       {wallet}")
    print(f"Window:       {fmt_ms(start_ms)}  ->  {fmt_ms(now_ms)}")
    print(f"Days:         {(now_ms - start_ms) / 86_400_000:.2f}")

    if args.dry_run:
        print("\nDRY RUN — no HTTP calls made.")
        print("Would POST to:", INFO_URL)
        print("  1. spotClearinghouseState (HYPE balance)")
        print("  2. clearinghouseState (perp account value)")
        print("  3. userFees (fee tier / rebate breakdown)")
        print("  4. userFillsByTime (paginated until now)")
        return 0

    print("\n[1/4] Fetching balances...")
    balances = fetch_hype_balance(wallet)
    print(f"  HYPE: {balances['hype_balance']:.6f}  USDC spot: ${balances['usdc_spot']:.2f}  Perp: ${balances['perp_account_value']:.2f}")

    print("\n[2/4] Fetching userFees...")
    fees_endpoint = fetch_user_fees(wallet)
    if fees_endpoint.get("_unsupported"):
        print("  endpoint not supported (422)")
    else:
        print(f"  keys: {list(fees_endpoint.keys())[:8]}")

    print(f"\n[3/4] Paginating userFillsByTime from {fmt_ms(start_ms)}...")
    fills = fetch_fills_paginated(wallet, start_ms, now_ms, verbose=args.verbose)
    print(f"  fetched {len(fills)} fills")

    print("\n[4/4] Summarising...")
    totals = summarize_fills(fills)
    days = (now_ms - start_ms) / 86_400_000
    decisions = decide(totals, balances["hype_balance"], days)

    print()
    print(f"  Maker vol:     ${totals['maker_volume_usd']:,.2f}")
    print(f"  Taker vol:     ${totals['taker_volume_usd']:,.2f}")
    print(f"  Fees paid:     ${totals['gross_fee_paid_usdc']:.4f}")
    print(f"  Rebates:       ${totals['gross_rebate_usdc']:.4f}")
    print(f"  Net fees:      ${totals['net_fee_usdc']:.4f}  ({totals['net_fee_usdc']/max(days,1e-9):+.4f}/day)")
    print(f"  Rebate rate:   ${totals['gross_rebate_usdc']/max(days,1e-9):.4f}/day")
    print()
    print(f"  Metric:  {decisions['metric']}")
    print(f"  Stake:   {decisions['stake']}")

    if args.report_to:
        report = render_markdown(wallet, start_ms, now_ms, balances, fees_endpoint, totals, decisions)
        args.report_to.parent.mkdir(parents=True, exist_ok=True)
        args.report_to.write_text(report)
        print(f"\nReport written: {args.report_to}")

    if args.json_out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "wallet": wallet,
            "window": {"start_ms": start_ms, "end_ms": now_ms, "days": days},
            "balances": balances,
            "fees_endpoint": fees_endpoint,
            "totals": totals,
            "decisions": decisions,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"JSON written:   {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
