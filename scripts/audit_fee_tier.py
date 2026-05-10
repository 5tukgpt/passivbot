#!/usr/bin/env python3
"""
Hyperliquid post-May-5 fee-tier audit + HYPE-staking breakeven (read-only).

Purpose
-------
HL switched to a tiered fee system on 2026-05-05 (~03:00 UTC) with three
orthogonal changes: (a) separate perp / spot fee schedules; (b) HYPE-staking
discount tiers Wood→Diamond (5%–40% off); (c) spot volume counts double
toward the unified fee tier. None of this was codified in the orchestrator
when this script was written — the audit gap was 5+ days at filing.

This script is the Tier-1 (read-only) audit half of tracker
`hl-fee-schedule-may5-tier-audit` (P3, comp `passivbot_health`).
Source: `openclaw-orchestrator/reports/research-20260510-1154.md` §1.

Outputs
-------
- Current fee-tier band per `userFees` (live, authoritative).
- Wood/Bronze/Silver/Gold/Platinum/Diamond classification given staked HYPE.
- Effective maker/taker rate including any active staking discount.
- Breakeven analysis: minimum 14d perp volume at which the next tier's
  discount * fees > stake-opportunity-cost (assume HL native staking APR).
- Static-vs-live drift check on the base maker/taker rates.

Mirrors `audit_builder_fee.py` shape: read-only `userFills*` + `userFees`
queries; no orders, no transfers, no config edits.

Usage
-----
    python scripts/audit_fee_tier.py                       # default: 14d window
    python scripts/audit_fee_tier.py --lookback-days 14    # explicit
    python scripts/audit_fee_tier.py --wallet 0x...        # override wallet
    python scripts/audit_fee_tier.py --staking-apr 0.04    # override 3% default
    python scripts/audit_fee_tier.py --json-out path.json
    python scripts/audit_fee_tier.py --dry-run             # no HTTP calls
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fee_schedule_constants import (
    PERP_BASE_MAKER_BP,
    PERP_BASE_TAKER_BP,
    HYPE_STAKING_TIERS,
    HL_NATIVE_STAKING_APR_DEFAULT,
    DRIFT_TOLERANCE_BP,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
API_KEYS_PATH = SCRIPT_DIR / "api-keys.json"

DEFAULT_WALLET = "0x7336c944A995c6447bc87b856291FCE2c6205196"
INFO_URL = "https://api.hyperliquid.xyz/info"
REQUEST_SLEEP_S = 0.25
FILL_PAGE_LIMIT = 2000


def resolve_wallet(cli_wallet: str | None) -> str:
    """Precedence: --wallet > env HL_WALLET_ADDR > api-keys.json > default."""
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
    data = json.dumps(body).encode()
    req = urllib.request.Request(INFO_URL, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_user_fees(wallet: str) -> dict[str, Any]:
    try:
        data = hl_info({"type": "userFees", "user": wallet})
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return {"_unsupported": True}
        raise
    finally:
        time.sleep(REQUEST_SLEEP_S)
    return data


def fetch_staked_hype(wallet: str) -> float:
    """
    Best-effort: HL surfaces staking under `delegatorSummary` (delegations
    to validators). Returns total staked HYPE (float), 0 on absence/error.
    """
    try:
        data = hl_info({"type": "delegatorSummary", "user": wallet})
    except urllib.error.HTTPError:
        return 0.0
    except Exception:
        return 0.0
    finally:
        time.sleep(REQUEST_SLEEP_S)
    total = 0.0
    for key in ("totalDelegated", "delegated", "stakedHype"):
        v = data.get(key) if isinstance(data, dict) else None
        if v is not None:
            try:
                total = float(v)
                break
            except (TypeError, ValueError):
                continue
    return total


def fetch_14d_perp_volume(wallet: str) -> float:
    """
    Pull paginated `userFillsByTime` over the last 14 days, sum |px * sz|
    for non-spot fills. (HL's `userFees.tier` cadence is rolling-14d.)
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 14 * 86_400_000
    cursor = start_ms
    volume_usd = 0.0
    pages = 0
    while cursor < now_ms:
        try:
            batch = hl_info({
                "type": "userFillsByTime",
                "user": wallet,
                "startTime": cursor,
                "endTime": now_ms,
                "aggregateByTime": False,
            })
        except urllib.error.HTTPError as e:
            print(f"WARN: userFillsByTime page {pages} HTTP {e.code}", file=sys.stderr)
            break
        if not batch:
            break
        for f in batch:
            # spot fills carry a "/" in coin like "@107/USDC"; perps are bare.
            coin = str(f.get("coin", ""))
            if "/" in coin or coin.startswith("@"):
                continue
            try:
                px = float(f.get("px", "0"))
                sz = float(f.get("sz", "0"))
                volume_usd += abs(px * sz)
            except (TypeError, ValueError):
                continue
        pages += 1
        if len(batch) < FILL_PAGE_LIMIT:
            break
        newest_ms = max(int(f.get("time", 0)) for f in batch)
        if newest_ms <= cursor:
            break
        cursor = newest_ms + 1
        time.sleep(REQUEST_SLEEP_S)
    return volume_usd


def classify_tier(staked_hype: float) -> dict[str, Any]:
    """Return the highest tier the staked amount qualifies for."""
    best = HYPE_STAKING_TIERS[0]
    for tier in HYPE_STAKING_TIERS:
        if staked_hype >= tier["min_hype"]:
            best = tier
    return best


def effective_rates_bp(discount_pct: float) -> tuple[float, float]:
    """Apply staking discount to base perp maker/taker."""
    maker = PERP_BASE_MAKER_BP * (1.0 - discount_pct)
    taker = PERP_BASE_TAKER_BP * (1.0 - discount_pct)
    return maker, taker


def compute_breakeven(
    current_tier: dict[str, Any],
    volume_14d_usd: float,
    staking_apr: float,
    hype_price_usd: float,
) -> list[dict[str, Any]]:
    """
    For each tier ABOVE the current one, compute the annualised perp volume
    at which the marginal discount * (base fee bp) > stake-opportunity-cost.
    Returns list ordered by min_hype ascending.

    Approximation: fee burn ~= volume * mean_bp where mean_bp blends maker/
    taker. Without a per-fill breakdown we assume 70% maker / 30% taker
    (passivbot grid is maker-heavy by design).
    """
    out = []
    blend_bp = 0.7 * PERP_BASE_MAKER_BP + 0.3 * PERP_BASE_TAKER_BP
    annualised_14d_to_yr = 365.0 / 14.0
    current_min = current_tier["min_hype"]
    current_disc = current_tier["discount_pct"]
    for tier in HYPE_STAKING_TIERS:
        if tier["min_hype"] <= current_min:
            continue
        marginal_disc = tier["discount_pct"] - current_disc
        marginal_hype = tier["min_hype"] - current_min
        # opportunity cost of locking the marginal HYPE for 1 year
        opportunity_cost_usd = marginal_hype * hype_price_usd * staking_apr
        # save_per_dollar_volume = marginal_disc * blend_bp / 10000
        # breakeven_14d_volume * 365/14 * save_per_dollar = opportunity_cost
        save_per_usd = marginal_disc * (blend_bp / 10000.0)
        if save_per_usd <= 0:
            be_14d = float("inf")
        else:
            be_14d = opportunity_cost_usd / (annualised_14d_to_yr * save_per_usd)
        out.append({
            "name": tier["name"],
            "min_hype": tier["min_hype"],
            "discount_pct": tier["discount_pct"],
            "marginal_discount_pct": marginal_disc,
            "marginal_hype": marginal_hype,
            "opportunity_cost_usd_per_yr": opportunity_cost_usd,
            "breakeven_14d_volume_usd": be_14d,
            "current_14d_volume_usd": volume_14d_usd,
            "qualifies_at_current_volume": volume_14d_usd >= be_14d,
        })
    return out


def detect_drift(fees_endpoint: dict[str, Any]) -> list[str]:
    """Compare `userFees` reported maker/taker against static constants."""
    warns: list[str] = []
    if not isinstance(fees_endpoint, dict) or fees_endpoint.get("_unsupported"):
        return warns
    # HL surfaces fee bands in different shapes across releases; try a few.
    candidates = []
    for key in ("userAddRate", "addRate", "makerRate"):
        v = fees_endpoint.get(key)
        if v is not None:
            candidates.append(("maker", key, v))
            break
    for key in ("userCrossRate", "crossRate", "takerRate"):
        v = fees_endpoint.get(key)
        if v is not None:
            candidates.append(("taker", key, v))
            break
    for side, key, raw in candidates:
        try:
            # HL reports these as a fraction (e.g. "0.00015"); convert to bp.
            bp = float(raw) * 10000.0
        except (TypeError, ValueError):
            continue
        base = PERP_BASE_MAKER_BP if side == "maker" else PERP_BASE_TAKER_BP
        if abs(bp - base) > DRIFT_TOLERANCE_BP:
            warns.append(
                f"fee_schedule_drift_warning: live {side}={bp:.4f}bp via "
                f"`{key}`, constants {base:.4f}bp (Δ={bp - base:+.4f}bp)"
            )
    return warns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--wallet")
    parser.add_argument("--lookback-days", type=float, default=14)
    parser.add_argument("--staking-apr", type=float, default=HL_NATIVE_STAKING_APR_DEFAULT,
                        help="Assumed HL native staking APR (default 0.03 = 3%%)")
    parser.add_argument("--hype-price-usd", type=float, default=40.0,
                        help="HYPE/USD reference price for breakeven math (rough)")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    wallet = resolve_wallet(args.wallet)
    print(f"Wallet:          {wallet}")
    print(f"Lookback:        {args.lookback_days:.1f}d")
    print(f"Staking APR:     {args.staking_apr:.2%}")
    print(f"HYPE/USD ref:    ${args.hype_price_usd:.2f}")

    if args.dry_run:
        print("\nDRY RUN — no HTTP calls made.")
        print(f"Would POST to: {INFO_URL}")
        print("  1. userFees")
        print("  2. delegatorSummary")
        print("  3. userFillsByTime (paginated, 14d window)")
        return 0

    print("\n[1/3] userFees endpoint...")
    fees_endpoint = fetch_user_fees(wallet)
    if fees_endpoint.get("_unsupported"):
        print("  endpoint not supported (422)")
    else:
        print(f"  keys: {list(fees_endpoint.keys())[:8]}")

    print("\n[2/3] delegatorSummary (staked HYPE)...")
    staked_hype = fetch_staked_hype(wallet)
    print(f"  staked HYPE: {staked_hype:.4f}")

    print("\n[3/3] userFillsByTime 14d perp volume...")
    volume_14d = fetch_14d_perp_volume(wallet)
    print(f"  14d perp volume: ${volume_14d:,.2f}")

    tier = classify_tier(staked_hype)
    eff_maker, eff_taker = effective_rates_bp(tier["discount_pct"])
    print()
    print(f"  Current tier:       {tier['name']} (min {tier['min_hype']} HYPE, "
          f"{tier['discount_pct'] * 100:.0f}% off)")
    print(f"  Effective maker:    {eff_maker:.4f}bp")
    print(f"  Effective taker:    {eff_taker:.4f}bp")

    breakeven = compute_breakeven(tier, volume_14d, args.staking_apr, args.hype_price_usd)
    print("\n  Breakeven for upgrading to each higher tier:")
    print("  " + "-" * 78)
    print(f"  {'Tier':<10}{'+HYPE':>10}{'+disc%':>10}{'opp$/yr':>12}{'BE 14d-vol$':>18}{'qualified':>12}")
    print("  " + "-" * 78)
    for be in breakeven:
        be_str = f"{be['breakeven_14d_volume_usd']:,.0f}" if be['breakeven_14d_volume_usd'] != float('inf') else "∞"
        q = "yes" if be["qualifies_at_current_volume"] else "no"
        print(f"  {be['name']:<10}{be['marginal_hype']:>10,}{be['marginal_discount_pct'] * 100:>9.0f}%"
              f"{be['opportunity_cost_usd_per_yr']:>12,.2f}{be_str:>18}{q:>12}")

    drift_warnings = detect_drift(fees_endpoint)
    if drift_warnings:
        print("\n  DRIFT WARNINGS (live `userFees` disagrees with constants):")
        for w in drift_warnings:
            print(f"    - {w}")
    else:
        print("\n  No drift detected (live `userFees` matches static constants within tolerance).")

    if args.json_out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "wallet": wallet,
            "staked_hype": staked_hype,
            "volume_14d_usd": volume_14d,
            "current_tier": tier,
            "effective_rates_bp": {"maker": eff_maker, "taker": eff_taker},
            "breakeven": breakeven,
            "drift_warnings": drift_warnings,
            "fees_endpoint": fees_endpoint,
            "params": {
                "staking_apr": args.staking_apr,
                "hype_price_usd": args.hype_price_usd,
                "lookback_days": args.lookback_days,
            },
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nJSON written: {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
