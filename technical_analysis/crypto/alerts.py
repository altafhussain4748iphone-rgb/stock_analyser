import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from statistics import median

import requests

from technical_analysis.common.emailing import send_email_message as send_html_email
from technical_analysis.config import DEFAULT_INTERVAL_MINUTES, get_env, get_interval_minutes


# -----------------------------------------------------------------------------
# Kraken/API configuration
# -----------------------------------------------------------------------------

KRAKEN_API_URL = "https://api.kraken.com/0/public"
VALID_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
QUOTE_FILTER = ("USD", "USDT")

REQUEST_TIMEOUT_SEC = 15
REQUEST_RETRIES = 3
REQUEST_RETRY_DELAY_SEC = 1.5
REQUEST_DELAY_SEC = 0.5
API_ERROR_ALERT_THRESHOLD = 5


# -----------------------------------------------------------------------------
# Breakout settings
#
# These are intentionally conservative starting values. Backtest them on the
# exact pairs you trade before treating alerts as trading signals.
# -----------------------------------------------------------------------------

# Previous range used to define the breakout level.
BREAKOUT_LOOKBACK = 20

# Twenty-four hours of 15-minute candles. This also works on other intervals,
# but represents a different amount of elapsed time on those intervals.
VOLUME_LOOKBACK = 96

ATR_PERIOD = 14

# The signal candle must close this far beyond the prior range.
BREAKOUT_BUFFER_ATR = 0.15

# Candle-quality filters.
MIN_BODY_RATIO = 0.60
MIN_CLOSE_LOCATION = 0.75
MIN_BODY_ATR = 0.80

# Robust quote-volume filters.
MIN_VOLUME_MULTIPLE = 2.0
MIN_VOLUME_ROBUST_Z = 3.0
MIN_MEDIAN_QUOTE_VOLUME = 25_000.0

# Liquidity/activity filters.
MIN_MEDIAN_TRADE_COUNT = 10
MIN_SIGNAL_TRADE_COUNT = 10

# Do not alert repeatedly on the same pair for this many candles.
COOLDOWN_CANDLES = 4

# Optional next-candle confirmation. This can also be enabled with the
# --confirm-next-candle command-line option.
DEFAULT_REQUIRE_NEXT_CANDLE_CONFIRMATION = False
CONFIRM_RETEST_TOLERANCE_ATR = 0.25


# -----------------------------------------------------------------------------
# Volume-spike settings
#
# A simpler check: alert on any pair whose latest closed candle shows a
# volume spike and a large price move, without the breakout/candle-quality
# filters above. Liquidity filters are still reused so alerts stay on
# tradable pairs.
# -----------------------------------------------------------------------------

VOLUME_SPIKE_MULTIPLE = 3.0
PRICE_CHANGE_ALERT_PCT = 1.5


# -----------------------------------------------------------------------------
# Application configuration and logging
# -----------------------------------------------------------------------------

CANDLE_INTERVAL_MIN = get_interval_minutes(DEFAULT_INTERVAL_MINUTES)
LOG_FILE = get_env("LOG_FILE")
LOG_LEVEL = get_env("LOG_LEVEL", "INFO").upper()
ALERT_STATE_FILE = get_env(
    "ALERT_STATE_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "kraken_alert_state.json",
    ),
)

log = logging.getLogger("kraken_breakout_alert")
log.setLevel(LOG_LEVEL)
log.propagate = False

_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if not log.handlers:
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_formatter)
    log.addHandler(_console_handler)

    if LOG_FILE:
        _file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5_000_000,
            backupCount=3,
        )
        _file_handler.setFormatter(_formatter)
        log.addHandler(_file_handler)
        log.info("Logging to file: %s", LOG_FILE)

