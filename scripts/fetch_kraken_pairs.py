#!/usr/bin/env python3
"""Fetch Kraken's supported trading pairs and sample candle data, saving both
to local JSON files.

Usage:
    python scripts/fetch_kraken_pairs.py [PAIR] [--interval MINUTES] [--count N]

If no pair is given, the first supported pair (alphabetically) is used.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from technical_analysis.crypto.alerts import get_candles, get_tradable_pairs
from technical_analysis.config import DEFAULT_INTERVAL_MINUTES

DATA_DIR = ROOT / "data"
PAIRS_FILE = DATA_DIR / "kraken_pairs.json"
CANDLES_FILE = DATA_DIR / "kraken_sample_candles.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pair", nargs="?", help="Pair key to fetch candles for (e.g. XBTUSD)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    print("Fetching tradable pairs from Kraken...")
    pairs = get_tradable_pairs()
    print(f"Got {len(pairs)} supported pairs.")

    with open(PAIRS_FILE, "w", encoding="utf-8") as handle:
        json.dump(pairs, handle, indent=2, sort_keys=True)
    print(f"Saved pairs list to {PAIRS_FILE}")

    pair_key = args.pair or sorted(pairs)[0]
    if pair_key not in pairs:
        print(f"Error: {pair_key!r} is not a supported pair.", file=sys.stderr)
        sys.exit(1)

    wsname = pairs[pair_key]
    print(f"Fetching {args.count} candles for {pair_key} ({wsname}) at {args.interval}m interval...")
    candles = get_candles(pair_key, args.count, args.interval)
    if candles is None:
        print(f"Error: failed to fetch candles for {pair_key}.", file=sys.stderr)
        sys.exit(1)

    output = {
        "pair_key": pair_key,
        "wsname": wsname,
        "interval_minutes": args.interval,
        "candles": candles,
    }
    with open(CANDLES_FILE, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True)
    print(f"Saved {len(candles)} candles to {CANDLES_FILE}")


if __name__ == "__main__":
    main()
