import argparse
import html
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from statistics import median
from zoneinfo import ZoneInfo

import requests

from technical_analysis.common.emailing import send_email_message as send_html_email
from technical_analysis.config import DEFAULT_INTERVAL_MINUTES, get_env, get_interval_minutes
from technical_analysis.crypto.config import (
    API_ERROR_ALERT_THRESHOLD,
    ATR_PERIOD,
    BREAKOUT_BUFFER_ATR,
    BREAKOUT_LOOKBACK,
    BREAKOUT_MIN_PRICE_CHANGE_PCT,
    CONFIRM_RETEST_TOLERANCE_ATR,
    COOLDOWN_CANDLES,
    DEFAULT_REQUIRE_NEXT_CANDLE_CONFIRMATION,
    EMA9_FAST_PERIOD,
    EMA9_MIN_SEPARATION_ATR,
    EMA9_MIN_SLOPE_ATR,
    EMA9_PULLBACK_TOUCH_ATR,
    EMA9_SLOW_PERIOD,
    EMA9_TREND_LOOKBACK,
    EMA9_WARMUP_CANDLES,
    EMA_FAST_PERIOD,
    EMA_MIN_SEPARATION_ATR,
    EMA_MIN_SLOPE_ATR,
    EMA_PULLBACK_TOUCH_ATR,
    EMA_SLOW_PERIOD,
    EMA_TREND_LOOKBACK,
    EMA_WARMUP_CANDLES,
    KRAKEN_API_URL,
    MIN_24H_QUOTE_VOLUME,
    MIN_BODY_ATR,
    MIN_BODY_RATIO,
    MIN_CLOSE_LOCATION,
    MIN_MEDIAN_QUOTE_VOLUME,
    MIN_MEDIAN_TRADE_COUNT,
    MIN_SIGNAL_TRADE_COUNT,
    MIN_VOLUME_MULTIPLE,
    MIN_VOLUME_ROBUST_Z,
    MOMENTUM_CANDLE_COUNT,
    MOMENTUM_PRICE_CHANGE_PCT,
    MOMENTUM_VOLUME_LOOKBACK,
    PRICE_CHANGE_ALERT_PCT,
    QUOTE_FILTER,
    REQUEST_DELAY_SEC,
    REQUEST_RETRIES,
    REQUEST_RETRY_DELAY_SEC,
    REQUEST_TIMEOUT_SEC,
    REQUIRE_LIQUIDITY_FILTER,
    SKIP_BASE_CURRENCIES,
    VALID_INTERVALS,
    VOLUME_LOOKBACK,
    VOLUME_SPIKE_MULTIPLE,
)


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

# Cloud Run Jobs have no persistent local disk across executions -- without
# this, cooldowns silently reset to empty on every single run. When set,
# state is read/written from this GCS object instead of ALERT_STATE_FILE.
# Local/dev runs without it fall back to the plain local file above.
ALERT_STATE_BUCKET = get_env("ALERT_STATE_BUCKET")
ALERT_STATE_BLOB_NAME = get_env("ALERT_STATE_BLOB_NAME", "kraken_alert_state.json")

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


# Kraken candle timestamps are UTC epoch seconds; alert emails/logs display
# them in US Eastern (EDT/EST, whichever is in effect for that timestamp)
# since that's the timezone the alerts are actually read in.
DISPLAY_TZ = ZoneInfo("America/New_York")


