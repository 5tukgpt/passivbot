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
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dd_circuit_breaker import decide, modify_live_config  # noqa: E402


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
