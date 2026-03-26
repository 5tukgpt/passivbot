#!/usr/bin/env python3
"""
Hyperliquid 1m candle fetcher for Passivbot backtesting.

Fetches historical 1-minute OHLCV data from Hyperliquid's public API
and saves in the format Passivbot's backtester expects:
  caches/ohlcv/hyperliquid/1m/{SYMBOL}_USDC:USDC/YYYY-MM-DD.npy

Each .npy file contains a float64 array with columns:
  [timestamp_ms, open, high, low, close, volume]

Usage:
  python hl_candle_fetcher.py --symbols ETH SOL BTC --days 7           # 1m candles (max ~5-7 days)
  python hl_candle_fetcher.py --symbols ETH SOL BTC --days 90 --interval 1h  # 1h candles (90+ days)
  python hl_candle_fetcher.py --symbols ETH --start 2026-03-01 --end 2026-03-25

Data availability (HL public API):
  1m candles: ~5-7 days of history (enough for quick validation)
  1h candles: 90+ days of history (enough for proper backtesting)
  For deeper 1m history: need AWS S3 pipeline (PBGui's hyperliquid_aws.py)

Rate limits: HL returns up to 5000 candles/request.
At 1200 req/min limit, 90 days × 3 symbols = ~30 requests, finishes in seconds.
"""

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:
    import requests
except ImportError:
    import urllib.request
    import urllib.error

    # Minimal requests-like wrapper for environments without requests
    class _Response:
        def __init__(self, data, status):
            self._data = data
            self.status_code = status

        def json(self):
            return json.loads(self._data)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    class requests:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            data = json if json else {}
            req = urllib.request.Request(
                url,
                data=__import__("json").dumps(data).encode(),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=timeout or 30)
                return _Response(resp.read().decode(), resp.status)
            except urllib.error.HTTPError as e:
                return _Response(e.read().decode(), e.code)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
CANDLES_PER_REQUEST = 5000  # HL max
MS_PER_MINUTE = 60_000
MS_PER_DAY = 86_400_000
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def get_cache_dir(interval: str = "1m") -> Path:
    return Path(__file__).parent / "caches" / "ohlcv" / "hyperliquid" / interval


def fetch_candles(symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[list]:
    """Fetch candles from HL public API. Returns list of [t, o, h, l, c, v]."""
    interval_ms = INTERVAL_MS.get(interval, MS_PER_MINUTE)
    all_candles = []
    cursor = start_ms

    while cursor < end_ms:
        chunk_end = min(cursor + CANDLES_PER_REQUEST * interval_ms, end_ms)

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": chunk_end,
            },
        }

        for attempt in range(3):
            try:
                resp = requests.post(HL_INFO_URL, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 2:
                    log.error(f"Failed after 3 attempts for {symbol} [{cursor}→{chunk_end}]: {e}")
                    raise
                wait = 2 ** attempt
                log.warning(f"Attempt {attempt + 1} failed for {symbol}: {e}. Retrying in {wait}s...")
                time.sleep(wait)

        if not data:
            log.warning(f"No data returned for {symbol} [{cursor}→{chunk_end}]")
            cursor = chunk_end
            continue

        # HL returns: {"t": ms, "T": ms, "s": symbol, "i": interval,
        #              "o": open, "c": close, "h": high, "l": low, "v": volume, "n": num_trades}
        for c in data:
            t = int(c["t"])
            if t >= end_ms:
                break
            all_candles.append([
                float(t),
                float(c["o"]),
                float(c["h"]),
                float(c["l"]),
                float(c["c"]),
                float(c["v"]),
            ])

        # Advance cursor past what we received
        if data:
            last_t = int(data[-1]["t"])
            cursor = last_t + interval_ms
        else:
            cursor = chunk_end

        # Small delay to be polite
        time.sleep(0.05)

    return all_candles


def save_daily_npy(symbol: str, candles: list[list], output_dir: Path = None, interval: str = "1m"):
    """Split candles by day and save as per-day .npy files in Passivbot format."""
    if not candles:
        log.warning(f"No candles to save for {symbol}")
        return 0

    # Passivbot uses {SYMBOL}_USDC:USDC as directory name for HL
    symbol_dir_name = f"{symbol}_USDC:USDC"
    sym_dir = (output_dir or get_cache_dir(interval)) / symbol_dir_name
    sym_dir.mkdir(parents=True, exist_ok=True)

    # Group by date
    by_day: dict[str, list] = {}
    for c in candles:
        ts_ms = c[0]
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append(c)

    saved = 0
    for day_key, day_candles in sorted(by_day.items()):
        arr = np.array(day_candles, dtype=np.float64)
        npy_path = sym_dir / f"{day_key}.npy"
        np.save(npy_path, arr)
        saved += 1
        log.debug(f"  {npy_path.name}: {len(day_candles)} candles")

    log.info(f"Saved {saved} daily files for {symbol} ({len(candles)} total candles)")
    return saved


def fetch_and_save(
    symbols: list[str],
    start_date: str = None,
    end_date: str = None,
    days: int = 30,
    output_dir: Path = None,
    interval: str = "1m",
):
    """Main entry: fetch candles for symbols and save as Passivbot-compatible .npy files."""
    # Resolve date range
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)

    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_dt = end_dt - timedelta(days=days)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    total_days = (end_dt - start_dt).days
    est_requests = math.ceil(total_days / 3.47) * len(symbols)

    log.info(f"Fetching {interval} candles: {', '.join(symbols)}")
    log.info(f"Range: {start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')} ({total_days} days)")
    log.info(f"Estimated requests: ~{est_requests} (well within rate limits)")

    results = {}
    for symbol in symbols:
        log.info(f"Fetching {symbol}...")
        t0 = time.time()
        candles = fetch_candles(symbol, start_ms, end_ms, interval=interval)
        elapsed = time.time() - t0
        log.info(f"  {symbol}: {len(candles)} candles in {elapsed:.1f}s")

        n_saved = save_daily_npy(symbol, candles, output_dir, interval=interval)
        results[symbol] = {"candles": len(candles), "days": n_saved, "elapsed": elapsed}

    # Summary
    log.info("\n=== Summary ===")
    total_candles = 0
    for sym, r in results.items():
        log.info(f"  {sym}: {r['candles']:,} candles across {r['days']} days ({r['elapsed']:.1f}s)")
        total_candles += r["candles"]
    log.info(f"  Total: {total_candles:,} candles")
    log.info(f"  Output: {output_dir or get_cache_dir(interval)}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Hyperliquid 1m candles for Passivbot backtesting"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["ETH", "SOL", "BTC"],
        help="Symbols to fetch (default: ETH SOL BTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to fetch (default: 30, ignored if --start is set)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date YYYY-MM-DD (default: --days ago)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date YYYY-MM-DD (default: now)",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1m",
        choices=["1m", "5m", "15m", "1h", "4h", "1d"],
        help="Candle interval (default: 1m). Note: 1m has ~5-7 days history, 1h has 90+ days",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override output directory (default: caches/ohlcv/hyperliquid/1m/)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    out_dir = Path(args.output_dir) if args.output_dir else None

    fetch_and_save(
        symbols=args.symbols,
        start_date=args.start,
        end_date=args.end,
        days=args.days,
        output_dir=out_dir,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
