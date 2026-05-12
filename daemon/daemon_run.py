#!/usr/bin/env python3
"""
Config Optimizer Daemon — nightly run wrapper.

Runs the full cycle once:
  1. NSGA2 on substrate A (recent)
  2. NSGA2 on substrate B (S3 era)
  3. Persist both runs to SQLite
  4. Cross-OOS-validate top-N from each against the other
  5. Surface cross-regime-passing candidates (Telegram alert on hits)
  6. Telegram alert on any failure

Designed to be invoked once nightly via launchd (com.tradingbots.config-optimizer-daemon).
Single-shot; not a long-running loop. Total wall time ~30-60 seconds.

Live Passivbot is untouched throughout. The daemon is read-only against the
live bot. Promotion candidates appear in DB + Telegram digest; James reviews
and decides whether to promote (Tier 3).
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DAEMON_ROOT = Path(__file__).parent.resolve()
PASSIVBOT_ROOT = Path("/Users/5tuktau/Projects/trading-bots/passivbot")
PYTHON = PASSIVBOT_ROOT / "venv/bin/python"
OPTIMIZE_SCRIPT = PASSIVBOT_ROOT / "src/optimize.py"
PERSIST_SCRIPT = DAEMON_ROOT / "persist_results.py"
OOS_VALIDATE_SCRIPT = DAEMON_ROOT / "oos_validate.py"
DB_PATH = DAEMON_ROOT / "config_optimizer.db"
LOG_DIR = DAEMON_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

SUBSTRATES = [
    {
        "label": "recent",
        "config": DAEMON_ROOT / "real_run_config.json",
        "val_start": "2025-12-26",
        "val_end": "2026-02-21",
    },
    {
        "label": "s3_era",
        "config": DAEMON_ROOT / "s3_substrate_config.json",
        "val_start": "2026-04-10",
        "val_end": "2026-05-10",
    },
]

# Cross-regime promotion gate: a candidate "passes" only if its TWE is non-trivial
# on BOTH the training and OOS substrate. The TWE collapse from 0.69→0.02 we saw
# today should be filtered out by this gate.
CROSS_REGIME_MIN_TWE = 0.10
CROSS_REGIME_MIN_GAIN = 0.95  # Allow modest losses but not bot disasters

TELEGRAM_BOT_TOKEN_FILE = Path.home() / "Projects/trading-bots/.telegram-bot-token"
TELEGRAM_CHAT_ID_FILE = Path.home() / "Projects/trading-bots/.telegram-chat-id"


def setup_logging(log_path: Path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )


def telegram_send(message: str, silent: bool = False) -> None:
    """Best-effort Telegram alert. Silent failure if not configured."""
    if not TELEGRAM_BOT_TOKEN_FILE.exists() or not TELEGRAM_CHAT_ID_FILE.exists():
        logging.info("Telegram not configured; skipping notification")
        return
    try:
        token = TELEGRAM_BOT_TOKEN_FILE.read_text().strip()
        chat_id = TELEGRAM_CHAT_ID_FILE.read_text().strip()
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_notification": "true" if silent else "false",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        urllib.request.urlopen(req, timeout=10).read()
        logging.info("Telegram alert sent (%d chars)", len(message))
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)


def run_subprocess(cmd: list[str], cwd: Path, log_path: Path, label: str) -> int:
    """Run a subprocess, tee output to log file. Return exit code."""
    logging.info("Running [%s]: %s", label, " ".join(str(c) for c in cmd))
    t0 = time.time()
    with open(log_path, "a") as logf:
        logf.write(f"\n===== {label} at {datetime.now(timezone.utc).isoformat()} =====\n")
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
    elapsed = time.time() - t0
    logging.info("[%s] completed in %.1fs (exit %d)", label, elapsed, result.returncode)
    return result.returncode


def latest_run_id_for_substrate(substrate_label: str) -> str | None:
    """Find the most recent run_id whose dir suggests it's from this substrate."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    try:
        # Heuristic: substrate "recent" → 30-day window, "s3_era" → 57-day window
        # Refine by matching window_start in runs table.
        substrate = next(s for s in SUBSTRATES if s["label"] == substrate_label)
        # Training window = opposite of validation window
        opposite = next(s for s in SUBSTRATES if s["label"] != substrate_label)
        train_start = opposite["val_start"]
        train_end = opposite["val_end"]
        row = conn.execute(
            "SELECT run_id FROM runs WHERE window_start = ? AND window_end = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (train_start, train_end),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def cross_regime_candidates() -> list[tuple[str, dict]]:
    """Query DB for candidates that pass the cross-regime gate.

    Returns list of (individual_hash, summary_dict). Each candidate appears once
    per OOS validation it survived; we collapse to unique hashes.
    """
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT individual_hash, training_run_id, val_window_start, val_window_end, "
        "train_metrics_json, val_metrics_json FROM oos_validations"
    ).fetchall()
    conn.close()

    survivors = {}
    for h, train_run, val_s, val_e, tm_json, vm_json in rows:
        tm = json.loads(tm_json)
        vm = json.loads(vm_json)

        def get(d, k):
            v = d.get(k)
            if isinstance(v, dict):
                return v.get("mean")
            return v if isinstance(v, (int, float)) else None

        ts = tm.get("stats", {})
        vs = vm.get("stats", vm)
        t_twe = get(ts, "total_wallet_exposure_max") or 0
        v_twe = get(vs, "total_wallet_exposure_max") or 0
        t_gain = get(ts, "gain_usd") or 0
        v_gain = get(vs, "gain_usd") or 0

        if (t_twe >= CROSS_REGIME_MIN_TWE and v_twe >= CROSS_REGIME_MIN_TWE
                and t_gain >= CROSS_REGIME_MIN_GAIN and v_gain >= CROSS_REGIME_MIN_GAIN):
            survivors[h] = {
                "training_run": train_run,
                "val_window": f"{val_s} → {val_e}",
                "train_twe": t_twe,
                "val_twe": v_twe,
                "train_gain": t_gain,
                "val_gain": v_gain,
                "train_sharpe": get(ts, "sharpe_ratio_pnl"),
                "val_sharpe": get(vs, "sharpe_ratio_pnl"),
            }
    return list(survivors.items())


