#!/usr/bin/env python3
"""
Manually re-arm the Passivbot drawdown circuit breaker after a fire event.

This does NOT restore the live config — that's the operator's job. The
breaker fires by mutating configs/live/optimized.json (entry_initial_qty_pct
and total_wallet_exposure_limit). Before running this reset, decide whether
to:
  1. Manually edit configs/live/optimized.json back to safe baseline values
     and restart the bot, OR
  2. Leave entries paused and only clear the fired state to allow the
     breaker to track a NEW peak from the current (recovered) value.

Either way, this script ONLY clears state.fired_at — it does not touch
configs/live/optimized.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = REPO / "data" / "dd_circuit_state.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=STATE_PATH,
        help=f"State file path (default: {STATE_PATH})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )
    args = parser.parse_args()

    if not args.state_path.exists():
        print(f"State file not found: {args.state_path}")
        return 1

    with args.state_path.open() as f:
        state = json.load(f)

    if not state.get("fired_at"):
        print(f"Circuit is already armed. Nothing to reset.")
        print(f"  peak=${state.get('peak', 0):.2f} current=${state.get('current', 0):.2f}")
        return 0

    print("Circuit was FIRED:")
    print(f"  fired_at:      {state.get('fired_at')}")
    print(f"  reason:        {state.get('fired_reason')}")
    print(f"  peak:          ${state.get('fired_peak', 0):.2f}")
    print(f"  current:       ${state.get('fired_current', 0):.2f}")
    print(f"  dd_pct:        {(state.get('fired_dd_pct') or 0)*100:.2f}%")
    print(f"  twe at fire:   {state.get('fired_twe')}")
    print(f"  config diff:   {json.dumps(state.get('fired_config_diff'), indent=2)}")
    print()
    print("This will CLEAR fired_at and re-arm the circuit.")
    print("It will NOT restore configs/live/optimized.json — that is your job.")

    if not args.yes:
        ans = input("Proceed? (yes/no): ").strip().lower()
        if ans != "yes":
            print("Aborted.")
            return 1

    history = state.setdefault("reset_history", [])
    history.append({
        "reset_at": datetime.now(timezone.utc).isoformat(),
        "was_fired_at": state.get("fired_at"),
        "was_fired_reason": state.get("fired_reason"),
        "was_fired_dd_pct": state.get("fired_dd_pct"),
    })

    for key in (
        "fired_at",
        "fired_reason",
        "fired_current",
        "fired_peak",
        "fired_dd_pct",
        "fired_twe",
        "fired_config_diff",
    ):
        state.pop(key, None)

    tmp = args.state_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(args.state_path)

    print(f"Circuit re-armed. New peak={state.get('peak', 0):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
