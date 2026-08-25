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
    ENABLED_ANALYSES,
    EMA9_FAST_PERIOD,
    EMA9_MIN_SEPARATION_ATR,
    EMA9_MIN_SLOPE_ATR,
    EMA9_PULLBACK_TOUCH_ATR,
    EMA9_SLOW_PERIOD,
    EMA9_TREND_LOOKBACK,
    EMA9_WARMUP_CANDLES,
    EMA50_FAST_PERIOD,
    EMA50_MIN_SEPARATION_ATR,
    EMA50_MIN_SLOPE_ATR,
    EMA50_PULLBACK_TOUCH_ATR,
    EMA50_SLOW_PERIOD,
    EMA50_TREND_LOOKBACK,
    EMA50_WARMUP_CANDLES,
    EMA_FAST_PERIOD,
    EMA_MIN_SEPARATION_ATR,
    EMA_MIN_SLOPE_ATR,
    EMA_PULLBACK_MIN_BODY_ATR,
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
    MIN_SIGNAL_QUOTE_VOLUME,
    MIN_SIGNAL_TRADE_COUNT,
    MIN_VOLUME_MULTIPLE,
    MIN_VOLUME_ROBUST_Z,
    MOMENTUM_CANDLE_COUNT,
    MOMENTUM_MIN_AVG_SIGNAL_VOLUME,
    MOMENTUM_PRICE_CHANGE_PCT,
    MOMENTUM_TREND_FAST_PERIOD,
    MOMENTUM_TREND_PRICE_CHANGE_PCT,
    MOMENTUM_TREND_SLOW_PERIOD,
    MOMENTUM_TREND_WARMUP_CANDLES,
    MOMENTUM_VOLUME_LOOKBACK,
    QUOTE_FILTER,
    REQUEST_DELAY_SEC,
    REQUEST_RETRIES,
    REQUEST_RETRY_DELAY_SEC,
    REQUEST_TIMEOUT_SEC,
    REQUIRE_LIQUIDITY_FILTER,
    SKIP_BASE_CURRENCIES,
    VALID_INTERVALS,
    VOLUME_LOOKBACK,
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

    if not _passes_signal_quote_volume(signal_candle):
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