def main():
    parser = argparse.ArgumentParser(description="Config Optimizer Daemon — nightly run")
    parser.add_argument("--top-n", type=int, default=5, help="OOS validate top-N per substrate (default: 5)")
    parser.add_argument("--skip-nsga2", action="store_true", help="Skip NSGA2 sweeps (just OOS + report)")
    parser.add_argument("--skip-telegram", action="store_true", help="Don't send Telegram alerts")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = LOG_DIR / f"daemon_{run_id}.log"
    setup_logging(log_path)
    logging.info("======================================================================")
    logging.info("Config Optimizer Daemon — nightly run %s", run_id)
    logging.info("======================================================================")

    try:
        # Phase 1: NSGA2 sweeps on each substrate
        if not args.skip_nsga2:
            for s in SUBSTRATES:
                rc = run_subprocess(
                    [str(PYTHON), str(OPTIMIZE_SCRIPT), str(s["config"]), "--log-level", "info"],
                    cwd=PASSIVBOT_ROOT,
                    log_path=log_path,
                    label=f"nsga2_{s['label']}",
                )
                if rc != 0:
                    raise RuntimeError(f"NSGA2 on substrate '{s['label']}' failed (exit {rc})")

        # Phase 2: Persist all new results
        rc = run_subprocess(
            [str(PYTHON), str(PERSIST_SCRIPT), "--all"],
            cwd=DAEMON_ROOT,
            log_path=log_path,
            label="persist",
        )
        if rc != 0:
            raise RuntimeError(f"persist_results failed (exit {rc})")

        # Phase 3: Cross-OOS-validate top-N from each substrate
        for s in SUBSTRATES:
            train_run = latest_run_id_for_substrate(s["label"])
            if not train_run:
                logging.warning("No training run found for substrate '%s'; skipping OOS", s["label"])
                continue
            logging.info("OOS-validating top-%d from %s against %s → %s",
                         args.top_n, train_run, s["val_start"], s["val_end"])
            rc = run_subprocess(
                [str(PYTHON), str(OOS_VALIDATE_SCRIPT),
                 "--training-run", train_run,
                 "--val-start", s["val_start"], "--val-end", s["val_end"],
                 "--top-n", str(args.top_n),
                 "--twe-cap", "1.0"],
                cwd=DAEMON_ROOT,
                log_path=log_path,
                label=f"oos_{s['label']}",
            )
            if rc != 0:
                logging.warning("OOS validation for %s exited %d (continuing)", s["label"], rc)

        # Phase 4: Report cross-regime survivors
        survivors = cross_regime_candidates()
        logging.info("Cross-regime survivors: %d candidates pass TWE >= %.2f and gain >= %.2f on both substrates",
                     len(survivors), CROSS_REGIME_MIN_TWE, CROSS_REGIME_MIN_GAIN)

        if survivors and not args.skip_telegram:
            lines = [
                f"*Config Optimizer Daemon* — {len(survivors)} cross-regime survivor(s)",
                f"Run ID: `{run_id}`",
                "",
                "Top survivors (passing TWE & gain gates on both substrates):",
            ]
            for h, s in survivors[:5]:
                lines.append(
                    f"`{h[:12]}` train_sharpe={s.get('train_sharpe', 0):.2f} "
                    f"val_sharpe={s.get('val_sharpe', 0):.2f} "
                    f"twe: {s['train_twe']:.2f}/{s['val_twe']:.2f} "
                    f"gain: {s['train_gain']:.2f}/{s['val_gain']:.2f}"
                )
            lines.append("")
            lines.append("Review in DB; James decides on Tier 3 promotion.")
            telegram_send("\n".join(lines))
        elif not survivors:
            logging.info("No cross-regime survivors this run. Daemon completed successfully.")
            # Don't spam Telegram for empty runs — only on hits or failures.

        logging.info("======================================================================")
        logging.info("Daemon run %s complete", run_id)
        logging.info("======================================================================")
        return 0

    except Exception as exc:
        logging.exception("Daemon run failed: %s", exc)
        if not args.skip_telegram:
            telegram_send(
                f"⚠️ *Config Optimizer Daemon FAILED*\n"
                f"Run ID: `{run_id}`\n"
                f"Error: `{exc}`\n"
                f"Log: `{log_path}`"
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
