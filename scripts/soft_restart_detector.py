#!/usr/bin/env python3
"""
soft_restart_detector.py — Detect v7.11 silent in-process soft restarts.

When passivbot-v7.11 trips its built-in 50/h error budget, it raises
RestartBotException, re-runs __init__ in-process, and resets
_health_start_ms (src/passivbot.py line 924). The OS process PID is
unchanged, so launchd and the cutover-watchdog see nothing — but uptime
in the [health] log stream resets (observed 2026-05-23 08:26:42Z:
uptime jumped 14h30m → 1m16s with pid 48251 unchanged).

This script consumes a v7.11 .log file, parses the [health] uptime
field, and emits one event=soft_restart_detected JSONL record per
uptime-monotonic decrease > 60 seconds (60s absorbs the ~15-min health
cadence + clock skew). State is persisted between runs in a JSON file
so the detector survives launchctl restarts of the consumer.

Spec source: research-20260523-1152.md Finding #1.
Tracker:     passivbot-v7.11-soft-restart-detector-uptime-monotonic-decrease.

Intended usage (Tau, polled via launchd or manual):
    python3 scripts/soft_restart_detector.py --log /path/to/v7.11.log

Read-only consumer of the log stream. Does not touch the live daemon,
the live config, the live watchdog plist, or passivbot/src/.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Matches a v7.11 [health] line, e.g.:
#   2026-05-23T08:14:54Z INFO     [hyperliquid] [health] uptime=14.0h30.0m | loop=...
HEALTH_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b.*\[health\]\s+uptime=(?P<uptime>\S+)"
)
# Matches one '<num><unit>' or '<num>.<num><unit>' token (d/h/m/s).
_UNIT_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([dhms])")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

DEFAULT_DROP_TOLERANCE_SEC = 60


def parse_uptime(value: str) -> int:
    """Sum all <num><unit> tokens to seconds.

    'uptime=14.0h30.0m' → 14*3600 + 30*60 = 52200
    '1.0m16.0s'         → 60 + 16 = 76
    '2.0d3.0h'          → 2*86400 + 3*3600 = 183600  (forward-compat)
    """
    total = 0.0
    for num, unit in _UNIT_TOKEN_RE.findall(value):
        total += float(num) * _UNIT_SECONDS[unit]
    return int(total)


def parse_health_line(line: str) -> dict | None:
    m = HEALTH_LINE_RE.match(line)
    if not m:
        return None
    return {"ts": m["ts"], "uptime_seconds": parse_uptime(m["uptime"])}


def detect_soft_restarts(
    lines,
    state: dict | None = None,
    daemon_label: str = "hyperliquid_live",
    drop_tolerance_sec: int = DEFAULT_DROP_TOLERANCE_SEC,
):
    """Process an iterable of log lines.

    Returns (events, new_state). state schema per daemon_label:
        {"last_uptime_seconds": int, "last_ts": str}

    A soft_restart_detected event is emitted when:
        current_uptime + drop_tolerance_sec < last_uptime_seen
    The check is inherently idempotent per restart: once uptime jumps
    back to small, subsequent lines climb monotonically until the next
    actual restart.
    """
    state = dict(state or {})
    slot = state.get(daemon_label, {})
    last_uptime = slot.get("last_uptime_seconds")
    last_ts = slot.get("last_ts")
    events: list[dict] = []
    for line in lines:
        h = parse_health_line(line)
        if h is None:
            continue
        cur = h["uptime_seconds"]
        ts = h["ts"]
        if last_uptime is not None and cur + drop_tolerance_sec < last_uptime:
            events.append({
                "event": "soft_restart_detected",
                "daemon_label": daemon_label,
                "prev_ts": last_ts,
                "prev_uptime_seconds": last_uptime,
                "current_ts": ts,
                "current_uptime_seconds": cur,
                "uptime_drop_seconds": last_uptime - cur,
            })
        last_uptime = cur
        last_ts = ts
    state[daemon_label] = {"last_uptime_seconds": last_uptime, "last_ts": last_ts}
    return events, state


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text().strip()
    if not raw:
        return {}
    return json.loads(raw)


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _append_events(path: Path, events) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", required=True, type=Path,
                   help="Path to v7.11 .log file (or '-' for stdin)")
    p.add_argument("--state", type=Path,
                   default=Path(__file__).resolve().parent.parent
                   / "data" / "soft_restart_state.json")
    p.add_argument("--events", type=Path,
                   default=Path(__file__).resolve().parent.parent
                   / "data" / "soft_restart_events.jsonl")
    p.add_argument("--daemon-label", default="hyperliquid_live")
    p.add_argument("--drop-tolerance-sec", type=int,
                   default=DEFAULT_DROP_TOLERANCE_SEC)
    args = p.parse_args(argv)

    state = _load_state(args.state)
    if str(args.log) == "-":
        events, new_state = detect_soft_restarts(
            sys.stdin, state=state, daemon_label=args.daemon_label,
            drop_tolerance_sec=args.drop_tolerance_sec,
        )
    else:
        with args.log.open() as fh:
            events, new_state = detect_soft_restarts(
                fh, state=state, daemon_label=args.daemon_label,
                drop_tolerance_sec=args.drop_tolerance_sec,
            )
    _save_state(args.state, new_state)
    _append_events(args.events, events)
    for e in events:
        print(json.dumps(e, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
