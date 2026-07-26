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

### Two liquidity filters apply to all five analyses

- **Per-candle liquidity filter** (`REQUIRE_LIQUIDITY_FILTER`, currently
  **on**): median quote volume over the trailing 96 candles ≥ **$1,000**,
  median trade count ≥ 5, and the signal candle itself ≥ 5 trades. Tables
  below list this as "Liquidity floor."
- **Absolute 24h volume floor** (`MIN_24H_QUOTE_VOLUME`, always enforced
  regardless of the flag above): trailing 24h quote volume ≥ **$500,000**.
  This is a coarser "is this pair even worth alerting on" check — a sum, not
  a median, so it can't be skewed low by one quiet candle — and its value is
  printed on every alert (`24h volume $X`) so you can sanity-check liquidity
  without cross-referencing anything.

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
| Volume vs 96-candle median | ≥ 2.0× **and** robust z-score ≥ 3.0 |
| Liquidity floor | median quote volume ≥ $1,000, ≥5 trades/candle |
| 24h volume floor | ≥ $500,000 |
| Cooldown | 4 candles per pair |

### Scenario A — the intended win: volume-backed breakout continues

BTC/USD has chopped between $60,000–$60,500 for 5 hours. ATR ≈ $150.

A candle prints: open $60,480 → close $60,690, high $60,700, low $60,470.

- Body = $210 = **1.4× ATR** (clears the 0.80 minimum)
- Close is 96% of the way to the candle's high (clears the 75% minimum)
- Clears the range ($60,500) by $190, well past the $22.50 ATR buffer
- Volume on the candle is 3.2× the 24h median, robust z-score 4.1

**Alert fires: UP breakout.** Real size pushed price out of the range and
held it near the high — stop-losses from range-bound shorts trigger,
trend-followers who watch this exact pattern start buying, price grinds up
toward $61,000+ over the next few hours. This is the case the strategy is
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

---

## 2. Volume Surge (`volume_surge`)

**What it checks:** no candle-quality or ATR filters at all — just
"something unusual just happened." Simpler and looser than breakout by
design.

| Filter | Threshold |
|---|---|
| Volume vs 96-candle median | ≥ 3.0× |
| Absolute price move on the candle | ≥ 1.5% |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |
| Cooldown | 4 candles per pair |

### Scenario A — the intended catch: news/whale activity

ETH/USD normally does ~$40M quote volume per 15-minute candle. A candle does
$135M (3.4× median) with price +2.1% — maybe ETF news, maybe a large
liquidation cascade of shorts. **Alert fires: UP.** You find out within 15
minutes of the move starting, which is faster than most retail traders will
notice.

### Scenario B — the failure mode: it can't tell continuation from exhaustion

XRP/USD has already rallied 15% over 6 hours on a real narrative. One candle
spikes to 4× median volume with a further +2.3% move. **Alert fires: UP.**
This could be the mid-trend continuation everyone's chasing — or it could be
the parabolic blow-off top right before it reverses. The rule has no way to
distinguish "more people are joining a healthy trend" from "this is the
exhaustion spike." That's why this analysis is a "go look at this now"
signal, not something to auto-trade blindly.

### Scenario C — correctly does *not* fire: a down move

DOGE/USD drops sharply on negative news: volume 5× median, price -3.4% on
the candle. **No alert.** The analysis only checks `price_change_pct >=
1.5` (not `abs(price_change_pct)`), so a violent selloff with volume behind
it is filtered out the same way any other DOWN move is — only pumps are
surfaced.

---

## 3. EMA Trend Pullback (`ema_trend_pullback`)

**What it checks:** "buy the dip in an established uptrend" — enter on a
pullback to the fast EMA within a confirmed uptrend, rather than chasing an
extended breakout.

| Filter | Threshold |
|---|---|
| Trend defined by 50 EMA slope | ≥ 0.05 × ATR per candle (over a 10-candle lookback) |
| EMA separation (not tangled/flat) | ≥ 0.25 × ATR |
| Pullback touch distance to 20 EMA | ≤ 0.35 × ATR |
| Reclaim | signal candle must close back beyond the 20 EMA, same direction as the trend |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |

### Scenario A — the intended win: low-risk trend continuation entry

SOL/USD has trended up for 3+ hours — 50 EMA rising at 0.4 ATR/candle (well
above the 0.05 minimum), 20 EMA sitting 2.1 ATR above the 50 EMA (clearly
separated, not flat). Price pulls back for 2–3 red candles to $142.30,
touching within 0.15 ATR of the 20 EMA ($142.10), then closes back up at
$143.80 — above the 20 EMA, bullish body. **Alert fires: UP.** Your natural
stop sits just below the pullback low; if the trend continues, this entry
has a much better risk/reward than chasing the original breakout candle
would have.

