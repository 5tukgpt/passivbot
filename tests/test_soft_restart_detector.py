"""Tests for scripts/soft_restart_detector.py.

Pins the verbatim 2026-05-23 08:14:54Z + 08:27:58Z health-line pair from
the v7.11 silent soft-restart event (research-20260523-1152.md Finding
#1), plus the monotonic-uptime no-op and fresh-state counter-factual
paths required by the tracker
`passivbot-v7.11-soft-restart-detector-uptime-monotonic-decrease`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import soft_restart_detector as srd  # noqa: E402

# Verbatim from /Users/5tuktau/Projects/passivbot-v7.11/logs/
# 20260522_174357_src_main.py..._hyperliquid_live_...log
PRE_RESTART = (
    "2026-05-23T08:14:54Z INFO     [hyperliquid] [health] "
    "uptime=14.0h30.0m | loop=7.1s | positions=5 long, 0 short | "
    "balance=1180.00 USDC (snap 1186.87) | orders=+597/-584 | "
    "fills=0 | errors=27/10 | ws_reconnects=7 | rate_limits=203 | rss=352MB"
)
POST_RESTART = (
    "2026-05-23T08:27:58Z INFO     [hyperliquid] [health] "
    "uptime=1.0m16.0s | loop=21.1s | positions=5 long, 0 short | "
    "balance=1180.41 USDC (snap 1180.43) | orders=+0/-2 | fills=0 | "
    "errors=0/10 | ws_reconnects=0 | rate_limits=0 | rss=352MB"
)


def test_replay_soft_restart_emits_one_event():
    events, state = srd.detect_soft_restarts([PRE_RESTART, POST_RESTART])
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "soft_restart_detected"
    assert e["daemon_label"] == "hyperliquid_live"
    assert e["prev_ts"] == "2026-05-23T08:14:54Z"
    assert e["current_ts"] == "2026-05-23T08:27:58Z"
    assert e["prev_uptime_seconds"] == 14 * 3600 + 30 * 60      # 52200
    assert e["current_uptime_seconds"] == 60 + 16               # 76
    assert e["uptime_drop_seconds"] == 52200 - 76
    assert state["hyperliquid_live"]["last_uptime_seconds"] == 76
    assert state["hyperliquid_live"]["last_ts"] == "2026-05-23T08:27:58Z"


def test_monotonic_uptime_emits_zero_events():
    a = "2026-05-22T17:47:15Z INFO [hyperliquid] [health] uptime=3.0m6.0s"
    b = "2026-05-22T18:02:44Z INFO [hyperliquid] [health] uptime=18.0m35.0s"
    events, _ = srd.detect_soft_restarts([a, b])
    assert events == []


def test_fresh_state_with_only_post_line_emits_zero():
    # Counter-factual: a normal `launchctl unload && load` starts a fresh
    # log file with empty state. The detector MUST NOT emit a soft-restart
    # event when there's no prior uptime to compare against.
    events, _ = srd.detect_soft_restarts([POST_RESTART])
    assert events == []


def test_external_restart_pid_change_emits_zero():
    # Two SEPARATE detector invocations with empty state in between
    # simulate the launchctl-unload-then-load case (new pid → new log
    # file → detector keyed on a fresh state slot). Each invocation sees
    # only a strictly-increasing uptime sequence within its own scope.
    events_pre, _ = srd.detect_soft_restarts([PRE_RESTART])
    events_post, _ = srd.detect_soft_restarts([POST_RESTART])  # fresh state
    assert events_pre == []
    assert events_post == []


def test_drop_within_tolerance_emits_zero():
    # Two health lines 30 seconds apart in uptime (within the 60s
    # drop_tolerance default). Should NOT trigger.
    a = "2026-05-22T17:47:15Z INFO [hyperliquid] [health] uptime=3.0m6.0s"
    b = "2026-05-22T17:48:00Z INFO [hyperliquid] [health] uptime=2.0m36.0s"
    events, _ = srd.detect_soft_restarts([a, b])
    assert events == []


def test_parse_uptime_units():
    assert srd.parse_uptime("14.0h30.0m") == 14 * 3600 + 30 * 60
    assert srd.parse_uptime("1.0m16.0s") == 60 + 16
    assert srd.parse_uptime("3.0m6.0s") == 3 * 60 + 6
    assert srd.parse_uptime("uptime=10.0h41.0m") == 10 * 3600 + 41 * 60
    assert srd.parse_uptime("0.0s") == 0


def test_non_health_lines_ignored():
    noise = [
        "2026-05-23T08:25:41Z INFO [hyperliquid] restarting bot...",
        "2026-05-23T08:25:41Z INFO [hyperliquid] stopped data maintainers: {...}",
        "random line without timestamp",
        "",
    ]
    events, state = srd.detect_soft_restarts(noise)
    assert events == []
    # State slot exists but with None values (nothing parsed).
    assert state["hyperliquid_live"]["last_uptime_seconds"] is None


def test_cli_persists_state_and_emits_jsonl(tmp_path):
    log = tmp_path / "v7.11.log"
    log.write_text(PRE_RESTART + "\n" + POST_RESTART + "\n")
    state_file = tmp_path / "state.json"
    events_file = tmp_path / "events.jsonl"
    rc = srd.main([
        "--log", str(log),
        "--state", str(state_file),
        "--events", str(events_file),
    ])
    assert rc == 0
    state = json.loads(state_file.read_text())
    assert state["hyperliquid_live"]["last_uptime_seconds"] == 76
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "soft_restart_detected"
    assert rec["uptime_drop_seconds"] == 52200 - 76
