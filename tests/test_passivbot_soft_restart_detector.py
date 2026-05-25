"""Tests for scripts/passivbot_soft_restart_detector.py.

Pins the trip-log signature detector against the canonical 2026-05-23
and 2026-05-25 events from passivbot-v7.11/logs/hyperliquid_live.log
(research-20260525-1152.md Finding #1, daily-plan-20260525-1807.md
Cycle 1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import passivbot_soft_restart_detector as psrd  # noqa: E402


# Verbatim lines from /Users/5tuktau/Projects/passivbot-v7.11/logs/hyperliquid_live.log
HEALTH_PRE = (
    "2026-05-25T04:37:13Z INFO     [hyperliquid] [health] uptime=1.0d20.0h10.0m | "
    "loop=10.1s | positions=5 long, 0 short | balance=1201.34 USDC (snap 1204.37) | "
    "orders=+555/-545 | fills=0 | errors=44/10 | ws_reconnects=15 | "
    "rate_limits=648 | rss=352MB"
)
TRIP_LINE = "2026-05-25T04:40:52Z INFO     [hyperliquid] error count: 50 of 50 errors per hour"
RESTART_LINE = "2026-05-25T04:40:52Z INFO     [hyperliquid] Initiating bot restart..."
HEALTH_POST = (
    "2026-05-25T04:42:50Z INFO     [hyperliquid] [health] uptime=55.0s | loop=18.4s | "
    "positions=5 long, 0 short | balance=1202.13 USDC (snap 1201.98) | orders=+0/-0 | "
    "fills=0 | errors=0/10 | ws_reconnects=0 | rate_limits=0 | rss=352MB"
)


def test_trip_match_in_middle_of_log_emits_one_event():
    # Wrap the trip in unrelated lines on either side so we exercise the
    # main "match in middle of log" branch the plan calls out.
    leading = "2026-05-25T04:30:00Z INFO     [hyperliquid] periodic noise line"
    trailing = "2026-05-25T04:45:00Z INFO     [hyperliquid] another noise line"
    lines = [leading, HEALTH_PRE, TRIP_LINE, RESTART_LINE, HEALTH_POST, trailing]
    events = psrd.detect_trip_restarts(lines, log_path="/x/y.log")
    assert len(events) == 1
    e = events[0]
    assert e["event_ts"] == "2026-05-25T04:40:52Z"
    assert e["log_path"] == "/x/y.log"
    # pre_restart_uptime_field is the most recent [health] uptime value.
    assert e["pre_restart_uptime_field"] == "1.0d20.0h10.0m"
    # post_restart_first_line_ts is the first [health] line strictly after
    # the event timestamp.
    assert e["post_restart_first_line_ts"] == "2026-05-25T04:42:50Z"


def test_trip_without_restart_emits_zero():
    # An `error count: 50 of 50` line that ISN'T followed by an
    # `Initiating bot restart...` line within the gap window must NOT
    # produce an event -- per-line health-counter noise, not a real
    # trip event.
    later = "2026-05-25T04:50:00Z INFO     [hyperliquid] periodic noise (no restart)"
    lines = [HEALTH_PRE, TRIP_LINE, later]
    events = psrd.detect_trip_restarts(lines, log_path="/x/y.log")
    assert events == []


def test_gap_over_two_seconds_emits_zero():
    # Trip at :40:52Z, restart at :40:55Z -- 3s gap, exceeds 2s tolerance.
    trip = "2026-05-25T04:40:52Z INFO     [hyperliquid] error count: 50 of 50 errors per hour"
    restart_late = "2026-05-25T04:40:55Z INFO     [hyperliquid] Initiating bot restart..."
    events = psrd.detect_trip_restarts([HEALTH_PRE, trip, restart_late])
    assert events == []


def test_merge_events_is_idempotent(tmp_path):
    state_path = tmp_path / "state.json"

    # First scan: 2 canonical events from the live log lines.
    trip_1 = "2026-05-23T08:25:40Z INFO     [hyperliquid] error count: 50 of 50 errors per hour"
    restart_1 = "2026-05-23T08:25:40Z INFO     [hyperliquid] Initiating bot restart..."
    trip_2 = TRIP_LINE
    restart_2 = RESTART_LINE
    lines = [trip_1, restart_1, trip_2, restart_2]
    new_events = psrd.detect_trip_restarts(lines, log_path="/log.log")
    assert len(new_events) == 2

    state = psrd._load_state(state_path)
    state["events"] = psrd.merge_events(state["events"], new_events)
    psrd._save_state(state_path, state)

    # Second scan over the same lines must not double-write.
    new_events_2 = psrd.detect_trip_restarts(lines, log_path="/log.log")
    state2 = psrd._load_state(state_path)
    state2["events"] = psrd.merge_events(state2["events"], new_events_2)
    psrd._save_state(state_path, state2)

    final = json.loads(state_path.read_text())
    assert len(final["events"]) == 2
    assert {e["event_ts"] for e in final["events"]} == {
        "2026-05-23T08:25:40Z",
        "2026-05-25T04:40:52Z",
    }
