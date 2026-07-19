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
# A simpler, separate job: alert on any pair whose latest closed candle shows
# a volume spike and a large price move, without the breakout/candle-quality
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
VOLUME_ALERT_STATE_FILE = get_env(
    "VOLUME_ALERT_STATE_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "kraken_volume_alert_state.json",
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


def evaluate_pair(
    pair,
    wsname,
    interval_minutes,
    state,
    require_next_candle_confirmation=False,
    error_sink=None,
):
    history_needed = max(
        BREAKOUT_LOOKBACK,
        VOLUME_LOOKBACK,
        ATR_PERIOD + 1,
    )

    # Extra candles:
    #   1 = Kraken's currently forming candle, which must never be evaluated.
    #   1 = the closed signal candle.
    #   1 more = optional next closed candle used for confirmation.
    extra_closed_candles = 2 if require_next_candle_confirmation else 1
    requested_count = history_needed + extra_closed_candles + 1

    candles = get_candles(
        pair,
        requested_count,
        interval_minutes,
        error_sink=error_sink,
    )

    if not candles or len(candles) < requested_count:
        return None

    # Kraken's final OHLC item is the currently forming candle.
    closed_candles = candles[:-1]

    if require_next_candle_confirmation:
        history = closed_candles[:-2]
        signal_candle = closed_candles[-2]
        confirmation_candle = closed_candles[-1]
    else:
        history = closed_candles[:-1]
        signal_candle = closed_candles[-1]
        confirmation_candle = None

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
        alert_epoch = confirmation_candle["time"]
    else:
        alert_epoch = signal_candle["time"]

    if in_cooldown(state, pair, alert_epoch, interval_minutes):
        log.debug(
            "%s: passed all filters but is still in cooldown",
            wsname or pair,
        )
        return None

    hit["alert_epoch"] = alert_epoch
    return hit


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


def evaluate_volume_price_pair(
    pair,
    wsname,
    interval_minutes,
    state,
    error_sink=None,
):
    # Extra candles: 1 = Kraken's currently forming candle (never evaluated),
    # 1 = the closed signal candle.
    requested_count = VOLUME_LOOKBACK + 2

    candles = get_candles(
        pair,
        requested_count,
        interval_minutes,
        error_sink=error_sink,
    )

    if not candles or len(candles) < requested_count:
        return None

    # Kraken's final OHLC item is the currently forming candle.
    closed_candles = candles[:-1]
    history = closed_candles[:-1]
    signal_candle = closed_candles[-1]

    hit = evaluate_volume_price_candle(pair, wsname, history, signal_candle)
    if hit is None:
        return None

    alert_epoch = signal_candle["time"]

    if in_cooldown(state, pair, alert_epoch, interval_minutes):
        log.debug(
            "%s: passed volume/price filters but is still in cooldown",
            wsname or pair,
        )
        return None

    hit["alert_epoch"] = alert_epoch
    return hit


# -----------------------------------------------------------------------------
# Email output
# -----------------------------------------------------------------------------


def build_email_content(
    alert_type,
    hits=None,
    interval_minutes=None,
    error_message=None,
    context=None,
    require_next_candle_confirmation=False,
):
    label = format_interval(interval_minutes) if interval_minutes else "unknown"

    if alert_type == "crypto_alert":
        hits = list(hits or [])
        confirmation_label = (
            "next-candle confirmation required"
            if require_next_candle_confirmation
            else "closed signal candle"
        )

        subject = f"Kraken Confirmed Breakouts: {len(hits)} pair(s) ({label})"
        lines = [
            "<html><body>",
            "<h2>Kraken Confirmed Breakouts</h2>",
            f"<p><strong>Interval:</strong> {html.escape(label)}</p>",
            f"<p><strong>Mode:</strong> {html.escape(confirmation_label)}</p>",
            "<p>Signals passed range, ATR, candle-quality, robust-volume, "
            "and liquidity filters.</p>",
            "<ul>",
        ]

        for hit in hits:
            pair = html.escape(str(hit["pair"]))
            signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M UTC")
            confirmation_text = ""

            if hit["confirmed_by_next_candle"]:
                confirmation_time = hit["confirmation_time"].strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                confirmation_text = (
                    f" · confirmed {confirmation_time}"
                    f" at {hit['confirmation_close']:.8g}"
                )

            lines.append(
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

        lines.extend(["</ul>", "</body></html>"])
        return subject, "".join(lines)

    if alert_type == "crypto_volume_alert":
        hits = list(hits or [])

        subject = f"Kraken Volume Spike Alerts: {len(hits)} pair(s) ({label})"
        lines = [
            "<html><body>",
            "<h2>Kraken Volume Spike Alerts</h2>",
            f"<p><strong>Interval:</strong> {html.escape(label)}</p>",
            f"<p>Signals passed volume &gt;= {VOLUME_SPIKE_MULTIPLE:.1f}x median and "
            f"price move &gt;= {PRICE_CHANGE_ALERT_PCT:.1f}% on the closed signal "
            "candle, plus liquidity filters.</p>",
            "<ul>",
        ]

        for hit in hits:
            pair = html.escape(str(hit["pair"]))
            signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M UTC")

            lines.append(
                f"<li><strong>{pair}</strong> {hit['direction']}"
                f" · move {hit['price_change_pct']:+.2f}%"
                f" · close {hit['close']:.8g}"
                f" · quote volume ${hit['quote_volume']:,.0f}"
                f" ({hit['volume_multiple']:.1f}x median)"
                f" · signal {signal_time}</li>"
            )

        lines.extend(["</ul>", "</body></html>"])
        return subject, "".join(lines)

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


def send_crypto_alert(
    hits,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    subject, body = build_email_content(
        "crypto_alert",
        hits=hits,
        interval_minutes=interval_minutes,
        require_next_candle_confirmation=require_next_candle_confirmation,
    )
    send_html_email(subject, body, html=True)


def send_volume_price_alert(hits, interval_minutes):
    subject, body = build_email_content(
        "crypto_volume_alert",
        hits=hits,
        interval_minutes=interval_minutes,
    )
    send_html_email(subject, body, html=True)


def send_error_alert(error_message, context=None, interval_minutes=None):
    subject, body = build_email_content(
        "error",
        interval_minutes=interval_minutes,
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
        "=== Starting scan (interval=%s, mode=%s) ===",
        label,
        mode,
    )

    state = load_state()
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
        return []

    log.info(
        "Scanning %d pairs (quote filter=%s)...",
        len(pairs),
        QUOTE_FILTER,
    )

    hits = []
    unexpected_errors = 0

    for index, (pair, wsname) in enumerate(pairs.items(), start=1):
        try:
            result = evaluate_pair(
                pair,
                wsname,
                interval_minutes,
                state,
                require_next_candle_confirmation=require_next_candle_confirmation,
                error_sink=api_errors,
            )

            if result:
                hits.append(result)
                log.info(
                    "HIT: %s %s breakout, volume %.1fx (z=%.1f), "
                    "price %+.2f%%, body %.2f ATR, breakout %.2f ATR (%s)",
                    result["pair"],
                    result["direction"],
                    result["volume_multiple"],
                    result["volume_robust_z"],
                    result["price_change_pct"],
                    result["body_atr"],
                    result["breakout_strength_atr"],
                    label,
                )
        except Exception as exc:
            unexpected_errors += 1
            message = f"Skipping {pair}: {exc}"
            log.exception(message)
            api_errors.append(message)

        time.sleep(REQUEST_DELAY_SEC)

        if index % 50 == 0:
            log.info(
                "...%d/%d scanned (%d hit(s), %d unexpected error(s))",
                index,
                len(pairs),
                len(hits),
                unexpected_errors,
            )

    log.info(
        "=== Scan complete: %d pairs, %d confirmed breakout(s), "
        "%d unexpected error(s), %d API issue(s) ===",
        len(pairs),
        len(hits),
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

    if hits:
        hits.sort(
            key=lambda entry: (
                entry["volume_robust_z"],
                entry["breakout_strength_atr"],
            ),
            reverse=True,
        )
        send_crypto_alert(
            hits,
            interval_minutes,
            require_next_candle_confirmation=require_next_candle_confirmation,
        )

        for hit in hits:
            state[hit["pair_key"]] = hit["alert_epoch"]
        save_state(state)
    else:
        log.info("No confirmed breakouts this scan.")

    return hits


def scan_once_volume_price(interval_minutes):
    label = format_interval(interval_minutes)
    log.info(
        "=== Starting volume-spike scan (interval=%s) ===",
        label,
    )

    state = load_state(VOLUME_ALERT_STATE_FILE)
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
        return []

    log.info(
        "Scanning %d pairs (quote filter=%s)...",
        len(pairs),
        QUOTE_FILTER,
    )

    hits = []
    unexpected_errors = 0

    for index, (pair, wsname) in enumerate(pairs.items(), start=1):
        try:
            result = evaluate_volume_price_pair(
                pair,
                wsname,
                interval_minutes,
                state,
                error_sink=api_errors,
            )

            if result:
                hits.append(result)
                log.info(
                    "HIT: %s %s volume %.1fx, price %+.2f%% (%s)",
                    result["pair"],
                    result["direction"],
                    result["volume_multiple"],
                    result["price_change_pct"],
                    label,
                )
        except Exception as exc:
            unexpected_errors += 1
            message = f"Skipping {pair}: {exc}"
            log.exception(message)
            api_errors.append(message)

        time.sleep(REQUEST_DELAY_SEC)

        if index % 50 == 0:
            log.info(
                "...%d/%d scanned (%d hit(s), %d unexpected error(s))",
                index,
                len(pairs),
                len(hits),
                unexpected_errors,
            )

    log.info(
        "=== Volume-spike scan complete: %d pairs, %d hit(s), "
        "%d unexpected error(s), %d API issue(s) ===",
        len(pairs),
        len(hits),
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

    if hits:
        hits.sort(key=lambda entry: entry["volume_multiple"], reverse=True)
        send_volume_price_alert(hits, interval_minutes)

        for hit in hits:
            state[hit["pair_key"]] = hit["alert_epoch"]
        save_state(state, VOLUME_ALERT_STATE_FILE)
    else:
        log.info("No volume-spike hits this scan.")

    return hits


def run_volume_price_scan(interval_minutes=None):
    interval_minutes = (
        interval_minutes or get_interval_minutes(DEFAULT_INTERVAL_MINUTES)
    )
    scan_once_volume_price(interval_minutes)


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
            "Kraken closed-candle breakout alert bot with ATR, robust volume, "
            "liquidity, and optional next-candle confirmation"
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
            "Require the candle after the breakout to hold the broken level. "
            "This reduces false breakouts but delays alerts by one candle."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["breakout", "volume"],
        default="breakout",
        help=(
            "'breakout' runs the ATR/candle-quality breakout scan (default). "
            "'volume' runs the simpler volume-spike scan: volume >= "
            f"{VOLUME_SPIKE_MULTIPLE:.1f}x median and price move >= "
            f"{PRICE_CHANGE_ALERT_PCT:.1f}% on the closed candle."
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

    if args.mode == "volume":
        run_volume_price_scan(interval_minutes=args.interval)
    else:
        run_crypto_scan(
            interval_minutes=args.interval,
            require_next_candle_confirmation=args.confirm_next_candle,
        )


if __name__ == "__main__":
    main()
