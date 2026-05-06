#!/usr/bin/env python3
"""
Hyperliquid ccxt-builder-fee audit (read-only).

Purpose
-------
ccxt's HL adapter ships with `builderFee=True` enabled by default — every order
placed via `passivbot/src/exchanges/hyperliquid.py` (which constructs the adapter
via ccxt) silently carries a 1bp builder fee routed to a ccxt-controlled
builder address (`0x6530512A6c89C7cfCEbC3BA7fcD9aDa5f30827a6`). On a maker-heavy
grid bot like Passivbot this fee accumulates per fill — research-20260503-1639 §1
sampled 35 days of fills and observed $5.74 in builder fees over 3239 fills,
exact 1.000bp/fill, ~$60/yr drag at current account size.

This script is the **Tier-1 audit half** of `ccxt-builderfee-default-disable`
(tracker id, P3, opened 2026-05-03). The Tier-3 disable step
(`passivbot/src/exchanges/hyperliquid.py` config edit) is JAMES ONLY and is NOT
performed here.

This script is read-only — it does NOT submit orders or mutate any state.
Output is a console summary plus an optional markdown audit report suitable
for openclaw-orchestrator/reports/.

Data sources (all POST https://api.hyperliquid.xyz/info):
  - `clearinghouseState` — perp account value (context)
  - `spotClearinghouseState` — USDC spot balance (context)
  - `userFees` — fee-tier snapshot (context, may 422 if unsupported)
  - `userFillsByTime` — paginated fill history; `builderFee` field on each fill
    is the bps-denominated routed-to-builder amount

Usage
-----
    python scripts/audit_builder_fee.py                    # full audit since Mar 22
    python scripts/audit_builder_fee.py --lookback-days 35 # last 35 days only (matches research §1 sample)
    python scripts/audit_builder_fee.py --wallet 0x...     # override wallet
    python scripts/audit_builder_fee.py --report-to path/to/report.md
    python scripts/audit_builder_fee.py --json-out path/to/audit.json
    python scripts/audit_builder_fee.py --dry-run          # print what would be queried, no API calls

Research source: research-20260503-1639.md#1 (ccxt-builderfee-default-disable, P3).
Tracker id: ccxt-builderfee-default-disable.

Rate-limit budget
-----------------
`info` endpoint costs weight-1 (HL public info limit is typically 1200 weight / min).
35-day lookback worst case:
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

# ccxt-controlled builder address per research-20260503-1639 §1.
# Surfaced here for cross-check against per-fill `builderAddr` if that field
# is present (HL has been adding fields to fills over time; absent fields are
# tolerated).
CCXT_BUILDER_ADDR = "0x6530512A6c89C7cfCEbC3BA7fcD9aDa5f30827a6"

INFO_URL = "https://api.hyperliquid.xyz/info"
REQUEST_SLEEP_S = 0.25  # gentle pacing to stay well under HL 429 budget
FILL_PAGE_LIMIT = 2000   # HL server-side cap

# Empirical reference from research-20260503-1639 §1.
# 35-day window, 3239 fills, $5.74 total builder fees, exact 1.000bp/fill.
# The verify step in the dev-loop spec checks the 35-day sample against this.
RESEARCH_SAMPLE_TOTAL_USD = 5.74
RESEARCH_SAMPLE_TOLERANCE_USD = 0.10
RESEARCH_SAMPLE_BP_PER_FILL = 1.000


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


def fetch_balances(wallet: str) -> dict[str, float]:
    """Return {'usdc_spot': float, 'perp_account_value': float}."""
    out = {"usdc_spot": 0.0, "perp_account_value": 0.0}

    spot = hl_info({"type": "spotClearinghouseState", "user": wallet})
    for b in spot.get("balances", []):
        if b.get("coin", "") == "USDC":
            out["usdc_spot"] = float(b.get("total", 0))
    time.sleep(REQUEST_SLEEP_S)

    perp = hl_info({"type": "clearinghouseState", "user": wallet})
    ms = perp.get("marginSummary", {})
    out["perp_account_value"] = float(ms.get("accountValue", 0))
    time.sleep(REQUEST_SLEEP_S)

    return out


def fetch_user_fees(wallet: str) -> dict[str, Any]:
    """`userFees` endpoint — may return cumulative maker/taker + builder breakdown."""
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


def _parse_builder_fee(fill: dict) -> float:
    """Extract builderFee from a fill record.

    HL surfaces `builderFee` as a string-typed positive USDC amount per fill.
    Older fills (pre-builder-fee infra) may omit the field — treat absence as 0.
    Some adapter versions nest the field under `info` rather than at the top
    level; check both, prefer top-level.
    """
    raw = fill.get("builderFee")
    if raw is None:
        info = fill.get("info") or {}
        raw = info.get("builderFee")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def summarize_fills(fills: list[dict]) -> dict[str, Any]:
    """Aggregate base fee + builder fee. Twin accumulators per dev-loop spec."""
    totals = {
        "n_fills": len(fills),
        "n_fills_with_builder_fee": 0,
        "gross_fee_paid_usdc": 0.0,       # excludes builder fee — base maker/taker fee net of rebate
        "gross_rebate_usdc": 0.0,
        "total_builder_fee_usdc": 0.0,    # sum of per-fill builderFee
        "fee_by_token": {},                # base fee by feeToken
        "maker_volume_usd": 0.0,
        "taker_volume_usd": 0.0,
        "builder_fee_bps_samples": [],     # bps = 10000 * builderFee / notional
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
            totals["gross_fee_paid_usdc"] += fee
        else:
            totals["gross_rebate_usdc"] += (-fee)

        totals["fee_by_token"][token] = totals["fee_by_token"].get(token, 0.0) + fee

        builder_fee = _parse_builder_fee(f)
        if builder_fee > 0:
            totals["n_fills_with_builder_fee"] += 1
            totals["total_builder_fee_usdc"] += builder_fee
            if notional > 0:
                totals["builder_fee_bps_samples"].append(10000.0 * builder_fee / notional)

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

    samples = totals["builder_fee_bps_samples"]
    if samples:
        totals["builder_fee_bps_mean"] = sum(samples) / len(samples)
        totals["builder_fee_bps_min"] = min(samples)
        totals["builder_fee_bps_max"] = max(samples)
    else:
        totals["builder_fee_bps_mean"] = 0.0
        totals["builder_fee_bps_min"] = 0.0
        totals["builder_fee_bps_max"] = 0.0

    return totals


def decide(totals: dict[str, Any], days_covered: float) -> dict[str, str]:
    """Return human-readable decisions."""
    dec = {}
    builder_fee = totals["total_builder_fee_usdc"]
    per_day = builder_fee / max(days_covered, 1e-9)
    annualized = per_day * 365.0

    bp_mean = totals["builder_fee_bps_mean"]
    bp_match = abs(bp_mean - RESEARCH_SAMPLE_BP_PER_FILL) < 0.05  # within 0.05bp

    if bp_match and totals["n_fills_with_builder_fee"] > 0:
        dec["bp_rate"] = (
            f"CONFIRMED 1.000bp/fill — observed {bp_mean:.4f}bp mean across "
            f"{totals['n_fills_with_builder_fee']} fills with builderFee>0. "
            "Matches research-20260503-1639 §1."
        )
    elif totals["n_fills_with_builder_fee"] == 0:
        dec["bp_rate"] = (
            "NO builderFee>0 fills observed in window. Either (a) builder-fee "
            "default already disabled, (b) HL stopped surfacing the field, "
            "or (c) wallet routes through an adapter that overrode the default."
        )
    else:
        dec["bp_rate"] = (
            f"DRIFT FROM RESEARCH — observed {bp_mean:.4f}bp/fill across "
            f"{totals['n_fills_with_builder_fee']} fills, expected ~"
            f"{RESEARCH_SAMPLE_BP_PER_FILL:.3f}bp. Investigate."
        )

    dec["disable_recommendation"] = (
        f"Tier-3 disable step pending James session — observed ${builder_fee:.4f} drained "
        f"over {days_covered:.1f} days (~${per_day:.4f}/day, ~${annualized:.2f}/yr). "
        "Edit `passivbot/src/exchanges/hyperliquid.py` to construct the ccxt adapter "
        "with `builderFee=False` (or omit the field entirely if a future ccxt release "
        "flips the default). NOT performed by this script — it's read-only."
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
    per_day_builder = totals["total_builder_fee_usdc"] / max(days, 1e-9)
    annualized = per_day_builder * 365.0

    lines = []
    lines.append(f"# ccxt Builder-Fee Audit — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"Tracker: `ccxt-builderfee-default-disable` · Research source: `research-20260503-1639.md#1`")
    lines.append("")
    lines.append("## Wallet & Window")
    lines.append("")
    lines.append(f"- Wallet: `{wallet}`")
    lines.append(f"- Window start: {fmt_ms(window_start_ms)}")
    lines.append(f"- Window end:   {fmt_ms(window_end_ms)}")
    lines.append(f"- Days covered: **{days:.2f}**")
    lines.append("")
    lines.append("## Balances (context)")
    lines.append("")
    lines.append(f"- USDC spot balance:   ${balances['usdc_spot']:.2f}")
    lines.append(f"- Perp account value:  ${balances['perp_account_value']:.2f}")
    lines.append("")
    lines.append("## Builder-Fee Activity")
    lines.append("")
    lines.append(f"- Fills observed:                 **{totals['n_fills']}**")
    lines.append(f"- Fills with builderFee > 0:      **{totals['n_fills_with_builder_fee']}**")
    lines.append(f"- Maker volume:                   ${totals['maker_volume_usd']:,.2f}")
    lines.append(f"- Taker volume:                   ${totals['taker_volume_usd']:,.2f}")
    lines.append(f"- **Total builder fees:**         **${totals['total_builder_fee_usdc']:.4f}**")
    lines.append(f"- Per-day builder fee rate:       ${per_day_builder:.4f}/day")
    lines.append(f"- Annualized (extrapolated):      ${annualized:.2f}/yr")
    lines.append(f"- Mean bp rate:                   {totals['builder_fee_bps_mean']:.4f}bp/fill")
    lines.append(f"- bp rate range:                  {totals['builder_fee_bps_min']:.4f}–{totals['builder_fee_bps_max']:.4f}bp")
    lines.append("")
    lines.append("## Base Fee Activity (context)")
    lines.append("")
    lines.append(f"- Gross fees paid:     ${totals['gross_fee_paid_usdc']:.4f}")
    lines.append(f"- Gross rebates:       ${totals['gross_rebate_usdc']:.4f}")
    if totals["fee_by_token"]:
        lines.append("")
        lines.append("### Base fee by token")
        lines.append("")
        for tok, amt in sorted(totals["fee_by_token"].items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"- `{tok}`: {amt:+.6f}")
    lines.append("")
    lines.append("## Research Cross-Check")
    lines.append("")
    lines.append(
        f"- Expected (research-20260503-1639 §1, 35d sample): "
        f"${RESEARCH_SAMPLE_TOTAL_USD:.2f} ± ${RESEARCH_SAMPLE_TOLERANCE_USD:.2f}, "
        f"{RESEARCH_SAMPLE_BP_PER_FILL:.3f}bp/fill"
    )
    delta = totals["total_builder_fee_usdc"] - RESEARCH_SAMPLE_TOTAL_USD
    within = abs(delta) <= RESEARCH_SAMPLE_TOLERANCE_USD
    lines.append(
        f"- Observed (this run, {days:.1f}d window): "
        f"${totals['total_builder_fee_usdc']:.4f}, {totals['builder_fee_bps_mean']:.4f}bp/fill"
    )
    lines.append(f"- Delta vs research: {'✅' if within else '⚠'} ${delta:+.4f}")
    lines.append("")
    lines.append("## ccxt Builder Address")
    lines.append("")
    lines.append(f"- Documented: `{CCXT_BUILDER_ADDR}` (research-20260503-1639 §1)")
    lines.append("- Per-fill `builderAddr` cross-check: not verified by this script (HL field availability varies).")
    lines.append("")
    lines.append("## `userFees` endpoint snapshot")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(fees_endpoint, indent=2, default=str)[:2000])
    lines.append("```")
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    lines.append(f"- **bp rate:**          {decisions['bp_rate']}")
    lines.append(f"- **Disable step:**     {decisions['disable_recommendation']}")
    lines.append("")
    lines.append("## Risk / Safety")
    lines.append("")
    lines.append("- Read-only audit. No orders, no transfers, no config edits. Rate-budget ≤2% of HL `info` 1200-weight/min cap.")
    lines.append("- Tier-3 disable step (passivbot/src/exchanges/hyperliquid.py) is JAMES ONLY.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="HL ccxt-builder-fee audit (read-only)")
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
        print("  1. spotClearinghouseState (USDC spot balance)")
        print("  2. clearinghouseState (perp account value)")
        print("  3. userFees (fee tier snapshot)")
        print("  4. userFillsByTime (paginated until now; reads `builderFee` per fill)")
        print(f"\nccxt builder address (documented): {CCXT_BUILDER_ADDR}")
        print(f"Research reference: ${RESEARCH_SAMPLE_TOTAL_USD:.2f} ± ${RESEARCH_SAMPLE_TOLERANCE_USD:.2f} over 35d, {RESEARCH_SAMPLE_BP_PER_FILL:.3f}bp/fill")
        return 0

    print("\n[1/4] Fetching balances...")
    balances = fetch_balances(wallet)
    print(f"  USDC spot: ${balances['usdc_spot']:.2f}  Perp: ${balances['perp_account_value']:.2f}")

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
    decisions = decide(totals, days)

    print()
    print(f"  Maker vol:        ${totals['maker_volume_usd']:,.2f}")
    print(f"  Taker vol:        ${totals['taker_volume_usd']:,.2f}")
    print(f"  Base fees paid:   ${totals['gross_fee_paid_usdc']:.4f}")
    print(f"  Base rebates:     ${totals['gross_rebate_usdc']:.4f}")
    print(f"  Builder fees:     ${totals['total_builder_fee_usdc']:.4f}  "
          f"({totals['n_fills_with_builder_fee']}/{totals['n_fills']} fills)")
    print(f"  Mean bp/fill:     {totals['builder_fee_bps_mean']:.4f}bp")
    print(f"  Per-day rate:     ${totals['total_builder_fee_usdc']/max(days,1e-9):.4f}/day")
    print(f"  Annualized:       ${(totals['total_builder_fee_usdc']/max(days,1e-9))*365:.2f}/yr")
    print()
    print(f"  bp rate:    {decisions['bp_rate']}")
    print(f"  disable:    {decisions['disable_recommendation']}")

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
            "research_reference": {
                "source": "research-20260503-1639.md#1",
                "total_usd": RESEARCH_SAMPLE_TOTAL_USD,
                "tolerance_usd": RESEARCH_SAMPLE_TOLERANCE_USD,
                "bp_per_fill": RESEARCH_SAMPLE_BP_PER_FILL,
                "sample_window_days": 35,
                "sample_fills": 3239,
            },
            "ccxt_builder_addr": CCXT_BUILDER_ADDR,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"JSON written:   {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