### Scenario B — the failure mode: "one pullback too many"

The same SOL/USD uptrend continues for another 8 hours through three more
pullback-and-bounce cycles — each one fires its own alert (once cooldown
elapses). The fourth pullback looks identical to the first three: touches
the 20 EMA, closes back above it. **Alert fires: UP.** But this time the
trend is actually exhausted — that "bounce" is the last gasp before the 20
EMA gets broken and the uptrend ends. The rule has no concept of "this is
the Nth pullback in this trend" — every pullback that meets the geometry
looks the same to it, whether it's early or late in the trend's life.

### Scenario C — correctly does *not* fire: market is flat, not trending

A pair chops in a $2 range for hours. The 50 EMA slope is only 0.02 ATR/candle
(below the 0.05 minimum) and the 20/50 EMA separation is 0.10 ATR (below the
0.25 minimum) — the EMAs are tangled together, not trending. Price touches
the 20 EMA repeatedly as it oscillates. **No alert on any of these
touches** — exactly the scenario this filter exists to reject, since a
"pullback" only means something in a market that's actually trending.

---

## 4. Momentum Surge (`momentum_surge`)

**What it checks:** the coarsest, fastest-firing analysis of the four — no
candle-quality or ATR filters at all. Has price already moved a meaningful
amount over the last few candles, with volume over that window running above
a longer baseline?

| Filter | Threshold |
|---|---|
| Price move over last 5 candles | ≥ 5.0% (open of the 5-candle window to close of the last) |
| Average volume over those 5 candles | > average volume over the last 20 candles |
| Liquidity floor | same as breakout |
| 24h volume floor | same as breakout |
| Cooldown | 4 candles per pair |

Note the 20-candle baseline *includes* the 5 signal candles — it isn't a
separate "prior" window. That makes the volume check closer to "volume
hasn't dropped off during the move" than "volume is unusually high," which
is a much weaker bar than the 2–3x multiples the other analyses require.

### Scenario A — the intended catch: a move already underway, confirmed by volume

ADA/USD grinds from $0.4200 to $0.4430 over 5 candles (+5.5%), and the
5-candle average quote volume is running above the 20-candle average — the
move isn't happening on fading interest. **Alert fires: UP.** Useful as a
"this is already moving, decide if you want in" signal, not an early entry.

### Scenario B — the failure mode: alerts near the top of an already-extended move

The same ADA/USD move continues for another 10 candles before reversing.
Because the trigger requires the move to have *already happened* over a
5-candle window, by construction the alert can only fire after most of the
gain is behind it — there's no mechanism here (unlike `ema_trend_pullback`)
that looks for a lower-risk entry point.

### Scenario C — correctly does *not* fire: choppy volume during the move

A pair moves +6% over 5 candles, but volume on those candles is actually
*lower* on average than the last 20 candles (the move happened on thinning
participation). **No alert** — the volume condition is the one thing keeping
this analysis from firing on random 5%+ chop.

### Scenario D — correctly does *not* fire (today): real move, too thin to matter

This one is real data, not a hypothetical. GWEI/USD on 2026-07-24 14:45 UTC:
5-candle move **+5.21%**, 5-candle average volume running **1.53×** the
20-candle baseline, price above both EMAs with the 20 EMA above the 50 EMA —
every momentum/EMA condition passes. But this pair's trailing 24h quote
volume at that moment was only **$192,165** — comfortably above the old
$100,000 floor, but now well under the current **$500,000** floor. **No
alert.** The move was genuine, but a pair doing under $200k/day in volume
isn't liquid enough to act on at any real size — this is exactly the
"real signal, wrong pair" case the 24h floor exists to catch.

---

## 5. 9 EMA Pullback (`ema9_pullback`)

**What it checks:** the same "buy the dip in an uptrend" idea as
`ema_trend_pullback`, but keyed off the faster 9/21 EMA pair instead of
20/50. A 50 EMA trend takes hours to establish, so `ema_trend_pullback` can
miss the *first* pullback after a fresh impulse move — e.g. a
`momentum_surge` hit, which by definition is only 5 candles (75 minutes on
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

## The honest caveat

All five analyses encode reasonable, standard technical-trading logic, but:

- **None of it executes trades.** There's no position sizing, stop-loss, or
  exit logic — these are alerts, not an automated strategy.
- **All four do better trending, worse chopping.** Crypto spends a lot of
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