def _render_alert_card(pair, headline, detail, badge=""):
    # `badge` is pre-rendered HTML from _render_volume_badge, not caller text --
    # every other argument here is escaped by its caller.
    return (
        '<div style="border-left:4px solid #2e7d32;background:#f6fbf6;'
        'padding:10px 14px;margin:10px 0;border-radius:4px;'
        'font-family:-apple-system,Arial,sans-serif;">'
        f'<div style="font-size:15px;font-weight:600;color:#1a1a1a;">'
        f'{pair} <span style="color:#2e7d32;">▲ {headline}</span>{badge}</div>'
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


def _passes_signal_quote_volume(signal_candle):
    """The one volume test every analysis shares.

    Distinct from the liquidity filters below it: those ask whether the *pair*
    is worth trading, over a trailing window, and the momentum analyses are
    exempt from them. This asks whether the candle being alerted on actually
    traded, and nothing is exempt. See MIN_SIGNAL_QUOTE_VOLUME in config.py.
    """
    return (
        signal_candle["volume"] * signal_candle["vwap"] >= MIN_SIGNAL_QUOTE_VOLUME
    )


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

    if not _passes_signal_quote_volume(signal_candle):
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

    # A positive body_atr already implies close > open, so this subsumes the
    # plain "is it green" check it replaces.
    body_atr = (signal_candle["close"] - signal_candle["open"]) / atr
    reclaimed = (
        signal_candle["close"] > ema_fast_now
        and body_atr >= EMA_PULLBACK_MIN_BODY_ATR
    )

    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: ema_trend_pullback trend_up=%s slope_atr=%.2f "
        "separation_atr=%.2f touch_distance_atr=%.2f body_atr=%.2f "
        "quote_volume_24h=%.2f "
        "filters(touched=%s reclaimed=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        trend_up,
        slope_atr,
        separation_atr,
        touch_distance_atr,
        body_atr,
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
        "body_atr": body_atr,
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
        f"{hit['direction']} 21 EMA pullback in uptrend {hit['price_change_pct']:+.2f}%"
    )
    detail = (
        f"close {format_price(hit['close'])}"
        f" · 21 EMA {format_price(hit['ema_fast'])} / 50 EMA {format_price(hit['ema_slow'])}"
        f" · trend slope {hit['trend_slope_atr']:+.2f} ATR/candle"
        f" · touched 21 EMA at {hit['touch_distance_atr']:.2f} ATR"
        f" · reclaim body {hit['body_atr']:.2f} ATR"
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

    if not _passes_signal_quote_volume(signal_candle):
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

    # A positive body_atr already implies close > open, so this subsumes the
    # plain "is it green" check it replaces.
    body_atr = (signal_candle["close"] - signal_candle["open"]) / atr
    reclaimed = (
        signal_candle["close"] > ema_fast_now
        and body_atr >= EMA_PULLBACK_MIN_BODY_ATR
    )

    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: ema9_pullback trend_up=%s slope_atr=%.2f "
        "separation_atr=%.2f touch_distance_atr=%.2f body_atr=%.2f "
        "quote_volume_24h=%.2f "
        "filters(touched=%s reclaimed=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        trend_up,
        slope_atr,
        separation_atr,
        touch_distance_atr,
        body_atr,
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
        "body_atr": body_atr,
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
        f" · reclaim body {hit['body_atr']:.2f} ATR"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


# -----------------------------------------------------------------------------
# 50 EMA pullback evaluation
#
# evaluate_ema50_pullback_candle: the same question as the two pullback
# analyses above, asked of the slowest EMA pair we track (50/200). A trend
# that clears these filters has held for days rather than hours, so this
# fires rarely -- but when it does, the dip is inside structural trend rather
# than a fresh impulse.
# -----------------------------------------------------------------------------


def _ema50_closed_candles_needed():
    return EMA50_SLOW_PERIOD + EMA50_WARMUP_CANDLES


def _ema50_ready(closed_candles):
    if len(closed_candles) < _ema50_closed_candles_needed():
        return None

    fast_series = calculate_ema_series(closed_candles, EMA50_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, EMA50_SLOW_PERIOD)
    if len(fast_series) < 2 or len(slow_series) < 2:
        return None

    atr = calculate_atr(closed_candles[:-1], ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    return fast_series, slow_series, atr


def evaluate_ema50_pullback_candle(pair, wsname, closed_candles):
    ready = _ema50_ready(closed_candles)
    if ready is None:
        return None
    fast_series, slow_series, atr = ready

    if len(slow_series) <= EMA50_TREND_LOOKBACK:
        return None

    signal_candle = closed_candles[-1]
    if signal_candle["open"] <= 0:
        return None

    if not _passes_signal_quote_volume(signal_candle):
        return None

    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]
    ema_slow_then = slow_series[-1 - EMA50_TREND_LOOKBACK]

    slope_atr = (ema_slow_now - ema_slow_then) / EMA50_TREND_LOOKBACK / atr
    separation_atr = abs(ema_fast_now - ema_slow_now) / atr

    trend_up = (
        ema_fast_now > ema_slow_now
        and slope_atr >= EMA50_MIN_SLOPE_ATR
        and separation_atr >= EMA50_MIN_SEPARATION_ATR
    )

    if not trend_up:
        return None

    touch_distance_atr = abs(signal_candle["low"] - ema_fast_now) / atr
    touched_fast_ema = touch_distance_atr <= EMA50_PULLBACK_TOUCH_ATR

    # A positive body_atr already implies close > open, so this subsumes the
    # plain "is it green" check it replaces.
    body_atr = (signal_candle["close"] - signal_candle["open"]) / atr
    reclaimed = (
        signal_candle["close"] > ema_fast_now
        and body_atr >= EMA_PULLBACK_MIN_BODY_ATR
    )

    passes_liquidity, passes_volume_24h, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: ema50_pullback trend_up=%s slope_atr=%.2f "
        "separation_atr=%.2f touch_distance_atr=%.2f body_atr=%.2f "
        "quote_volume_24h=%.2f "
        "filters(touched=%s reclaimed=%s liquidity=%s volume_24h=%s)",
        wsname or pair,
        trend_up,
        slope_atr,
        separation_atr,
        touch_distance_atr,
        body_atr,
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
        "body_atr": body_atr,
        "quote_volume_24h": quote_volume_24h,
        "signal_time": to_display_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def run_ema50_pullback_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    hit = evaluate_ema50_pullback_candle(pair, wsname, closed_candles)
    if hit is None:
        return None

    hit["alert_epoch"] = hit["signal_epoch"]
    return hit


def log_ema50_pullback_hit(hit, label):
    log.info(
        "HIT[ema50_pullback]: %s %s pullback, slope %+.2f ATR/candle, "
        "touch %.2f ATR, price %+.2f%% (%s)",
        hit["pair"],
        hit["direction"],
        hit["trend_slope_atr"],
        hit["touch_distance_atr"],
        hit["price_change_pct"],
        label,
    )


def render_ema50_pullback_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = (
        f"{hit['direction']} 50 EMA pullback in uptrend {hit['price_change_pct']:+.2f}%"
    )
    detail = (
        f"close {format_price(hit['close'])}"
        f" · 50 EMA {format_price(hit['ema_fast'])} / 200 EMA {format_price(hit['ema_slow'])}"
        f" · trend slope {hit['trend_slope_atr']:+.2f} ATR/candle"
        f" · touched 50 EMA at {hit['touch_distance_atr']:.2f} ATR"
        f" · reclaim body {hit['body_atr']:.2f} ATR"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(pair, headline, detail)


# -----------------------------------------------------------------------------
# Momentum-surge evaluation
#
# evaluate_momentum_surge_candles: over the last MOMENTUM_CANDLE_COUNT
# candles, has price moved MOMENTUM_PRICE_CHANGE_PCT or more, on average quote
# volume of at least MOMENTUM_MIN_AVG_SIGNAL_VOLUME, with the signal candle
# closing above both the 21 and 50 EMA and the 21 EMA above the 50 EMA? Those
# three tests are the whole analysis.
#
# The first two look only at the signal window; the third is the uptrend
# filter, measured on the signal candle. There is still no candle-quality or
# ATR condition, and no trailing-window volume test can veto a hit:
#
#   - Direction is verified again. The EMA filter was removed in 5d7ae06 and
#     restored here, so a dead-cat bounce inside a downtrend no longer fires:
#     "direction": "UP" once more implies the pair is in a 21/50 uptrend, not
#     merely that these three candles rose.
#   - The per-candle liquidity filter and MIN_24H_QUOTE_VOLUME are computed
#     but only badge the alert LIQUID/THIN. The other analyses still gate on
#     them; this asymmetry is specific to the two momentum analyses.
#
# The volume floor and the badge answer different questions, and disagree on
# purpose. The floor asks "did real money move in *this* window" and can block
# an alert; the badge asks "is this pair liquid *in general*" and cannot. A
# dormant pair whose 5% move traded $8k fires badged THIN; a normally busy
# pair whose 5% move traded $500 is filtered out despite being liquid daily.
# -----------------------------------------------------------------------------


def _momentum_window_stats(closed_candles):
    """Price and volume measurements for the shared momentum signal window.

    Both momentum analyses measure the same window and share the volume floor,
    so that arithmetic lives here rather than being duplicated and left to
    drift. The *price* threshold is deliberately not applied here: the two
    analyses use different ones (MOMENTUM_PRICE_CHANGE_PCT vs
    MOMENTUM_TREND_PRICE_CHANGE_PCT), so each caller compares
    `price_change_pct` against its own. Returns None when the window opens at a
    non-positive price (bad data), the one case neither caller can express as a
    percentage.
    """
    window = closed_candles[-MOMENTUM_CANDLE_COUNT:]
    signal_candle = window[-1]

    if window[0]["open"] <= 0:
        return None

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

    # Unreachable while MOMENTUM_MIN_AVG_SIGNAL_VOLUME is positive: the signal
    # candles are a subset of the baseline window, so a window clearing that
    # floor guarantees a positive baseline. Kept for the case where the floor
    # is set to 0, which is the supported way to turn this filter off.
    volume_multiple = (
        average_signal_volume / average_baseline_volume
        if average_baseline_volume > 0
        else None
    )

    return {
        "window": window,
        "signal_candle": signal_candle,
        "price_change_pct": price_change_pct,
        "average_signal_volume": average_signal_volume,
        "average_baseline_volume": average_baseline_volume,
        "volume_multiple": volume_multiple,
        "passes_signal_volume": (
            average_signal_volume >= MOMENTUM_MIN_AVG_SIGNAL_VOLUME
        ),
    }


def _momentum_hit(pair, wsname, stats, quote_volume_24h, volume_ok):
    """The hit dict fields both momentum analyses report identically."""
    window = stats["window"]
    signal_candle = stats["signal_candle"]

    return {
        "pair": wsname or pair,
        "pair_key": pair,
        "direction": "UP",
        "open": window[0]["open"],
        "high": max(candle["high"] for candle in window),
        "low": min(candle["low"] for candle in window),
        "close": signal_candle["close"],
        "price_change_pct": stats["price_change_pct"],
        "average_signal_volume": stats["average_signal_volume"],
        "average_baseline_volume": stats["average_baseline_volume"],
        "volume_multiple": stats["volume_multiple"],
        "quote_volume_24h": quote_volume_24h,
        "volume_ok": volume_ok,
        "signal_time": to_display_datetime(signal_candle["time"]),
        "signal_epoch": signal_candle["time"],
    }


def evaluate_momentum_surge_candles(pair, wsname, closed_candles):
    if len(closed_candles) < _momentum_surge_closed_candles_needed():
        return None

    stats = _momentum_window_stats(closed_candles)
    if stats is None:
        return None

    signal_candle = stats["signal_candle"]
    if not _passes_signal_quote_volume(signal_candle):
        return None

    passes_price = stats["price_change_pct"] >= MOMENTUM_PRICE_CHANGE_PCT

    fast_series = calculate_ema_series(closed_candles, EMA_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, EMA_SLOW_PERIOD)
    if not fast_series or not slow_series:
        return None
    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]

    # The uptrend filter, on the signal candle only -- unlike
    # trend_momentum_surge, which checks its (faster) EMA pair on every candle
    # of the window. Here the question is just "is the pair in a 21/50 uptrend
    # at the moment this move finished", which is what keeps a dead-cat bounce
    # inside a downtrend out of the section.
    passes_ema_trend = (
        signal_candle["close"] > ema_fast_now
        and signal_candle["close"] > ema_slow_now
        and ema_fast_now > ema_slow_now
    )

    # Computed for the badge only -- neither of these can suppress a hit. Note
    # `volume_ok` (trailing 24h) is independent of `passes_signal_volume` (this
    # move): a quiet pair that wakes up fires while badged THIN.
    _, volume_ok, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: momentum_surge price=%+.2f%% signal_volume=%.2f "
        "baseline_volume=%.2f multiple=%s ema_fast=%.6f ema_slow=%.6f "
        "quote_volume_24h=%.2f "
        "filters(price=%s signal_volume=%s ema_trend=%s) badge(volume_ok=%s)",
        wsname or pair,
        stats["price_change_pct"],
        stats["average_signal_volume"],
        stats["average_baseline_volume"],
        f"{stats['volume_multiple']:.2f}x"
        if stats["volume_multiple"] is not None
        else "n/a",
        ema_fast_now,
        ema_slow_now,
        quote_volume_24h,
        passes_price,
        stats["passes_signal_volume"],
        passes_ema_trend,
        volume_ok,
    )

    if not (passes_price and stats["passes_signal_volume"] and passes_ema_trend):
        return None

    hit = _momentum_hit(pair, wsname, stats, quote_volume_24h, volume_ok)
    hit["ema_fast"] = ema_fast_now
    hit["ema_slow"] = ema_slow_now
    return hit


