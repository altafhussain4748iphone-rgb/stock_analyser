# Crypto Alert Strategies

`technical_analysis/crypto/alerts.py` runs five independent analyses against
one shared Kraken 15-minute candle fetch per pair (see the `ANALYSES`
registry). Each scan sends **one combined email** with a section per analysis
that had hits. All five only alert on upside signals — no DOWN/bearish
alerts are sent. This document explains what each analysis actually checks
and walks through example trades in a few different market scenarios —
including scenarios where the strategy is *wrong*, since that's just as
important as the winning case.

None of this is a backtested, guaranteed-profitable system. These are
alerting heuristics built on well-established technical trading logic. Treat
every example below as illustrating the *mechanism*, not a promise of the
outcome.

### Each analysis can be switched on or off

`ENABLED_ANALYSES` in `technical_analysis/crypto/config.py` is the master
on/off switch per analysis. **Only `momentum_surge` is enabled**; the other
four are opt-in:

```python
ENABLED_ANALYSES = {
    "breakout": False,
    "ema_trend_pullback": False,
    "momentum_surge": True,
    "ema9_pullback": False,
    "ema50_pullback": False,
}
```

Note what turning `ema9_pullback` off gives up: it was the one analysis fast
enough to catch the *first* dip after a `momentum_surge` impulse (see §4
below), so the surge alert now stands alone rather than being followed by an
entry-shaped pullback signal on the same pair.

A disabled analysis is not registered at all — it never evaluates a candle
and never contributes an email section. Its candle requirement is also
excluded from the shared per-pair fetch, so `momentum_surge` alone fetches
**98** candles per pair rather than the 601 that `ema50_pullback` demands.
That number dropped from 201 when the EMA filter was removed from
`momentum_surge`: without an EMA warmup to satisfy, its requirement is now
set by the 96-candle window behind the 24h volume badge rather than by the
200-candle 50 EMA. Cooldown state for a disabled analysis is left untouched
in the state file, so re-enabling it resumes where it left off rather than
re-alerting on pairs it had already covered.

The rest of this document describes all five analyses regardless of whether
they are currently enabled.

### Two liquidity filters apply to four of the five analyses

> **`momentum_surge` is exempt from both.** Neither filter below can suppress
> a `momentum_surge` alert — it reports volume instead of gating on it, via a
> LIQUID/THIN badge (see §3). Everything in this section applies to
> `breakout`, `ema_trend_pullback`, `ema9_pullback` and `ema50_pullback`.

- **Per-candle liquidity filter** (`REQUIRE_LIQUIDITY_FILTER`, currently
  **on**): median quote volume over the trailing 96 candles ≥ **$1,000**,
  median trade count ≥ 5, and the signal candle itself ≥ 5 trades. Tables
  below list this as "Liquidity floor."
- **Absolute 24h volume floor** (`MIN_24H_QUOTE_VOLUME`): trailing 24h quote
  volume ≥ **$50,000**. This is a coarser "is this pair even worth alerting
  on" check — a sum, not a median, so it can't be skewed low by one quiet
  candle — and its value is printed on every alert (`24h volume $X`) so you
  can sanity-check liquidity without cross-referencing anything. For
  `momentum_surge` this same number is still computed and printed, but only
  to colour the badge.
- **`breakout` has one more, non-optional guard** on top of both filters
  above: it divides the signal candle's volume by the trailing 96-candle
  *median* volume to compute a volume multiple. If that median is exactly
  **$0** (more than half the trailing candles had zero trades — a dead
  pair), the division is skipped and the function returns no hit, regardless
  of `REQUIRE_LIQUIDITY_FILTER`. This is a structural guard against dividing
  by zero, not the configurable liquidity filter — so even if you turn
  `REQUIRE_LIQUIDITY_FILTER` off, `breakout` still won't alert on a pair
  whose trailing volume is entirely zero.

### A reclaim-body floor applies to all three pullback analyses

`ema_trend_pullback`, `ema9_pullback` and `ema50_pullback` all end with the
same question: did the signal candle close back beyond the fast EMA? That
test used to be satisfied by *any* green candle, which in practice meant a
lot of alerts on candles that had done nothing. On 2026-08-10 the live scan
fired CC/USD at **+0.01%** and CRV/USD at +0.11% — technically green,
economically meaningless. Measured over 30 pairs × 120 candles, the median
pullback hit had a body of just **0.20 ATR**, so more than half of these
alerts were dojis sitting on an EMA.

