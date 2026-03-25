#!/usr/bin/env python3
"""
Passivbot AutoResearch — LLM-Guided Config Optimiser
=====================================================
Karpathy-style overnight research loop adapted for Passivbot.

Unlike Passivbot's built-in NSGA2 optimizer (evolutionary, brute-force),
this uses LLM reasoning to propose hypothesis-driven config changes
and validates them via backtesting.

Each iteration:
  1. READ   — load current config JSON, optimize bounds, recent results
  2. PROPOSE — call Claude to generate one config hypothesis
  3. APPLY   — patch config JSON, git commit
  4. EVAL    — run Passivbot backtester, capture scoring metrics
  5. DECIDE  — keep if metrics improve, else git revert
  6. LOG     — append row to experiments/results.tsv

Run on Tau overnight:
    cd ~/Projects/trading-bots/passivbot
    nohup python autoresearch.py > experiments/autoresearch.log 2>&1 &

Or interactively (single experiment):
    python autoresearch.py --max-experiments 1 --verbose

Options:
    --max-experiments N   Stop after N experiments (default: 0 = infinite)
    --min-delta F         Min ADG improvement to keep (default: 0.0001)
    --config PATH         Config to optimise (default: configs/live/optimized.json)
    --verbose             INFO-level logging
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [pb-autoresearch] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler(sys.stdout)],
    )

logger = logging.getLogger('pb-autoresearch')


# ── Constants ─────────────────────────────────────────────────────────────────

SCRIPT_DIR      = Path(__file__).parent.resolve()
CONFIG_PATH     = SCRIPT_DIR / 'configs' / 'live' / 'optimized.json'
BASELINE_PATH   = SCRIPT_DIR / 'configs' / 'live' / 'baseline.json'
PROGRAM_PATH    = SCRIPT_DIR / 'autoresearch_program.md'
EXPERIMENTS_DIR = SCRIPT_DIR / 'experiments'
RESULTS_PATH    = EXPERIMENTS_DIR / 'pb_results.tsv'

# Sections that should NEVER be modified by the LLM
PROTECTED_PREFIXES = {
    'live.user',
    'live.balance_override',
    'live.leverage',
    'live.approved_coins',
    'backtest',
    'optimize',
    'logging',
    'coin_overrides',
}

# Only these sections are optimizable
OPTIMIZABLE_SECTIONS = {'bot.long', 'bot.short'}

TSV_HEADER = '\t'.join([
    'experiment', 'timestamp', 'hypothesis',
    'json_path', 'old_value', 'new_value',
    'adg_before', 'adg_after', 'delta', 'kept',
    'drawdown', 'loss_profit_ratio',
]) + '\n'


# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_config(data: dict, path: Path):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def get_nested(d: dict, dotpath: str):
    """Get a value using dot-notation path."""
    keys = dotpath.split('.')
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def set_nested(d: dict, dotpath: str, value):
    """Set a value using dot-notation path. Creates intermediate dicts."""
    keys = dotpath.split('.')
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def is_protected(json_path: str) -> bool:
    """Check if a json_path touches a protected section."""
    for prefix in PROTECTED_PREFIXES:
        if json_path == prefix or json_path.startswith(prefix + '.'):
            return True
    return False


def is_optimizable(json_path: str) -> bool:
    """Check if a json_path is in an optimizable section."""
    for section in OPTIMIZABLE_SECTIONS:
        if json_path.startswith(section + '.') or json_path == section:
            return True
    return False


def get_bounds_for_param(config: dict, json_path: str) -> tuple | None:
    """
    Look up the optimize.bounds for a given bot param.
    Passivbot bounds use flat names like 'long_ema_span_0' for 'bot.long.ema_span_0'.
    """
    bounds = config.get('optimize', {}).get('bounds', {})
    # Convert bot.long.ema_span_0 → long_ema_span_0
    flat_key = json_path.replace('bot.', '').replace('.', '_')
    if flat_key in bounds:
        return tuple(bounds[flat_key])
    return None


# ── Git helpers ───────────────────────────────────────────────────────────────

def git(args: list, cwd: Path = SCRIPT_DIR) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git'] + args,
        cwd=cwd.parent,  # trading-bots root
        capture_output=True,
        text=True,
    )


def git_commit(message: str) -> bool:
    git(['add', 'passivbot/configs/live/optimized.json'])
    result = git(['commit', '-m', message])
    if result.returncode != 0:
        logger.warning(f"git commit failed: {result.stderr.strip()}")
        return False
    logger.info(f"git commit: {message[:80]}")
    return True


def git_revert_last() -> bool:
    result = git(['reset', 'HEAD~1', '--hard'])
    if result.returncode != 0:
        logger.error(f"git revert failed: {result.stderr.strip()}")
        return False
    logger.info("git revert: experiment discarded")
    return True


def git_current_sha() -> str:
    result = git(['rev-parse', '--short', 'HEAD'])
    return result.stdout.strip() if result.returncode == 0 else 'unknown'


# ── Eval runner ───────────────────────────────────────────────────────────────

def run_backtest(config_path: Path) -> dict | None:
    """
    Run Passivbot backtester and return metrics dict or None on crash.
    Returns: {adg_pnl, drawdown_worst_usd, loss_profit_ratio, ...}
    """
    venv_python = SCRIPT_DIR / 'venv' / 'bin' / 'python'
    python = str(venv_python) if venv_python.exists() else sys.executable

    cmd = [
        python, str(SCRIPT_DIR / 'src' / 'passivbot.py'),
        'backtest',
        str(config_path),
    ]
    logger.info(f"Running backtest: {' '.join(cmd[-3:])}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
        timeout=600,
    )

    if result.returncode != 0:
        logger.warning(f"Backtest crashed (exit {result.returncode}): {result.stderr[-300:]}")
        return None

    stdout = result.stdout.strip()
    if not stdout:
        logger.warning("Backtest produced no stdout")
        return None

    # Try to parse the JSON metrics from stdout
    # Passivbot backtest outputs a JSON summary on the last line
    try:
        # Look for JSON block in output
        for line in reversed(stdout.split('\n')):
            line = line.strip()
            if line.startswith('{'):
                return json.loads(line)
        # Fallback: try parsing key=value pairs
        metrics = {}
        for line in stdout.split('\n'):
            if 'adg_pnl' in line or 'drawdown' in line:
                parts = line.split()
                for part in parts:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        try:
                            metrics[k] = float(v)
                        except ValueError:
                            metrics[k] = v
        if metrics:
            return metrics
        logger.warning(f"Could not parse backtest metrics from output")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse backtest output: {e}")
        return None


# ── Results log ───────────────────────────────────────────────────────────────

def init_results_log():
    EXPERIMENTS_DIR.mkdir(exist_ok=True)
    if not RESULTS_PATH.exists():
        with open(RESULTS_PATH, 'w') as f:
            f.write(TSV_HEADER)
        logger.info(f"Created results log: {RESULTS_PATH}")


def count_experiments() -> int:
    if not RESULTS_PATH.exists():
        return 0
    with open(RESULTS_PATH) as f:
        lines = [l for l in f if not l.startswith('experiment')]
    return len(lines)


def append_result(experiment: int, timestamp: str, hypothesis: str,
                  json_path: str, old_value, new_value,
                  adg_before: float, adg_after: float | None,
                  kept: bool, drawdown: float = 0, lpr: float = 0):
    delta = (adg_after - adg_before) if adg_after is not None else 'CRASH'
    row = '\t'.join([
        str(experiment), timestamp, hypothesis[:120],
        json_path, str(old_value), str(new_value),
        f'{adg_before:.6f}',
        f'{adg_after:.6f}' if adg_after is not None else 'CRASH',
        f'{delta:.6f}' if isinstance(delta, float) else delta,
        'KEPT' if kept else 'DISCARDED',
        f'{drawdown:.4f}', f'{lpr:.4f}',
    ]) + '\n'
    with open(RESULTS_PATH, 'a') as f:
        f.write(row)


def read_recent_results(n: int = 20) -> str:
    if not RESULTS_PATH.exists():
        return "(no results yet)"
    with open(RESULTS_PATH) as f:
        lines = f.readlines()
    header = lines[0] if lines else ''
    data = lines[1:]
    recent = data[-n:]
    return header + ''.join(recent)


# ── Baseline management ───────────────────────────────────────────────────────

def save_baseline(config: dict):
    save_config(config, BASELINE_PATH)
    logger.info(f"Baseline saved: {BASELINE_PATH}")


def load_baseline() -> dict | None:
    if BASELINE_PATH.exists():
        return load_config(BASELINE_PATH)
    return None


def restore_baseline():
    if BASELINE_PATH.exists():
        shutil.copy(BASELINE_PATH, CONFIG_PATH)
        logger.info("Baseline config restored")


# ── Claude API call ───────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str | None:
    """
    Call Claude via the claude CLI tool (same as orchestrator).
    Falls back to OpenRouter if CLI not available.
    """
    # Try claude CLI first
    try:
        result = subprocess.run(
            ['claude', '-p', prompt, '--model', 'sonnet', '--output-format', 'text'],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: OpenRouter
    import urllib.request
    import urllib.error

    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        logger.error("Neither claude CLI nor OPENROUTER_API_KEY available")
        return None

    url = 'https://openrouter.ai/api/v1/chat/completions'
    payload = json.dumps({
        'model': 'anthropic/claude-sonnet-4-20250514',
        'max_tokens': 1024,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        return body['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"LLM API error: {e}")
        return None


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt(current_config: dict, baseline_adg: float,
                 recent_results: str, experiment_number: int) -> str:
    # Extract bot params (the optimizable part)
    bot_config = json.dumps(current_config.get('bot', {}), indent=2)
    # Extract bounds for reference
    bounds = json.dumps(current_config.get('optimize', {}).get('bounds', {}), indent=2)
    # Extract scoring metrics
    scoring = current_config.get('optimize', {}).get('scoring', [])

    program = ""
    if PROGRAM_PATH.exists():
        program = PROGRAM_PATH.read_text()

    return f"""You are the research agent for an autonomous Passivbot parameter optimization loop.