def _momentum_surge_closed_candles_needed():
    # Two independent constraints, both load-bearing:
    #
    #   - _ema_closed_candles_needed() (200) warms the 50 EMA behind the
    #     uptrend filter. An EMA seeded from a plain SMA is inaccurate for its
    #     first few periods, and here that error would show up as false
    #     21-over-50 orderings on pairs that just came into range.
    #   - VOLUME_LOOKBACK + 1 (97) is what the LIQUID/THIN badge needs:
    #     _liquidity_stats slices VOLUME_LOOKBACK candles off
    #     closed_candles[:-1], so short it and "24h volume" silently becomes
    #     "however many hours we happened to fetch", badging liquid pairs THIN.
    #
    # The EMA warmup is the larger of the two today, exactly as it was before
    # the filter was removed in 5d7ae06 -- so restoring the filter takes the
    # per-pair fetch back from 98 to 201 candles. MOMENTUM_VOLUME_LOOKBACK (20)
    # is well under both but is kept in the max so raising it can't outgrow the
    # fetch.
    return max(
        MOMENTUM_VOLUME_LOOKBACK,
        VOLUME_LOOKBACK + 1,
        _ema_closed_candles_needed(),
    )


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
        "HIT[momentum_surge]: %s %s move %+.2f%% over %d candles in a %d/%d "
        "EMA uptrend, volume %s %d-candle average, 24h volume %.0f [%s] (%s)",
        hit["pair"],
        hit["direction"],
        hit["price_change_pct"],
        MOMENTUM_CANDLE_COUNT,
        EMA_FAST_PERIOD,
        EMA_SLOW_PERIOD,
        f"{hit['volume_multiple']:.1f}x"
        if hit["volume_multiple"] is not None
        else "n/a",
        MOMENTUM_VOLUME_LOOKBACK,
        hit["quote_volume_24h"],
        "LIQUID" if hit["volume_ok"] else "THIN",
        label,
    )


