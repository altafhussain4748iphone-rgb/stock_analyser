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
# (e.g. BTC) -- this catches that case, same idea as PRICE_CHANGE_ALERT_PCT
# on volume_surge.
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
MIN_24H_QUOTE_VOLUME = 200_000.0

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
# EMA trend settings
#
# "ema_trend_pullback": alert when price pulls back to the fast EMA *within*
# an established uptrend (defined by slow-EMA slope and EMA ordering) and
# closes back up. Reuses the liquidity filters from the breakout analysis so
# alerts stay on tradable pairs, and warms up the EMA over EMA_WARMUP_CANDLES
# extra bars before trusting it (an EMA seeded from a plain SMA is inaccurate
# at first).
# -----------------------------------------------------------------------------

EMA_FAST_PERIOD = 20
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

MOMENTUM_CANDLE_COUNT = 5
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
# than the 20 EMA does.
EMA9_PULLBACK_TOUCH_ATR = 0.30