logging.getLogger("urllib3").setLevel(
    logging.WARNING if LOG_LEVEL != "DEBUG" else logging.DEBUG
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kraken-breakout-alert/2.0"})


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------


def format_interval(minutes):
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def utc_datetime(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def load_state(state_file=ALERT_STATE_FILE):
    if not os.path.exists(state_file):
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception as exc:
        log.warning("Could not read state file, starting fresh: %s", exc)
        return {}


def save_state(state, state_file=ALERT_STATE_FILE):
    directory = os.path.dirname(os.path.abspath(state_file))
    temporary_path = f"{state_file}.tmp"

    try:
        os.makedirs(directory, exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
        os.replace(temporary_path, state_file)
    except Exception as exc:
        log.warning("Could not write state file: %s", exc)
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def in_cooldown(state, pair, current_candle_time, interval_minutes):
    last = state.get(pair)
    if last is None:
        return False

    try:
        elapsed_candles = (
            int(current_candle_time) - int(last)
        ) / (interval_minutes * 60)
    except (TypeError, ValueError):
        return False

    return elapsed_candles < COOLDOWN_CANDLES


def median_absolute_deviation(values, center=None):
    if not values:
        return 0.0

    center = median(values) if center is None else center
    return median(abs(value - center) for value in values)


def robust_z_score(value, samples):
    """Return a median/MAD-based z-score that is resistant to outliers."""
    if not samples:
        return 0.0

    center = median(samples)
    mad = median_absolute_deviation(samples, center)

    if mad <= 0:
        return float("inf") if value > center else 0.0

    return 0.67448975 * (value - center) / mad


def calculate_atr(candles, period):
    """Calculate a simple average true range from completed candles."""
    if len(candles) < period + 1:
        return None

    sample = candles[-(period + 1):]
    true_ranges = []

    for previous, current in zip(sample, sample[1:]):
        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        true_ranges.append(true_range)

    if not true_ranges:
        return None

    return sum(true_ranges) / len(true_ranges)


# -----------------------------------------------------------------------------
# Kraken requests
# -----------------------------------------------------------------------------


def request_kraken_json(endpoint, params=None, error_sink=None, context=None):
    url = f"{KRAKEN_API_URL}/{endpoint}"
    context = context or endpoint
    last_message = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = SESSION.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            response.raise_for_status()
            data = response.json()

            api_errors = data.get("error") or []
            if api_errors:
                raise RuntimeError(f"Kraken returned: {api_errors}")

            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_message = (
                f"{context}: request attempt {attempt}/{REQUEST_RETRIES} failed: {exc}"
            )
            log.debug(last_message)

            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_RETRY_DELAY_SEC * attempt)

    if error_sink is not None and last_message:
        error_sink.append(last_message)

    return None


def get_tradable_pairs(error_sink=None):
    log.debug("Requesting AssetPairs from Kraken...")
    data = request_kraken_json(
        "AssetPairs",
        error_sink=error_sink,
        context="AssetPairs",
    )

    if data is None:
        raise RuntimeError("Unable to fetch Kraken AssetPairs after retries")

    result = data.get("result", {})
    pairs = {}

    for pair_key, info in result.items():
        wsname = info.get("wsname", "")
        status = info.get("status")

        if not wsname:
            continue
        if status and status != "online":
            continue
        if QUOTE_FILTER and not any(
            wsname.endswith(f"/{quote}") for quote in QUOTE_FILTER
        ):
            continue

        pairs[pair_key] = wsname

    log.debug(
        "AssetPairs returned %d total, %d after filters.",
        len(result),
        len(pairs),
    )
    return pairs


def get_candles(pair, count, interval_minutes, error_sink=None):
    data = request_kraken_json(
        "OHLC",
        params={"pair": pair, "interval": interval_minutes},
        error_sink=error_sink,
        context=f"{pair} OHLC",
    )

    if data is None:
        return None

    result = data.get("result", {})
    data_key = next((key for key in result if key != "last"), None)

    if data_key is None:
        message = f"{pair}: no OHLC data key in response"
        log.debug(message)
        if error_sink is not None:
            error_sink.append(message)
        return None

    raw_candles = result[data_key][-count:]
    if len(raw_candles) < count:
        log.debug(
            "%s: only %d/%d candles available (new or thin listing)",
            pair,
            len(raw_candles),
            count,
        )

    parsed = []
    try:
        for candle in raw_candles:
            parsed.append(
                {
                    "time": int(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "vwap": float(candle[5]),
                    "volume": float(candle[6]),
                    "count": int(candle[7]),
                }
            )
    except (TypeError, ValueError, IndexError) as exc:
        message = f"{pair}: malformed OHLC response: {exc}"
        log.debug(message)
        if error_sink is not None:
            error_sink.append(message)
        return None

    return parsed


# -----------------------------------------------------------------------------
# Breakout evaluation
#
# Pure functions of already-fetched candle data, so they can run against a
# single Kraken fetch shared with any other analysis (see "Analysis registry"
# below).
# -----------------------------------------------------------------------------


def evaluate_signal_candle(pair, wsname, history, signal_candle):
    if len(history) < max(BREAKOUT_LOOKBACK, VOLUME_LOOKBACK, ATR_PERIOD + 1):
        return None

    atr = calculate_atr(history, ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    range_window = history[-BREAKOUT_LOOKBACK:]
    range_high = max(candle["high"] for candle in range_window)
    range_low = min(candle["low"] for candle in range_window)
    breakout_buffer = atr * BREAKOUT_BUFFER_ATR

    candle_range = signal_candle["high"] - signal_candle["low"]
    if candle_range <= 0 or signal_candle["open"] <= 0:
        return None

    body = abs(signal_candle["close"] - signal_candle["open"])
    body_ratio = body / candle_range
    body_atr = body / atr
    close_location = (
        signal_candle["close"] - signal_candle["low"]
    ) / candle_range

    is_up_breakout = (
        signal_candle["close"] > signal_candle["open"]
        and signal_candle["close"] >= range_high + breakout_buffer
        and close_location >= MIN_CLOSE_LOCATION
    )
    is_down_breakout = (
        signal_candle["close"] < signal_candle["open"]
        and signal_candle["close"] <= range_low - breakout_buffer
        and close_location <= 1.0 - MIN_CLOSE_LOCATION
    )

    if not (is_up_breakout or is_down_breakout):
        return None

    direction = "UP" if is_up_breakout else "DOWN"
    breakout_level = range_high if is_up_breakout else range_low
    breakout_distance = (
        signal_candle["close"] - breakout_level
        if is_up_breakout
        else breakout_level - signal_candle["close"]
    )
    breakout_strength_atr = breakout_distance / atr

    volume_window = history[-VOLUME_LOOKBACK:]
    historical_quote_volumes = [
        candle["volume"] * candle["vwap"] for candle in volume_window
    ]
    current_quote_volume = signal_candle["volume"] * signal_candle["vwap"]
    median_quote_volume = median(historical_quote_volumes)

    if median_quote_volume <= 0:
        return None

    volume_multiple = current_quote_volume / median_quote_volume
    volume_robust_z = robust_z_score(
        current_quote_volume,
        historical_quote_volumes,
    )

    historical_trade_counts = [candle["count"] for candle in volume_window]
    median_trade_count = median(historical_trade_counts)

    price_change_pct = (
        (signal_candle["close"] - signal_candle["open"])
        / signal_candle["open"]
        * 100.0
    )

    passes_conviction = body_ratio >= MIN_BODY_RATIO
    passes_move = body_atr >= MIN_BODY_ATR
    passes_volume = (
        volume_multiple >= MIN_VOLUME_MULTIPLE
        and volume_robust_z >= MIN_VOLUME_ROBUST_Z
    )
    passes_liquidity = (
        median_quote_volume >= MIN_MEDIAN_QUOTE_VOLUME
        and median_trade_count >= MIN_MEDIAN_TRADE_COUNT
        and signal_candle["count"] >= MIN_SIGNAL_TRADE_COUNT
    )

    log.debug(
        "%s: direction=%s price=%+.2f%% body=%.2f body_atr=%.2f "
        "close_location=%.2f breakout_atr=%.2f volume=%.2fx z=%.2f "
        "median_quote_volume=%.2f median_trades=%.1f signal_trades=%d "
        "filters(conviction=%s move=%s volume=%s liquidity=%s)",
        wsname or pair,
        direction,
        price_change_pct,
        body_ratio,
        body_atr,
        close_location,
        breakout_strength_atr,
        volume_multiple,
        volume_robust_z,
        median_quote_volume,
        median_trade_count,
        signal_candle["count"],
        passes_conviction,
        passes_move,
        passes_volume,
        passes_liquidity,
    )

    if not (
        passes_conviction
        and passes_move
        and passes_volume
        and passes_liquidity
    ):
        return None

    close_extreme_ratio = (
        close_location if is_up_breakout else 1.0 - close_location
    )

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": direction,
        "range_high": range_high,
        "range_low": range_low,
        "breakout_level": breakout_level,
        "breakout_buffer": breakout_buffer,
        "breakout_strength_atr": breakout_strength_atr,
        "atr": atr,
        "open": signal_candle["open"],
        "high": signal_candle["high"],
        "low": signal_candle["low"],
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "body_ratio": body_ratio,
        "body_atr": body_atr,
        "close_extreme_ratio": close_extreme_ratio,
        "base_volume": signal_candle["volume"],
        "quote_volume": current_quote_volume,
        "median_quote_volume": median_quote_volume,
        "volume_multiple": volume_multiple,
        "volume_robust_z": volume_robust_z,
        "signal_trade_count": signal_candle["count"],
        "median_trade_count": median_trade_count,
        "signal_time": utc_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
        "confirmation_time": None,
        "confirmation_epoch": None,
        "confirmation_close": None,
        "confirmed_by_next_candle": False,
    }


def next_candle_confirms(hit, signal_candle, confirmation_candle, interval_minutes):
    expected_gap = interval_minutes * 60
    actual_gap = confirmation_candle["time"] - signal_candle["time"]

    if actual_gap != expected_gap:
        log.debug(
            "%s: confirmation candle gap was %ss, expected %ss",
            hit["pair"],
            actual_gap,
            expected_gap,
        )
        return False

    level = hit["breakout_level"]
    tolerance = hit["atr"] * CONFIRM_RETEST_TOLERANCE_ATR

    if hit["direction"] == "UP":
        confirmed = (
            confirmation_candle["close"] > level
            and confirmation_candle["low"] >= level - tolerance
        )
    else:
        confirmed = (
            confirmation_candle["close"] < level
            and confirmation_candle["high"] <= level + tolerance
        )

    if not confirmed:
        log.debug(
            "%s: breakout failed next-candle confirmation; level=%.8g "
            "confirmation high/low/close=%.8g/%.8g/%.8g",
            hit["pair"],
            level,
            confirmation_candle["high"],
            confirmation_candle["low"],
            confirmation_candle["close"],
        )
        return False

    hit["confirmation_time"] = utc_datetime(confirmation_candle["time"])
    hit["confirmation_epoch"] = confirmation_candle["time"]
    hit["confirmation_close"] = confirmation_candle["close"]
    hit["confirmed_by_next_candle"] = True
    return True


def _breakout_closed_candles_needed(require_next_candle_confirmation):
    history_needed = max(BREAKOUT_LOOKBACK, VOLUME_LOOKBACK, ATR_PERIOD + 1)
    extra_closed_candles = 2 if require_next_candle_confirmation else 1
    return history_needed + extra_closed_candles


def run_breakout_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    needed = _breakout_closed_candles_needed(require_next_candle_confirmation)
    if len(closed_candles) < needed:
        return None

    tail = closed_candles[-needed:]
    if require_next_candle_confirmation:
        history, signal_candle, confirmation_candle = tail[:-2], tail[-2], tail[-1]
    else:
        history, signal_candle, confirmation_candle = tail[:-1], tail[-1], None

    hit = evaluate_signal_candle(pair, wsname, history, signal_candle)
    if hit is None:
        return None

    if require_next_candle_confirmation:
        if not next_candle_confirms(
            hit,
            signal_candle,
            confirmation_candle,
            interval_minutes,
        ):
            return None
        hit["alert_epoch"] = confirmation_candle["time"]
    else:
        hit["alert_epoch"] = signal_candle["time"]

    return hit


def log_breakout_hit(hit, label):
    log.info(
        "HIT[breakout]: %s %s breakout, volume %.1fx (z=%.1f), "
        "price %+.2f%%, body %.2f ATR, breakout %.2f ATR (%s)",
        hit["pair"],
        hit["direction"],
        hit["volume_multiple"],
        hit["volume_robust_z"],
        hit["price_change_pct"],
        hit["body_atr"],
        hit["breakout_strength_atr"],
        label,
    )


def render_breakout_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M UTC")
    confirmation_text = ""

    if hit["confirmed_by_next_candle"]:
        confirmation_time = hit["confirmation_time"].strftime("%Y-%m-%d %H:%M UTC")
        confirmation_text = (
            f" · confirmed {confirmation_time}"
            f" at {hit['confirmation_close']:.8g}"
        )

    return (
        f"<li><strong>{pair}</strong> {hit['direction']} breakout"
        f" · move {hit['price_change_pct']:+.2f}%"
        f" · close {hit['close']:.8g}"
        f" · level {hit['breakout_level']:.8g}"
        f" · breakout {hit['breakout_strength_atr']:.2f} ATR"
        f" · body {hit['body_ratio'] * 100:.0f}%/{hit['body_atr']:.2f} ATR"
        f" · close-at-extreme {hit['close_extreme_ratio'] * 100:.0f}%"
        f" · quote volume ${hit['quote_volume']:,.0f}"
        f" ({hit['volume_multiple']:.1f}x median, z={hit['volume_robust_z']:.1f})"
        f" · signal {signal_time}{confirmation_text}</li>"
    )


# -----------------------------------------------------------------------------
# Volume-spike evaluation
# -----------------------------------------------------------------------------


def evaluate_volume_price_candle(pair, wsname, history, signal_candle):
    if len(history) < VOLUME_LOOKBACK:
        return None

    if signal_candle["open"] <= 0:
        return None

    volume_window = history[-VOLUME_LOOKBACK:]
    historical_quote_volumes = [
        candle["volume"] * candle["vwap"] for candle in volume_window
    ]
    current_quote_volume = signal_candle["volume"] * signal_candle["vwap"]
    median_quote_volume = median(historical_quote_volumes)

    if median_quote_volume <= 0:
        return None

    volume_multiple = current_quote_volume / median_quote_volume

    historical_trade_counts = [candle["count"] for candle in volume_window]
    median_trade_count = median(historical_trade_counts)

    price_change_pct = (
        (signal_candle["close"] - signal_candle["open"])
        / signal_candle["open"]
        * 100.0
    )

    passes_volume = volume_multiple >= VOLUME_SPIKE_MULTIPLE
    passes_price = abs(price_change_pct) >= PRICE_CHANGE_ALERT_PCT
    passes_liquidity = (
        median_quote_volume >= MIN_MEDIAN_QUOTE_VOLUME
        and median_trade_count >= MIN_MEDIAN_TRADE_COUNT
        and signal_candle["count"] >= MIN_SIGNAL_TRADE_COUNT
    )

    log.debug(
        "%s: price=%+.2f%% volume=%.2fx median_quote_volume=%.2f "
        "median_trades=%.1f signal_trades=%d "
        "filters(volume=%s price=%s liquidity=%s)",
        wsname or pair,
        price_change_pct,
        volume_multiple,
        median_quote_volume,
        median_trade_count,
        signal_candle["count"],
        passes_volume,
        passes_price,
        passes_liquidity,
    )

    if not (passes_volume and passes_price and passes_liquidity):
        return None

    direction = "UP" if price_change_pct > 0 else "DOWN"

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": direction,
        "open": signal_candle["open"],
        "high": signal_candle["high"],
        "low": signal_candle["low"],
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "base_volume": signal_candle["volume"],
        "quote_volume": current_quote_volume,
        "median_quote_volume": median_quote_volume,
        "volume_multiple": volume_multiple,
        "signal_trade_count": signal_candle["count"],
        "median_trade_count": median_trade_count,
        "signal_time": utc_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def _volume_surge_closed_candles_needed():
    return VOLUME_LOOKBACK + 1


def run_volume_surge_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    needed = _volume_surge_closed_candles_needed()
    if len(closed_candles) < needed:
        return None

    tail = closed_candles[-needed:]
    history, signal_candle = tail[:-1], tail[-1]

    hit = evaluate_volume_price_candle(pair, wsname, history, signal_candle)
    if hit is None:
        return None

    hit["alert_epoch"] = signal_candle["time"]
    return hit


def log_volume_surge_hit(hit, label):
    log.info(
        "HIT[volume_surge]: %s %s volume %.1fx, price %+.2f%% (%s)",
        hit["pair"],
        hit["direction"],
        hit["volume_multiple"],
        hit["price_change_pct"],
        label,
    )


def render_volume_surge_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"<li><strong>{pair}</strong> {hit['direction']}"
        f" · move {hit['price_change_pct']:+.2f}%"
        f" · close {hit['close']:.8g}"
        f" · quote volume ${hit['quote_volume']:,.0f}"
        f" ({hit['volume_multiple']:.1f}x median)"
        f" · signal {signal_time}</li>"
    )


# -----------------------------------------------------------------------------
# Analysis registry
#
# Each entry is a self-contained analysis that runs against the *same*
# Kraken candle fetch for a pair. To add a new analysis (e.g. RSI divergence,
# moving-average cross):
#   1. Write an `evaluate_*_candle(pair, wsname, history, signal_candle)`
#      function returning a hit dict (with at least "pair", "pair_key",
#      "direction", "price_change_pct", "signal_time", "alert_epoch") or None.
#   2. Write a `run_*_analysis(pair, wsname, closed_candles, interval_minutes,
#      require_next_candle_confirmation)` wrapper that slices `closed_candles`
#      to the history/signal candle it needs and calls the evaluator.
#   3. Write `log_*_hit(hit, label)` and `render_*_item(hit)` for logging and
#      the email section.
#   4. Append a new entry below. No other code needs to change: the scanner
#      loop, cooldown state, and combined email all iterate this list.
# -----------------------------------------------------------------------------

ANALYSES = [
    {
        "key": "breakout",
        "section_title": "Confirmed Breakouts",
        "run": run_breakout_analysis,
        "sort_key": lambda hit: (hit["volume_robust_z"], hit["breakout_strength_atr"]),
        "log_hit": log_breakout_hit,
        "render_item": render_breakout_item,
        "section_intro": lambda confirm_label: (
            "<p>Signals passed range, ATR, candle-quality, robust-volume, and "
            f"liquidity filters ({html.escape(confirm_label)}).</p>"
        ),
    },
    {
        "key": "volume_surge",
        "section_title": "Volume Surge Alerts",
        "run": run_volume_surge_analysis,
        "sort_key": lambda hit: hit["volume_multiple"],
        "log_hit": log_volume_surge_hit,
        "render_item": render_volume_surge_item,
        "section_intro": lambda _confirm_label: (
            f"<p>Signals passed volume &gt;= {VOLUME_SPIKE_MULTIPLE:.1f}x median and "
            f"price move &gt;= {PRICE_CHANGE_ALERT_PCT:.1f}% on the closed candle, "
            "plus liquidity filters.</p>"
        ),
    },
]


def _max_requested_candle_count(require_next_candle_confirmation):
    closed_needed = max(
        _breakout_closed_candles_needed(require_next_candle_confirmation),
        _volume_surge_closed_candles_needed(),
    )
    # +1 for Kraken's currently forming candle, which is never evaluated.
    return closed_needed + 1


# -----------------------------------------------------------------------------
# Email output
# -----------------------------------------------------------------------------


def build_combined_email(hits_by_analysis, interval_minutes, require_next_candle_confirmation):
    label = format_interval(interval_minutes)
    confirm_label = (
        "next-candle confirmation required"
        if require_next_candle_confirmation
        else "closed signal candle"
    )

    subject_parts = []
    lines = [
        "<html><body>",
        "<h2>Kraken Crypto Alerts</h2>",
        f"<p><strong>Interval:</strong> {html.escape(label)}</p>",
    ]

    for spec in ANALYSES:
        hits = hits_by_analysis.get(spec["key"]) or []
        if not hits:
            continue

        subject_parts.append(f"{len(hits)} {spec['section_title'].lower()}")
        lines.append(f"<h3>{html.escape(spec['section_title'])} ({len(hits)})</h3>")
        lines.append(spec["section_intro"](confirm_label))
        lines.append("<ul>")
        lines.extend(spec["render_item"](hit) for hit in hits)
        lines.append("</ul>")

    lines.extend(["</body></html>"])
    subject = f"Kraken Alerts: {', '.join(subject_parts)} ({label})"
    return subject, "".join(lines)


def build_error_email(interval_minutes, error_message=None, context=None):
    label = format_interval(interval_minutes) if interval_minutes else "unknown"
    subject = "Kraken API Error"
    details = [
        "<html><body>",
        "<h2>Kraken API Error</h2>",
        f"<p><strong>Interval:</strong> {html.escape(label)}</p>",
    ]

    if error_message:
        details.append(
            f"<p><strong>Message:</strong> {html.escape(str(error_message))}</p>"
        )
    if context:
        details.append(
            f"<p><strong>Context:</strong> {html.escape(str(context))}</p>"
        )

    details.append(
        "<p><strong>Details:</strong> Review the workflow logs and Kraken API "
        "responses.</p>"
    )
    details.append("</body></html>")
    return subject, "".join(details)


def send_combined_alert(hits_by_analysis, interval_minutes, require_next_candle_confirmation=False):
    subject, body = build_combined_email(
        hits_by_analysis,
        interval_minutes,
        require_next_candle_confirmation,
    )
    send_html_email(subject, body, html=True)


def send_error_alert(error_message, context=None, interval_minutes=None):
    subject, body = build_error_email(
        interval_minutes,
        error_message=error_message,
        context=context,
    )
    send_html_email(subject, body, html=True)


# -----------------------------------------------------------------------------
# Scanner and scheduling
# -----------------------------------------------------------------------------


def scan_once(interval_minutes, require_next_candle_confirmation=False):
    label = format_interval(interval_minutes)
    mode = (
        "next-candle confirmation"
        if require_next_candle_confirmation
        else "closed-candle confirmation"
    )
    log.info(
        "=== Starting scan (interval=%s, mode=%s, analyses=%s) ===",
        label,
        mode,
        [spec["key"] for spec in ANALYSES],
    )

    state = load_state()
    for spec in ANALYSES:
        state.setdefault(spec["key"], {})

    api_errors = []

    try:
        pairs = get_tradable_pairs(error_sink=api_errors)
    except Exception as exc:
        log.error("Unable to fetch Kraken pairs: %s", exc)
        send_error_alert(
            str(exc),
            context="Fetching tradable pairs",
            interval_minutes=interval_minutes,
        )
        return {}

    log.info(
        "Scanning %d pairs (quote filter=%s)...",
        len(pairs),
        QUOTE_FILTER,
    )

    hits_by_analysis = {spec["key"]: [] for spec in ANALYSES}
    unexpected_errors = 0
    requested_count = _max_requested_candle_count(require_next_candle_confirmation)

    for index, (pair, wsname) in enumerate(pairs.items(), start=1):
        try:
            # One Kraken OHLC fetch per pair, shared across every analysis.
            candles = get_candles(
                pair,
                requested_count,
                interval_minutes,
                error_sink=api_errors,
            )

            if candles:
                # Kraken's final OHLC item is the currently forming candle.
                closed_candles = candles[:-1]

                for spec in ANALYSES:
                    result = spec["run"](
                        pair,
                        wsname,
                        closed_candles,
                        interval_minutes,
                        require_next_candle_confirmation=require_next_candle_confirmation,
                    )

                    if result is None:
                        continue

                    if in_cooldown(
                        state[spec["key"]],
                        pair,
                        result["alert_epoch"],
                        interval_minutes,
                    ):
                        log.debug(
                            "%s: %s passed filters but is still in cooldown",
                            wsname or pair,
                            spec["key"],
                        )
                        continue

                    hits_by_analysis[spec["key"]].append(result)
                    spec["log_hit"](result, label)
        except Exception as exc:
            unexpected_errors += 1
            message = f"Skipping {pair}: {exc}"
            log.exception(message)
            api_errors.append(message)

        time.sleep(REQUEST_DELAY_SEC)

        if index % 50 == 0:
            total_hits_so_far = sum(len(hits) for hits in hits_by_analysis.values())
            log.info(
                "...%d/%d scanned (%d hit(s), %d unexpected error(s))",
                index,
                len(pairs),
                total_hits_so_far,
                unexpected_errors,
            )

    total_hits = sum(len(hits) for hits in hits_by_analysis.values())
    log.info(
        "=== Scan complete: %d pairs, %d hit(s) (%s), "
        "%d unexpected error(s), %d API issue(s) ===",
        len(pairs),
        total_hits,
        ", ".join(f"{spec['key']}={len(hits_by_analysis[spec['key']])}" for spec in ANALYSES),
        unexpected_errors,
        len(api_errors),
    )

    if len(api_errors) >= API_ERROR_ALERT_THRESHOLD:
        send_error_alert(
            "Multiple Kraken API requests failed during the scan.",
            context=(
                f"{len(api_errors)} issue(s) recorded; "
                f"first issue: {api_errors[0]}"
            ),
            interval_minutes=interval_minutes,
        )

    if total_hits:
        for spec in ANALYSES:
            hits = hits_by_analysis[spec["key"]]
            if not hits:
                continue

            hits.sort(key=spec["sort_key"], reverse=True)
            for hit in hits:
                state[spec["key"]][hit["pair_key"]] = hit["alert_epoch"]

        send_combined_alert(
            hits_by_analysis,
            interval_minutes,
            require_next_candle_confirmation=require_next_candle_confirmation,
        )
        save_state(state)
    else:
        log.info("No hits this scan.")

    return hits_by_analysis


def run_crypto_scan(
    interval_minutes=None,
    require_next_candle_confirmation=False,
):
    interval_minutes = (
        interval_minutes or get_interval_minutes(DEFAULT_INTERVAL_MINUTES)
    )
    scan_once(
        interval_minutes,
        require_next_candle_confirmation=require_next_candle_confirmation,
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Kraken closed-candle alert bot: breakout (ATR/robust-volume/"
            "liquidity filters) and volume-surge analyses share a single "
            "Kraken fetch and one combined email per scan."
        )
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CANDLE_INTERVAL_MIN,
        help=(
            f"Candle size in minutes. Valid Kraken values: {VALID_INTERVALS} "
            f"(default {CANDLE_INTERVAL_MIN})"
        ),
    )
    parser.add_argument(
        "--confirm-next-candle",
        action="store_true",
        default=DEFAULT_REQUIRE_NEXT_CANDLE_CONFIRMATION,
        help=(
            "Require the candle after a breakout to hold the broken level. "
            "This reduces false breakouts but delays breakout alerts by one "
            "candle. Does not affect the volume-surge analysis."
        ),
    )
    args = parser.parse_args()

    if args.interval not in VALID_INTERVALS:
        log.error(
            "Invalid --interval %s. Must be one of %s.",
            args.interval,
            VALID_INTERVALS,
        )
        sys.exit(1)

    run_crypto_scan(
        interval_minutes=args.interval,
        require_next_candle_confirmation=args.confirm_next_candle,
    )


if __name__ == "__main__":
    main()
