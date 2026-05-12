# Config Optimizer Daemon — Phase 1 Scaffold

**Date:** 2026-05-12
**Status:** ✅ Daemon orchestrator working end-to-end. Nightly cycle (NSGA2 × 2 substrates → persist → cross-OOS → survivor gate → Telegram) runs in 30-60s. Launchd plist authored, deployment pending.

## Files

| File | Purpose |
|------|---------|
| `smoke_config.json` | Tiny budget (10 iters, pop 5, 4 cpus). Sanity testing. |
| `real_run_config.json` | Real budget (500 iters, pop 50, 8 cpus). Apr 10 → May 10 substrate. |
| `s3_substrate_config.json` | Same budget. Dec 26 → Feb 21 (S3 era) substrate. |
| `multi_substrate_config.json` | Suite mode (both substrates in one run). **Currently degenerate — see notes below.** |
| `daemon_run.py` | Nightly orchestrator: NSGA2 × 2 → persist → OOS → survivor gate → Telegram. |
| `persist_results.py` | Idempotent SQLite ingester for `optimize_results/{run}/pareto/*.json`. |
| `oos_validate.py` | Top-N OOS validation across an arbitrary window. |
| `config_optimizer.db` | SQLite (runs + individuals + oos_validations). Gitignored as regenerable. |
| `../plists/com.tradingbots.config-optimizer-daemon.plist` | Launchd entry, 03:00 PDT daily. Deployment pending. |

## How to run end-to-end

**Full nightly cycle (one invocation):**
```bash
/Users/5tuktau/Projects/trading-bots/passivbot/venv/bin/python \
  /Users/5tuktau/Projects/trading-bots/.claude/worktrees/romantic-booth-83e2cb/config_optimizer_daemon/daemon_run.py
```

Runs: NSGA2 on substrate A → NSGA2 on substrate B → persist → cross-OOS-validate top-5 each → survivor gate → Telegram digest. ~30-60s total. Logs to `logs/daemon_{run_id}.log`.

**Useful flags:**
- `--skip-nsga2` — skip the expensive sweeps, just re-run OOS+report against existing data
- `--skip-telegram` — no notifications (testing)
- `--top-n N` — validate top N per substrate (default: 5)

**Cross-regime promotion gate (configurable in `daemon_run.py`):**
- `CROSS_REGIME_MIN_TWE = 0.10` — must trade at >10% wallet on BOTH substrates
- `CROSS_REGIME_MIN_GAIN = 0.95` — allow modest losses, filter disasters

A candidate must clear both gates on training AND validation substrate to be reported. With current data, expect **0 survivors** — the TWE-collapse finding means no S3-trained config trades on recent (and vice versa). That's the correct filter behavior, not a bug.

**Multi-substrate suite mode (`multi_substrate_config.json`) is currently degenerate.** Passivbot's time-based constraints (`position_held_hours_max < 1344`) are calibrated for single-window backtests. The S3 substrate is 57 days (1368 hours), so mean-aggregating across two windows violates the limit by construction → all candidates fail constraints → Pareto front of size 1 with penalty objectives. v2 work: per-substrate limit tuning or shorter S3 window.

## What this is

---

## What this is

System 1 of the Daemon + Karpathy plan ([research/daemon-orch-action-plan.md](../research/daemon-orch-action-plan.md)). Wraps Passivbot's NSGA2 optimizer (`src/optimize.py`, 1940 lines) instead of reimplementing search. Generates candidate configs mechanically, scores via backtest on the local S3+live OHLCV archive, persists Pareto fronts.

## Files

- `smoke_config.json` — Constrained NSGA2 config (n_cpus=4, iters=10, population=5). Derived from `passivbot/configs/live/optimized.json` with: source_dir set, dates constrained to post-gap window (2026-03-25 → 2026-05-10), suite scenarios stripped.
- `README.md` — this file.

## Phase 1 progress

### What works now

