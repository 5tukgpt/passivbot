#!/usr/bin/env python3
"""Refuse to let a MIXED L2->OHLCV cache be consumed silently.

THE HAZARD. Until 2026-08-13 `batch_l2_to_ohlcv.py` wrote a literal 0.0 into
every `bv` (volume) cell. It now writes a mid-path proxy and stamps the parquet
schema metadata with `bv_kind=midpath_abs_return_v1`. Files produced before that
carry no stamp and are constant-zero.

Mixing them is the dangerous case, and it is dangerous BECAUSE NOTHING ERRORS: a
strategy that uses volume runs green over a blend of real proxy values and
constant zeros, and reports a confident number about a mechanism that fired on
only part of the data. This project has already shipped one wrong answer from a
silently-disabled mechanism (2026-08-09) and reverted another for a stale config
(2026-08-11). Same shape, so this exits non-zero rather than printing a warning
nobody reads.

Usage:
    python scripts/verify_ohlcv_cache.py                 # whole cache
    python scripts/verify_ohlcv_cache.py --dir <path>
    python scripts/verify_ohlcv_cache.py --require-proxy # fail unless ALL stamped

Exit codes: 0 uniform, 1 MIXED, 2 nothing found.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent.parent
DEFAULT = HERE / "caches" / "ohlcv" / "hyperliquid"
STAMP = b"midpath_abs_return_v1"


def main() -> int:
    root = Path(sys.argv[sys.argv.index("--dir") + 1]) if "--dir" in sys.argv else DEFAULT
    require_proxy = "--require-proxy" in sys.argv

    files = sorted(root.glob("**/*.parquet"))
    if not files:
        print(f"no parquet files under {root}")
        return 2

    kinds: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for f in files:
        md = pq.read_schema(f).metadata or {}
        kind = (md.get(b"bv_kind") or b"UNSTAMPED(bv==0)").decode()
        kinds[kind] += 1
        examples.setdefault(kind, str(f.relative_to(root)))

    print(f"cache: {root}")
    print(f"files: {len(files)}\n")
    for kind, n in kinds.most_common():
        print(f"  {n:6d}  {kind}")
        print(f"          e.g. {examples[kind]}")

    proxy = kinds.get(STAMP.decode(), 0)
    stale = len(files) - proxy

    if stale and proxy:
        print(f"\nMIXED CACHE — {proxy} proxy-volume files alongside {stale} constant-zero files.")
        print("Do NOT backtest a volume-dependent strategy across this. Either")
        print("rebuild the stale days, or restrict the run to stamped files only.")
        return 1
    if require_proxy and stale:
        print(f"\n{stale} file(s) carry no proxy volume and --require-proxy was set.")
        return 1

    print("\nuniform — safe to consume." if proxy else
          "\nuniform, but ALL files are constant-zero volume: price-only arms only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
