#!/usr/bin/env python3
"""
Drawdown Circuit Breaker for live Passivbot (T3-001, revised 2026-05-14).

WHAT THIS DOES — and why it's a TIER-3 EXCEPTION:

This script is authorized to autonomously stop the LIVE Passivbot when an
account-level drawdown threshold is breached. That is normally a Tier-3
action requiring James's approval. This script IS the approval: James
approved T3-001 (and the 2026-05-14 kill_switch revision) explicitly so the
breaker can self-fire without a human in the loop. See
orchestrator.db.tier3_items T3-001.

Fire condition: account_value drops >= 20% below the rolling peak.

Fire-action MODES (configurable via DD_FIRE_MODE env var or --fire-mode):

  kill_switch (default, 2026-05-14):
    1) launchctl unload com.tradingbots.passivbot.plist  (stop new orders)
    2) ccxt cancel_all_orders                            (clear the book)
    3) market-close each open position with reduceOnly,  (flatten exposure)
       3 retries with exponential backoff per position
    4) fetch_positions again to verify all flat
    5) Telegram alert with per-position results
    6) Mark state.fired_at — script will NOT fire again until reset.

  pause_freeze (legacy T3-001 original):
    1) bot.long.entry_initial_qty_pct = 0  (pause new entries)
    2) bot.long.total_wallet_exposure_limit = current TWE (freeze ceiling)
    3) Telegram alert
    4) Mark state.fired_at.
    OPEN POSITIONS ARE NOT CLOSED. Unstuck logic still runs.

Data source: passivbot/logs/hl_daily_metrics.json (refreshed daily 00:05 UTC
by com.tradingbots.passivbot-metrics). The breaker does NOT poll HL on the
normal path. HL API calls happen only on fire (kill_switch: many; pause_freeze:
one read-only for TWE calc).

Telegram creds: prefer TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID env vars; otherwise
read from <trading-bots-root>/.telegram-bot-token and .telegram-chat-id files.

Manual re-enable: python3 scripts/dd_circuit_reset.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TRADING_BOTS_ROOT = REPO.parent
METRICS_JSON = REPO / "logs" / "hl_daily_metrics.json"
# v7.11 cutover (2026-05-22): live bot moved from fork-master to a separate
# repo tree at ~/Projects/passivbot-v7.11/. The fork-master config at
# REPO/configs/live/optimized.json is now INERT — editing it would have no
# effect on the running bot. Point at the v7.11 live config that
# com.tradingbots.passivbot-v7.11-live actually loads. Path verified
# against `ps` output of the running PID. (T3-001's pause_freeze action
# was effectively decorative pre-fix; fixed 2026-05-27.)
V7_11_REPO = TRADING_BOTS_ROOT.parent / "passivbot-v7.11"
LIVE_CONFIG = V7_11_REPO / "configs" / "v7.11-live" / "optimized.live.json"
STATE_PATH = REPO / "data" / "dd_circuit_state.json"
API_KEYS_PATH = REPO / "api-keys.json"
LIVE_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.tradingbots.passivbot-v7.11-live.plist"
PLIST_LABEL = "com.tradingbots.passivbot-v7.11-live"

DD_THRESHOLD = 0.20  # fire at >= 20% drawdown from peak
INFO_URL = "https://api.hyperliquid.xyz/info"

# Fire-action modes. `kill_switch` is the new default (T3-001 revised 2026-05-14):
# unload Passivbot plist, cancel all orders, market-close all positions.
# `pause_freeze` is the original T3-001 behavior: set entry_initial_qty_pct=0
# and freeze TWEL — leaves positions open, no plist unload.
DEFAULT_FIRE_MODE = "kill_switch"
ALLOWED_FIRE_MODES = ("kill_switch", "pause_freeze")
KILL_CLOSE_MAX_RETRIES = 3
KILL_CLOSE_INITIAL_BACKOFF_S = 5.0
KILL_POST_UNLOAD_WAIT_S = 5.0


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


def _load_telegram_creds() -> tuple[str, str]:
    """Return (token, chat_id). Prefer env vars; fall back to .telegram-*
    files at the trading-bots repo root (already gitignored)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "") or ""
    if not token:
        f = TRADING_BOTS_ROOT / ".telegram-bot-token"
        if f.exists():
            try:
                token = f.read_text().strip()
            except OSError:
                pass
    if not chat_id:
        f = TRADING_BOTS_ROOT / ".telegram-chat-id"
        if f.exists():
            try:
                chat_id = f.read_text().strip()
            except OSError:
                pass
    return token, chat_id