| Capability | Status |
|------------|--------|
| Live config `ohlcv_source_dir: "caches/ohlcv"` added (Tier 3, James-approved 2026-05-12) | ✅ |
| Backtester reads cached `.npy` files via source_dir loader | ✅ |
| `HLCVManager.get_ohlcvs("BTC")` returns 112 days of data (no Binance fallback) | ✅ |
| BTC benchmark source resolves from Hyperliquid (`using BTC/USD benchmark source: hyperliquid`) | ✅ |
| NSGA2 multi-objective optimization runs (deap installed) | ✅ |
| Real-budget run: 537 evals × 10 gens, 100 Pareto-front individuals in 7.3s | ✅ |
| Pareto fronts persisted as `optimize_results/{run}/pareto/{hash}.json` (one file per individual) | ✅ |
| SQLite DB with runs + individuals tables; idempotent `persist_results.py --latest \| --all` | ✅ |
| DB queryable for top candidates by any metric | ✅ |

### How NSGA2 was unblocked

`deap==1.4.3` was listed in `passivbot/requirements.txt` but not installed in `passivbot/venv/`. James approved Tier 3 install (`./venv/bin/pip install deap==1.4.3`) on 2026-05-12. Venv now in compliance with documented requirements.

### Smoke run artifacts

Output at `/Users/5tuktau/Projects/trading-bots/passivbot/optimize_results/2026-05-12T17_26_31_hyperliquid_46days_ARB_BTC_ETH_SOL_291cba52/`:
- `all_results.bin` — msgpack-packed raw history of all 15 evaluations
- `pareto/{hash}.json` — one JSON per Pareto-optimal config (currently 2 files)

Directory naming convention (`hyperliquid_46days_ARB_BTC_ETH_SOL_{hash}`) confirms the gap problem: of 14 approved coins, only 4 (ARB, BTC, ETH, SOL) survived the gap-tolerance filter on the Mar 25 → May 10 window. See "Coin coverage" below.

## Headline finding: S3-trained configs don't trade in the recent window

OOS validation (Step 5) of the top 5 S3-substrate Pareto candidates against the Apr 10 → May 10 window:

| hash | train_sharpe (S3) | val_sharpe (Apr-May) | train_gain | val_gain | train_twe | **val_twe** |
|------|--------|--------|--------|--------|--------|---------|
| f01808b4dd7c | 0.863 | 0.697 | 0.811 (−19%) | 1.000 (flat) | 0.887 | **0.022** |
| d613d82c9bd5 | 0.825 | 0.880 | 0.813 | 1.001 | 0.691 | **0.017** |
| 1f1a6b04b8a6 | 0.811 | 0.697 | 0.813 | 1.000 | 0.893 | **0.022** |
| bd4ea95344b2 | 0.702 | 0.690 | 0.803 | 1.001 | 0.896 | **0.019** |
| 382f6a8fa4c1 | 0.611 | 0.789 | 0.826 | 1.002 | 0.945 | **0.078** |

**TWE collapses 40× across regimes.** Configs that used 87-95% of wallet exposure in S3 era use only 1-8% in the recent window. The bot effectively doesn't open positions. Sharpe values look similar (~0.6-0.88) but they're measured on essentially flat equity curves, so the comparison is meaningless.

**What this means:**
- Configs optimized on Dec 26 → Feb 21 do not translate to Apr 10 → May 10. The entry filters/conditions that fired in S3 era almost never fire in recent.
- This is exactly the kind of regime sensitivity the daemon was designed to detect. The OOS step is doing its job.
- Possible drivers: different market vol regimes, different coin liquidity profiles (S3 = BTC/ETH/SOL; recent = +ARB), or Passivbot's forager filters reacting differently to recent volume/spread patterns.
- **Implication for daemon roadmap:** the daemon shouldn't promote candidates based on training-substrate metrics alone. Cross-regime stability (i.e., both substrates produce reasonable trading activity AND positive metrics) becomes a promotion criterion.

This is a useful negative result — Pareto-front-on-S3 is not enough to promote.

## Substrate landscape (corrected from Workstream C scoping)