`EMA_PULLBACK_MIN_BODY_ATR` (**0.15 ATR**) now requires the reclaim candle to
have a real body. It is deliberately **one shared constant, not three**:
unlike the slope, separation and touch thresholds — which describe EMA
geometry and so legitimately differ per pair speed — this asks a question
about the signal candle alone, and ATR already normalises it across pairs.

It is measured in ATR rather than as a fixed percentage because a percentage
that's meaningful for a micro-cap is noise on a major, and vice versa. Note
`breakout` pairs its ATR body filter with an *absolute* percentage floor
(`BREAKOUT_MIN_PRICE_CHANGE_PCT`) precisely because ATR alone can still pass
economically tiny moves on a low-volatility pair; if that gap shows up here,
the fix is to add a percentage floor rather than to raise this one.

At 0.15 this removes ~32% of pullback hits — the indefensible ones, while
keeping marginal-but-real movers. There is a natural gap in the data between
0.25 and 0.49 ATR; raising it to **0.30** cuts the entire doji cluster at
the cost of ~53% of hits.

---

## 1. Breakout (`breakout`)

**What it checks:** a candle that closes decisively beyond the prior
20-candle range (5 hours on 15m), backed by real volume — not just a wick
that pokes through and fades.

| Filter | Threshold |
|---|---|
| Range lookback | 20 candles |
| Clears range by | ≥ 0.15 × ATR(14) |
| Candle body / range | ≥ 60% (not a doji or long-wick candle) |
| Close location in candle | ≥ 75% toward the extreme |
| Body size | ≥ 0.80 × ATR |
| Price move (open→close) | ≥ 1.0% |
| Volume vs 96-candle median | ≥ 2.0× **and** robust z-score ≥ 3.0 |
| Liquidity floor | median quote volume ≥ $1,000, ≥5 trades/candle |
| 24h volume floor | ≥ $50,000 |
| Cooldown | 4 candles per pair |

### Scenario A — the intended win: volume-backed breakout continues

BTC/USD has chopped between $60,000–$60,500 for 5 hours. ATR ≈ $150.

A candle prints: open $60,480 → close $61,150, high $61,200, low $60,470.

- Body = $670 = **4.5× ATR** (clears the 0.80 minimum)
- Price move is **+1.11%** (clears the 1.0% minimum)
- Close is 96% of the way to the candle's high (clears the 75% minimum)
- Clears the range ($60,500) by $650, well past the $22.50 ATR buffer
- Volume on the candle is 3.2× the 24h median, robust z-score 4.1

**Alert fires: UP breakout.** Real size pushed price out of the range and
held it near the high — stop-losses from range-bound shorts trigger,
trend-followers who watch this exact pattern start buying, price grinds up
toward $62,000+ over the next few hours. This is the case the strategy is
designed to catch.

### Scenario B — the failure mode: exhaustion candle looks identical

ETH/USD has been climbing gradually for a day. A candle prints open $3,180 →
close $3,240, high $3,245, low $3,175 — big body, closes near the high,
volume 3.5× median. Every filter above passes. **Alert fires: UP breakout.**

But this candle is actually the last gasp of an already-extended move — it's
the point where late longs pile in and the *next* candle reverses hard back
into the range. From the alert's data alone, this is indistinguishable from
Scenario A. This is exactly why the code has an optional
`--confirm-next-candle` flag: it delays the alert by one candle and requires
that next candle to hold the broken level before firing, trading a slower
entry for fewer of these false breakouts.

### Scenario C — correctly does *not* fire: no volume behind the move

SOL/USD grinds out of its 20-candle range with a similarly strong candle
(body 1.1× ATR, closes near the high) — but volume on that candle is only
1.3× the median (below the 2.0× minimum) and the robust z-score is 1.8
(below 3.0). **No alert.** This is a low-conviction breakout — plenty of
these fail immediately because no real size is behind the move — and the
volume/liquidity filters are specifically what keep the analysis from firing
on it.

### Scenario D — correctly does *not* fire: ATR-valid breakout, economically too small

BTC/USD chops the same $60,000–$60,500 range, ATR ≈ $150. A candle prints
open $60,480 → close $60,690, high $60,700, low $60,470 — body $210 (1.4×
ATR, clears the 0.80 minimum), closes 96% toward the high, clears the range
by $190 (past the ATR buffer), volume 3.2× median with robust z-score 4.1.
Every ATR-relative and volume filter passes. But the price move is only
**+0.35%** — below the 1.0% floor. **No alert.** On a low-volatility,
high-price pair like BTC, the ATR filters alone can validate a move that's
too small to be worth trading after fees and slippage; the price-move floor
exists specifically to catch that gap. (This is the exact candle from an
earlier draft of Scenario A, before the 1.0% floor was added — it used to
fire.)