Your job: propose ONE config change to Passivbot's grid/DCA market making parameters that will improve performance.

---
## CONTEXT

Passivbot is a grid/DCA market-making bot running on Hyperliquid perpetuals.
It places entry orders at intervals below (long) / above (short) the current price,
and close orders at profit targets. Key dynamics:
- entry_grid_spacing_pct: how far apart entry orders are
- entry_initial_qty_pct: size of first entry
- entry_grid_double_down_factor: how much to increase size on each deeper entry
- total_wallet_exposure_limit: max fraction of wallet at risk
- ema_span_0/1: EMA periods for entry signals
- unstuck_*: params for getting out of stuck positions
- close_trailing_*: trailing take-profit params
- entry_trailing_*: trailing entry params

{f"## PROGRAM (your instructions){chr(10)}{program}" if program else ""}

---
## CURRENT BOT CONFIG (bot section only — the optimizable part)

```json
{bot_config}
```

---
## PARAMETER BOUNDS (from optimize.bounds — stay within these)

```json
{bounds}
```

---
## SCORING METRICS: {', '.join(scoring)}
Primary: adg_pnl (average daily gain)
Constraints: drawdown_worst_usd < 0.9, loss_profit_ratio < 0.6

---
## EXPERIMENT HISTORY (last 20 rows)