| Window | Days | Coins available | Notes |
|--------|------|----------------|-------|
| Dec 26 → Feb 21 | 57 | **BTC, ETH, SOL only** | S3-era. Workstream C said all 14 coins; the cache only has 3. |
| Feb 22 → Mar 19 | ~26 | none (gap) | No data source |
| Mar 19/22 → Apr 10 | ~16 | BTC, ETH, SOL only | Continuation; newer coins have intra-window gaps |
| Apr 10 → May 12 | 32 | BTC, ETH, SOL, ARB (4 of 14) | Other 10 coins blocked by warmup-zone gap detection (see below) |

The daemon currently has access to **two trainable substrates**, both of which are 3-4 coin BTC/ETH/SOL-centric. The 14-coin full-substrate is only available going forward (live-bot downloader is filling the cache).

## Top Pareto candidates from real run (Apr 10 → May 10, BTC/ETH/SOL/ARB)

Top 8 individuals by `adg_pnl` (average daily gain on PnL basis):

| adg_pnl | sharpe | sortino | gain_usd | twe_max | dd_worst | hash |
|---------|--------|---------|----------|---------|----------|------|
| 0.0178 | 0.610 | 15.2 | 1.07 | 0.983 | 0.064 | 3bf443926f6b... |
| 0.0172 | 0.619 | 11.5 | 1.07 | 0.981 | 0.064 | ec9a143dd219... |
| **0.0108** | **0.836** | **323** | **1.05** | **0.416** | **0.035** | **97b877a40ebb...** |
| 0.0101 | 0.817 | 274 | 1.04 | 0.552 | 0.039 | d77b1a6df822... |
| 0.0097 | 1.030 | 0.0 | 1.03 | 0.444 | 0.048 | 081a6d733cc1... |
| 0.0086 | 1.110 | 0.0 | 1.03 | 0.524 | 0.031 | c9774b19558c... |
| 0.0080 | 0.502 | 2.74 | 1.06 | 0.926 | 0.069 | 2a64daaf19e7... |
| 0.0079 | 1.000 | 0.0 | 1.02 | 0.444 | 0.048 | 420834ad81e9... |

Bolded row = best Sharpe with bounded exposure (TWE < 50%). Sortino of 323 is suspicious — likely an artifact of zero downside variance over a 30-day single-regime window. Worth investigating, but it's exactly what walk-forward CV will catch.

These are NOT promotable yet — single substrate (30 days, one regime), only 4 coins, no OOS validation. The infrastructure is what's validated, not the results.

## Known issue: warmup-expanded gap detection

Even with `start_date: 2026-04-10`, NSGA2 only loaded data for 4 coins (BTC/ETH/SOL/ARB) — same as on the Mar 25 → May 10 window. The cause: gap detection runs over the *warmup-expanded* range, not just the requested backtest range. Per-coin EMA warmups (`ema_span_0: 968`, `ema_span_1: 1070`) pull data back ~18 hours, plus `minimum_coin_age_days` checks pull back further. The Mar 25 → Apr 10 gap (23,542 min for newer coins) falls within the warmup zone and trips the gap-tolerance threshold.

**Options to investigate (separate task, not blocking):**
1. Reduce `ema_span_*` bounds in the optimize section so warmup is shorter
2. Loosen `gap_tolerance_ohlcvs_minutes` from 120 → higher (less safety but more data)
3. Push backtest start_date to Apr 12 or later to give warmup buffer past the gap end
4. Patch `prepare_hlcvs_internal` to only gap-check the requested window, not the warmup zone

## New finding from the smoke run: a second data gap

The Workstream C scoping identified the Feb 22 → Mar 19 gap. The NSGA2 smoke run revealed a **second gap**: many of the newer approved coins (HYPE, AAVE, AVAX, DOGE, ENA, LINK, NEAR, SUI, WLD, XRP, ZEC) have a 23,542-minute gap (~16 days) between **2026-03-25 and 2026-04-10**.

Result: even on the post-gap window (Mar 25 → May 10), only 4 of 14 coins pass the gap-tolerance check and get included in the unified array:
- BTC, ETH, SOL (full coverage Dec 26 → May 12)
- One additional coin (likely the smallest gap among the rest)

