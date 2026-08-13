#!/usr/bin/env python3
"""
Batch Hyperliquid L2 → 1m OHLCV Pipeline
Downloads L2 orderbook snapshots from S3 and converts to 1m candles.

Usage:
    python scripts/batch_l2_to_ohlcv.py
    python scripts/batch_l2_to_ohlcv.py --coins BTC ETH SOL --start 2026-01-01 --end 2026-02-21
    python scripts/batch_l2_to_ohlcv.py --workers 10
"""

import argparse
import json
import lz4.frame
import logging
import numpy as np
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("batch_l2")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
BUCKET = "hyperliquid-archive"
RAW_DIR = SCRIPT_DIR / "caches" / "l2book_raw" / "hyperliquid"
# Two output formats for passivbot
PARQUET_DIR = SCRIPT_DIR / "caches" / "ohlcv" / "hyperliquid"  # archive format: {coin}/{date}.parquet
NPY_DIR = SCRIPT_DIR / "caches" / "ohlcv" / "hyperliquid" / "1m"  # source_dir format: {symbol}/{date}.npy

COIN_TO_SYMBOL = {
    "BTC": "BTC_USDC:USDC",
    "ETH": "ETH_USDC:USDC",
    "SOL": "SOL_USDC:USDC",
}


def iter_dates(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def download_hour(s3, coin: str, day_str: str, hour: int, out_dir: Path) -> bool:
    """Download one hour of L2 data. Returns True if downloaded, False if skipped/failed."""
    out_path = out_dir / f"{hour}.lz4"
    if out_path.exists():
        return False  # skip existing

    key = f"market_data/{day_str}/{hour}/l2Book/{coin}.lz4"
    try:
        s3.download_file(BUCKET, key, str(out_path), ExtraArgs={"RequestPayer": "requester"})
        return True
    except Exception as e:
        # Some hours may not exist (e.g., maintenance)
        if "404" in str(e) or "NoSuchKey" in str(e):
            return False
        log.warning(f"  Failed {coin}/{day_str}/{hour}: {e}")
        return False


def download_day(s3, coin: str, day: date) -> int:
    """Download all 24 hours for a coin/day. Returns number of new files."""
    day_str = day.strftime("%Y%m%d")
    out_dir = RAW_DIR / coin / day_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check if already fully processed (parquet exists)
    parquet_path = PARQUET_DIR / coin / f"{day.isoformat()}.parquet"
    if parquet_path.exists():
        return 0  # already converted

    new = 0
    for hour in range(24):
        if download_hour(s3, coin, day_str, hour, out_dir):
            new += 1
    return new


def convert_day(coin: str, day: date) -> int:
    """Convert downloaded L2 data to 1m OHLCV. Returns number of candles."""
    day_str = day.strftime("%Y%m%d")
    raw_dir = RAW_DIR / coin / day_str

    # Check output already exists
    parquet_path = PARQUET_DIR / coin / f"{day.isoformat()}.parquet"
    if parquet_path.exists():
        return 0

    if not raw_dir.exists():
        return 0

    # Parse all hours
    mid_prices = []
    for hour in range(24):
        path = raw_dir / f"{hour}.lz4"
        if not path.exists():
            continue
        try:
            with open(path, "rb") as f:
                raw = lz4.frame.decompress(f.read())
            for line in raw.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    data = obj.get("raw", {}).get("data", {})
                    ts = data.get("time", 0)
                    levels = data.get("levels", [[], []])
                    if len(levels) < 2 or not levels[0] or not levels[1]:
                        continue
                    best_bid = float(levels[0][0]["px"])
                    best_ask = float(levels[1][0]["px"])
                    mid = (best_bid + best_ask) / 2
                    mid_prices.append((ts, mid))
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"  Failed to decompress {path}: {e}")
            continue

    if not mid_prices:
        return 0

    mid_prices.sort(key=lambda x: x[0])

    # Build 1m candles
    #
    # [2026-08-13] `bv` used to be written as a literal 0.0 on every row. That is
    # not "sparse volume" — it is NO volume, and it fails SILENTLY downstream: a
    # strategy that uses volume runs green over constant-zero terms and reports a
    # confident result about a mechanism that never fired. gooner_bot depends on
    # volume in three scoring terms (+10 volume, the Donchian breakout leg, the
    # short_score mirror) plus the 1w OBV informative.
    #
    # L2 snapshots carry no traded volume, so the honest options were "leave it
    # zero and refuse to use it" or "carry a proxy and label it". We carry a
    # proxy: MID-PATH LENGTH, the sum of |Δmid| over the minute, normalised by
    # price so it is dimensionless (a sum of absolute returns) and therefore
    # comparable across a 29-pair whitelist of very different prices.
    #
    # MEASURED FIDELITY vs true traded volume (Spearman rank, 2026-07-27..29):
    #     BTC  1h +0.845   15m +0.734      vol_spike agreement 92.3% / 84.0%
    #     SOL  1h +0.803   15m +0.629                          84.6% / 85.1%
    #     DOGE 1h +0.453   15m +0.418                          82.7% / 81.0%
    # Two alternatives were tested and rejected: snapshot COUNT is negatively
    # correlated (worse than useless), top-5 book DEPTH is weak (+0.16..+0.39).
    #
    # LIMITS — read before trusting a volume-dependent result:
    #   * It degrades with liquidity AND with shorter timeframes. Recall of true
    #     vol_spike events is 81% on DOGE 1h but 42% on DOGE 15m. Treat sub-1h
    #     volume terms on illiquid pairs as unmeasured, not measured.
    #   * Only the ORDERING is meaningful. Every consumer here is relative
    #     (vol_spike = volume > vol_sma_20 * 1.5), so absolute scale does not
    #     matter — but do not compare this number to an exchange volume figure.
    #   * A minute with no mid movement yields exactly 0, where true volume would
    #     rarely be 0. gooner's entry conditions carry `& (volume > 0)`, so such a
    #     candle cannot trade. Rare once aggregated to 1h+, common at 1m.
    minute_ms = 60_000
    candles = {}
    prev_price = None
    for ts, price in mid_prices:
        minute_ts = ts - (ts % minute_ms)
        if minute_ts not in candles:
            candles[minute_ts] = {"o": price, "h": price, "l": price, "c": price, "path": 0.0}
        else:
            c = candles[minute_ts]
            c["h"] = max(c["h"], price)
            c["l"] = min(c["l"], price)
            c["c"] = price
        # Attribute each leg to the minute it ARRIVES in, so movement across a
        # minute boundary is counted once and never dropped.
        if prev_price is not None and price > 0:
            candles[minute_ts]["path"] += abs(price - prev_price) / price
        prev_price = price

    # Convert to numpy
    sorted_ts = sorted(candles.keys())
    arr = np.zeros(
        len(sorted_ts),
        dtype=[("ts", "i8"), ("o", "f8"), ("h", "f8"), ("l", "f8"), ("c", "f8"), ("bv", "f8")],
    )
    for i, ts in enumerate(sorted_ts):
        c = candles[ts]
        arr[i] = (ts, c["o"], c["h"], c["l"], c["c"], c["path"])

    # Save parquet (archive format)
    import pyarrow as pa
    import pyarrow.parquet as pq

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {"ts": arr["ts"], "o": arr["o"], "h": arr["h"], "l": arr["l"], "c": arr["c"], "bv": arr["bv"]}
    )
    # Stamp the producer so a bv=0 file from before 2026-08-13 can never be
    # silently mixed with a proxy file. Files written by the old code carry no
    # such key; anything reading these MUST check it rather than assume.
    table = table.replace_schema_metadata({
        b"bv_kind": b"midpath_abs_return_v1",
        b"bv_note": (b"sum |dmid|/mid per minute; a RANK proxy for traded volume, "
                     b"not a volume figure. Spearman vs true vol: BTC 1h +0.845, "
                     b"SOL 1h +0.803, DOGE 1h +0.453; degrades below 1h and with "
                     b"illiquidity (DOGE 15m +0.418, 42% spike recall)."),
        b"producer": b"batch_l2_to_ohlcv.py@2026-08-13",
    })
    pq.write_table(table, parquet_path)

    # Save npy (source_dir format)
    symbol = COIN_TO_SYMBOL.get(coin, f"{coin}_USDC:USDC")
    npy_dir = NPY_DIR / symbol
    npy_dir.mkdir(parents=True, exist_ok=True)
    npy_path = npy_dir / f"{day.isoformat()}.npy"
    np.save(npy_path, arr)

    return len(arr)


