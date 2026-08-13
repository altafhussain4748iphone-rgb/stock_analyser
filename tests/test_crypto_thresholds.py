from datetime import datetime, timezone

from technical_analysis.crypto import alerts as ca
from technical_analysis.crypto.alerts import build_combined_email


def test_momentum_surge_alert_is_formatted():
    hits_by_analysis = {
        "breakout": [],
        "ema_trend_pullback": [],
        "momentum_surge": [
            {
                "pair": "ADA/USD",
                "pair_key": "ADAUSD",
                "direction": "UP",
                "price_change_pct": 5.5,
                "close": 0.4430,
                "average_signal_volume": 120_000.0,
                "average_baseline_volume": 90_000.0,
                "quote_volume_24h": 2_100_000.0,
                "volume_multiple": 1.33,
                "volume_ok": True,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "1 momentum surge alerts" in subject
    assert "ADA/USD" in body
    assert "1.33x" in body
    assert "+5.50%" in body
    # Both words appear in the section legend, so assert on the badge's own
    # background colour rather than the label text.
    assert "LIQUID" in body
    assert "#e8f5e9" in body  # green badge rendered
    assert "#fdecea" not in body  # red badge not rendered


def test_momentum_surge_thin_pair_is_badged_not_dropped():
    """A pair under MIN_24H_QUOTE_VOLUME still alerts, badged THIN."""
    hits_by_analysis = {
        "momentum_surge": [
            {
                "pair": "BOBA/USD",
                "pair_key": "BOBAUSD",
                "direction": "UP",
                "price_change_pct": 5.63,
                "close": 0.19842,
                "average_signal_volume": 900.0,
                "average_baseline_volume": 880.0,
                "quote_volume_24h": 18_900.0,
                "volume_multiple": 1.02,
                "volume_ok": False,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    _subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "BOBA/USD" in body
    assert "THIN" in body
    assert "#b71c1c" in body


def test_momentum_surge_renders_undefined_volume_multiple():
    """A zero-baseline pair used to be rejected; it now renders as n/a."""
    hits_by_analysis = {
        "momentum_surge": [
            {
                "pair": "KEEP/USD",
                "pair_key": "KEEPUSD",
                "direction": "UP",
                "price_change_pct": 5.11,
                "close": 0.08733,
                "average_signal_volume": 0.0,
                "average_baseline_volume": 0.0,
                "quote_volume_24h": 0.0,
                "volume_multiple": None,
                "volume_ok": False,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    _subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "n/a" in body
    assert "THIN" in body


def _momentum_series(signal_volume=50_000.0, baseline_volume=50_000.0,
                     trade_count=50, drift=1.002, move=1.02):
    """Synthetic candles: `drift` per candle, then 3 candles of `move`.

    Volumes are quote volume per candle (vwap == price, so volume * vwap is
    exactly the figure passed in). Defaults clear
    MOMENTUM_MIN_AVG_SIGNAL_VOLUME so tests of other conditions aren't
    silently blocked by the volume floor.
    """
    needed = ca._momentum_surge_closed_candles_needed()
    out = []
    price = 100.0
    for i in range(needed - 3):
        price *= drift
        out.append({
            "time": i * 900, "open": price, "high": price * 1.001,
            "low": price * 0.999, "close": price, "vwap": price,
            "volume": baseline_volume / price, "count": 50,
        })
    for i in range(3):
        price *= move
        out.append({
            "time": (needed - 3 + i) * 900, "open": price / move,
            "high": price, "low": price / move, "close": price,
            "vwap": price, "volume": signal_volume / price,
            "count": trade_count,
        })
    return out


def test_momentum_surge_ignores_relative_volume():
    """Fading volume no longer blocks a hit -- only the absolute floor does."""
    faded = ca.evaluate_momentum_surge_candles(
        "X", "X/USD",
        _momentum_series(signal_volume=10_000.0, baseline_volume=200_000.0),
    )
    assert faded is not None
    # Well under the trailing baseline, but still real money in the move.
    assert faded["volume_multiple"] < 1.0
    assert faded["average_signal_volume"] >= ca.MOMENTUM_MIN_AVG_SIGNAL_VOLUME


def test_momentum_surge_blocks_a_move_on_too_little_volume():
    """The signal-window floor is the one volume check that can veto."""
    under = ca.evaluate_momentum_surge_candles(
        "X", "X/USD",
        _momentum_series(signal_volume=ca.MOMENTUM_MIN_AVG_SIGNAL_VOLUME - 1),
    )
    assert under is None

    at_floor = ca.evaluate_momentum_surge_candles(
        "X", "X/USD",
        _momentum_series(signal_volume=ca.MOMENTUM_MIN_AVG_SIGNAL_VOLUME),
    )
    assert at_floor is not None  # compared with >=

    dead = ca.evaluate_momentum_surge_candles(
        "X", "X/USD",
        _momentum_series(signal_volume=0.0, baseline_volume=0.0, trade_count=0),
    )
    assert dead is None


def test_momentum_surge_floor_is_independent_of_the_badge():
    """A dormant pair waking up fires while still badged THIN.

    The floor measures the 3 signal candles; the badge measures trailing 24h
    volume. A pair with a near-dead day and one lively move clears the first
    and fails the second.
    """
    candles = _momentum_series(signal_volume=8_000.0, baseline_volume=1.0)
    hit = ca.evaluate_momentum_surge_candles("X", "X/USD", candles)

    assert hit is not None
    assert hit["average_signal_volume"] >= ca.MOMENTUM_MIN_AVG_SIGNAL_VOLUME
    assert hit["quote_volume_24h"] < ca.MIN_24H_QUOTE_VOLUME
    assert hit["volume_ok"] is False


def test_momentum_surge_fires_on_a_bounce_inside_a_downtrend():
    """The 21/50 EMA filter is gone: trend direction no longer gates a hit."""
    downtrend = _momentum_series(drift=0.997)

    fast = ca.calculate_ema_series(downtrend, ca.EMA_FAST_PERIOD)
    slow = ca.calculate_ema_series(downtrend, ca.EMA_SLOW_PERIOD)
    # Confirm the fixture really is a downtrend by the old filter's own test,
    # so this asserts the filter's removal rather than a weak fixture.
    assert fast[-1] < slow[-1]

    hit = ca.evaluate_momentum_surge_candles("X", "X/USD", downtrend)
    assert hit is not None
    assert hit["direction"] == "UP"
    assert hit["price_change_pct"] >= ca.MOMENTUM_PRICE_CHANGE_PCT


def test_momentum_surge_still_requires_the_price_move():
    """The one remaining gate."""
    assert ca.evaluate_momentum_surge_candles(
        "X", "X/USD", _momentum_series(move=1.001)
    ) is None


def test_momentum_surge_fetches_a_full_24h_volume_window():
    """The badge is only honest if the whole VOLUME_LOOKBACK window is fetched.

    Without the EMA warmup, nothing else forces the fetch to be this large --
    a short window would silently understate 24h volume and badge liquid
    pairs THIN.
    """
    assert ca._momentum_surge_closed_candles_needed() >= ca.VOLUME_LOOKBACK + 1

    candles = _momentum_series()
    hit = ca.evaluate_momentum_surge_candles("X", "X/USD", candles)
    expected = sum(
        c["volume"] * c["vwap"] for c in candles[-(ca.VOLUME_LOOKBACK + 1):]
    )
    assert hit["quote_volume_24h"] == expected


def test_ema50_pullback_alert_is_formatted():
    hits_by_analysis = {
        "ema50_pullback": [
            {
                "pair": "ADA/USD",
                "pair_key": "ADAUSD",
                "direction": "UP",
                "price_change_pct": 1.2,
                "close": 0.4430,
                "ema_fast": 0.4400,
                "ema_slow": 0.4100,
                "ema_separation_atr": 2.4,
                "trend_slope_atr": 0.09,
                "touch_distance_atr": 0.18,
                "body_atr": 0.42,
                "quote_volume_24h": 2_100_000.0,
                "signal_time": datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
                "signal_epoch": 1,
                "alert_epoch": 1,
            }
        ],
    }

    subject, body = build_combined_email(hits_by_analysis, 15, False)

    assert "1 50 ema pullback alerts" in subject
    assert "ADA/USD" in body
    assert "50 EMA pullback in uptrend" in body
    assert "+1.20%" in body


# -----------------------------------------------------------------------------
# ema50_pullback evaluator
#
# Synthetic candles, since the 50/200 pair needs 600 bars of warmed-up history
# before it will evaluate anything at all -- far too much to spell out inline.
# -----------------------------------------------------------------------------

_CANDLE_RANGE = 0.006  # high/low span as a fraction of price -- this sets ATR


def _candle(index, open_price, close, low=None):
    return {
        "time": 1_700_000_000 + index * 900,
        "open": open_price,
        "high": max(open_price, close) * (1 + _CANDLE_RANGE / 2),
        "low": low if low is not None else min(open_price, close) * (1 - _CANDLE_RANGE / 2),
        "close": close,
        "vwap": (open_price + close) / 2,
        "volume": 2_000.0,
        "count": 50,
    }


def _series(count=610, drift=0.0015):
    """A steady uptrend of `drift` per candle, or sideways chop when it is 0.

    Candles alternate up/down around the drift so ATR stays a realistic
    fraction of price rather than collapsing to near zero.
    """
    candles, price = [], 100.0
    for index in range(count):
        rising = index % 2 == 0
        if drift:
            close = price * (1 + drift * 2) if rising else price
        else:
            close = price * (1.002 if rising else 0.998)
        candles.append(_candle(index, price, close))
        price = close
    return candles


def _add_pullback(candles, reclaim=True, body_atr=None, fast_period=None):
    """Walk price back down to the fast EMA, then bounce off it (or fail to).

    `fast_period` selects which analysis' fast EMA to aim the dip at (50 by
    default, for ema50_pullback). `body_atr` pins the size of the final green
    body in ATR, for exercising EMA_PULLBACK_MIN_BODY_ATR; the default lets
    it fall out of the geometry.
    """
    fast_period = fast_period or ca.EMA50_FAST_PERIOD

    for _ in range(40):
        fast = ca.calculate_ema_series(candles, fast_period)[-1]
        atr = ca.calculate_atr(candles, ca.ATR_PERIOD)
        last = candles[-1]["close"]
        if last - fast <= 0.4 * atr:
            break
        candles.append(
            _candle(len(candles), last, max(last * (1 - 0.006), fast + 0.2 * atr))
        )

    fast = ca.calculate_ema_series(candles, fast_period)[-1]
    atr = ca.calculate_atr(candles, ca.ATR_PERIOD)
    close = fast + 0.6 * atr if reclaim else fast - 0.6 * atr
    open_price = close - body_atr * atr if body_atr is not None else candles[-1]["close"]
    candles.append(_candle(len(candles), open_price, close, low=fast - 0.1 * atr))
    return candles


def _evaluate(candles):
    return ca.evaluate_ema50_pullback_candle("TESTUSD", "TEST/USD", candles)


def test_ema50_pullback_fires_on_dip_to_50_ema_in_uptrend():
    hit = _evaluate(_add_pullback(_series()))

    assert hit is not None
    assert hit["direction"] == "UP"
    assert hit["trend_slope_atr"] >= ca.EMA50_MIN_SLOPE_ATR
    assert hit["ema_separation_atr"] >= ca.EMA50_MIN_SEPARATION_ATR
    assert hit["touch_distance_atr"] <= ca.EMA50_PULLBACK_TOUCH_ATR
    assert hit["ema_fast"] > hit["ema_slow"]


def test_ema50_pullback_ignores_dip_that_never_reclaims():
    assert _evaluate(_add_pullback(_series(), reclaim=False)) is None


def test_ema50_pullback_ignores_doji_reclaim():
    """A candle can close above the EMA and still be economically nothing --
    a +0.01% green body. EMA_PULLBACK_MIN_BODY_ATR is what rejects those."""
    below = ca.EMA_PULLBACK_MIN_BODY_ATR / 2
    above = ca.EMA_PULLBACK_MIN_BODY_ATR * 2

    assert _evaluate(_add_pullback(_series(), body_atr=below)) is None

    hit = _evaluate(_add_pullback(_series(), body_atr=above))
    assert hit is not None
    assert hit["body_atr"] >= ca.EMA_PULLBACK_MIN_BODY_ATR


def test_all_three_pullback_analyses_enforce_the_body_floor():
    """The floor is deliberately one shared constant rather than three, so
    guard that every pullback analysis actually reads it.

    Each analysis needs the dip aimed at its own fast EMA, and each is
    asserted to fire on a healthy body first -- otherwise "returns None on a
    doji" would pass vacuously for an analysis that never fires at all.
    """
    analyses = (
        (ca.evaluate_ema_trend_pullback_candle, ca.EMA_FAST_PERIOD),
        (ca.evaluate_ema9_pullback_candle, ca.EMA9_FAST_PERIOD),
        (ca.evaluate_ema50_pullback_candle, ca.EMA50_FAST_PERIOD),
    )

    for evaluate, fast_period in analyses:
        healthy = _add_pullback(
            _series(), body_atr=ca.EMA_PULLBACK_MIN_BODY_ATR * 2, fast_period=fast_period
        )
        doji = _add_pullback(
            _series(), body_atr=ca.EMA_PULLBACK_MIN_BODY_ATR / 2, fast_period=fast_period
        )

        assert evaluate("TESTUSD", "TEST/USD", healthy) is not None, evaluate.__name__
        assert evaluate("TESTUSD", "TEST/USD", doji) is None, evaluate.__name__


def test_ema50_pullback_ignores_extended_price_with_no_dip():
    assert _evaluate(_series()) is None


def test_ema50_pullback_ignores_flat_market():
    assert _evaluate(_add_pullback(_series(drift=0.0))) is None


def test_ema50_pullback_needs_full_warmup_history():
    candles = _add_pullback(_series())
    short = candles[-(ca._ema50_closed_candles_needed() - 1):]

    assert _evaluate(short) is None
    assert _evaluate(candles[-ca._ema50_closed_candles_needed():]) is not None


def test_requested_candle_count_fits_within_kraken_ohlc_limit():
    """Kraken's OHLC endpoint returns at most 720 closed candles (plus the
    forming one) per pair no matter what we ask for. An analysis needing more
    warmed-up history than that would silently never fire, so this guards the
    total budget rather than any one analysis.

    Checked against ALL_ANALYSES, not the enabled subset: an over-budget
    analysis is still a bug when it is toggled off in ENABLED_ANALYSES, and
    the check would otherwise pass for the wrong reason."""
    for confirm in (False, True):
        needed = max(
            spec["closed_candles_needed"](confirm) for spec in ca.ALL_ANALYSES
        )
        assert needed + 1 <= 721
        assert ca._max_requested_candle_count(confirm) <= 721