def _render_volume_badge(volume_ok):
    """Green LIQUID / red THIN badge for momentum_surge alerts.

    Inline styles with explicit background *and* border: Gmail strips
    <style> blocks, and a colour-blind-unfriendly red/green pair is why the
    badge carries a word rather than relying on colour alone.
    """
    if volume_ok:
        text, color, background, border = ("LIQUID", "#1b5e20", "#e8f5e9", "#2e7d32")
    else:
        text, color, background, border = ("THIN", "#b71c1c", "#fdecea", "#c62828")

    return (
        f'<span style="display:inline-block;background:{background};'
        f"color:{color};border:1px solid {border};border-radius:3px;"
        f'padding:1px 6px;font-size:11px;font-weight:700;'
        f'letter-spacing:0.03em;margin-left:6px;">{text}</span>'
    )


def render_momentum_surge_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    volume_multiple_text = (
        f"{hit['volume_multiple']:.2f}x {MOMENTUM_VOLUME_LOOKBACK}-candle average"
        if hit["volume_multiple"] is not None
        else f"n/a (no volume in the last {MOMENTUM_VOLUME_LOOKBACK} candles)"
    )
    threshold_text = format_compact_volume(MIN_24H_QUOTE_VOLUME)
    comparison = "≥" if hit["volume_ok"] else "<"

    headline = f"{hit['direction']} momentum {hit['price_change_pct']:+.2f}%"
    detail = (
        f"close {format_price(hit['close'])}"
        f" · {MOMENTUM_CANDLE_COUNT}-candle move"
        f" · {EMA_FAST_PERIOD} EMA {format_price(hit['ema_fast'])}"
        f" / {EMA_SLOW_PERIOD} EMA {format_price(hit['ema_slow'])}"
        f" · move volume {format_compact_volume(hit['average_signal_volume'])}/candle"
        f" · volume {volume_multiple_text}"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" ({comparison} {threshold_text} threshold)"
        f" · signal {signal_time}"
    )
    return _render_alert_card(
        pair, headline, detail, badge=_render_volume_badge(hit["volume_ok"])
    )


