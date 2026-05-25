#!/usr/bin/env python3
"""
passivbot_soft_restart_detector.py — detect v7.11 silent in-process
soft restarts via the trip-log signature.

Companion to `soft_restart_detector.py` (which detects post-restart
uptime monotonic decrease). This one watches for the trip event
itself in the live log: an `error count: 50 of 50 errors per hour`
line followed within <=2 seconds by an `Initiating bot restart...`
line. Together they're the deterministic signature of passivbot
v7.11 tripping its 50/h error budget and raising
RestartBotException (research-20260525-1152.md Finding #1).

State is persisted as a list of events at
    ~/Projects/openclaw-orchestrator/data/passivbot_soft_restart_events.json
keyed by (event_ts, log_path) for idempotent re-runs.

Read-only consumer of the log stream. Does not touch the live
daemon, the live config, the live watchdog plist, or passivbot/src/.
Spec: openclaw-orchestrator/reports/daily-plan-20260525-1807.md
      Cycle 1.
Tracker: passivbot-v7.11-soft-restart-detector-second-event-44h-cadence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG_PATH = "/Users/5tuktau/Projects/passivbot-v7.11/logs/hyperliquid_live.log"
DEFAULT_STATE_PATH = "~/Projects/openclaw-orchestrator/data/passivbot_soft_restart_events.json"
GAP_TOLERANCE_SEC = 2

TRIP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b.*error count: 50 of 50 errors per hour\s*$"
)
RESTART_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b.*Initiating bot restart\.\.\.\s*$"
)
HEALTH_UPTIME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b.*\[health\]\s+uptime=(?P<uptime>\S+)"
)
ANY_HEALTH_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b.*\[health\]\s"
)


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def detect_trip_restarts(
    lines,
    log_path: str = "",
    gap_tolerance_sec: int = GAP_TOLERANCE_SEC,
):
    """Scan an iterable of lines for the trip+restart two-line pattern.

    Returns a list of {event_ts, log_path, pre_restart_uptime_field,
    post_restart_first_line_ts} dicts. The optional fields are None
    when the surrounding context isn't in the same line window.
    """
    events: list[dict] = []
    pending_trip: dict | None = None  # {"ts": str, "dt": datetime}
    last_health_uptime: str | None = None
    for line in lines:
        s = line.rstrip("\n")
        m_h = HEALTH_UPTIME_RE.match(s)
        if m_h:
            last_health_uptime = m_h["uptime"]
        m_t = TRIP_RE.match(s)
        if m_t:
            pending_trip = {
                "ts": m_t["ts"],
                "dt": _parse_ts(m_t["ts"]),
                "pre_uptime": last_health_uptime,
            }
            continue
        m_r = RESTART_RE.match(s)
        if m_r and pending_trip is not None:
            restart_dt = _parse_ts(m_r["ts"])
            gap = (restart_dt - pending_trip["dt"]).total_seconds()
            if 0 <= gap <= gap_tolerance_sec:
                events.append({
                    "event_ts": pending_trip["ts"],
                    "log_path": log_path,
                    "pre_restart_uptime_field": pending_trip["pre_uptime"],
                    "post_restart_first_line_ts": None,
                })
            pending_trip = None
            continue
        # Any line strictly after a confirmed event: capture
        # post_restart_first_line_ts for the most recent event if it's
        # still missing and this line is a [health] line.
        if events and events[-1]["post_restart_first_line_ts"] is None:
            m_post = ANY_HEALTH_RE.match(s)
            if m_post and _parse_ts(m_post["ts"]) > _parse_ts(events[-1]["event_ts"]):
                events[-1]["post_restart_first_line_ts"] = m_post["ts"]
    return events


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"events": []}
    raw = path.read_text().strip()
    if not raw:
        return {"events": []}
    data = json.loads(raw)
    data.setdefault("events", [])
    return data


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def merge_events(existing: list[dict], new_events: list[dict]) -> list[dict]:
    """Append only events whose (event_ts, log_path) isn't already present."""
    seen = {(e["event_ts"], e.get("log_path", "")) for e in existing}
    merged = list(existing)
    for e in new_events:
        key = (e["event_ts"], e.get("log_path", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    merged.sort(key=lambda x: x["event_ts"])
    return merged


def _tail_lines(path: Path, since_line: int = 0):
    """Yield log lines starting at the 0-indexed line `since_line`.

    --tail follows the file in append mode after the initial scan.
    Polls every 1s. Yields lines as they appear. Exits on KeyboardInterrupt.
    """
    with path.open() as fh:
        for _ in range(since_line):
            if fh.readline() == "":
                return
        for line in fh:
            yield line


def _tail_follow(path: Path):
    """Follow a growing file -- yields new lines as they're written."""
    with path.open() as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if line:
                yield line
            else:
                time.sleep(1.0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log-path", type=Path, default=Path(DEFAULT_LOG_PATH))
    p.add_argument(
        "--state-path",
        type=Path,
        default=Path(os.path.expanduser(DEFAULT_STATE_PATH)),
    )
    p.add_argument("--tail", action="store_true",
                   help="After the initial scan, follow the log file for new lines.")
    p.add_argument("--since-line", type=int, default=0,
                   help="Skip the first N lines of the log file (0-indexed).")
    p.add_argument("--gap-tolerance-sec", type=int, default=GAP_TOLERANCE_SEC)
    args = p.parse_args(argv)

    state = _load_state(args.state_path)
    log_path_str = str(args.log_path)

    new_events = detect_trip_restarts(
        _tail_lines(args.log_path, since_line=args.since_line),
        log_path=log_path_str,
        gap_tolerance_sec=args.gap_tolerance_sec,
    )
    state["events"] = merge_events(state["events"], new_events)
    _save_state(args.state_path, state)
    for e in new_events:
        print(json.dumps(e, sort_keys=True))

    if args.tail:
        # Stream-follow: emit each detected event individually.
        try:
            for ev in detect_trip_restarts(
                _tail_follow(args.log_path),
                log_path=log_path_str,
                gap_tolerance_sec=args.gap_tolerance_sec,
            ):
                state = _load_state(args.state_path)
                state["events"] = merge_events(state["events"], [ev])
                _save_state(args.state_path, state)
                print(json.dumps(ev, sort_keys=True))
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