def main():
    parser = argparse.ArgumentParser(description="Batch HL L2 → 1m OHLCV")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--start", default="2025-12-26", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-02-21", help="End date YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent S3 downloads")
    parser.add_argument("--convert-only", action="store_true", help="Skip download, just convert")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    coins = args.coins
    days = list(iter_dates(start, end))

    log.info(f"Pipeline: {len(coins)} coins × {len(days)} days = {len(coins)*len(days)} coin-days")
    log.info(f"Coins: {coins}")
    log.info(f"Range: {start} → {end}")
    log.info(f"Workers: {args.workers}")

    import boto3

    s3 = boto3.client("s3")
    total_downloaded = 0
    total_candles = 0
    t0 = time.time()

    for coin in coins:
        log.info(f"\n{'='*60}")
        log.info(f"Processing {coin} ({len(days)} days)")
        log.info(f"{'='*60}")

        if not args.convert_only:
            # Phase 1: Download
            coin_downloaded = 0
            coin_skipped = 0
            for i, day in enumerate(days):
                n = download_day(s3, coin, day)
                if n > 0:
                    coin_downloaded += n
                else:
                    coin_skipped += 1
                if (i + 1) % 10 == 0 or i == len(days) - 1:
                    elapsed = time.time() - t0
                    log.info(
                        f"  {coin} download: {i+1}/{len(days)} days | "
                        f"{coin_downloaded} new files | {coin_skipped} skipped | "
                        f"{elapsed:.0f}s elapsed"
                    )
            total_downloaded += coin_downloaded

        # Phase 2: Convert
        coin_candles = 0
        for i, day in enumerate(days):
            n = convert_day(coin, day)
            coin_candles += n
            if (i + 1) % 10 == 0 or i == len(days) - 1:
                log.info(f"  {coin} convert: {i+1}/{len(days)} days | {coin_candles:,} candles so far")
        total_candles += coin_candles
        log.info(f"  {coin} done: {coin_candles:,} total candles")

    elapsed = time.time() - t0
    log.info(f"\n{'='*60}")
    log.info(f"COMPLETE")
    log.info(f"  Downloaded: {total_downloaded} new hour files")
    log.info(f"  Candles: {total_candles:,} total 1m candles")
    log.info(f"  Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    log.info(f"  Parquet dir: {PARQUET_DIR}")
    log.info(f"  NPY dir: {NPY_DIR}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