10 of 14 approved coins are unusable for backtest until either:
1. The Mar 25 → Apr 10 gap is bridged (HL account fill history reconstruction), OR
2. The backtest start_date is moved to Apr 10 or later (~30 days of usable window, vs 46 days)
3. The `gap_tolerance_ohlcvs_minutes` is widened (currently 120 = 2 hours; would need to be ~24,000 to swallow the gap, but that erases the gap-detection safety entirely)

**Recommendation for daemon Phase 1:** Run on the Apr 10 → May 10 window (~30 days, 14 coins, no gaps). For walk-forward CV, start with this single 30-day fold; expand as data fills in.

## Next concrete steps

### Step 1 — Switch window to Apr 10 → May 10 for full 14-coin coverage
Edit `smoke_config.json` `backtest.start_date` to `"2026-04-10"`. Re-run. Should include all 14 approved coins (no gap-affected window).

### Step 2 — Larger NSGA2 budget for a real run
Scale up `optimize.iters` from 10 → 1000 or more, `population_size` from 5 → 50, `n_cpus` from 4 → 12. Wall time at current rate (~0.13s/eval): 1000 evals × 0.13 = ~130s. With richer multi-objective scoring, expect 2-5× slower per eval → 5-10 minutes per run.

### Step 3 — Persistence layer (~80 LoC Python)
Read `optimize_results/{run_id}/pareto/*.json` + `all_results.bin` (msgpack), write to daemon SQLite DB at `config_optimizer.db` with schema:
```sql
runs(id, ts_start, ts_end, window_start, window_end, coins, gen_count, total_evals)
individuals(run_id, eval_id, params_json, scoring_json, in_pareto, hash)
pareto_history(run_id, hash, dominated_at_eval)
```

### Step 4 — Walk-forward CV harness (~100 LoC)
Wrap optimize.py call in a loop that runs NSGA2 on N non-overlapping windows. Compare Pareto fronts across windows to find configs that dominate cross-fold. Initial substrate: 3 × 10-day windows within Apr 10 → May 10.

### Step 5 — OOS validation against S3 era
Take top Pareto-front configs from training and backtest them on Dec 26 → Feb 21 (S3 archive). If a config dominates on both substrates → cross-regime robust. This is the "no-bridge" alternative to gap reconstruction.

### Step 6 — Basic dashboard (HTML, ~150 LoC)
- Pareto front scatter (Sharpe vs MaxDD, colored by recency)
- Hypervolume over generations
- Per-coin contribution heatmap
- Current live config delta vs best Pareto candidate

### Step 7 — launchd plist for scheduled daemon runs
Once Steps 3-6 are validated, schedule overnight runs (e.g., 2am PDT, before orch-eval at 4am). One run per night, ~50-100 evals each. Results accumulate in DB.

### Step 8 — `orch-planning` integration
`orch-planning` (11am PDT) reads `config_optimizer.db`, surfaces "best Pareto candidate dominates current live by X%" findings in daily plan. James reviews. Tier 3 promotion via manual config swap.

## What's NOT touched

- Live Passivbot process, plist, capital, positions
- Forward-collection cron (not needed — live bot downloads continue to fill the cache)
- CoinGecko / Binance fallback wiring (not needed — HL source_dir works)
- The `_archived` retirement of `autoresearch.py` (still pending, per original plan; not blocking)

## Open questions for James

1. ~~Tier 3: pip install deap==1.4.3 in passivbot venv?~~ ✅ Approved + installed 2026-05-12.
2. ~~Daemon CV substrate decision~~ — Recommendation: skip bridging, use Apr 10 → May 10 (clean 14-coin window) for training + Dec 26 → Feb 21 (S3 archive) for OOS validation. Awaiting confirmation.
3. **Greenlight Steps 1-3** (full-coin window + larger budget + SQLite persistence)? Estimated 4-6 hours of work.
4. **Daemon final home:** keep scaffold in this worktree, or graduate to `trading-bots/passivbot/daemon/` once Step 3 is done?