---

## 2. EMA Trend Pullback (`ema_trend_pullback`)

**What it checks:** "buy the dip in an established uptrend" — enter on a
pullback to the fast EMA within a confirmed uptrend, rather than chasing an
extended breakout.

| Filter | Threshold |
|---|---|
| Trend defined by 50 EMA slope | ≥ 0.05 × ATR per candle (over a 10-candle lookback) |
| EMA separation (not tangled/flat) | ≥ 0.25 × ATR |
| Pullback touch distance to 21 EMA | ≤ 0.35 × ATR |
| Reclaim | signal candle must close back beyond the 21 EMA, same direction as the trend |
| Reclaim body | ≥ 0.15 × ATR (close − open) |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |

### Scenario A — the intended win: low-risk trend continuation entry

SOL/USD has trended up for 3+ hours — 50 EMA rising at 0.4 ATR/candle (well
above the 0.05 minimum), 21 EMA sitting 2.1 ATR above the 50 EMA (clearly
separated, not flat). Price pulls back for 2–3 red candles to $142.30,
touching within 0.15 ATR of the 21 EMA ($142.10), then closes back up at
$143.80 — above the 21 EMA, bullish body. **Alert fires: UP.** Your natural
stop sits just below the pullback low; if the trend continues, this entry
has a much better risk/reward than chasing the original breakout candle
would have.

### Scenario B — the failure mode: "one pullback too many"

The same SOL/USD uptrend continues for another 8 hours through three more
pullback-and-bounce cycles — each one fires its own alert (once cooldown
elapses). The fourth pullback looks identical to the first three: touches
the 21 EMA, closes back above it. **Alert fires: UP.** But this time the
trend is actually exhausted — that "bounce" is the last gasp before the 21
EMA gets broken and the uptrend ends. The rule has no concept of "this is
the Nth pullback in this trend" — every pullback that meets the geometry
looks the same to it, whether it's early or late in the trend's life.

### Scenario C — correctly does *not* fire: market is flat, not trending

A pair chops in a $2 range for hours. The 50 EMA slope is only 0.02 ATR/candle
(below the 0.05 minimum) and the 21/50 EMA separation is 0.10 ATR (below the
0.25 minimum) — the EMAs are tangled together, not trending. Price touches
the 21 EMA repeatedly as it oscillates. **No alert on any of these
touches** — exactly the scenario this filter exists to reject, since a
"pullback" only means something in a market that's actually trending.

---

## 3. Momentum Surge (`momentum_surge`)

**What it checks:** one thing. Has price moved ≥ 5.0% over the last 3
candles? There is no other condition — no trend, volume, liquidity,
candle-quality or ATR filter remains.

| Filter | Threshold |
|---|---|
| Price move over last 3 candles | ≥ 5.0% (open of the 3-candle window to close of the last) |
| Uptrend (21/50 EMA) | **removed** — not checked at all |
| Relative volume | **not checked** — reported only |
| Liquidity floor | **not checked** — reported only |
| 24h volume floor | **not checked** — drives the LIQUID/THIN badge |
| Cooldown | 4 candles per pair |

#### "UP" no longer means "in an uptrend"

The EMA filter used to require close > 21 EMA, close > 50 EMA and 21 EMA >
50 EMA. With it gone, a qualifying 3-candle rise fires **regardless of what
the larger trend is doing** — including a dead-cat bounce partway down a
sustained decline, which is exactly the case the filter existed to exclude.

The `direction: UP` in each alert describes the sign of the 3-candle move and
nothing more. Judging whether that move sits inside an uptrend is now
entirely on you, from the chart.

This also makes `MOMENTUM_PRICE_CHANGE_PCT` the sole tuning knob: every other
threshold this analysis touches affects only what the email *says*, not how
many alerts arrive.

#### Volume is reported, not enforced

Price and the EMA uptrend are the *only* things that can stop an alert here.
Every hit carries a badge set by trailing 24h quote volume against
`MIN_24H_QUOTE_VOLUME` (**$50,000**):

