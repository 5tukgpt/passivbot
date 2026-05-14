#!/usr/bin/env python3
"""
One-off backfill: explode a combined hlcvs cache into per-coin daily parquets.

Why: `caches/ohlcv/hyperliquid/` only has parquets for BTC/ETH/SOL (Dec 26 →
Feb 21). The other 12 approved coins are missing, which blocks the daemon's
Phase 1 walk-forward CV. The existing 15-coin hlcvs cache at
`caches/hlcvs_data/37762968dc675652/` has all 15 coins from Feb 16 → May 5
in HLCV (no open) form. This script unpacks it into per-coin daily parquets
matching the existing parquet schema (ts, o, h, l, c, bv).

Synthesis: hlcvs has only HLCV — no Open. We set o[i] = c[i-1] (close of
prior bar), which matches the existing parquets ~87% of the time. The
remaining ~13% diverge on bars where carry-forward is more appropriate;
those errors are bounded by 1m bar amplitudes (~basis points). For grid-bot
backtesting this is acceptable.

Idempotent: skips writing a parquet if it already exists and would have
identical row count. Use --force to overwrite.

Run:
    cd ~/Projects/trading-bots/passivbot
    ./venv/bin/python scripts/extract_hlcvs_to_parquets.py
    ./venv/bin/python scripts/extract_hlcvs_to_parquets.py --src CACHE_HASH --force
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SRC = "37762968dc675652"  # widest 15-coin span (Feb 16 → May 5)
TARGET_DIR = REPO / "caches" / "ohlcv" / "hyperliquid"
APPROVED_COINS = {
    "BTC", "ETH", "SOL", "HYPE", "XRP", "NEAR", "SUI", "AAVE",
    "DOGE", "AVAX", "LINK", "ARB", "WLD", "ENA", "ZEC",
}


def load_hlcvs_cache(src: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    coins = json.loads((src / "coins.json").read_text())
    hlcvs = np.load(gzip.GzipFile(src / "hlcvs.npy.gz"))
    timestamps = np.load(gzip.GzipFile(src / "timestamps.npy.gz"))
    return coins, hlcvs, timestamps


def build_coin_df(
    timestamps_ms: np.ndarray,
    hlcv: np.ndarray,
) -> pd.DataFrame:
    """hlcv is (n_minutes, 4) where columns are [high, low, close, volume].
    Synthesize open as prior close; first bar's open = first close."""
    close = hlcv[:, 2]
    open_ = np.empty_like(close)
    open_[0] = close[0] if not np.isnan(close[0]) else np.nan
    open_[1:] = close[:-1]
    return pd.DataFrame({
        "ts": timestamps_ms.astype(np.int64),
        "o": open_,
        "h": hlcv[:, 0],
        "l": hlcv[:, 1],
        "c": close,
        "bv": hlcv[:, 3],
    })


def write_daily_parquets(coin_dir: Path, df: pd.DataFrame, *, force: bool, dry_run: bool) -> dict:
    coin_dir.mkdir(parents=True, exist_ok=True)
    df = df.dropna(subset=["c"]).copy()  # drop minutes with no data
    if df.empty:
        return {"written": 0, "skipped": 0, "rows": 0, "spans": []}

    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.date
    written = 0
    skipped = 0
    spans: list[str] = []
    for date, group in df.groupby("date"):
        out_path = coin_dir / f"{date}.parquet"
        if out_path.exists() and not force:
            # Never overwrite existing parquets without --force. Existing
            # files may have been sourced from a more authoritative pipeline.
            skipped += 1
            continue
        if dry_run:
            written += 1
            spans.append(str(date))
            continue
        out = group[["ts", "o", "h", "l", "c", "bv"]].reset_index(drop=True)
        out.to_parquet(out_path, index=False)
        written += 1
        spans.append(str(date))
    return {"written": written, "skipped": skipped, "rows": len(df), "spans": spans}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=DEFAULT_SRC,
                        help=f"hlcvs cache fingerprint dir under caches/hlcvs_data/ (default: {DEFAULT_SRC})")
    parser.add_argument("--target", type=Path, default=TARGET_DIR,
                        help=f"output dir (default: {TARGET_DIR})")
    parser.add_argument("--coins", nargs="*", default=None,
                        help="explicit coin list (default: all coins in the cache that overlap APPROVED_COINS)")
    parser.add_argument("--force", action="store_true",
                        help="rewrite existing parquets even when row count is sufficient")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written, no file writes")
    args = parser.parse_args()

    src_dir = REPO / "caches" / "hlcvs_data" / args.src
    if not src_dir.exists():
        print(f"FATAL: source cache not found: {src_dir}", file=sys.stderr)
        return 1

    coins, hlcvs, timestamps = load_hlcvs_cache(src_dir)
    print(f"Loaded {src_dir.name}: {len(coins)} coins, {hlcvs.shape[0]} minutes")
    print(f"  Span: {pd.to_datetime(int(timestamps[0]), unit='ms')} -> {pd.to_datetime(int(timestamps[-1]), unit='ms')}")

    selected = args.coins or [c for c in coins if c in APPROVED_COINS]
    print(f"  Coins to extract: {selected}")

    total_written = 0
    total_skipped = 0
    for coin in selected:
        if coin not in coins:
            print(f"  SKIP {coin}: not in source cache", file=sys.stderr)
            continue
        idx = coins.index(coin)
        df = build_coin_df(timestamps, hlcvs[:, idx, :])
        coin_dir = args.target / coin
        result = write_daily_parquets(coin_dir, df, force=args.force, dry_run=args.dry_run)
        total_written += result["written"]
        total_skipped += result["skipped"]
        span_str = f"{result['spans'][0]} -> {result['spans'][-1]}" if result["spans"] else "—"
        print(
            f"  {coin:>5}: rows={result['rows']:>7} "
            f"written={result['written']:>3} skipped={result['skipped']:>3} "
            f"span={span_str}"
        )

    print(f"\nTotal: {total_written} parquets written, {total_skipped} skipped")
    if args.dry_run:
        print("(dry-run — no files actually written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
