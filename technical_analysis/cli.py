import argparse
import os
import sys

from technical_analysis.crypto.alerts import run_crypto_scan
from technical_analysis.stocks.alerts import run_stock_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run crypto or stock analysis alerts")
    parser.add_argument(
        "workspace",
        choices=["crypto", "stocks"],
        help="Which analysis workflow to run",
    )
    parser.add_argument("--interval", type=int, default=15, help="Candle interval in minutes for crypto scans")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workspace == "crypto":
        os.environ["CANDLE_INTERVAL_MIN"] = str(args.interval)
        run_crypto_scan()
    else:
        run_stock_scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