def send_telegram(message: str) -> bool:
    token, chat_id = _load_telegram_creds()
    if not token or not chat_id:
        print(
            "WARN: telegram creds not found in env or "
            f"{TRADING_BOTS_ROOT}/.telegram-{{bot-token,chat-id}}; skipping alert",
            file=sys.stderr,
        )
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
# Kill-switch fire mode (T3-001 revised 2026-05-14): unload plist, cancel
# orders, market-close positions. Uses ccxt (vendored in passivbot venv) so
# no new dependency. Order matters — unload FIRST stops the bot from
# re-entering while we close, which would cause flapping + 429s.
# --------------------------------------------------------------------------
def resolve_fire_mode(cli_override: str | None = None) -> str:
    raw = cli_override or os.environ.get("DD_FIRE_MODE") or DEFAULT_FIRE_MODE
    mode = raw.strip().lower()
    if mode not in ALLOWED_FIRE_MODES:
        print(
            f"WARN: invalid fire mode {raw!r}; allowed={ALLOWED_FIRE_MODES}; "
            f"falling back to {DEFAULT_FIRE_MODE!r}",
            file=sys.stderr,
        )
        return DEFAULT_FIRE_MODE
    return mode


def _load_hl_creds(api_keys_path: Path) -> dict | None:
    if not api_keys_path.exists():
        return None
    try:
        keys = json.loads(api_keys_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    creds = keys.get("hyperliquid_live") or {}
    if not creds.get("wallet_address") or not creds.get("private_key"):
        return None
    return creds


def _close_position_with_retry(
    exchange: Any,
    position: dict,
    max_retries: int,
    initial_backoff: float,
) -> dict:
    """Market-close ONE position with reduceOnly. Returns a result dict.

    Retries only on exception. On structural failure (e.g., below-min notional)
    ccxt raises, we capture the message and report — operator decides.
    """
    coin = position.get("symbol") or position.get("info", {}).get("coin") or "?"
    side = position.get("side")
    size_raw = position.get("contracts")
    if size_raw is None:
        size_raw = position.get("info", {}).get("szi", 0)
    try:
        size = abs(float(size_raw or 0))
    except (TypeError, ValueError):
        size = 0.0

    if size == 0:
        return {"coin": coin, "ok": True, "attempts": 0,
                "error": None, "fill_price": None, "note": "already flat"}

    # Close = opposite side
    close_side = "sell" if side == "long" else "buy"

    last_err: str | None = None
    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            order = exchange.create_market_order(
                coin, close_side, size,
                params={"reduceOnly": True},
            )
            return {
                "coin": coin, "ok": True, "attempts": attempt,
                "error": None,
                "fill_price": order.get("average") or order.get("price"),
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:300]
            print(
                f"close attempt {attempt}/{max_retries} for {coin} failed: {last_err}",
                file=sys.stderr,
            )
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 3
    return {"coin": coin, "ok": False, "attempts": max_retries,
            "error": last_err, "fill_price": None}


def kill_switch_fire(
    api_keys_path: Path = API_KEYS_PATH,
    plist_path: Path = LIVE_PLIST_PATH,
    plist_label: str = PLIST_LABEL,
    max_retries: int = KILL_CLOSE_MAX_RETRIES,
    initial_backoff: float = KILL_CLOSE_INITIAL_BACKOFF_S,
    post_unload_wait: float = KILL_POST_UNLOAD_WAIT_S,
) -> dict:
    """Execute the kill-switch sequence and return a structured result.

    Sequence:
      1. launchctl unload <plist>            (stop bot from placing new orders)
      2. sleep ~5s for clean exit
      3. ccxt cancel_all_orders              (clear the book)
      4. ccxt fetch_positions                (snapshot what to close)
      5. for each open position: market-close with reduceOnly, retry on error
      6. ccxt fetch_positions again          (verify all flat)
    """
    start = time.time()
    result: dict = {
        "fire_mode": "kill_switch",
        "unload": {"ok": False, "stderr": "", "skipped": False},
        "cancel_orders": {"ok": False, "error": None, "skipped": False},
        "positions_at_fire": [],
        "close_results": [],
        "all_flat": False,
        "duration_s": 0.0,
        "fatal": None,
    }

    # ---- Step 1: unload plist ------------------------------------------------
    if plist_path.exists():
        try:
            r = subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True, text=True, timeout=15,
            )
            result["unload"] = {
                "ok": r.returncode == 0,
                "stderr": (r.stderr or "").strip()[:500],
                "skipped": False,
            }
        except (subprocess.TimeoutExpired, OSError) as e:
            result["unload"] = {"ok": False, "stderr": f"{type(e).__name__}: {e}"[:300],
                                "skipped": False}
    else:
        result["unload"] = {"ok": False, "stderr": f"plist not at {plist_path}",
                            "skipped": True}

    # Brief pause for the bot process to exit cleanly before we mutate the book
    time.sleep(post_unload_wait)

    # ---- Step 2: load ccxt + creds -------------------------------------------
    try:
        import ccxt  # noqa: PLC0415 — lazy import; ccxt only needed on fire path
    except ImportError as e:
        result["fatal"] = f"ccxt import failed: {e}"
        result["duration_s"] = time.time() - start
        return result

    creds = _load_hl_creds(api_keys_path)
    if creds is None:
        result["fatal"] = f"hyperliquid_live creds missing/invalid at {api_keys_path}"
        result["duration_s"] = time.time() - start
        return result

    try:
        exchange = ccxt.hyperliquid({
            "walletAddress": creds["wallet_address"],
            "privateKey": creds["private_key"],
        })
    except Exception as e:  # ccxt raises on bad creds, etc.
        result["fatal"] = f"ccxt.hyperliquid init failed: {type(e).__name__}: {e}"[:300]
        result["duration_s"] = time.time() - start
        return result

    # ---- Step 3: cancel all orders -------------------------------------------
    try:
        exchange.cancel_all_orders()
        result["cancel_orders"]["ok"] = True
    except Exception as e:
        # Cancel failure isn't fatal — we still try to close positions, since
        # market closes with reduceOnly will succeed even if resting orders
        # collide (they'd just partially fill the close).
        result["cancel_orders"]["error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- Step 4: fetch positions ---------------------------------------------
    try:
        positions = exchange.fetch_positions()
    except Exception as e:
        result["fatal"] = f"fetch_positions failed: {type(e).__name__}: {e}"[:300]
        result["duration_s"] = time.time() - start
        return result

    def _open(p: dict) -> bool:
        try:
            return abs(float(p.get("contracts", 0) or 0)) > 0
        except (TypeError, ValueError):
            return False

    open_positions = [p for p in positions if _open(p)]
    result["positions_at_fire"] = [
        {
            "coin": p.get("symbol"),
            "side": p.get("side"),
            "size": p.get("contracts"),
            "notional": p.get("notional"),
            "upnl": p.get("unrealizedPnl"),
        }
        for p in open_positions
    ]

    # ---- Step 5: market-close each position ----------------------------------
    for p in open_positions:
        cr = _close_position_with_retry(exchange, p, max_retries, initial_backoff)
        result["close_results"].append(cr)

    # ---- Step 6: verify all flat ---------------------------------------------
    try:
        after = exchange.fetch_positions()
        result["all_flat"] = not any(_open(p) for p in after)
    except Exception as e:
        # We can't verify, but the closes already ran. Mark unknown.
        result["all_flat"] = False
        result["verify_error"] = f"{type(e).__name__}: {e}"[:200]

    result["duration_s"] = time.time() - start
    return result


def format_kill_switch_alert(
    current_value: float,
    peak: float,
    dd_pct: float,
    fire_result: dict,
) -> str:
    unload = fire_result.get("unload", {})
    cancel = fire_result.get("cancel_orders", {})
    fatal = fire_result.get("fatal")

    if unload.get("skipped"):
        unload_line = f"⚠️ Plist unload SKIPPED ({unload.get('stderr','')[:80]})"
    elif unload.get("ok"):
        unload_line = "✓ Plist unloaded"
    else:
        unload_line = f"✗ Plist unload FAILED: {unload.get('stderr','')[:120]}"

    if cancel.get("ok"):
        cancel_line = "✓ All open orders cancelled"
    elif cancel.get("error"):
        cancel_line = f"✗ cancel_all_orders FAILED: {cancel.get('error','')[:120]}"
    else:
        cancel_line = "— cancel_all_orders skipped"

    close_lines = []
    for cr in fire_result.get("close_results", []):
        mark = "✓" if cr.get("ok") else "✗"
        line = f"  {mark} {cr.get('coin','?')} (attempts={cr.get('attempts')})"
        if cr.get("fill_price"):
            try:
                line += f" @ ${float(cr['fill_price']):,.4f}"
            except (TypeError, ValueError):
                line += f" @ {cr['fill_price']}"
        if cr.get("note"):
            line += f" — {cr['note']}"
        if cr.get("error"):
            line += f" — ERROR: {cr['error'][:100]}"
        close_lines.append(line)
    close_block = "\n".join(close_lines) if close_lines else "  (no open positions at fire-time)"

    flat_line = "✓ All positions flat" if fire_result.get("all_flat") else "✗ Some positions NOT confirmed flat — check Hyperliquid"

    fatal_block = ""
    if fatal:
        fatal_block = f"\n🛑 *FATAL:* {fatal}\n"

    return (
        "🚨 *PASSIVBOT KILL-SWITCH FIRED*\n"
        f"\n"
        f"Account value: ${current_value:,.2f}\n"
        f"Peak: ${peak:,.2f}\n"
        f"Drawdown: {dd_pct*100:.2f}% (threshold -{DD_THRESHOLD*100:.0f}%)\n"
        f"Duration: {fire_result.get('duration_s', 0):.1f}s\n"
        f"{fatal_block}"
        f"\n"
        f"{unload_line}\n"
        f"{cancel_line}\n"
        f"{flat_line}\n"
        f"\n"
        f"*Position closes ({len(fire_result.get('close_results', []))}):*\n"
        f"{close_block}\n"
        f"\n"
        f"⚠️  *Manual reconciliation required.* Steps on Tau:\n"
        f"1. Verify HL UI shows zero positions.\n"
        f"2. Investigate the drawdown root cause.\n"
        f"3. Re-arm breaker: `cd ~/Projects/trading-bots/passivbot && "
        f"python3 scripts/dd_circuit_reset.py`\n"
        f"4. Reload Passivbot ONLY after review: "
        f"`launchctl load ~/Library/LaunchAgents/{PLIST_LABEL}.plist`"
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
    parser.add_argument(
        "--fire-mode",
        type=str,
        default=None,
        choices=ALLOWED_FIRE_MODES,
        help=(
            "Override the fire action. Defaults to DD_FIRE_MODE env var or "
            f"{DEFAULT_FIRE_MODE!r}. kill_switch: unload plist + cancel orders "
            "+ market-close. pause_freeze: set entry_initial_qty_pct=0 + "
            "freeze TWEL (T3-001 original behavior)."
        ),
    )
    args = parser.parse_args()
    fire_mode = resolve_fire_mode(args.fire_mode)

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
        f"peak=${new_peak:.2f} dd={dd_pct*100:.2f}% mode={fire_mode} "
        f"fired={bool(state.get('fired_at'))}"
    )
    print(f"  → {result['reason']}")

    # ---- Update state (peak / current only, never fired) ------------------
    state["peak"] = new_peak
    state["current"] = current_value
    state["last_check_utc"] = datetime.now(timezone.utc).isoformat()

    # ---- Fire ------------------------------------------------------------
    if should_fire:
        if args.dry_run:
            print(f"DRY-RUN: would fire (mode={fire_mode}; skipping config/api/telegram/state)")
            return 0

        fired_at = datetime.now(timezone.utc).isoformat()
        state["fired_at"] = fired_at
        state["fired_reason"] = result["reason"]
        state["fired_current"] = current_value
        state["fired_peak"] = new_peak
        state["fired_dd_pct"] = dd_pct
        state["fired_mode"] = fire_mode

        if fire_mode == "kill_switch":
            fire_result = kill_switch_fire()
            state["fired_kill_switch_result"] = fire_result
            save_state(args.state_path, state)
            alert = format_kill_switch_alert(current_value, new_peak, dd_pct, fire_result)
        else:  # pause_freeze (legacy T3-001 behavior)
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