```
{recent_results}
```

---
## YOUR TASK

This is experiment #{experiment_number}.
Current baseline ADG: {baseline_adg:.6f}

Propose ONE parameter change. Output ONLY a JSON block — no other text:

```json
{{
  "hypothesis": "One sentence explaining the expected effect on grid behavior and ADG.",
  "json_path": "bot.long.some_param_name",
  "old_value": <current value>,
  "new_value": <proposed value>
}}
```

Rules:
- json_path must start with "bot.long." or "bot.short."
- Do NOT repeat a change already tried and discarded
- Stay WITHIN the optimize.bounds ranges
- Only change numeric values
- Consider interaction effects (e.g., wider spacing + larger qty = same exposure)
- The JSON must be valid and parseable
"""


# ── JSON extractor ────────────────────────────────────────────────────────────

def extract_proposal(response: str) -> dict | None:
    """Extract the JSON proposal from the LLM response."""
    patterns = [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'(\{[^{}]*"hypothesis"[^{}]*\})',
    ]
    for pat in patterns:
        m = re.search(pat, response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        return None


def validate_proposal(proposal: dict, config: dict) -> str | None:
    """Validate proposal. Returns error string or None if valid."""
    required = {'hypothesis', 'json_path', 'old_value', 'new_value'}
    missing = required - proposal.keys()
    if missing:
        return f"Missing fields: {missing}"

    json_path = proposal['json_path']
    if is_protected(json_path):
        return f"Protected path: {json_path}"
    if not is_optimizable(json_path):
        return f"Not optimizable: {json_path} (must be in bot.long.* or bot.short.*)"

    current = get_nested(config, json_path)
    if current is None:
        return f"json_path not found in config: {json_path}"

    # Check bounds
    bounds = get_bounds_for_param(config, json_path)
    if bounds:
        try:
            new_val = float(proposal['new_value'])
            if new_val < bounds[0] or new_val > bounds[1]:
                return f"new_value {new_val} outside bounds [{bounds[0]}, {bounds[1]}] for {json_path}"
        except (ValueError, TypeError):
            pass

    # Verify old_value matches
    try:
        if abs(float(current) - float(proposal['old_value'])) > 1e-6:
            return (f"old_value mismatch for {json_path}: "
                    f"expected {current!r}, got {proposal['old_value']!r}")
    except (ValueError, TypeError):
        if str(current) != str(proposal['old_value']):
            return f"old_value mismatch: {current!r} vs {proposal['old_value']!r}"

    if str(proposal['old_value']) == str(proposal['new_value']):
        return "old_value == new_value — no change"

    return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Passivbot AutoResearch — LLM-guided config optimiser')
    parser.add_argument('--max-experiments', type=int, default=0,
                        help='Max experiments to run (0 = infinite)')
    parser.add_argument('--min-delta', type=float, default=0.0001,
                        help='Min ADG improvement to keep change (default: 0.0001)')
    parser.add_argument('--config', type=str, default='configs/live/optimized.json',
                        help='Config file to optimise')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
                        help='Propose changes but skip backtest and git')
    args = parser.parse_args()

    setup_logging(args.verbose)
    os.chdir(SCRIPT_DIR)

    config_path = SCRIPT_DIR / args.config
    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        sys.exit(1)

    init_results_log()

    logger.info("=" * 60)
    logger.info("Passivbot AutoResearch starting")
    logger.info(f"  Config:      {config_path}")
    logger.info(f"  Min delta:   {args.min_delta}")
    logger.info(f"  Max exps:    {args.max_experiments or 'infinite'}")
    logger.info(f"  Dry run:     {args.dry_run}")
    logger.info("=" * 60)

    # ── Establish baseline ────────────────────────────────────────────────────
    baseline_config = load_config(config_path)
    saved_baseline = load_baseline()

    if args.dry_run:
        baseline_adg = 0.001  # Placeholder for dry-run
        logger.info(f"Dry-run mode — using placeholder ADG: {baseline_adg}")
    elif saved_baseline is None:
        logger.info("No baseline found — running initial backtest...")
        metrics = run_backtest(config_path)
        if metrics is None:
            logger.error("Initial backtest failed — check Passivbot setup")
            sys.exit(1)
        baseline_adg = metrics.get('adg_pnl', metrics.get('adg', 0))
        save_baseline(baseline_config)
        logger.info(f"Baseline ADG: {baseline_adg:.6f}")
        append_result(
            experiment=0, timestamp=datetime.utcnow().isoformat(),
            hypothesis='Initial baseline', json_path='—',
            old_value='—', new_value='—',
            adg_before=baseline_adg, adg_after=baseline_adg,
            kept=True,
            drawdown=metrics.get('drawdown_worst_usd', 0),
            lpr=metrics.get('loss_profit_ratio', 0),
        )
    else:
        logger.info("Baseline config exists — evaluating current config...")
        metrics = run_backtest(config_path)
        if metrics is None:
            logger.error("Baseline backtest failed")
            sys.exit(1)
        baseline_adg = metrics.get('adg_pnl', metrics.get('adg', 0))
        logger.info(f"Current ADG: {baseline_adg:.6f}")

    exp_number = count_experiments()

    # ── Research loop ─────────────────────────────────────────────────────────
    try:
        while True:
            exp_number += 1
            if args.max_experiments > 0 and exp_number > args.max_experiments:
                logger.info(f"Reached max experiments ({args.max_experiments}) — stopping")
                break

            logger.info(f"\n{'─'*60}")
            logger.info(f"Experiment #{exp_number}  |  baseline ADG: {baseline_adg:.6f}")
            logger.info(f"{'─'*60}")

            # 1. Load current config + recent results
            current_config = load_config(config_path)
            recent_results = read_recent_results(n=20)

            # 2. Ask LLM for a proposal
            prompt = build_prompt(current_config, baseline_adg, recent_results, exp_number)
            response = call_claude(prompt)

            if response is None:
                logger.warning("LLM call failed — skipping experiment")
                time.sleep(30)
                continue

            logger.debug(f"LLM response:\n{response}")

            # 3. Parse and validate proposal
            proposal = extract_proposal(response)
            if proposal is None:
                logger.warning("Could not parse JSON proposal — skipping")
                logger.debug(f"Raw response: {response[:500]}")
                continue

            error = validate_proposal(proposal, current_config)
            if error:
                logger.warning(f"Invalid proposal ({error}) — skipping")
                continue

            hypothesis = proposal['hypothesis']
            json_path = proposal['json_path']
            old_val = proposal['old_value']
            new_val = proposal['new_value']

            logger.info(f"Hypothesis: {hypothesis}")
            logger.info(f"Change:     {json_path}: {old_val!r} → {new_val!r}")

            if args.dry_run:
                logger.info("DRY RUN — skipping backtest and git")
                append_result(exp_number, datetime.utcnow().isoformat(),
                              hypothesis, json_path, old_val, new_val,
                              baseline_adg, None, kept=False)
                continue

            # 4. Apply change to config
            try:
                current_val = get_nested(current_config, json_path)
                if isinstance(current_val, float):
                    new_val = float(new_val)
                elif isinstance(current_val, int):
                    new_val = int(float(new_val))
            except (ValueError, TypeError):
                pass

            set_nested(current_config, json_path, new_val)
            save_config(current_config, config_path)

            # 5. Git commit
            commit_msg = f"pb-exp {exp_number}: {json_path} {old_val!r}→{new_val!r} | {hypothesis[:60]}"
            if not git_commit(commit_msg):
                logger.warning("Commit failed — restoring and skipping")
                restore_baseline()
                continue

            sha = git_current_sha()
            logger.info(f"Committed: {sha}")

            # 6. Run backtest
            timestamp = datetime.utcnow().isoformat()
            metrics = run_backtest(config_path)

            # 7. Decide: keep or revert
            if metrics is None:
                logger.warning("Backtest crashed — reverting")
                git_revert_last()
                restore_baseline()
                append_result(exp_number, timestamp, hypothesis,
                              json_path, old_val, new_val,
                              baseline_adg, None, kept=False)

            else:
                new_adg = metrics.get('adg_pnl', metrics.get('adg', 0))
                drawdown = metrics.get('drawdown_worst_usd', 0)
                lpr = metrics.get('loss_profit_ratio', 0)

                # Check constraints
                constraint_violated = False
                if drawdown > 0.9:
                    logger.warning(f"Constraint violated: drawdown {drawdown:.4f} > 0.9")
                    constraint_violated = True
                if lpr > 0.6:
                    logger.warning(f"Constraint violated: loss_profit_ratio {lpr:.4f} > 0.6")
                    constraint_violated = True

                if not constraint_violated and new_adg >= baseline_adg + args.min_delta:
                    delta = new_adg - baseline_adg
                    logger.info(f"Result: KEPT  ✓  ADG {baseline_adg:.6f} → {new_adg:.6f} (+{delta:.6f})")
                    baseline_adg = new_adg
                    save_baseline(current_config)
                    append_result(exp_number, timestamp, hypothesis,
                                  json_path, old_val, new_val,
                                  baseline_adg - delta, new_adg, kept=True,
                                  drawdown=drawdown, lpr=lpr)
                else:
                    delta = new_adg - baseline_adg
                    reason = "constraint" if constraint_violated else f"delta {delta:+.6f} < {args.min_delta}"
                    logger.info(f"Result: DISCARDED  ADG {baseline_adg:.6f} → {new_adg:.6f} ({reason})")
                    git_revert_last()
                    restore_baseline()
                    append_result(exp_number, timestamp, hypothesis,
                                  json_path, old_val, new_val,
                                  baseline_adg, new_adg, kept=False,
                                  drawdown=drawdown, lpr=lpr)

            # Brief pause between experiments
            time.sleep(2)

    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")

    # ── Summary ───────────────────────────────────────────────────────────────
    total = count_experiments() - 1
    kept_count = 0
    if RESULTS_PATH.exists():
        with open(RESULTS_PATH) as f:
            kept_count = sum(1 for l in f if '\tKEPT\t' in l)

    logger.info("=" * 60)
    logger.info("Passivbot AutoResearch complete")
    logger.info(f"  Experiments run:   {exp_number - 1}")
    logger.info(f"  Improvements kept: {kept_count}")
    logger.info(f"  Final ADG:         {baseline_adg:.6f}")
    logger.info(f"  Results log:       {RESULTS_PATH}")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
