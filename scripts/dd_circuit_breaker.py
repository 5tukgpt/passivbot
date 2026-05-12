#!/usr/bin/env python3
"""
Drawdown Circuit Breaker for live Passivbot (T3-001).

WHAT THIS DOES — and why it's a TIER-3 EXCEPTION:

This script is authorized to autonomously modify the LIVE Passivbot config
(configs/live/optimized.json) when an account-level drawdown threshold is
breached. That is normally a Tier-3 action requiring James's approval.
This script IS the approval: James approved T3-001 explicitly so the
breaker can self-fire without a human in the loop. See
orchestrator.db.tier3_items T3-001.

Fire condition:    account_value drops >= 20% below the rolling peak.
Fire actions:      1) bot.long.entry_initial_qty_pct = 0 (pause new entries)
                   2) bot.long.total_wallet_exposure_limit = current TWE
                                                   (freeze exposure ceiling)
                   3) Telegram alert (manual re-enable required)
                   4) Mark state.fired_at — script will NOT fire again
                      until dd_circuit_reset.py is run.

OPEN POSITIONS ARE NOT CLOSED. Unstuck logic still runs. The breaker is
account-level catastrophe protection, not micromanagement.

Data source: passivbot/logs/hl_daily_metrics.json (refreshed daily 00:05 UTC
by com.tradingbots.passivbot-metrics). The breaker does NOT poll HL on the
normal path — only ONE HL call is made, ONLY at fire-time, to fetch current
positions for TWE calculation.

Manual re-enable: python3 scripts/dd_circuit_reset.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
METRICS_JSON = REPO / "logs" / "hl_daily_metrics.json"
LIVE_CONFIG = REPO / "configs" / "live" / "optimized.json"
STATE_PATH = REPO / "data" / "dd_circuit_state.json"
API_KEYS_PATH = REPO / "api-keys.json"

DD_THRESHOLD = 0.20  # fire at >= 20% drawdown from peak
INFO_URL = "https://api.hyperliquid.xyz/info"


# --------------------------------------------------------------------------
# Pure functions — no side effects, easy to test.
# --------------------------------------------------------------------------
def decide(
    current_value: float,
    state: dict,
    dd_threshold: float = DD_THRESHOLD,
) -> dict:
    """Decide what should happen given current account value and prior state.

    Returns:
      {
        "new_peak": float,
        "dd_pct": float,           # negative for drawdowns
        "should_fire": bool,
        "reason": str,             # human-readable
      }
    """
    already_fired = bool(state.get("fired_at"))
    prior_peak = float(state.get("peak", 0.0))

    # Peak only advances while armed. Once fired we freeze the peak so a
    # post-fire recovery doesn't mask the original event.
    new_peak = prior_peak if already_fired else max(prior_peak, current_value)

    if already_fired:
        return {
            "new_peak": new_peak,
            "dd_pct": 0.0,
            "should_fire": False,
            "reason": "Already fired — manual reset required",
        }

    if new_peak <= 0:
        return {
            "new_peak": new_peak,
            "dd_pct": 0.0,
            "should_fire": False,
            "reason": "No peak yet (account_value=0)",
        }

    dd_pct = (current_value - new_peak) / new_peak
    should_fire = dd_pct <= -dd_threshold
    reason = (
        f"DD={dd_pct*100:.2f}% breaches threshold -{dd_threshold*100:.0f}%"
        if should_fire
        else f"Armed: DD={dd_pct*100:.2f}% (threshold -{dd_threshold*100:.0f}%)"
    )
    return {
        "new_peak": new_peak,
        "dd_pct": dd_pct,
        "should_fire": should_fire,
        "reason": reason,
    }


def latest_account_value(metrics: list[dict]) -> tuple[str, float]:
    """Return (date, account_value) of the most recent non-zero metric.

    Daily metrics occasionally records zeros (e.g., when the HL API call
    failed on that day). We skip those when picking 'current' value.
    """
    for entry in reversed(metrics):
        av = float(entry.get("account_value", 0) or 0)
        if av > 0:
            return entry.get("date", "?"), av
    return "?", 0.0


def historical_peak(metrics: list[dict]) -> float:
    """Compute peak across all recorded daily metrics (zeros ignored)."""
    return max(
        (float(e.get("account_value", 0) or 0) for e in metrics),
        default=0.0,
    )


# --------------------------------------------------------------------------
# I/O helpers — side-effectful, kept thin.
# --------------------------------------------------------------------------
def load_metrics(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"peak": 0.0, "current": 0.0, "fired_at": None, "fired_reason": None}
    with path.open() as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)


def resolve_wallet() -> str:
    env = os.environ.get("HL_WALLET_ADDR")
    if env:
        return env
    if API_KEYS_PATH.exists():
        try:
            keys = json.loads(API_KEYS_PATH.read_text())
            return keys.get("hyperliquid_live", {}).get("wallet_address", "")
        except Exception:
            pass
    return ""


def hl_info(body: dict[str, Any], timeout: int = 15) -> Any:
    data = json.dumps(body).encode()
    req = urllib.request.Request(INFO_URL, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_current_twe(wallet: str) -> tuple[float, list[dict]]:
    """ONE HL API call. Returns (twe, position_summary).

    TWE = sum(|positionValue|) / accountValue.

    position_summary is a list of {coin, side, size, notional, upnl} dicts
    used only for the Telegram alert.

    On any failure returns (-1.0, []) and the caller should treat TWE as
    unknown — we still fire (pause entries) but skip the TWEL freeze.
    """
    try:
        state = hl_info({"type": "clearinghouseState", "user": wallet})
        account_value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
        positions = state.get("assetPositions", []) or []
        total_notional = 0.0
        summary = []
        for ap in positions:
            p = ap.get("position", {}) or {}
            coin = p.get("coin", "?")
            try:
                size = float(p.get("szi", 0) or 0)
                notional = abs(float(p.get("positionValue", 0) or 0))
                upnl = float(p.get("unrealizedPnl", 0) or 0)
            except (TypeError, ValueError):
                continue
            total_notional += notional
            summary.append({
                "coin": coin,
                "side": "long" if size > 0 else "short" if size < 0 else "flat",
                "size": size,
                "notional": notional,
                "upnl": upnl,
            })
        if account_value <= 0:
            return -1.0, summary
        return total_notional / account_value, summary
    except Exception as e:
        print(f"WARN: Could not fetch positions for TWE calc: {e}", file=sys.stderr)
        return -1.0, []


def modify_live_config(
    config_path: Path,
    pause_entries: bool,
    freeze_twel: float | None,
) -> dict:
    """Apply circuit-breaker mutations to the live config.

    Returns the diff applied: {"before": {...}, "after": {...}}.
    """
    with config_path.open() as f:
        config = json.load(f)

    long_cfg = config.setdefault("bot", {}).setdefault("long", {})
    before = {
        "entry_initial_qty_pct": long_cfg.get("entry_initial_qty_pct"),
        "total_wallet_exposure_limit": long_cfg.get("total_wallet_exposure_limit"),
    }
    if pause_entries:
        long_cfg["entry_initial_qty_pct"] = 0.0
    if freeze_twel is not None and freeze_twel >= 0:
        # Never INCREASE TWEL. If current TWE somehow exceeds configured
        # TWEL, freeze at the configured value (no-op upward).
        current_twel = float(long_cfg.get("total_wallet_exposure_limit", 0))
        long_cfg["total_wallet_exposure_limit"] = min(freeze_twel, current_twel)
    after = {
        "entry_initial_qty_pct": long_cfg.get("entry_initial_qty_pct"),
        "total_wallet_exposure_limit": long_cfg.get("total_wallet_exposure_limit"),
    }

    tmp = config_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(config, f, indent=2)
    tmp.replace(config_path)
    return {"before": before, "after": after}


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("WARN: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping alert", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"WARN: Telegram send failed: {e}", file=sys.stderr)
        return False


def format_alert(
    current_value: float,
    peak: float,
    dd_pct: float,
    twe: float,
    positions: list[dict],
    diff: dict,
) -> str:
    pos_lines = []
    for p in sorted(positions, key=lambda x: -x["notional"])[:10]:
        pos_lines.append(
            f"  - {p['coin']} {p['side']} notional=${p['notional']:.2f} uPnL=${p['upnl']:+.2f}"
        )
    pos_block = "\n".join(pos_lines) if pos_lines else "  (none)"

    twe_line = (
        f"TWE: {twe:.4f} (frozen at this level)"
        if twe >= 0
        else "TWE: unknown (HL fetch failed) — TWEL NOT frozen"
    )

    return (
        "🚨 *PASSIVBOT DRAWDOWN CIRCUIT FIRED*\n"
        f"\n"
        f"Account value: ${current_value:,.2f}\n"
        f"Peak: ${peak:,.2f}\n"
        f"Drawdown: {dd_pct*100:.2f}% (threshold -{DD_THRESHOLD*100:.0f}%)\n"
        f"{twe_line}\n"
        f"\n"
        f"*Config mutations applied:*\n"
        f"  entry_initial_qty_pct: {diff['before']['entry_initial_qty_pct']} → "
        f"{diff['after']['entry_initial_qty_pct']}\n"
        f"  total_wallet_exposure_limit: {diff['before']['total_wallet_exposure_limit']} → "
        f"{diff['after']['total_wallet_exposure_limit']}\n"
        f"\n"
        f"*Open positions ({len(positions)}):*\n"
        f"{pos_block}\n"
        f"\n"
        f"⚠️  *Manual re-enable required.* Run on Tau:\n"
        f"`cd ~/Projects/trading-bots/passivbot && python3 scripts/dd_circuit_reset.py`\n"
        f"\n"
        f"Open positions are NOT closed. Unstuck logic still active."
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute decision, but do NOT modify config / state / send telegram.",
    )
    parser.add_argument(
        "--simulate-current",
        type=float,
        default=None,
        help="Override current account_value (testing only).",
    )
    parser.add_argument(
        "--simulate-peak",
        type=float,
        default=None,
        help="Override prior-state peak (testing only).",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=STATE_PATH,
        help=f"State file path (default: {STATE_PATH})",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=LIVE_CONFIG,
        help=f"Live config path (default: {LIVE_CONFIG})",
    )
    parser.add_argument(
        "--metrics-path",
        type=Path,
        default=METRICS_JSON,
        help=f"Daily metrics JSON (default: {METRICS_JSON})",
    )
    args = parser.parse_args()

    # ---- Load inputs ------------------------------------------------------
    state = load_state(args.state_path)
    metrics = load_metrics(args.metrics_path)

    if args.simulate_current is not None:
        current_date = "SIMULATED"
        current_value = float(args.simulate_current)
    else:
        current_date, current_value = latest_account_value(metrics)

    # Seed peak from historical metrics if state has none yet (first run).
    if not state.get("peak"):
        state["peak"] = max(state.get("peak", 0.0), historical_peak(metrics))

    if args.simulate_peak is not None:
        state["peak"] = float(args.simulate_peak)

    # ---- Decide -----------------------------------------------------------
    result = decide(current_value, state)
    new_peak = result["new_peak"]
    dd_pct = result["dd_pct"]
    should_fire = result["should_fire"]

    print(
        f"[dd_circuit_breaker] date={current_date} current=${current_value:.2f} "
        f"peak=${new_peak:.2f} dd={dd_pct*100:.2f}% fired={bool(state.get('fired_at'))}"
    )
    print(f"  → {result['reason']}")

    # ---- Update state (peak / current only, never fired) ------------------
    state["peak"] = new_peak
    state["current"] = current_value
    state["last_check_utc"] = datetime.now(timezone.utc).isoformat()

    # ---- Fire ------------------------------------------------------------
    if should_fire:
        if args.dry_run:
            print("DRY-RUN: would fire (skipping config write, telegram, state mark)")
            return 0

        wallet = resolve_wallet()
        twe, positions = (-1.0, [])
        if wallet:
            twe, positions = fetch_current_twe(wallet)
        else:
            print("WARN: no wallet resolved; skipping TWE calc", file=sys.stderr)

        diff = modify_live_config(
            args.config_path,
            pause_entries=True,
            freeze_twel=twe if twe >= 0 else None,
        )

        state["fired_at"] = datetime.now(timezone.utc).isoformat()
        state["fired_reason"] = result["reason"]
        state["fired_current"] = current_value
        state["fired_peak"] = new_peak
        state["fired_dd_pct"] = dd_pct
        state["fired_twe"] = twe if twe >= 0 else None
        state["fired_config_diff"] = diff
        save_state(args.state_path, state)

        alert = format_alert(current_value, new_peak, dd_pct, twe, positions, diff)
        send_telegram(alert)
        print(alert)
        return 0

    if not args.dry_run:
        save_state(args.state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