def to_display_datetime(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone(DISPLAY_TZ)


def format_price(value, sig_figs=4):
    """Format a price to a fixed number of significant figures without ever
    switching to scientific notation (unlike `:.8g`, which reads as
    unreadable exponents like `2.823e-06` for low-price tokens)."""
    if value == 0:
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    decimals = max(0, sig_figs - 1 - magnitude)
    return f"{value:,.{decimals}f}"


def format_compact_volume(value):
    """Format a dollar volume compactly (e.g. $812.45M, $200K) instead of
    a long digit string like $812,450,000 -- easier to scan in an email."""
    sign = "-" if value < 0 else ""
    value = abs(value)

    if value >= 1_000_000_000:
        number, suffix = value / 1_000_000_000, "B"
    elif value >= 1_000_000:
        number, suffix = value / 1_000_000, "M"
    elif value >= 1_000:
        number, suffix = value / 1_000, "K"
    else:
        return f"{sign}${value:,.0f}"

    text = f"{number:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{sign}${text}{suffix}"


def _gcs_state_blob():
    from google.cloud import storage

    client = storage.Client()
    return client.bucket(ALERT_STATE_BUCKET).blob(ALERT_STATE_BLOB_NAME)


def _load_state_from_gcs():
    from google.api_core.exceptions import NotFound

    try:
        raw = _gcs_state_blob().download_as_text()
    except NotFound:
        log.debug(
            "No existing state object at gs://%s/%s yet.",
            ALERT_STATE_BUCKET,
            ALERT_STATE_BLOB_NAME,
        )
        return {}
    except Exception as exc:
        log.warning(
            "Could not read state from gs://%s/%s, starting fresh: %s",
            ALERT_STATE_BUCKET,
            ALERT_STATE_BLOB_NAME,
            exc,
        )
        return {}

    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except Exception as exc:
        log.warning("Could not parse state from GCS, starting fresh: %s", exc)
        return {}


def _save_state_to_gcs(state):
    try:
        _gcs_state_blob().upload_from_string(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            content_type="application/json",
        )
    except Exception as exc:
        log.warning(
            "Could not write state to gs://%s/%s: %s",
            ALERT_STATE_BUCKET,
            ALERT_STATE_BLOB_NAME,
            exc,
        )


def load_state(state_file=ALERT_STATE_FILE):
    if ALERT_STATE_BUCKET:
        return _load_state_from_gcs()

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
    if ALERT_STATE_BUCKET:
        _save_state_to_gcs(state)
        return

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


def calculate_ema_series(candles, period):
    """Return an EMA series over `candles`, seeded with an SMA of the first
    `period` closes. series[-1] is the EMA as of candles[-1], series[-2] as
    of candles[-2], and so on -- callers only need the last one or two
    values, so alignment to the *start* of `candles` does not matter.
    """
    if len(candles) < period:
        return []

    closes = [candle["close"] for candle in candles]
    multiplier = 2 / (period + 1)
    ema_values = [sum(closes[:period]) / period]

    for close in closes[period:]:
        ema_values.append((close - ema_values[-1]) * multiplier + ema_values[-1])

    return ema_values


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

    if not is_up_breakout:
        return None

    direction = "UP"
    breakout_level = range_high
    breakout_distance = signal_candle["close"] - breakout_level
    breakout_strength_atr = breakout_distance / atr

    volume_window = history[-VOLUME_LOOKBACK:]
    historical_quote_volumes = [
        candle["volume"] * candle["vwap"] for candle in volume_window
    ]
    current_quote_volume = signal_candle["volume"] * signal_candle["vwap"]
    median_quote_volume = median(historical_quote_volumes)
    quote_volume_24h = sum(historical_quote_volumes) + current_quote_volume

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
    passes_price_change = price_change_pct >= BREAKOUT_MIN_PRICE_CHANGE_PCT
    passes_volume = (
        volume_multiple >= MIN_VOLUME_MULTIPLE
        and volume_robust_z >= MIN_VOLUME_ROBUST_Z
    )
    passes_liquidity = (not REQUIRE_LIQUIDITY_FILTER) or (
        median_quote_volume >= MIN_MEDIAN_QUOTE_VOLUME
        and median_trade_count >= MIN_MEDIAN_TRADE_COUNT
        and signal_candle["count"] >= MIN_SIGNAL_TRADE_COUNT
    )
    passes_volume_24h = quote_volume_24h >= MIN_24H_QUOTE_VOLUME

    log.debug(
        "%s: direction=%s price=%+.2f%% body=%.2f body_atr=%.2f "
        "close_location=%.2f breakout_atr=%.2f volume=%.2fx z=%.2f "
        "median_quote_volume=%.2f median_trades=%.1f signal_trades=%d "
        "quote_volume_24h=%.2f "
        "filters(conviction=%s move=%s price_change=%s volume=%s liquidity=%s volume_24h=%s)",
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
        quote_volume_24h,
        passes_conviction,
        passes_move,
        passes_price_change,
        passes_volume,
        passes_liquidity,
        passes_volume_24h,
    )

    if not (
        passes_conviction
        and passes_move
        and passes_price_change
        and passes_volume
        and passes_liquidity
        and passes_volume_24h
    ):
        return None

    close_extreme_ratio = close_location

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
        "quote_volume_24h": quote_volume_24h,
        "volume_multiple": volume_multiple,
        "volume_robust_z": volume_robust_z,
        "signal_trade_count": signal_candle["count"],
        "median_trade_count": median_trade_count,
        "signal_time": to_display_datetime(signal_candle["time"]),
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

    confirmed = (
        confirmation_candle["close"] > level
        and confirmation_candle["low"] >= level - tolerance
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

    hit["confirmation_time"] = to_display_datetime(confirmation_candle["time"])
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


def _render_alert_card(pair, headline, detail):
    return (
        '<div style="border-left:4px solid #2e7d32;background:#f6fbf6;'
        'padding:10px 14px;margin:10px 0;border-radius:4px;'
        'font-family:-apple-system,Arial,sans-serif;">'
        f'<div style="font-size:15px;font-weight:600;color:#1a1a1a;">'
        f'{pair} <span style="color:#2e7d32;">▲ {headline}</span></div>'
        f'<div style="font-size:12.5px;color:#5a5a5a;margin-top:4px;'
        f'line-height:1.5;">{detail}</div>'
        "</div>"
    )


def render_breakout_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")
    confirmation_text = ""

    if hit["confirmed_by_next_candle"]:
        confirmation_time = hit["confirmation_time"].strftime("%Y-%m-%d %H:%M %Z")
        confirmation_text = (
            f" · confirmed {confirmation_time}"
            f" at {format_price(hit['confirmation_close'])}"
        )

    headline = f"{hit['direction']} breakout {hit['price_change_pct']:+.2f}%"
    detail = (
        f"close {format_price(hit['close'])}"
        f" · level {format_price(hit['breakout_level'])} ({hit['breakout_strength_atr']:.2f} ATR)"
        f" · body {hit['body_ratio'] * 100:.0f}%/{hit['body_atr']:.2f} ATR"
        f" · close-at-extreme {hit['close_extreme_ratio'] * 100:.0f}%"
        f" · volume ${hit['quote_volume']:,.0f}"
        f" ({hit['volume_multiple']:.1f}x median, z={hit['volume_robust_z']:.1f})"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}{confirmation_text}"
    )
    return _render_alert_card(pair, headline, detail)


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
    quote_volume_24h = sum(historical_quote_volumes) + current_quote_volume

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
    passes_price = price_change_pct >= PRICE_CHANGE_ALERT_PCT
    passes_liquidity = (not REQUIRE_LIQUIDITY_FILTER) or (
        median_quote_volume >= MIN_MEDIAN_QUOTE_VOLUME
        and median_trade_count >= MIN_MEDIAN_TRADE_COUNT
        and signal_candle["count"] >= MIN_SIGNAL_TRADE_COUNT
    )
    passes_volume_24h = quote_volume_24h >= MIN_24H_QUOTE_VOLUME

    log.debug(
        "%s: price=%+.2f%% volume=%.2fx median_quote_volume=%.2f "
        "median_trades=%.1f signal_trades=%d quote_volume_24h=%.2f "
        "filters(volume=%s price=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        price_change_pct,
        volume_multiple,
        median_quote_volume,
        median_trade_count,
        signal_candle["count"],
        quote_volume_24h,
        passes_volume,
        passes_price,
        passes_liquidity,
        passes_volume_24h,
    )

    if not (passes_volume and passes_price and passes_liquidity and passes_volume_24h):
        return None

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": "UP",
        "open": signal_candle["open"],
        "high": signal_candle["high"],
        "low": signal_candle["low"],
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "base_volume": signal_candle["volume"],
        "quote_volume": current_quote_volume,
        "median_quote_volume": median_quote_volume,
        "quote_volume_24h": quote_volume_24h,
        "volume_multiple": volume_multiple,
        "signal_trade_count": signal_candle["count"],
        "median_trade_count": median_trade_count,
        "signal_time": to_display_datetime(signal_candle["time"]),
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
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = f"{hit['direction']} volume surge {hit['price_change_pct']:+.2f}%"
    detail = (
        f"close {format_price(hit['close'])}"
        f" · volume ${hit['quote_volume']:,.0f} ({hit['volume_multiple']:.1f}x median)"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


# -----------------------------------------------------------------------------
# EMA evaluation
#
# evaluate_ema_trend_pullback_candle: is there an established uptrend, and
# did price just bounce off the fast EMA in that trend's direction?
# -----------------------------------------------------------------------------


def _ema_ready(closed_candles):
    """Compute the fast/slow EMA series and ATR needed by the EMA analysis,
    or None if there isn't enough warmed-up history yet."""
    if len(closed_candles) < _ema_closed_candles_needed():
        return None

    fast_series = calculate_ema_series(closed_candles, EMA_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, EMA_SLOW_PERIOD)
    if len(fast_series) < 2 or len(slow_series) < 2:
        return None

    # ATR is a "how big is a normal move" baseline, so -- same as the
    # breakout analysis -- it must be computed as of *before* the signal
    # candle, not including it. Otherwise the signal candle's own range
    # would inflate the baseline it's being measured against.
    atr = calculate_atr(closed_candles[:-1], ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    return fast_series, slow_series, atr


def _liquidity_stats(history, signal_candle):
    volume_window = history[-VOLUME_LOOKBACK:]
    historical_quote_volumes = [
        candle["volume"] * candle["vwap"] for candle in volume_window
    ]
    historical_trade_counts = [candle["count"] for candle in volume_window]
    median_quote_volume = median(historical_quote_volumes)
    median_trade_count = median(historical_trade_counts)
    quote_volume_24h = sum(historical_quote_volumes) + (
        signal_candle["volume"] * signal_candle["vwap"]
    )

    passes_liquidity = (not REQUIRE_LIQUIDITY_FILTER) or (
        median_quote_volume >= MIN_MEDIAN_QUOTE_VOLUME
        and median_trade_count >= MIN_MEDIAN_TRADE_COUNT
        and signal_candle["count"] >= MIN_SIGNAL_TRADE_COUNT
    )
    passes_volume_24h = quote_volume_24h >= MIN_24H_QUOTE_VOLUME
    return passes_liquidity, passes_volume_24h, quote_volume_24h


def _ema_closed_candles_needed():
    return EMA_SLOW_PERIOD + EMA_WARMUP_CANDLES


def evaluate_ema_trend_pullback_candle(pair, wsname, closed_candles):
    ready = _ema_ready(closed_candles)
    if ready is None:
        return None
    fast_series, slow_series, atr = ready

    if len(slow_series) <= EMA_TREND_LOOKBACK:
        return None

    signal_candle = closed_candles[-1]
    if signal_candle["open"] <= 0:
        return None

    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]
    ema_slow_then = slow_series[-1 - EMA_TREND_LOOKBACK]

    slope_atr = (ema_slow_now - ema_slow_then) / EMA_TREND_LOOKBACK / atr
    separation_atr = abs(ema_fast_now - ema_slow_now) / atr

    trend_up = (
        ema_fast_now > ema_slow_now
        and slope_atr >= EMA_MIN_SLOPE_ATR
        and separation_atr >= EMA_MIN_SEPARATION_ATR
    )

    if not trend_up:
        return None

    touch_distance_atr = abs(signal_candle["low"] - ema_fast_now) / atr
    touched_fast_ema = touch_distance_atr <= EMA_PULLBACK_TOUCH_ATR

    reclaimed = (
        signal_candle["close"] > ema_fast_now
        and signal_candle["close"] > signal_candle["open"]
    )

    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: ema_trend_pullback trend_up=%s slope_atr=%.2f "
        "separation_atr=%.2f touch_distance_atr=%.2f quote_volume_24h=%.2f "
        "filters(touched=%s reclaimed=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        trend_up,
        slope_atr,
        separation_atr,
        touch_distance_atr,
        quote_volume_24h,
        touched_fast_ema,
        reclaimed,
        passes_liquidity,
        passes_volume_24h,
    )

    if not (touched_fast_ema and reclaimed and passes_liquidity and passes_volume_24h):
        return None

    price_change_pct = (
        (signal_candle["close"] - signal_candle["open"])
        / signal_candle["open"]
        * 100.0
    )

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": "UP",
        "open": signal_candle["open"],
        "high": signal_candle["high"],
        "low": signal_candle["low"],
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "ema_fast": ema_fast_now,
        "ema_slow": ema_slow_now,
        "ema_separation_atr": separation_atr,
        "trend_slope_atr": slope_atr,
        "touch_distance_atr": touch_distance_atr,
        "quote_volume_24h": quote_volume_24h,
        "signal_time": to_display_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def run_ema_trend_pullback_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    hit = evaluate_ema_trend_pullback_candle(pair, wsname, closed_candles)
    if hit is None:
        return None

    hit["alert_epoch"] = hit["signal_epoch"]
    return hit


def log_ema_trend_pullback_hit(hit, label):
    log.info(
        "HIT[ema_trend_pullback]: %s %s pullback, slope %+.2f ATR/candle, "
        "touch %.2f ATR, price %+.2f%% (%s)",
        hit["pair"],
        hit["direction"],
        hit["trend_slope_atr"],
        hit["touch_distance_atr"],
        hit["price_change_pct"],
        label,
    )


def render_ema_trend_pullback_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = (
        f"{hit['direction']} 20 EMA pullback in uptrend {hit['price_change_pct']:+.2f}%"
    )
    detail = (
        f"close {format_price(hit['close'])}"
        f" · 20 EMA {format_price(hit['ema_fast'])} / 50 EMA {format_price(hit['ema_slow'])}"
        f" · trend slope {hit['trend_slope_atr']:+.2f} ATR/candle"
        f" · touched 20 EMA at {hit['touch_distance_atr']:.2f} ATR"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


# -----------------------------------------------------------------------------
# 9 EMA pullback evaluation
#
# evaluate_ema9_pullback_candle: same idea as evaluate_ema_trend_pullback_candle
# (is there an established uptrend, and did price just bounce off the fast
# EMA in that trend's direction?) but keyed off the faster 9/21 EMA pair so
# it can confirm a trend -- and catch the first pullback within it -- much
# sooner after a fresh impulse move.
# -----------------------------------------------------------------------------


def _ema9_closed_candles_needed():
    return EMA9_SLOW_PERIOD + EMA9_WARMUP_CANDLES


def _ema9_ready(closed_candles):
    if len(closed_candles) < _ema9_closed_candles_needed():
        return None

    fast_series = calculate_ema_series(closed_candles, EMA9_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, EMA9_SLOW_PERIOD)
    if len(fast_series) < 2 or len(slow_series) < 2:
        return None

    atr = calculate_atr(closed_candles[:-1], ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    return fast_series, slow_series, atr


def evaluate_ema9_pullback_candle(pair, wsname, closed_candles):
    ready = _ema9_ready(closed_candles)
    if ready is None:
        return None
    fast_series, slow_series, atr = ready

    if len(slow_series) <= EMA9_TREND_LOOKBACK:
        return None

    signal_candle = closed_candles[-1]
    if signal_candle["open"] <= 0:
        return None

    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]
    ema_slow_then = slow_series[-1 - EMA9_TREND_LOOKBACK]

    slope_atr = (ema_slow_now - ema_slow_then) / EMA9_TREND_LOOKBACK / atr
    separation_atr = abs(ema_fast_now - ema_slow_now) / atr

    trend_up = (
        ema_fast_now > ema_slow_now
        and slope_atr >= EMA9_MIN_SLOPE_ATR
        and separation_atr >= EMA9_MIN_SEPARATION_ATR
    )

    if not trend_up:
        return None

    touch_distance_atr = abs(signal_candle["low"] - ema_fast_now) / atr
    touched_fast_ema = touch_distance_atr <= EMA9_PULLBACK_TOUCH_ATR

    reclaimed = (
        signal_candle["close"] > ema_fast_now
        and signal_candle["close"] > signal_candle["open"]
    )

    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: ema9_pullback trend_up=%s slope_atr=%.2f "
        "separation_atr=%.2f touch_distance_atr=%.2f quote_volume_24h=%.2f "
        "filters(touched=%s reclaimed=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        trend_up,
        slope_atr,
        separation_atr,
        touch_distance_atr,
        quote_volume_24h,
        touched_fast_ema,
        reclaimed,
        passes_liquidity,
        passes_volume_24h,
    )

    if not (touched_fast_ema and reclaimed and passes_liquidity and passes_volume_24h):
        return None

    price_change_pct = (
        (signal_candle["close"] - signal_candle["open"])
        / signal_candle["open"]
        * 100.0
    )

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": "UP",
        "open": signal_candle["open"],
        "high": signal_candle["high"],
        "low": signal_candle["low"],
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "ema_fast": ema_fast_now,
        "ema_slow": ema_slow_now,
        "ema_separation_atr": separation_atr,
        "trend_slope_atr": slope_atr,
        "touch_distance_atr": touch_distance_atr,
        "quote_volume_24h": quote_volume_24h,
        "signal_time": to_display_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def run_ema9_pullback_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    hit = evaluate_ema9_pullback_candle(pair, wsname, closed_candles)
    if hit is None:
        return None

    hit["alert_epoch"] = hit["signal_epoch"]
    return hit


def log_ema9_pullback_hit(hit, label):
    log.info(
        "HIT[ema9_pullback]: %s %s pullback, slope %+.2f ATR/candle, "
        "touch %.2f ATR, price %+.2f%% (%s)",
        hit["pair"],
        hit["direction"],
        hit["trend_slope_atr"],
        hit["touch_distance_atr"],
        hit["price_change_pct"],
        label,
    )


def render_ema9_pullback_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = (
        f"{hit['direction']} 9 EMA pullback in uptrend {hit['price_change_pct']:+.2f}%"
    )
    detail = (
        f"close {format_price(hit['close'])}"
        f" · 9 EMA {format_price(hit['ema_fast'])} / 21 EMA {format_price(hit['ema_slow'])}"
        f" · trend slope {hit['trend_slope_atr']:+.2f} ATR/candle"
        f" · touched 9 EMA at {hit['touch_distance_atr']:.2f} ATR"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


# -----------------------------------------------------------------------------
# Momentum-surge evaluation
#
# evaluate_momentum_surge_candles: has price moved MOMENTUM_PRICE_CHANGE_PCT
# or more over the last MOMENTUM_CANDLE_COUNT candles, with average volume
# over that window running above the average volume of the last
# MOMENTUM_VOLUME_LOOKBACK candles (which includes the signal window itself,
# same as a simple relative-volume reading), while price is trading above
# both the 20 and 50 EMA and the 20 EMA is above the 50 EMA (uptrend filter)?
# -----------------------------------------------------------------------------


def evaluate_momentum_surge_candles(pair, wsname, closed_candles):
    if len(closed_candles) < _momentum_surge_closed_candles_needed():
        return None

    window = closed_candles[-MOMENTUM_CANDLE_COUNT:]
    signal_candle = window[-1]

    if window[0]["open"] <= 0:
        return None

    fast_series = calculate_ema_series(closed_candles, EMA_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, EMA_SLOW_PERIOD)
    if not fast_series or not slow_series:
        return None
    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]

    price_change_pct = (
        (signal_candle["close"] - window[0]["open"])
        / window[0]["open"]
        * 100.0
    )

    baseline_window = closed_candles[-MOMENTUM_VOLUME_LOOKBACK:]
    average_baseline_volume = sum(
        candle["volume"] * candle["vwap"] for candle in baseline_window
    ) / len(baseline_window)
    average_signal_volume = sum(
        candle["volume"] * candle["vwap"] for candle in window
    ) / len(window)

    if average_baseline_volume <= 0:
        return None

    volume_multiple = average_signal_volume / average_baseline_volume

    passes_price = price_change_pct >= MOMENTUM_PRICE_CHANGE_PCT
    passes_volume = average_signal_volume > average_baseline_volume
    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )
    passes_ema_trend = (
        signal_candle["close"] > ema_fast_now
        and signal_candle["close"] > ema_slow_now
        and ema_fast_now > ema_slow_now
    )

    log.debug(
        "%s: momentum_surge price=%+.2f%% signal_volume=%.2f "
        "baseline_volume=%.2f multiple=%.2fx quote_volume_24h=%.2f "
        "filters(price=%s volume=%s liquidity=%s ema_trend=%s volume_24h=%s)",
        wsname or pair,
        price_change_pct,
        average_signal_volume,
        average_baseline_volume,
        volume_multiple,
        quote_volume_24h,
        passes_price,
        passes_volume,
        passes_liquidity,
        passes_ema_trend,
        passes_volume_24h,
    )

    if not (
        passes_price
        and passes_volume
        and passes_liquidity
        and passes_ema_trend
        and passes_volume_24h
    ):
        return None

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": "UP",
        "open": window[0]["open"],
        "high": max(candle["high"] for candle in window),
        "low": min(candle["low"] for candle in window),
        "close": signal_candle["close"],
        "price_change_pct": price_change_pct,
        "average_signal_volume": average_signal_volume,
        "average_baseline_volume": average_baseline_volume,
        "volume_multiple": volume_multiple,
        "quote_volume_24h": quote_volume_24h,
        "signal_time": to_display_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def _momentum_surge_closed_candles_needed():
    return max(MOMENTUM_VOLUME_LOOKBACK, _ema_closed_candles_needed())


