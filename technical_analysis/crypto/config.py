# -----------------------------------------------------------------------------
# Kraken/API configuration
# -----------------------------------------------------------------------------

KRAKEN_API_URL = "https://api.kraken.com/0/public"
VALID_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
QUOTE_FILTER = ("USD", "USDT")

# Base currencies to skip entirely -- these pairs are never fetched or
# scanned by any analysis. Matched case-insensitively against the base
# symbol in Kraken's wsname (the part before "/"), which is stable across
# Kraken's legacy pair-key naming quirks (e.g. the pair key for BTC/USD is
# XXBTZUSD, but its wsname base is "XBT"). Both "XBT" and "BTC" are listed
# since Kraken itself only ever uses "XBT", but a stray listing could use
# either.
SKIP_BASE_CURRENCIES = frozenset({
    "HYPE", "XBT", "BTC", "SOL", "ETH", "LINK", "TAO", "AAVE", "UNI", "XRP",
})

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

# Absolute price-move floor (open->close of the signal candle), on top of
# the ATR-relative filters above. The ATR filters alone can pass on a move
# that's economically tiny in % terms for a low-volatility, high-price pair
# (e.g. BTC) -- this catches that case.
BREAKOUT_MIN_PRICE_CHANGE_PCT = 1.0

# Robust quote-volume filters.
MIN_VOLUME_MULTIPLE = 2.0
MIN_VOLUME_ROBUST_Z = 3.0
MIN_MEDIAN_QUOTE_VOLUME = 1_000.0

# Absolute 24h liquidity floor, applied to every alert type regardless of
# REQUIRE_LIQUIDITY_FILTER. Uses the same 24h (VOLUME_LOOKBACK) window as the
# liquidity filters above, but summed rather than medianed -- this is the
# "24h volume" figure a trader would recognize from an exchange ticker, and
# is also surfaced on every alert for manual review.
MIN_24H_QUOTE_VOLUME = 50_000.0

# Liquidity/activity filters. Enable once you've picked floors
# (MIN_MEDIAN_QUOTE_VOLUME, MIN_MEDIAN_TRADE_COUNT, MIN_SIGNAL_TRADE_COUNT)
# that fit the pairs you trade.
REQUIRE_LIQUIDITY_FILTER = True
MIN_MEDIAN_TRADE_COUNT = 5
MIN_SIGNAL_TRADE_COUNT = 5

# Do not alert repeatedly on the same pair for this many candles.
COOLDOWN_CANDLES = 4

# Optional next-candle confirmation. This can also be enabled with the
# --confirm-next-candle command-line option.
DEFAULT_REQUIRE_NEXT_CANDLE_CONFIRMATION = False
CONFIRM_RETEST_TOLERANCE_ATR = 0.25


# -----------------------------------------------------------------------------
# EMA trend settings
#
# "ema_trend_pullback": alert when price pulls back to the fast EMA *within*
# an established uptrend (defined by slow-EMA slope and EMA ordering) and
# closes back up. Reuses the liquidity filters from the breakout analysis so
# alerts stay on tradable pairs, and warms up the EMA over EMA_WARMUP_CANDLES
# extra bars before trusting it (an EMA seeded from a plain SMA is inaccurate
# at first).
# -----------------------------------------------------------------------------

EMA_FAST_PERIOD = 21
EMA_SLOW_PERIOD = 50
EMA_WARMUP_CANDLES = EMA_SLOW_PERIOD * 3

# A cross/trend is only actionable once the EMAs are this far apart (in ATR),
# otherwise they are tangled together and any "cross" is just noise.
EMA_MIN_SEPARATION_ATR = 0.25

# How many candles back to measure the slow EMA's slope for trend direction.
EMA_TREND_LOOKBACK = 10

# Minimum slow-EMA slope, in ATR per candle, to call the market "trending"
# rather than flat.
EMA_MIN_SLOPE_ATR = 0.05

# How close the signal candle must dip toward the fast EMA to count as a
# pullback touch, in ATR.
EMA_PULLBACK_TOUCH_ATR = 0.35


# -----------------------------------------------------------------------------
# Momentum-surge settings
#
# "momentum_surge": a coarser, faster-firing check than breakout or
# ema_trend_pullback -- no candle-quality or ATR filters at all. Alert when
# price has moved MOMENTUM_PRICE_CHANGE_PCT or more over the last
# MOMENTUM_CANDLE_COUNT candles, while average volume over that same window
# is running above the average volume of the last MOMENTUM_VOLUME_LOOKBACK
# candles. Reuses the breakout liquidity floor so alerts stay on tradable
# pairs.
# -----------------------------------------------------------------------------

MOMENTUM_CANDLE_COUNT = 3
MOMENTUM_VOLUME_LOOKBACK = 20
MOMENTUM_PRICE_CHANGE_PCT = 5.0