# -----------------------------------------------------------------------------
# Trend momentum-surge evaluation
#
# evaluate_trend_momentum_surge_candles: the shape of the move rather than its
# size. Over the same MOMENTUM_CANDLE_COUNT window momentum_surge measures,
# *every* candle must have closed above its open AND above the previous
# candle's close -- the "above the previous close" test reaches one bar further
# back than the window, so it covers the window's first candle too. Then, on
# the signal candle alone, the 9 EMA must be above the 21 EMA with the close
# above the 21 EMA.
#
# The per-candle tests describe the run; the EMA test places it in a trend.
# Both EMA comparisons used to run across the whole window, which also required
# the 9/21 cross to predate the run -- that excluded the freshest crosses, and
# was relaxed to match momentum_surge's signal-candle-only reading.
#
# It shares momentum_surge's volume floor but applies its own, lower price
# threshold: MOMENTUM_TREND_PRICE_CHANGE_PCT (3%) against
# MOMENTUM_PRICE_CHANGE_PCT (5%). So this is NOT a subset of momentum_surge --
# a clean 3-5% staircase fires only here, and plenty of 5%+ moves fire only
# there because their candles are not a staircase.
#

# The LIQUID/THIN badge behaves exactly as it does for momentum_surge: it is
# computed from trailing 24h volume and can never suppress a hit.
# -----------------------------------------------------------------------------


def _trend_momentum_surge_closed_candles_needed():
    # Spelled out rather than deferring to _momentum_surge_closed_candles_needed
    # so this analysis stays cheap on its own terms: momentum_surge's 21/50 EMA
    # warmup needs 200 candles, but nothing here does, and borrowing its number
    # would make disabling momentum_surge fail to shrink the fetch.
    #
    # VOLUME_LOOKBACK + 1 (97) is what the LIQUID/THIN badge needs -- see
    # _momentum_surge_closed_candles_needed for why one short of that quietly
    # mislabels liquid pairs THIN -- and it beats the 21 EMA plus warmup (84).
    return max(
        MOMENTUM_VOLUME_LOOKBACK,
        VOLUME_LOOKBACK + 1,
        MOMENTUM_TREND_SLOW_PERIOD + MOMENTUM_TREND_WARMUP_CANDLES,
    )