def run_momentum_surge_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    if len(closed_candles) < _momentum_surge_closed_candles_needed():
        return None

    hit = evaluate_momentum_surge_candles(pair, wsname, closed_candles)
    if hit is None:
        return None

    hit["alert_epoch"] = hit["signal_epoch"]
    return hit


def log_momentum_surge_hit(hit, label):
    log.info(
        "HIT[momentum_surge]: %s %s move %+.2f%% over %d candles, "
        "volume %.1fx %d-candle average (%s)",
        hit["pair"],
        hit["direction"],
        hit["price_change_pct"],
        MOMENTUM_CANDLE_COUNT,
        hit["volume_multiple"],
        MOMENTUM_VOLUME_LOOKBACK,
        label,
    )


def render_momentum_surge_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = f"{hit['direction']} momentum {hit['price_change_pct']:+.2f}%"
    detail = (
        f"close {format_price(hit['close'])}"
        f" · {MOMENTUM_CANDLE_COUNT}-candle move"
        f" · volume {hit['volume_multiple']:.2f}x {MOMENTUM_VOLUME_LOOKBACK}-candle average"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


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
#
# Two optional hooks are available for analyses whose notion of "duplicate"
# is more than "same pair within COOLDOWN_CANDLES":
#   - "is_duplicate(state, pair, hit) -> bool": checked before the cooldown
#     check; return True to suppress this hit without recording it anywhere.
#   - "on_alert(state, hit)": called once per hit that survives every check
#     and makes it into the sent email, for recording whatever extra state
#     is_duplicate needs to check next time.
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
            "<p>Signals passed range, ATR, candle-quality, robust-volume, "
            f"a &gt;= {BREAKOUT_MIN_PRICE_CHANGE_PCT:.1f}% price move, and "
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
    {
        "key": "ema_trend_pullback",
        "section_title": "20 EMA Pullback Alerts",
        "run": run_ema_trend_pullback_analysis,
        "sort_key": lambda hit: abs(hit["trend_slope_atr"]),
        "log_hit": log_ema_trend_pullback_hit,
        "render_item": render_ema_trend_pullback_item,
        "section_intro": lambda _confirm_label: (
            f"<p>50 EMA trending (slope &gt;= {EMA_MIN_SLOPE_ATR:.2f} ATR/candle, "
            f"separation from 20 EMA &gt;= {EMA_MIN_SEPARATION_ATR:.2f} ATR) with "
            f"price pulling back to the 20 EMA (within "
            f"{EMA_PULLBACK_TOUCH_ATR:.2f} ATR) and closing back in the trend "
            "direction, plus liquidity filters.</p>"
        ),
    },
    {
        "key": "momentum_surge",
        "section_title": "Momentum Surge Alerts",
        "run": run_momentum_surge_analysis,
        "sort_key": lambda hit: hit["price_change_pct"],
        "log_hit": log_momentum_surge_hit,
        "render_item": render_momentum_surge_item,
        "section_intro": lambda _confirm_label: (
            f"<p>Signals passed a {MOMENTUM_CANDLE_COUNT}-candle price move "
            f"&gt;= {MOMENTUM_PRICE_CHANGE_PCT:.1f}% with average volume over "
            f"those candles above the {MOMENTUM_VOLUME_LOOKBACK}-candle "
            "average, plus liquidity filters.</p>"
        ),
    },
    {
        "key": "ema9_pullback",
        "section_title": "9 EMA Pullback Alerts",
        "run": run_ema9_pullback_analysis,
        "sort_key": lambda hit: abs(hit["trend_slope_atr"]),
        "log_hit": log_ema9_pullback_hit,
        "render_item": render_ema9_pullback_item,
        "section_intro": lambda _confirm_label: (
            f"<p>21 EMA trending (slope &gt;= {EMA9_MIN_SLOPE_ATR:.2f} ATR/candle, "
            f"separation from 9 EMA &gt;= {EMA9_MIN_SEPARATION_ATR:.2f} ATR) with "
            f"price pulling back to the 9 EMA (within "
            f"{EMA9_PULLBACK_TOUCH_ATR:.2f} ATR) and closing back in the trend "
            "direction, plus liquidity filters.</p>"
        ),
    },
]