# -----------------------------------------------------------------------------
# 9 EMA pullback settings
#
# "ema9_pullback": the same "buy the dip in an uptrend" idea as
# ema_trend_pullback, but keyed off the faster 9/21 EMA pair instead of
# 20/50. A 50 EMA trend takes hours to establish, so ema_trend_pullback can
# miss the *first* pullback after a fresh impulse move (e.g. a momentum_surge
# hit) -- by the time its trend filter confirms, that early, better-risk/
# reward dip is often already gone. The 9/21 pair reacts fast enough to catch
# it, at the cost of being noisier. Reuses the liquidity filters from the
# breakout analysis.
# -----------------------------------------------------------------------------

EMA9_FAST_PERIOD = 9
EMA9_SLOW_PERIOD = 21
EMA9_WARMUP_CANDLES = EMA9_SLOW_PERIOD * 3

# Tighter than EMA_MIN_SEPARATION_ATR since the 9/21 pair naturally sits
# closer together (in ATR terms) than 20/50 even in a real trend.
EMA9_MIN_SEPARATION_ATR = 0.20

# Shorter than EMA_TREND_LOOKBACK -- this pair should confirm a trend within
# ~1.5 hours on 15m candles, not multiple hours.
EMA9_TREND_LOOKBACK = 6

# Higher than EMA_MIN_SLOPE_ATR: the 21 EMA moves faster than the 50 EMA, so
# a real trend clears a higher ATR/candle bar here.
EMA9_MIN_SLOPE_ATR = 0.08

# Tighter than EMA_PULLBACK_TOUCH_ATR since the 9 EMA hugs price more closely
# than the 21 EMA does.
EMA9_PULLBACK_TOUCH_ATR = 0.30


# -----------------------------------------------------------------------------
# 50 EMA pullback settings
#
# "ema50_pullback": the same "buy the dip in an uptrend" idea again, one step
# slower than ema_trend_pullback -- keyed off the classic 50/200 pair. Where
# ema9_pullback catches the first dip in a move that's minutes old and
# ema_trend_pullback catches dips in a move that's hours old, this one only
# fires inside a trend that has held for *days* (a 200 EMA on 15m candles is
# a ~50-hour average). Far fewer alerts, but each one is a pullback within a
# structurally established trend rather than a short-lived impulse. Reuses
# the liquidity filters from the breakout analysis.
# -----------------------------------------------------------------------------

EMA50_FAST_PERIOD = 50
EMA50_SLOW_PERIOD = 200

# NOTE: 2x the slow period, not the 3x used by the faster pairs. Kraken's OHLC
# endpoint returns at most 720 closed candles per pair regardless of what we
# ask for, and 200 + 200*3 = 800 would exceed that -- the analysis could then
# never accumulate enough history and would silently never fire. At 2x, the
# EMA is seeded 400 candles before the signal candle, leaving ~2% of the
# initial SMA seed's error unconverged, which is immaterial for the
# slope/separation comparisons below (both measured in ATR).
EMA50_WARMUP_CANDLES = EMA50_SLOW_PERIOD * 2

# Wider than EMA_MIN_SEPARATION_ATR: over the long horizon this pair measures,
# a genuine trend separates the 50 and 200 EMA by more than a 21/50 trend
# separates its pair, so a low bar here would mostly admit tangled EMAs.
EMA50_MIN_SEPARATION_ATR = 0.30

# Longer than EMA_TREND_LOOKBACK (5 hours on 15m candles). The 200 EMA barely
# moves candle to candle, so a short lookback measures rounding noise rather
# than trend direction.
EMA50_TREND_LOOKBACK = 20

# Lower than EMA_MIN_SLOPE_ATR: the 200 EMA advances roughly a quarter as fast
# per candle as the 50 EMA does for the same underlying trend, so holding it
# to the same ATR/candle bar would reject every real trend.
EMA50_MIN_SLOPE_ATR = 0.03

# Wider than EMA_PULLBACK_TOUCH_ATR -- price ranges further from the 50 EMA
# than from the 21 EMA, so pullbacks to it are looser.
#
# Measured over 25 Kraken pairs x 120 recent 15m candles (2026-08-10), this
# is the binding filter of the three, and it keeps this analysis the rarest
# of the pullback family by design:
#
#   analysis          median low->fast EMA distance   touch threshold   fires
#   ema9   (9/21)                 1.10 ATR                 0.30          3.9%
#   ema21  (21/50)                1.97 ATR                 0.35          2.6%
#   ema50  (50/200)               3.43 ATR                 0.45          0.8%
#
# ("fires" = share of candles already passing that analysis' trend filter.)
# Note the thresholds deliberately do NOT scale with the median distance --
# holding them nearly flat as the pair slows is what makes each successive
# analysis more selective than the last. Raise this toward ~0.60 if 50 EMA
# pullbacks turn out to be too rare to be useful in practice.
EMA50_PULLBACK_TOUCH_ATR = 0.45
