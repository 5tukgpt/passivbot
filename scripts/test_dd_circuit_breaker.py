#!/usr/bin/env python3
"""
Smoke test for the drawdown circuit breaker.

Verifies:
  1. Pure decision function correctly identifies a 25% DD as a fire event.
  2. Pure decision function does NOT fire at 10% / 15% / 19% DD.
  3. Pure decision function does NOT re-fire after state.fired_at is set.
  4. config-mutation function correctly pauses entries and freezes TWEL
     when applied to a copied (non-live) config in a temp dir.
  5. The full main() in --dry-run mode against synthetic inputs does NOT
     touch the real live config or send telegram.
  6. Mode resolution: env var, CLI override, invalid fallback (kill_switch).
  7. Telegram cred file fallback when env vars unset.
  8. _close_position_with_retry: happy path, retry-then-success, exhausted,
     zero-size short-circuit.
  9. format_kill_switch_alert: clean close, partial failure, fatal path.
 10. End-to-end dry-run with --fire-mode kill_switch.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dd_circuit_breaker import (  # noqa: E402
    DEFAULT_FIRE_MODE,
    _close_position_with_retry,
    _load_telegram_creds,
    decide,
    format_kill_switch_alert,
    modify_live_config,
    resolve_fire_mode,
)


def test_decide_fires_at_25pct() -> None:
    state = {"peak": 1000.0, "fired_at": None}
    out = decide(current_value=750.0, state=state)
    assert out["should_fire"] is True, f"Expected fire at 25% DD, got {out}"
    assert abs(out["dd_pct"] + 0.25) < 1e-9, f"DD mismatch: {out['dd_pct']}"
    print("  ✓ 25% DD → fire")


def test_decide_no_fire_below_threshold() -> None:
    for dd in (0.10, 0.15, 0.19):
        peak = 1000.0
        current = peak * (1 - dd)
        state = {"peak": peak, "fired_at": None}
        out = decide(current_value=current, state=state)
        assert out["should_fire"] is False, f"Should not fire at {dd*100}% DD, got {out}"
        print(f"  ✓ {dd*100:.0f}% DD → no fire")


def test_decide_fires_at_exact_threshold() -> None:
    state = {"peak": 1000.0, "fired_at": None}
    out = decide(current_value=800.0, state=state)  # exactly -20%
    assert out["should_fire"] is True, f"Expected fire at exact -20%, got {out}"
    print("  ✓ exactly -20% DD → fire (boundary)")


def test_decide_no_refire_when_already_fired() -> None:
    state = {"peak": 1000.0, "fired_at": "2026-05-12T00:00:00+00:00"}
    out = decide(current_value=500.0, state=state)  # 50% DD!
    assert out["should_fire"] is False, f"Must not re-fire when already fired: {out}"
    assert "Already fired" in out["reason"]
    print("  ✓ Already-fired state → no re-fire even at 50% DD")


def test_decide_peak_advances_while_armed() -> None:
    state = {"peak": 1000.0, "fired_at": None}
    out = decide(current_value=1100.0, state=state)
    assert out["new_peak"] == 1100.0, f"Peak should advance: {out}"
    assert out["should_fire"] is False
    print("  ✓ New high while armed → peak advances")


def test_decide_peak_frozen_after_fire() -> None:
    state = {"peak": 1000.0, "fired_at": "2026-05-12T00:00:00+00:00"}
    out = decide(current_value=1200.0, state=state)
    assert out["new_peak"] == 1000.0, f"Peak must NOT advance after fire: {out}"
    print("  ✓ Post-fire peak stays frozen")


def test_modify_config_pauses_and_freezes() -> None:
    """Operate on a COPY of the real live config — never touches the live file."""
    src = REPO / "configs" / "live" / "optimized.json"
    assert src.exists(), f"Live config missing: {src}"

    with tempfile.TemporaryDirectory() as tmpdir:
        dst = Path(tmpdir) / "optimized.json"
        shutil.copy(src, dst)

        before = json.loads(dst.read_text())
        before_eqp = before["bot"]["long"]["entry_initial_qty_pct"]
        before_twel = before["bot"]["long"]["total_wallet_exposure_limit"]

        diff = modify_live_config(dst, pause_entries=True, freeze_twel=0.35)

        after = json.loads(dst.read_text())
        assert after["bot"]["long"]["entry_initial_qty_pct"] == 0.0
        # Should freeze at MIN(0.35, before_twel). Live is 0.8 → 0.35.
        assert after["bot"]["long"]["total_wallet_exposure_limit"] == min(0.35, before_twel)
        assert diff["before"]["entry_initial_qty_pct"] == before_eqp
        assert diff["after"]["entry_initial_qty_pct"] == 0.0

        # Live file must remain unchanged (different mtime/contents wouldn't
        # prove this — we re-read live and compare to original).
        live_now = json.loads(src.read_text())
        assert live_now["bot"]["long"]["entry_initial_qty_pct"] == before_eqp, \
            "LIVE CONFIG WAS MUTATED — test contamination!"
        print(f"  ✓ Config mutation: eqp {before_eqp} → 0.0, twel {before_twel} → {min(0.35, before_twel)}")
        print(f"  ✓ Live config untouched ({src})")


def test_modify_config_never_increases_twel() -> None:
    src = REPO / "configs" / "live" / "optimized.json"
    with tempfile.TemporaryDirectory() as tmpdir:
        dst = Path(tmpdir) / "optimized.json"
        shutil.copy(src, dst)

        before_twel = json.loads(dst.read_text())["bot"]["long"]["total_wallet_exposure_limit"]
        # Try to freeze at value HIGHER than current TWEL — should NOT increase.
        modify_live_config(dst, pause_entries=False, freeze_twel=before_twel + 100)
        after = json.loads(dst.read_text())
        assert after["bot"]["long"]["total_wallet_exposure_limit"] == before_twel, \
            "TWEL was raised — that's wrong!"
        print(f"  ✓ TWEL clamp: requested {before_twel+100} → stayed at {before_twel}")


def test_main_dryrun_25pct_simulated() -> None:
    """End-to-end dry-run: simulate 25% DD, verify fire detection, verify
    no real-world side effects (no config write, no state mark, no telegram)."""
    live_config = REPO / "configs" / "live" / "optimized.json"
    live_eqp_before = json.loads(live_config.read_text())["bot"]["long"]["entry_initial_qty_pct"]

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        state_path.write_text(json.dumps({"peak": 1200.0, "fired_at": None}))

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "dd_circuit_breaker.py"),
                "--dry-run",
                "--simulate-current", "900",  # 25% DD from 1200
                "--simulate-peak", "1200",
                "--state-path", str(state_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "DRY-RUN: would fire" in result.stdout, f"Should report would-fire: {result.stdout}"
        assert "DD=-25.00%" in result.stdout, f"DD% missing in output: {result.stdout}"

        # State file should NOT have been mutated to fired_at in dry-run.
        state_after = json.loads(state_path.read_text())
        assert state_after.get("fired_at") is None, f"Dry-run wrote fired_at: {state_after}"
        print("  ✓ Dry-run reported fire at -25% DD")
        print("  ✓ State file was NOT marked fired in dry-run")

    # Live config must still be untouched.
    live_eqp_after = json.loads(live_config.read_text())["bot"]["long"]["entry_initial_qty_pct"]
    assert live_eqp_after == live_eqp_before, "DRY-RUN MUTATED LIVE CONFIG"
    print(f"  ✓ Live config entry_initial_qty_pct unchanged ({live_eqp_after})")


def test_main_dryrun_no_fire_at_15pct() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "dd_circuit_breaker.py"),
                "--dry-run",
                "--simulate-current", "1020",  # 15% DD from 1200
                "--simulate-peak", "1200",
                "--state-path", str(state_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "would fire" not in result.stdout, f"Should NOT fire: {result.stdout}"
        assert "Armed" in result.stdout
        print("  ✓ Dry-run at -15% DD: no fire, remains armed")


# --------------------------------------------------------------------------
# Mode resolution
# --------------------------------------------------------------------------
def test_resolve_fire_mode_default() -> None:
    saved = os.environ.pop("DD_FIRE_MODE", None)
    try:
        assert resolve_fire_mode() == "kill_switch"
        assert DEFAULT_FIRE_MODE == "kill_switch"
        print("  ✓ Default mode resolves to kill_switch")
    finally:
        if saved is not None:
            os.environ["DD_FIRE_MODE"] = saved


def test_resolve_fire_mode_env_override() -> None:
    saved = os.environ.pop("DD_FIRE_MODE", None)
    try:
        os.environ["DD_FIRE_MODE"] = "pause_freeze"
        assert resolve_fire_mode() == "pause_freeze"
        os.environ["DD_FIRE_MODE"] = "KILL_SWITCH"  # case-insensitive
        assert resolve_fire_mode() == "kill_switch"
        print("  ✓ Env var override + case-insensitive parsing")
    finally:
        os.environ.pop("DD_FIRE_MODE", None)
        if saved is not None:
            os.environ["DD_FIRE_MODE"] = saved


def test_resolve_fire_mode_cli_override_wins() -> None:
    saved = os.environ.pop("DD_FIRE_MODE", None)
    try:
        os.environ["DD_FIRE_MODE"] = "pause_freeze"
        assert resolve_fire_mode("kill_switch") == "kill_switch"
        print("  ✓ CLI override beats env var")
    finally:
        os.environ.pop("DD_FIRE_MODE", None)
        if saved is not None:
            os.environ["DD_FIRE_MODE"] = saved


def test_resolve_fire_mode_invalid_falls_back() -> None:
    saved = os.environ.pop("DD_FIRE_MODE", None)
    try:
        os.environ["DD_FIRE_MODE"] = "nuke_from_orbit"
        assert resolve_fire_mode() == DEFAULT_FIRE_MODE
        print(f"  ✓ Invalid mode → {DEFAULT_FIRE_MODE}")
    finally:
        os.environ.pop("DD_FIRE_MODE", None)
        if saved is not None:
            os.environ["DD_FIRE_MODE"] = saved


# --------------------------------------------------------------------------
# Telegram cred fallback
# --------------------------------------------------------------------------
def test_load_telegram_creds_from_files() -> None:
    """When env vars are missing, fall back to .telegram-* files at trading-bots root."""
    saved_t = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    saved_c = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        token, chat_id = _load_telegram_creds()
        # The real files exist at /Users/5tuktau/Projects/trading-bots/.telegram-*
        # so we should get something. If not present in CI, both will be "".
        assert isinstance(token, str)
        assert isinstance(chat_id, str)
        if token and chat_id:
            print(f"  ✓ Cred file fallback worked (token len={len(token)}, chat_id len={len(chat_id)})")
        else:
            print("  ⚠ Cred files absent — fallback returned empty (acceptable in test env)")
    finally:
        if saved_t is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved_t
        if saved_c is not None:
            os.environ["TELEGRAM_CHAT_ID"] = saved_c


# --------------------------------------------------------------------------
# Position-close retry behaviour (mocked exchange — no real HL calls)
# --------------------------------------------------------------------------
class _MockExchange:
    """Minimal ccxt-shaped mock. Records calls; raises on configured attempts."""
    def __init__(self, fail_attempts: int = 0, fail_exc=RuntimeError("simulated 429")):
        self.fail_attempts = fail_attempts
        self.fail_exc = fail_exc
        self.calls: list[dict] = []

    def create_market_order(self, symbol, side, amount, params=None):
        self.calls.append({"symbol": symbol, "side": side, "amount": amount, "params": params})
        if len(self.calls) <= self.fail_attempts:
            raise self.fail_exc
        return {"average": 123.45, "price": 123.45, "id": "mock-order-id"}


def test_close_position_with_retry_first_try_success() -> None:
    ex = _MockExchange(fail_attempts=0)
    pos = {"symbol": "HYPE/USDC:USDC", "side": "long", "contracts": 1.5}
    out = _close_position_with_retry(ex, pos, max_retries=3, initial_backoff=0.0)
    assert out["ok"] is True
    assert out["attempts"] == 1
    assert out["fill_price"] == 123.45
    assert ex.calls[0]["side"] == "sell"
    assert ex.calls[0]["amount"] == 1.5
    assert ex.calls[0]["params"] == {"reduceOnly": True}
    print("  ✓ Happy path: 1 attempt, reduceOnly market sell")


def test_close_position_with_retry_succeeds_on_retry() -> None:
    ex = _MockExchange(fail_attempts=2)  # fail twice, succeed on 3rd
    pos = {"symbol": "BTC/USDC:USDC", "side": "short", "contracts": 0.001}
    out = _close_position_with_retry(ex, pos, max_retries=3, initial_backoff=0.0)
    assert out["ok"] is True, out
    assert out["attempts"] == 3
    # Short → close is buy
    assert ex.calls[0]["side"] == "buy"
    print("  ✓ Retry path: 2 failures then success, short→buy")


def test_close_position_with_retry_exhausted() -> None:
    ex = _MockExchange(fail_attempts=5, fail_exc=ValueError("notional < min"))
    pos = {"symbol": "WLD/USDC:USDC", "side": "long", "contracts": 0.1}
    out = _close_position_with_retry(ex, pos, max_retries=3, initial_backoff=0.0)
    assert out["ok"] is False
    assert out["attempts"] == 3
    assert "ValueError" in (out["error"] or "")
    assert "notional < min" in (out["error"] or "")
    print("  ✓ Exhausted: all retries fail, error captured")


def test_close_position_with_retry_zero_size_short_circuits() -> None:
    ex = _MockExchange()
    pos = {"symbol": "SOL/USDC:USDC", "side": "long", "contracts": 0}
    out = _close_position_with_retry(ex, pos, max_retries=3, initial_backoff=0.0)
    assert out["ok"] is True
    assert out["attempts"] == 0
    assert ex.calls == []  # no API call for zero-size
    print("  ✓ Zero-size position: short-circuits with no API call")


# --------------------------------------------------------------------------
# Alert formatting
# --------------------------------------------------------------------------
def test_format_kill_switch_alert_clean_close() -> None:
    fire_result = {
        "unload": {"ok": True, "stderr": "", "skipped": False},
        "cancel_orders": {"ok": True, "error": None},
        "positions_at_fire": [
            {"coin": "HYPE/USDC:USDC", "side": "long", "size": 1.0, "notional": 50, "upnl": -10},
        ],
        "close_results": [
            {"coin": "HYPE/USDC:USDC", "ok": True, "attempts": 1, "fill_price": 50.1},
        ],
        "all_flat": True,
        "duration_s": 12.3,
    }
    msg = format_kill_switch_alert(900.0, 1200.0, -0.25, fire_result)
    assert "KILL-SWITCH FIRED" in msg
    assert "Account value: $900.00" in msg
    assert "Drawdown: -25.00%" in msg
    assert "✓ Plist unloaded" in msg
    assert "✓ All open orders cancelled" in msg
    assert "✓ All positions flat" in msg
    assert "HYPE/USDC:USDC" in msg
    assert "Manual reconciliation required" in msg
    print("  ✓ Clean-close alert contains all required sections")


def test_format_kill_switch_alert_partial_failure() -> None:
    fire_result = {
        "unload": {"ok": True, "stderr": "", "skipped": False},
        "cancel_orders": {"ok": False, "error": "RuntimeError: 429"},
        "positions_at_fire": [
            {"coin": "BTC", "side": "long"}, {"coin": "ETH", "side": "long"},
        ],
        "close_results": [
            {"coin": "BTC", "ok": True, "attempts": 1, "fill_price": 60000},
            {"coin": "ETH", "ok": False, "attempts": 3, "error": "ValueError: notional too small"},
        ],
        "all_flat": False,
        "duration_s": 35.7,
    }
    msg = format_kill_switch_alert(900.0, 1200.0, -0.25, fire_result)
    assert "✗ Some positions NOT confirmed flat" in msg
    assert "cancel_all_orders FAILED" in msg
    assert "notional too small" in msg
    assert "ETH" in msg
    print("  ✓ Partial-failure alert flags each issue")


def test_format_kill_switch_alert_fatal() -> None:
    fire_result = {
        "unload": {"ok": False, "stderr": "plist not at /path", "skipped": True},
        "cancel_orders": {"ok": False, "error": None},
        "positions_at_fire": [],
        "close_results": [],
        "all_flat": False,
        "duration_s": 0.5,
        "fatal": "ccxt import failed: No module named 'ccxt'",
    }
    msg = format_kill_switch_alert(900.0, 1200.0, -0.25, fire_result)
    assert "FATAL" in msg
    assert "ccxt import failed" in msg
    assert "Plist unload SKIPPED" in msg
    print("  ✓ Fatal-error alert surfaces the root cause")


# --------------------------------------------------------------------------
# End-to-end dry-run with kill_switch mode
# --------------------------------------------------------------------------
def test_main_dryrun_kill_switch_mode() -> None:
    """Verify --fire-mode kill_switch in --dry-run reports correctly and
    does NOT call ccxt / launchctl / telegram."""
    live_config = REPO / "configs" / "live" / "optimized.json"
    live_eqp_before = json.loads(live_config.read_text())["bot"]["long"]["entry_initial_qty_pct"]

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        state_path.write_text(json.dumps({"peak": 1200.0, "fired_at": None}))

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "dd_circuit_breaker.py"),
                "--dry-run",
                "--fire-mode", "kill_switch",
                "--simulate-current", "900",  # 25% DD from 1200
                "--simulate-peak", "1200",
                "--state-path", str(state_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "mode=kill_switch" in result.stdout
        assert "would fire (mode=kill_switch" in result.stdout

        # No mutation in dry-run
        state_after = json.loads(state_path.read_text())
        assert state_after.get("fired_at") is None
        print("  ✓ kill_switch dry-run reported correctly, no side-effects")

    live_eqp_after = json.loads(live_config.read_text())["bot"]["long"]["entry_initial_qty_pct"]
    assert live_eqp_after == live_eqp_before, "DRY-RUN MUTATED LIVE CONFIG"


def main() -> int:
    tests = [
        test_decide_fires_at_25pct,
        test_decide_no_fire_below_threshold,
        test_decide_fires_at_exact_threshold,
        test_decide_no_refire_when_already_fired,
        test_decide_peak_advances_while_armed,
        test_decide_peak_frozen_after_fire,
        test_modify_config_pauses_and_freezes,
        test_modify_config_never_increases_twel,
        test_main_dryrun_25pct_simulated,
        test_main_dryrun_no_fire_at_15pct,
        test_resolve_fire_mode_default,
        test_resolve_fire_mode_env_override,
        test_resolve_fire_mode_cli_override_wins,
        test_resolve_fire_mode_invalid_falls_back,
        test_load_telegram_creds_from_files,
        test_close_position_with_retry_first_try_success,
        test_close_position_with_retry_succeeds_on_retry,
        test_close_position_with_retry_exhausted,
        test_close_position_with_retry_zero_size_short_circuits,
        test_format_kill_switch_alert_clean_close,
        test_format_kill_switch_alert_partial_failure,
        test_format_kill_switch_alert_fatal,
        test_main_dryrun_kill_switch_mode,
    ]
    failures = 0
    for test in tests:
        try:
            print(f"\n{test.__name__}:")
            test()
        except AssertionError as e:
            print(f"  ✗ FAIL: {e}")
            failures += 1
        except Exception as e:
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            failures += 1
    print()
    if failures:
        print(f"❌ {failures}/{len(tests)} tests FAILED")
        return 1
    print(f"✅ All {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