def evaluate_trend_momentum_surge_candles(pair, wsname, closed_candles):
    if len(closed_candles) < _trend_momentum_surge_closed_candles_needed():
        return None

    stats = _momentum_window_stats(closed_candles)
    if stats is None:
        return None

    fast_series = calculate_ema_series(closed_candles, MOMENTUM_TREND_FAST_PERIOD)
    slow_series = calculate_ema_series(closed_candles, MOMENTUM_TREND_SLOW_PERIOD)
    if not fast_series or not slow_series:
        return None

    window = stats["window"]
    signal_candle = stats["signal_candle"]

    if not _passes_signal_quote_volume(signal_candle):
        return None

    all_candles_up = all(
        candle["close"] > candle["open"] for candle in window
    )

    # "Every candle closed above the previous one" includes the first candle of
    # the window, so this reaches one bar further back for its comparison.
    # Distinct from all_candles_up: a candle that gaps up and then fades closes
    # above the previous close while still printing red, and one that opens
    # below the previous close and recovers only part of the way is green while
    # the sequence of closes steps down.
    closes = [closed_candles[-MOMENTUM_CANDLE_COUNT - 1]["close"]] + [
        candle["close"] for candle in window
    ]
    closes_rising = all(
        later > earlier for earlier, later in zip(closes, closes[1:])
    )

    ema_fast_now = fast_series[-1]
    ema_slow_now = slow_series[-1]

    # Signal candle only, matching momentum_surge -- the EMA pair answers "is
    # the pair in a 9/21 uptrend as this run finishes", and the staircase
    # conditions above are what describe the run itself. This used to be
    # checked on every candle of the window, which additionally required the
    # 9/21 cross to predate the run; relaxing it lets the freshest crosses
    # through, which are often the better entries.
    ema_stacked = ema_fast_now > ema_slow_now
    above_slow_ema = signal_candle["close"] > ema_slow_now

    ema_gap_pct = (
        (ema_fast_now - ema_slow_now) / ema_slow_now * 100.0
        if ema_slow_now > 0
        else 0.0
    )

    # Its own threshold, not momentum_surge's -- lower, because the candle and
    # EMA conditions above carry more of the evidence here. See the config
    # block for why it is 3% rather than 0 or 5%.
    passes_price = stats["price_change_pct"] >= MOMENTUM_TREND_PRICE_CHANGE_PCT

    # Badge only, as in momentum_surge -- it cannot suppress a hit.
    _, volume_ok, quote_volume_24h = _liquidity_stats(
        closed_candles[:-1], signal_candle
    )

    log.debug(
        "%s: trend_momentum_surge price=%+.2f%% signal_volume=%.2f "
        "ema_fast=%.6f ema_slow=%.6f gap=%+.2f%% quote_volume_24h=%.2f "
        "filters(price=%s signal_volume=%s candles_up=%s closes_rising=%s "
        "ema_stacked=%s above_slow_ema=%s) badge(volume_ok=%s)",
        wsname or pair,
        stats["price_change_pct"],
        stats["average_signal_volume"],
        ema_fast_now,
        ema_slow_now,
        ema_gap_pct,
        quote_volume_24h,
        passes_price,
        stats["passes_signal_volume"],
        all_candles_up,
        closes_rising,
        ema_stacked,
        above_slow_ema,
        volume_ok,
    )

    if not (
        passes_price
        and stats["passes_signal_volume"]
        and all_candles_up
        and closes_rising
        and ema_stacked
        and above_slow_ema
    ):
        return None

    hit = _momentum_hit(pair, wsname, stats, quote_volume_24h, volume_ok)
    hit["ema_fast"] = ema_fast_now
    hit["ema_slow"] = ema_slow_now
    hit["ema_gap_pct"] = ema_gap_pct
    return hit


def run_trend_momentum_surge_analysis(
    pair,
    wsname,
    closed_candles,
    interval_minutes,
    require_next_candle_confirmation=False,
):
    hit = evaluate_trend_momentum_surge_candles(pair, wsname, closed_candles)
    if hit is None:
        return None

    hit["alert_epoch"] = hit["signal_epoch"]
    return hit


def log_trend_momentum_surge_hit(hit, label):
    log.info(
        "HIT[trend_momentum_surge]: %s %s move %+.2f%% over %d up candles, "
        "%d EMA %+.2f%% above the %d EMA, 24h volume %.0f [%s] (%s)",
        hit["pair"],
        hit["direction"],
        hit["price_change_pct"],
        MOMENTUM_CANDLE_COUNT,
        MOMENTUM_TREND_FAST_PERIOD,
        hit["ema_gap_pct"],
        MOMENTUM_TREND_SLOW_PERIOD,
        hit["quote_volume_24h"],
        "LIQUID" if hit["volume_ok"] else "THIN",
        label,
    )


