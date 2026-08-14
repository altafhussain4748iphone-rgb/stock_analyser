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
# Strategy toggles
#
# Master on/off switch per analysis. A disabled analysis is not registered at
# all: it never evaluates a candle, never contributes an email section, and
# its candle requirement is excluded from the per-pair Kraken fetch size, so
# disabling the slow ones (ema50_pullback needs 600 closed candles) makes each
# scan cheaper as well as quieter. Cooldown state for a disabled analysis is
# left untouched in the state file, so re-enabling it resumes where it left
# off rather than re-alerting on pairs it already covered.
#
# Every key here must match an analysis key in the ALL_ANALYSES registry in
# alerts.py -- a typo or a stale key raises at import rather than silently
# leaving a strategy off.
#
# Currently on: momentum_surge and trend_momentum_surge. The other four are
# opt-in. Note that turning ema9_pullback off gives up the only analysis fast
# enough to catch the first dip after a momentum_surge impulse -- the surge
# alerts stand alone.
#
# The two momentum analyses read the same 3-candle window but ask different
# questions, and neither is a subset of the other:
#
#   - momentum_surge asks how big the move was: >= 5%, ending in a 21/50 EMA
#     uptrend measured on the signal candle.
#   - trend_momentum_surge asks what shape it was: three green candles each
#     closing higher than the last, above a 9/21 pair stacked on every one of
#     them, at any size.
#
# A pair showing up in both sections is the strongest version of the signal.
# momentum_surge is also the expensive one -- its 50 EMA warmup sets the
# per-pair fetch at 201 candles, against the 97 trend_momentum_surge needs on
# its own.
# -----------------------------------------------------------------------------

ENABLED_ANALYSES = {
    "breakout": False,
    "ema_trend_pullback": False,
    "momentum_surge": True,
    "trend_momentum_surge": True,
    "ema9_pullback": False,
    "ema50_pullback": False,
}


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


# Minimum green body (close - open) on the reclaim candle, in ATR. Shared by
# all three pullback analyses: unlike the slope/separation/touch thresholds,
# which describe EMA geometry and so differ per pair speed, this asks a
# question about the signal candle alone -- "is this a real bullish reclaim
# or a doji sitting on the EMA?" -- and ATR already normalises it across
# pairs, so there is no reason for 9/21 and 50/200 to disagree.
#
# Without it the reclaim test was just "close > fast EMA and close > open",
# which any green candle passes. Live hits on 2026-08-10 included CC/USD at
# **+0.01%** and CRV/USD at +0.11% -- technically green, economically
# nothing. Measured over 30 pairs x 120 candles, the median pullback hit had
# a body of only 0.20 ATR, so more than half of these alerts were dojis.
#
# 0.15 is the conservative setting: it removes ~32% of hits, killing the
# indefensible ones while keeping marginal-but-real movers (CAP/USD, +0.52%
# on a genuine 29% run, has a 0.18 ATR body and survives). There is a natural
# gap in the data between 0.25 and 0.49 ATR -- raise this to 0.30 to cut the
# whole doji cluster at the cost of ~53% of hits.
#
# Note this is ATR-relative only, unlike breakout, which pairs MIN_BODY_ATR
# with an absolute BREAKOUT_MIN_PRICE_CHANGE_PCT floor because ATR alone can
# pass economically tiny moves on low-volatility pairs. If that gap shows up
# here too, add a percentage floor rather than raising this.
EMA_PULLBACK_MIN_BODY_ATR = 0.15


# -----------------------------------------------------------------------------
# Momentum-surge settings
#
# "momentum_surge": three tests. Alert when price has moved
# MOMENTUM_PRICE_CHANGE_PCT or more over the last MOMENTUM_CANDLE_COUNT
# candles AND average quote volume across those same candles clears
# MOMENTUM_MIN_AVG_SIGNAL_VOLUME AND the pair is in an uptrend on the signal
# candle -- close above both the EMA_FAST_PERIOD (21) and EMA_SLOW_PERIOD (50)
# EMA, with the 21 EMA above the 50 EMA. Still no candle-quality or ATR
# filters.
#
# The uptrend filter was removed in 5d7ae06 and restored on 2026-08-14, so
# "direction": "UP" once again means the move happened inside a 21/50 uptrend
# rather than merely describing the sign of three candles. A dead-cat bounce
# partway down a sustained decline no longer fires.
#
# Restoring it costs fetch size: the 50 EMA's warmup makes
# _ema_closed_candles_needed() (200 closed candles) the binding constraint
# again, so each pair requests 201 rows instead of 98. That is the same size
# this analysis needed before 5d7ae06, and it is still well inside Kraken's
# 720-row OHLC cap. trend_momentum_surge is unaffected -- it still needs only
# 97, so turning momentum_surge off takes the fetch straight back down.
#
# It is checked on the signal candle only, unlike trend_momentum_surge, which
# checks its faster 9/21 pair on every candle of the window. The two are still
# different questions: a big move that ends in a 21/50 uptrend, versus a
# staircase of any size inside a 9/21 uptrend.
#
# The volume floor below is absolute, and deliberately different in kind from
# the two filters this analysis is still exempt from:
#
#   - the per-candle liquidity filter (medians over 96 candles) and
#     MIN_24H_QUOTE_VOLUME are still computed but cannot suppress a hit; they
#     only badge each alert LIQUID or THIN.
#   - MOMENTUM_MIN_AVG_SIGNAL_VOLUME *does* suppress hits, and it measures the
#     3 signal candles rather than a trailing window -- "was there real money
#     in this specific move", not "is this pair usually liquid".
#
# So a pair can fire while badged THIN (a quiet pair that woke up: >= $5k
# traded across the move, but under $50k over the day), and a normally busy
# pair can be filtered out if its 5% move happened on almost no volume. Both
# are intended.
#
# Three things now change how many alerts arrive: MOMENTUM_PRICE_CHANGE_PCT,
# MOMENTUM_MIN_AVG_SIGNAL_VOLUME and the uptrend filter (tunable only by
# changing EMA_FAST_PERIOD/EMA_SLOW_PERIOD, which ema_trend_pullback shares).
# MIN_24H_QUOTE_VOLUME and REQUIRE_LIQUIDITY_FILTER still only move where the
# badge flips colour.
# -----------------------------------------------------------------------------

