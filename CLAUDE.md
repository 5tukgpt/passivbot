# Passivbot — Operator Notes

Grid-trading bot running on Hyperliquid perps. Rust computes orders; Python orchestrates exchange I/O. This file is your project context — read it plus `AGENTS.md` before doing anything non-trivial.

## Current status (as of 2026-05-02)

- **Exchange:** Hyperliquid. Stake currency USDC.
- **Mode:** Live (paper-equivalent via small capital). `balance_override: 1108.0` in `configs/live/optimized.json`.
- **Approved coins (14):** BTC, ETH, SOL, HYPE, XRP, NEAR, SUI, AAVE, DOGE, AVAX, LINK, ARB, WLD, ENA. Configured in `configs/approved_coins.json` and referenced by `configs/live/optimized.json`.
- **Leverage:** 10×.
- **Supervision:** launchd plist (see `../plists/`). Same stop/start discipline as gooner_bot — unload before kill.

## Domain concepts

- **TWEL (Total Wallet Exposure Limit):** The maximum fraction of wallet balance that can be deployed across all positions on one side (long or short). Configured per-side in the bot params as `total_wallet_exposure_limit`. CLI overrides: `-ltwel 1.5` (long) / `-stwel 1.5` (short). A TWEL of 0 disables that side entirely. The bot logs current TWEL in its banner.
- **Wallet exposure (WE):** A single position's notional value divided by wallet balance. The sum of all WEs on a side must stay ≤ TWEL. Entry orders are sized so that the resulting WE stays within budget.
- **Forager mode vs clock mode:** When `n_positions < len(approved_coins)` for a side, the bot enters **forager mode** — it dynamically selects which coins to trade based on trailing price action, volatility, and available WE slots. When `n_positions ≥ len(approved_coins)`, every approved coin gets a slot (**clock mode**). Forager rotates into favorable setups; clock holds all coins simultaneously.
- **balance_override:** Hyperliquid's `marginSummary` only reports the perp sub-account balance. If funds sit in spot or vault, the bot under-sizes. `balance_override` in `config.live` hard-codes the effective balance so sizing uses the intended capital. Current value: `1108.0`.
- **Ghost positions:** When the bot's internal state diverges from exchange state (e.g. after a manual close, partial fill, or connectivity gap), reduce-only close orders can target a position that no longer exists. Hyperliquid rejects these with *"Reduce only order would increase position"*. The bot recovers on the next reconciliation cycle — no manual intervention needed, but the log warning is expected.
- **Unstucking:** When a position is underwater beyond `unstuck_threshold` and the side's cumulative realized loss is within `unstuck_loss_allowance_pct × TWEL × balance`, the Rust engine places a close order at EMA distance (`unstuck_ema_dist`) to bleed the position down incrementally rather than taking the full loss at once. Parameters: `unstuck_close_pct`, `unstuck_ema_dist`, `unstuck_loss_allowance_pct`, `unstuck_threshold`.
- **EMA grid behavior:** Entries use a grid of limit orders spaced by EMA-derived thresholds. `entry_trailing_threshold_pct` sets how far price must move past EMA before the grid activates. `entry_trailing_retracement_pct` sets how much price must retrace from the extreme before the entry fills. Volatility and WE weights scale these dynamically. The grid vs trailing ratio is controlled by `entry_trailing_grid_ratio` (0 = pure grid, 1 = pure trailing).
- **Signed qty convention:** `qty` and `pos_size` are **signed** internally (negative = short). Use `abs()` only for exchange payloads. `position_side`/`pside` = long/short; `side`/`order_side` = buy/sell. Mixing these is a common bug source.

## Key files

- `AGENTS.md` — Coding-assistant instructions (read before `docs/ai/`).
- `configs/live/optimized.json` — Active live config (TWEL, coins, balance_override, bot params).
- `configs/approved_coins.json` — The 14-symbol allow-list.
- `src/passivbot.py` — Live loop: reconciliation, order execution, forager logic.
- `passivbot-rust/src/` — Source of truth for order behavior (entries, closes, risk, unstuck, backtest).
- `src/suite_runner.py` — Suite/optimizer runner, handles approved_coins filtering.
- `docs/ai/` — Task-specific agent docs. Start with `README.md`, load only what's relevant.
- `api-keys.json` — Credentials. **Never commit.**
- `logs/hl_daily_metrics.json` — Daily metric snapshots.

## What to do / not do

**Do:** read `AGENTS.md` + `docs/ai/principles.yaml` before coding. Implement behavior changes in Rust. Fail loudly in trading-critical paths. Run targeted tests. Keep EMA spans as floats.

**Don't:** patch order behavior in Python only. Silently catch exceptions in exchange fetch methods. Round derived EMA spans. Add runtime state that can't be rederived from exchange state + config. Commit `api-keys.json`.

## Context outside this file

- `docs/ai/error_contract.md` — Fallback matrix for every critical path.
- `docs/ai/exchange_api_quirks.md` — Pagination/data gotchas per exchange.
- `docs/ai/pitfalls.md` — Common implementation mistakes with examples.
- `docs/ai/features/balance_routing.md` — Raw vs snapped balance contract.
- Root `../CLAUDE.md` — Cross-project rules and subproject map.