def render_trend_momentum_surge_item(hit):
    pair = html.escape(str(hit["pair"]))
    signal_time = hit["signal_time"].strftime("%Y-%m-%d %H:%M %Z")

    headline = f"{hit['direction']} trend momentum {hit['price_change_pct']:+.2f}%"
    detail = (
        f"close {format_price(hit['close'])}"
        f" · {MOMENTUM_CANDLE_COUNT} consecutive up candles, each closing higher"
        f" · {MOMENTUM_TREND_FAST_PERIOD} EMA {format_price(hit['ema_fast'])}"
        f" / {MOMENTUM_TREND_SLOW_PERIOD} EMA {format_price(hit['ema_slow'])}"
        f" ({hit['ema_gap_pct']:+.2f}%)"
        f" · move volume {format_compact_volume(hit['average_signal_volume'])}/candle"
        f" · 24h volume {format_compact_volume(hit['quote_volume_24h'])}"
        f" · signal {signal_time}"
    )
    return _render_alert_card(
        pair, headline, detail, badge=_render_volume_badge(hit["volume_ok"])
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
#   4. Append a new entry below, and add its key to ENABLED_ANALYSES in
#      config.py. No other code needs to change: the scanner loop, cooldown
#      state, and combined email all iterate this list.
#
# Two optional hooks are available for analyses whose notion of "duplicate"
# is more than "same pair within COOLDOWN_CANDLES":
#   - "is_duplicate(state, pair, hit) -> bool": checked before the cooldown
#     check; return True to suppress this hit without recording it anywhere.
#   - "on_alert(state, hit)": called once per hit that survives every check
#     and makes it into the sent email, for recording whatever extra state
#     is_duplicate needs to check next time.
#
# "closed_candles_needed(require_next_candle_confirmation)" reports how much
# history the analysis needs, so the shared per-pair fetch is sized to the
# *enabled* analyses only.
#
# ALL_ANALYSES is the full registry; ANALYSES below is the enabled subset that
# every other part of the scanner iterates.
# -----------------------------------------------------------------------------

ALL_ANALYSES = [
    {
        "key": "breakout",
        "section_title": "Confirmed Breakouts",
        "run": run_breakout_analysis,
        "closed_candles_needed": _breakout_closed_candles_needed,
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
        "key": "ema_trend_pullback",
        "section_title": "21 EMA Pullback Alerts",
        "run": run_ema_trend_pullback_analysis,
        "closed_candles_needed": lambda _confirm: _ema_closed_candles_needed(),
        "sort_key": lambda hit: abs(hit["trend_slope_atr"]),
        "log_hit": log_ema_trend_pullback_hit,
        "render_item": render_ema_trend_pullback_item,
        "section_intro": lambda _confirm_label: (
            f"<p>50 EMA trending (slope &gt;= {EMA_MIN_SLOPE_ATR:.2f} ATR/candle, "
            f"separation from 21 EMA &gt;= {EMA_MIN_SEPARATION_ATR:.2f} ATR) with "
            f"price pulling back to the 21 EMA (within "
            f"{EMA_PULLBACK_TOUCH_ATR:.2f} ATR) and closing back in the trend "
            f"direction on a body &gt;= {EMA_PULLBACK_MIN_BODY_ATR:.2f} ATR, "
            "plus liquidity filters.</p>"
        ),
    },
    {
        "key": "momentum_surge",
        "section_title": "Momentum Surge Alerts",
        "run": run_momentum_surge_analysis,
        "closed_candles_needed": lambda _confirm: _momentum_surge_closed_candles_needed(),
        "sort_key": lambda hit: hit["price_change_pct"],
        "log_hit": log_momentum_surge_hit,
        "render_item": render_momentum_surge_item,
        "section_intro": lambda _confirm_label: (
            f"<p>Signals passed a {MOMENTUM_CANDLE_COUNT}-candle price move "
            f"&gt;= {MOMENTUM_PRICE_CHANGE_PCT:.1f}% on average volume of "
            f"&ge; {format_compact_volume(MOMENTUM_MIN_AVG_SIGNAL_VOLUME)} "
            "per candle across the move, <strong>with the signal candle "
            f"closing above both the {EMA_FAST_PERIOD} and "
            f"{EMA_SLOW_PERIOD} EMA and the {EMA_FAST_PERIOD} EMA above the "
            f"{EMA_SLOW_PERIOD} EMA</strong> &mdash; so these are moves "
            "inside an uptrend, not bounces inside a downtrend. "
            "The badge is a separate, trailing measure that never blocks an "
            "alert: "
            f"<span style=\"color:#1b5e20;font-weight:700;\">LIQUID</span> = "
            f"24h volume &ge; {format_compact_volume(MIN_24H_QUOTE_VOLUME)}, "
            f"<span style=\"color:#b71c1c;font-weight:700;\">THIN</span> = "
            "below it &mdash; a quiet pair that just woke up can clear the "
            "move-volume floor while still being badged THIN.</p>"
        ),
    },
    {
        "key": "trend_momentum_surge",
        "section_title": "Trend Momentum Surge Alerts",
        "run": run_trend_momentum_surge_analysis,
        "closed_candles_needed": lambda _confirm: _trend_momentum_surge_closed_candles_needed(),
        "sort_key": lambda hit: hit["price_change_pct"],
        "log_hit": log_trend_momentum_surge_hit,
        "render_item": render_trend_momentum_surge_item,
        "section_intro": lambda _confirm_label: (
            f"<p>All {MOMENTUM_CANDLE_COUNT} candles closed above their open "
            "<em>and</em> above the previous candle's close, with the signal "
            f"candle closing above the {MOMENTUM_TREND_SLOW_PERIOD} EMA and "
            f"the {MOMENTUM_TREND_FAST_PERIOD} EMA above the "
            f"{MOMENTUM_TREND_SLOW_PERIOD} EMA, on "
            f"average volume of &ge; "
            f"{format_compact_volume(MOMENTUM_MIN_AVG_SIGNAL_VOLUME)} per "
            f"candle, over a move of &gt;= "
            f"{MOMENTUM_TREND_PRICE_CHANGE_PCT:.1f}%. That size bar is "
            f"<strong>lower than momentum surge's "
            f"{MOMENTUM_PRICE_CHANGE_PCT:.1f}%</strong> because the structure "
            "above is doing more of the work here. The badge is "
            "the same trailing measure and never blocks an alert: "
            f"<span style=\"color:#1b5e20;font-weight:700;\">LIQUID</span> = "
            f"24h volume &ge; {format_compact_volume(MIN_24H_QUOTE_VOLUME)}, "
            f"<span style=\"color:#b71c1c;font-weight:700;\">THIN</span> = "
            "below it.</p>"
        ),
    },
    {
        "key": "ema9_pullback",
        "section_title": "9 EMA Pullback Alerts",
        "run": run_ema9_pullback_analysis,
        "closed_candles_needed": lambda _confirm: _ema9_closed_candles_needed(),
        "sort_key": lambda hit: abs(hit["trend_slope_atr"]),
        "log_hit": log_ema9_pullback_hit,
        "render_item": render_ema9_pullback_item,
        "section_intro": lambda _confirm_label: (
            f"<p>21 EMA trending (slope &gt;= {EMA9_MIN_SLOPE_ATR:.2f} ATR/candle, "
            f"separation from 9 EMA &gt;= {EMA9_MIN_SEPARATION_ATR:.2f} ATR) with "
            f"price pulling back to the 9 EMA (within "
            f"{EMA9_PULLBACK_TOUCH_ATR:.2f} ATR) and closing back in the trend "
            f"direction on a body &gt;= {EMA_PULLBACK_MIN_BODY_ATR:.2f} ATR, "
            "plus liquidity filters.</p>"
        ),
    },
    {
        "key": "ema50_pullback",
        "section_title": "50 EMA Pullback Alerts",
        "run": run_ema50_pullback_analysis,
        "closed_candles_needed": lambda _confirm: _ema50_closed_candles_needed(),
        "sort_key": lambda hit: abs(hit["trend_slope_atr"]),
        "log_hit": log_ema50_pullback_hit,
        "render_item": render_ema50_pullback_item,
        "section_intro": lambda _confirm_label: (
            f"<p>200 EMA trending (slope &gt;= {EMA50_MIN_SLOPE_ATR:.2f} ATR/candle, "
            f"separation from 50 EMA &gt;= {EMA50_MIN_SEPARATION_ATR:.2f} ATR) with "
            f"price pulling back to the 50 EMA (within "
            f"{EMA50_PULLBACK_TOUCH_ATR:.2f} ATR) and closing back in the trend "
            f"direction on a body &gt;= {EMA_PULLBACK_MIN_BODY_ATR:.2f} ATR, "
            "plus liquidity filters.</p>"
        ),
    },
]


def _enabled_analyses():
    """Filter ALL_ANALYSES down to the analyses enabled in config.

    Unknown keys in ENABLED_ANALYSES are an error rather than a no-op: a typo
    there would otherwise silently leave a strategy disabled, which looks
    exactly like a quiet market.
    """
    known_keys = {spec["key"] for spec in ALL_ANALYSES}
    unknown_keys = sorted(set(ENABLED_ANALYSES) - known_keys)
    if unknown_keys:
        raise ValueError(
            f"ENABLED_ANALYSES contains unknown analysis key(s): "
            f"{', '.join(unknown_keys)}. Known keys: {', '.join(sorted(known_keys))}."
        )

    return [spec for spec in ALL_ANALYSES if ENABLED_ANALYSES.get(spec["key"], False)]


ANALYSES = _enabled_analyses()


def _max_requested_candle_count(require_next_candle_confirmation):
    # Sized to the enabled analyses only -- disabling the slow ones shrinks
    # every per-pair fetch. Zero enabled analyses means nothing to scan; the
    # fallback just keeps the fetch valid rather than raising on max([]).
    closed_needed = max(
        (
            spec["closed_candles_needed"](require_next_candle_confirmation)
            for spec in ANALYSES
        ),
        default=1,
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

    # ALL_ANALYSES, not ANALYSES: rendering is a pure function of the hits it
    # is handed. A scan only ever produces hits for enabled analyses, so
    # filtering here would change nothing except to silently drop a section if
    # a caller passes hits for a disabled one.
    for spec in ALL_ANALYSES:
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

    if not ANALYSES:
        log.warning(
            "Every analysis is disabled in ENABLED_ANALYSES -- nothing to scan."
        )
        return {}

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