def _max_requested_candle_count(require_next_candle_confirmation):
    closed_needed = max(
        _breakout_closed_candles_needed(require_next_candle_confirmation),
        _volume_surge_closed_candles_needed(),
        _ema_closed_candles_needed(),
        _momentum_surge_closed_candles_needed(),
        _ema9_closed_candles_needed(),
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
        '<html><body style="font-family:-apple-system,Arial,sans-serif;">',
        '<h2 style="margin-bottom:4px;">Kraken Crypto Alerts</h2>',
        f'<p style="color:#555;margin-top:0;"><strong>Interval:</strong> {html.escape(label)}</p>',
    ]

    for spec in ANALYSES:
        hits = hits_by_analysis.get(spec["key"]) or []
        if not hits:
            continue

        subject_parts.append(f"{len(hits)} {spec['section_title'].lower()}")
        lines.append(
            '<h3 style="border-bottom:2px solid #2e7d32;padding-bottom:4px;'
            f'margin-top:28px;">{html.escape(spec["section_title"])} ({len(hits)})</h3>'
        )
        lines.append(
            '<div style="font-size:12.5px;color:#777;margin:2px 0 4px 0;">'
            f'{spec["section_intro"](confirm_label)}</div>'
        )
        lines.extend(spec["render_item"](hit) for hit in hits)

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

    if SKIP_BASE_CURRENCIES:
        before_count = len(pairs)
        pairs = {
            pair_key: wsname
            for pair_key, wsname in pairs.items()
            if wsname.split("/", 1)[0].upper() not in SKIP_BASE_CURRENCIES
        }
        log.info(
            "Skipped %d pair(s) in SKIP_BASE_CURRENCIES.",
            before_count - len(pairs),
        )

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

                    is_duplicate = spec.get("is_duplicate")
                    if is_duplicate is not None and is_duplicate(state, pair, result):
                        log.debug(
                            "%s: %s repeats the last alerted direction, skipping",
                            wsname or pair,
                            spec["key"],
                        )
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
            on_alert = spec.get("on_alert")
            for hit in hits:
                state[spec["key"]][hit["pair_key"]] = hit["alert_epoch"]
                if on_alert is not None:
                    on_alert(state, hit)

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