- <span title="green">**LIQUID**</span> (green) — 24h volume ≥ $50,000.
- <span title="red">**THIN**</span> (red) — below it. The move is real in
  percentage terms but may be one small order crossing a wide spread, and the
  size you can actually trade is limited.

The relative-volume multiple is still printed (`volume 3.18x 20-candle
average`) as an unfiltered reading. A pair with *no* volume at all over the
trailing 20 candles renders it as `n/a` rather than being dropped — that case
used to be rejected outright by a divide-by-zero guard, and now surfaces
badged THIN.

**Expect more alerts, and expect some of them to be junk.** This trades
precision for coverage: you see every qualifying price move and do the
liquidity triage yourself from the badge. The old behaviour — volume as a
hard gate — is what the other four analyses still do.

### Scenario A — the intended catch: a move already underway

ADA/USD grinds from $0.4200 to $0.4430 over 3 candles (+5.5%) inside a clean
uptrend, and the 3-candle average quote volume is running above the 20-candle
average — the move isn't happening on fading interest. **Alert fires: UP,
badged LIQUID.** Useful as a "this is already moving, decide if you want in"
signal, not an early entry.

Both of the things that make this the *good* case — the healthy volume and
the surrounding uptrend — are now yours to read off the alert and the chart.
Neither is what qualified it; only the +5.5% did.

### Scenario B — the failure mode: alerts near the top of an already-extended move

The same ADA/USD move continues for another 10 candles before reversing.
Because the trigger requires the move to have *already happened* over a
3-candle window, by construction the alert can only fire after most of the
gain is behind it — there's no mechanism here (unlike `ema_trend_pullback`)
that looks for a lower-risk entry point.

### Scenario C — now fires, and used not to: choppy volume during the move

A pair moves +6% over 3 candles, but volume on those candles is *lower* on
average than the last 20 (the move happened on thinning participation).
**Alert fires** — badged by 24h volume, which may well still be LIQUID, since
the badge measures absolute 24h turnover and not whether volume faded during
this particular move.

This is the clearest cost of removing the volume gate. That condition was
previously "the one thing keeping this analysis from firing on random 5%+
chop," and it is gone. The `volume 0.8x 20-candle average` reading in the
alert body is what now tells you this — a multiple below ~1.0x means the move
happened on fading participation, and it is worth reading before acting even
when the badge is green.

### Scenario D — now fires: a thin pair below the 24h floor

ESP/USD's 2026-07-26 10:00 spike (+25% on the candle, $110k of volume and 438
trades on that candle alone) sat on a pair where **54 of the 96 trailing
candles had zero volume and zero trades** — trailing median quote volume of
exactly **$0**, far under the $1,000 per-candle minimum. Under the old rules
that failed the liquidity filter outright and produced **no alert on any of
the five analyses**.

Now `momentum_surge` **does alert** on it. Its trailing 24h volume of
$128,265 clears $50,000, so it would even be badged **LIQUID** despite half
its recent candles being dead — a case where the badge and the per-candle
liquidity picture disagree, and the badge is the less informative of the two.
The other four analyses still reject it. `breakout` in particular would
reject it even with `REQUIRE_LIQUIDITY_FILTER` off, since it divides by that
same $0 median and skips the division as a structural divide-by-zero guard.

For historical context on the floor that no longer applies here: GWEI/USD on
2026-07-24 14:45 (measured when `MOMENTUM_CANDLE_COUNT` was still 5) printed
a +5.21% move on 1.53× baseline volume with $192,165 of 24h volume. It was
blocked by the original $500,000 floor and successively unblocked as that
floor came down to $50,000. That whole line of tuning is now moot for
`momentum_surge` — no 24h floor value can block it — and matters only for the
other four analyses.

### Scenario E — now fires: a bounce inside a downtrend

A token has bled −45% over two days. Price is well below both the 21 and 50
EMA, with the 21 EMA below the 50 EMA — a textbook downtrend. It then bounces
+5.4% over 3 candles on decent turnover, as falling assets regularly do.

**Alert fires: UP, badged LIQUID.** The EMA filter existed precisely to
exclude this, and it is gone. The email will describe a genuine +5.4% move on
a liquid pair, and every word of that will be true while the pair is still in
free-fall. Nothing in the alert distinguishes this from Scenario A — that
distinction now lives only on the chart.

---

## 4. 9 EMA Pullback (`ema9_pullback`)