MOMENTUM_CANDLE_COUNT = 3
MOMENTUM_PRICE_CHANGE_PCT = 5.0

# Average quote volume (USD) across the MOMENTUM_CANDLE_COUNT signal candles.
# Compared with >=, matching the other MIN_* floors in this file.
#
# This is the one volume check that can block a momentum_surge alert. Setting
# it to 0 disables it -- and is the only way the "n/a" relative-volume
# rendering in alerts.py becomes reachable again, since a window clearing a
# positive floor here always leaves a positive baseline to divide by.
MOMENTUM_MIN_AVG_SIGNAL_VOLUME = 5_000.0

# Sizes the relative-volume figure printed on each alert. It does not decide
# whether one fires, and it does not size the Kraken fetch either -- the 50 EMA
# warmup (200 candles) dwarfs it, so raising this below 200 costs nothing.
MOMENTUM_VOLUME_LOOKBACK = 20


# -----------------------------------------------------------------------------
# Trend momentum-surge settings
#
# "trend_momentum_surge": the same window as momentum_surge
# (MOMENTUM_CANDLE_COUNT candles) and the same volume floor
# (MOMENTUM_MIN_AVG_SIGNAL_VOLUME), but the *shape* of the move rather than its
# size. Four conditions, all checked on *every* candle of the window rather
# than only the last one:
#
#   1. the candle closed above its open (a real up candle, not a wick that
#      happened to land higher),
#   2. the candle closed above the previous candle's close,
#   3. the 9 EMA was above the 21 EMA, and
#   4. the close was above the 21 EMA.
#
# (1) and (2) are not the same test and neither implies the other: a candle
# that gaps up and fades closes above the previous close while printing red,
# and one that opens below the previous close and recovers only part of the
# way is green while the sequence of closes steps down. Together they mean the
# window is a staircase -- each bar both bullish in itself and higher than the
# last. (2) compares the window's first candle against the bar immediately
# *before* the window, so "every candle" really is every candle.
#
# Checking all three candles is the strict reading: it means the whole run
# happened inside an established 9/21 uptrend, not that the move itself dragged
# the EMAs into line by its last bar. That excludes the freshest 9/21 crosses,
# which is the trade-off -- momentum_surge still fires on those (whenever they
# clear its 5% bar), they just aren't badged as trend-confirmed. To relax it,
# compare only the last element of each EMA window in
# evaluate_trend_momentum_surge_candles.
#
# This analysis is NOT a subset of momentum_surge. It applies its own price
# threshold, MOMENTUM_TREND_PRICE_CHANGE_PCT below, which is 0 -- three green
# candles stacked over a rising 9/21 pair is the signal, whatever its size. So
# most of its hits are moves far under MOMENTUM_PRICE_CHANGE_PCT that
# momentum_surge never sees, and the two email sections overlap only on the
# rare move that is both big and clean.
#
# No ATR, slope or separation test: unlike the pullback analyses, which need
# the EMAs to be meaningfully apart before a "touch" means anything, the
# evidence here is the run of green candles holding above a stacked EMA pair.
# The EMAs only answer "was this with the trend or against it".
#
# The 9/21 periods are duplicated from the ema9_pullback block rather than
# imported from it so the two can be tuned independently -- a pullback and an
# impulse ask different questions of the same EMA pair.
# -----------------------------------------------------------------------------

MOMENTUM_TREND_FAST_PERIOD = 9
MOMENTUM_TREND_SLOW_PERIOD = 21

# Deliberately 0, not MOMENTUM_PRICE_CHANGE_PCT: size is momentum_surge's
# question, and this analysis asks about structure instead.
#
# At 0 this test cannot actually reject anything -- green candles with rising
# closes force the last close above the first open, so the move is always
# positive. It is kept as the knob for putting a size floor back (set it to
# 1.0 and sub-1% staircases stop alerting) and as the guarantee behind
# "direction": "UP" if the candle conditions above are ever loosened.
#
# Measured over 12,060 candle evaluations (60 live Kraken pairs x ~2 days of
# 15m candles, 2026-08-14, before cooldown): momentum_surge fired 71 times,
# this analysis 65, and only 37 of those were the same candle. So the two
# sections come out a similar size but overlap on about half their hits -- 34
# surges did not qualify here (mostly closes that failed to step up cleanly),
# and 28 staircases fired here that were too small for the 5% bar. Median hit
# size here was +5.78%.
#
# (Requiring each close above the previous one, rather than only requiring
# green candles, cut this analysis from 78 hits to 65 on that same sample.)
#
# Raise this if the section gets noisy: it is the one knob that trades alert
# count against how big a move has to be to qualify.
MOMENTUM_TREND_PRICE_CHANGE_PCT = 0.0

# Same 3x rule as EMA9_WARMUP_CANDLES: an EMA seeded from a plain SMA is
# inaccurate for its first few periods, and here that error would show up as
# false 9-over-21 orderings on pairs that just came into range.
MOMENTUM_TREND_WARMUP_CANDLES = MOMENTUM_TREND_SLOW_PERIOD * 3


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