**What it checks:** the same "buy the dip in an uptrend" idea as
`ema_trend_pullback`, but keyed off the faster 9/21 EMA pair instead of
20/50. A 50 EMA trend takes hours to establish, so `ema_trend_pullback` can
miss the *first* pullback after a fresh impulse move — e.g. a
`momentum_surge` hit, which by definition is only 3 candles (45 minutes on
15m) old — because by the time the 50 EMA trend filter confirms, that
early, better-risk/reward dip is often already gone. This analysis exists to
close that gap: spot the mover with `momentum_surge`, then let this fire on
the earliest pullback worth buying.

| Filter | Threshold |
|---|---|
| Trend defined by 21 EMA slope | ≥ 0.08 × ATR per candle (over a 6-candle lookback) |
| EMA separation (not tangled/flat) | ≥ 0.20 × ATR |
| Pullback touch distance to 9 EMA | ≤ 0.30 × ATR |
| Reclaim | signal candle must close back beyond the 9 EMA, same direction as the trend |
| Reclaim body | ≥ 0.15 × ATR (close − open) |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |

### Scenario A — the intended win: catching the first dip after a fresh impulse

DOGE/USD just fired a `momentum_surge` alert 30 minutes ago (+6% over 5
candles). The 21 EMA is now rising at 0.15 ATR/candle (above the 0.08
minimum) and sits 0.9 ATR below the 9 EMA (above the 0.20 separation
minimum) — a trend by this faster pair's definition, even though it's far
too young to register on the slower 50 EMA. Price pulls back for one red
candle to within 0.10 ATR of the 9 EMA, then closes back up above it.
**Alert fires: UP.** This is the entry `ema_trend_pullback` would have
missed — its 50 EMA trend filter wouldn't confirm for hours yet.

### Scenario B — the failure mode: same "one pullback too many" problem, faster

Because the 9/21 pair reacts quickly, it also *ends* its read on a trend
quickly. A token chops through several shallow pullback-and-bounce cycles in
under two hours, each one firing its own alert once cooldown clears. The
third one looks identical to the first but is actually the top — the 9 EMA
gets broken on the next candle and the move is over. Same blind spot as
`ema_trend_pullback` (no concept of "which pullback in the trend is this"),
just compressed into a much shorter window, so acting on every hit here is
riskier than acting on every `ema_trend_pullback` hit.

### Scenario C — correctly does *not* fire: trend too young/flat by this pair's own bar

A pair has moved sideways-to-slightly-up for the last few candles — 21 EMA
slope is only 0.03 ATR/candle (below the 0.08 minimum). **No alert**, even
though price is technically drifting up, because there isn't yet a real
trend to call this a "pullback within."

---

## 5. 50 EMA Pullback (`ema50_pullback`)

**What it checks:** the same "buy the dip in an uptrend" idea a third time,
now one step *slower* than `ema_trend_pullback` — keyed off the classic
50/200 pair. The three pullback analyses differ only in the timescale of
trend they'll accept: `ema9_pullback` (9/21) fires inside a move that's
minutes old, `ema_trend_pullback` (21/50) inside one that's hours old, and
this one only inside a trend that has held for **days** — a 200 EMA on 15m
candles is a ~50-hour average. It is by far the rarest of the three, and
each hit is a dip within structural trend rather than a fresh impulse.

| Filter | Threshold |
|---|---|
| Trend defined by 200 EMA slope | ≥ 0.03 × ATR per candle (over a 20-candle lookback) |
| EMA separation (not tangled/flat) | ≥ 0.30 × ATR |
| Pullback touch distance to 50 EMA | ≤ 0.45 × ATR |
| Reclaim | signal candle must close back beyond the 50 EMA, same direction as the trend |
| Reclaim body | ≥ 0.15 × ATR (close − open) |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |

The thresholds move in the same direction the pair does relative to 21/50:
looser on slope (a 200 EMA advances roughly a quarter as fast per candle as
a 50 EMA for the same trend, so the 0.05 bar would reject everything),
wider on separation and touch distance (price ranges further from a 50 EMA
than from a 21 EMA), and a longer lookback (the 200 EMA barely moves candle
to candle, so a short one measures rounding noise).

**How often it actually fires.** Measured over 25 Kraken pairs × 120 recent
15m candles (2026-08-10), counting the share of candles that had already
passed each analysis' own trend filter:

| Analysis | Median low→fast EMA distance | Touch threshold | Fires on |
|---|---|---|---|
| `ema9_pullback` (9/21) | 1.10 ATR | ≤ 0.30 ATR | 3.9% |
| `ema_trend_pullback` (21/50) | 1.97 ATR | ≤ 0.35 ATR | 2.6% |
| `ema50_pullback` (50/200) | 3.43 ATR | ≤ 0.45 ATR | 0.8% |

Touch distance is the binding filter in all three. Note the thresholds
deliberately *don't* scale with the median distance — price ranges 3× further
from a 50 EMA than from a 9 EMA, but the threshold only widens by half.
Holding them nearly flat as the pair slows is precisely what makes each
successive analysis more selective, and it's why this one fires roughly a
third as often as 21/50 on top of having a much harder trend filter to
satisfy in the first place.

**A hard constraint worth knowing:** Kraken's OHLC endpoint returns at most
720 closed candles per pair regardless of what's requested. The other EMA
analyses warm their EMA up over 3× the slow period before trusting it; at
3× this pair would need 200 + 600 = 800 candles, more than Kraken will ever
return, and the analysis would silently never fire. `EMA50_WARMUP_CANDLES`
is therefore 2× (400), for 600 candles total — the seed error left
unconverged at 2× is ~2%, immaterial for comparisons measured in ATR.
`test_requested_candle_count_fits_within_kraken_ohlc_limit` guards the
budget so a future analysis can't quietly blow past the ceiling.

### Scenario A — the intended win: the dip everyone waits for

SOL/USD has trended up for four days. The 200 EMA is rising at 0.06
ATR/candle (double the 0.03 minimum) and the 50 EMA sits 3.2 ATR above it —
unambiguous structural uptrend. Price sells off for most of a day, and one
candle wicks down to within 0.2 ATR of the 50 EMA, then closes back above it
green. **Alert fires: UP.** This is the highest-conviction of the three
pullback signals: the trend behind it is measured in days, so a single bad
candle is far less likely to have invalidated it.

### Scenario B — the failure mode: it's the last pullback, and it's a big one

The same SOL/USD trend is four days old — which also means it's four days
*closer to over*. Price touches the 50 EMA and bounces, firing the alert;
two days later the 50 EMA breaks and the whole multi-day structure unwinds.
Because this pair is slow, the trend filter will keep reading "uptrend" for
many candles after the top is in — the 200 EMA takes a long time to roll
over. So the same "no concept of which pullback this is" blind spot as the
other two costs *more* here, not less: the trends are bigger, so the
reversals are too.

### Scenario C — correctly does *not* fire: a strong but young trend

A token rips +40% over eight hours — `momentum_surge` and `ema9_pullback`
both fire, and `ema_trend_pullback` fires a few hours in. This analysis
stays silent for days: 32 candles of history don't move a 200 EMA enough to
clear 0.03 ATR/candle of slope, and the 50 EMA hasn't separated from it yet.
**No alert** — correctly, since "pullback in an established trend" isn't
what's happening; that's an impulse move, and the faster pairs are the ones
built to catch it.

### Scenario D — correctly does *not* fire: newly listed pair

A token listed on Kraken three days ago is trending up cleanly. On 15m
candles, three days is only ~288 candles — short of the 600 this analysis
needs before it will evaluate anything. **No alert**, on any pair younger
than about six and a half days, no matter how good the setup looks. The
other four analyses still cover it.

---

## The honest caveat

All five analyses encode reasonable, standard technical-trading logic, but:

- **None of it executes trades.** There's no position sizing, stop-loss, or
  exit logic — these are alerts, not an automated strategy.
- **All five do better trending, worse chopping.** Crypto spends a lot of
  time chopping.
- **Fees and slippage aren't modeled.** A signal that "works" on the chart
  can still lose money after Kraken's taker fee and spread on a thin pair.
- **This hasn't been backtested against historical data.** The thresholds
  above are reasonable starting points, not numbers tuned against actual
  outcomes on the pairs you trade.

Before sizing real money behind any of these alerts, backtest it on your own
watchlist, and consider combining signals (e.g. only act on an
`ema9_pullback` alert that follows a recent `momentum_surge` hit in the same
pair, or an `ema_trend_pullback` alert that also has a recent `breakout`)
rather than trading any one analysis in isolation.

The three pullback analyses are worth reading as a set rather than
individually — they ask the same question of three different timescales, so
which ones fire together tells you something none of them says alone. All
three on one pair means a dip that's a pullback by every horizon; only
`ema9_pullback` means a young move the slower pairs haven't confirmed and
may never.
