# Crypto Alert Strategies

`technical_analysis/crypto/alerts.py` runs six independent analyses against
one shared Kraken 15-minute candle fetch per pair (see the `ANALYSES`
registry). Each scan sends **one combined email** with a section per analysis
that had hits. All six only alert on upside signals — no DOWN/bearish
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
on/off switch per analysis. **Only the two momentum analyses are enabled**;
the other four are opt-in:

```python
ENABLED_ANALYSES = {
    "breakout": False,
    "ema_trend_pullback": False,
    "momentum_surge": True,
    "trend_momentum_surge": True,
    "ema9_pullback": False,
    "ema50_pullback": False,
}
```

The two enabled ones ask different questions about the same 3-candle window.
`momentum_surge` asks **how big** the move was (≥ 5%, ending in a 21/50 EMA
uptrend); `trend_momentum_surge` asks **what shape** it was (three green
candles, each closing higher than the last, over a lower ≥ 3% bar). Both
require an uptrend on the signal candle, but of different EMA pairs, and
neither is a subset of the other. Measured over 12,060 candle evaluations — 60
pairs × ~2 days of 15m candles, 2026-08-14, before cooldown — they fired **36**
and **30** times and agreed on **19** of those candles. When a pair does appear
in both sections, that is the strongest version of the signal: a large move
that was also structurally clean.

Absolute counts swing widely with the market window — samples taken hours
apart put the same two analyses at 71/65 and 36/30. Every figure quoted below
comes from one 2026-08-14 dataset, so the ratios between them are comparable
even though the totals are not a forecast of any given day.

Note what turning `ema9_pullback` off gives up: it was the one analysis fast
enough to catch the *first* dip after a `momentum_surge` impulse (see §5
below), so the surge alerts now stand alone rather than being followed by an
entry-shaped pullback signal on the same pair.

A disabled analysis is not registered at all — it never evaluates a candle
and never contributes an email section. Its candle requirement is also
excluded from the shared per-pair fetch, so the two momentum analyses fetch
**201** candles per pair rather than the 601 that `ema50_pullback` demands.
That figure is set entirely by `momentum_surge`: its 50 EMA needs a 200-candle
warmup before the uptrend filter can be trusted. (It was 98 while that filter
was removed, between `5d7ae06` and 2026-08-14.) `trend_momentum_surge` needs
only **97** — its 21 EMA plus warmup comes to 84, and the 96-candle window
behind the 24h volume badge is what actually binds — so turning `momentum_surge`
off takes the fetch back down to 98. Cooldown state for a disabled analysis is
left untouched in the state file, so re-enabling it resumes where it left off
rather than re-alerting on pairs it had already covered.

The rest of this document describes all six analyses regardless of whether
they are currently enabled.

### Two liquidity filters apply to four of the six analyses

> **Both momentum analyses are exempt from both filters.** Neither filter
> below can suppress a `momentum_surge` or `trend_momentum_surge` alert — they
> report volume instead of gating on it, via a LIQUID/THIN badge (see §3).
> Everything in this section applies to `breakout`, `ema_trend_pullback`,
> `ema9_pullback` and `ema50_pullback`.

- **Per-candle liquidity filter** (`REQUIRE_LIQUIDITY_FILTER`, currently
  **on**): median quote volume over the trailing 96 candles ≥ **$1,000**,
  median trade count ≥ 5, and the signal candle itself ≥ 5 trades. Tables
  below list this as "Liquidity floor."
- **Absolute 24h volume floor** (`MIN_24H_QUOTE_VOLUME`): trailing 24h quote
  volume ≥ **$50,000**. This is a coarser "is this pair even worth alerting
  on" check — a sum, not a median, so it can't be skewed low by one quiet
  candle — and its value is printed on every alert (`24h volume $X`) so you
  can sanity-check liquidity without cross-referencing anything. For
  the two momentum analyses this same number is still computed and printed,
  but only to colour the badge.
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

**What it checks:** three things. Did price move ≥ 5.0% over 3 candles, did
an average of ≥ $5,000 per candle actually trade while it did, and is the pair
in a 21/50 EMA uptrend as the move finishes? No candle-quality or ATR filter
remains.

| Filter | Threshold |
|---|---|
| Price move over last 3 candles | ≥ 5.0% (open of the 3-candle window to close of the last) |
| Average quote volume over those 3 candles | ≥ **$5,000** per candle (`MOMENTUM_MIN_AVG_SIGNAL_VOLUME`) |
| Uptrend (21/50 EMA) | signal candle closes above **both** EMAs, and 21 EMA > 50 EMA |
| Relative volume | **not checked** — reported only |
| Liquidity floor | **not checked** — reported only |
| 24h volume floor | **not checked** — drives the LIQUID/THIN badge |
| Cooldown | 4 candles per pair |

#### "UP" means "in an uptrend" again

This filter — close > 21 EMA, close > 50 EMA, 21 EMA > 50 EMA — was removed in
`5d7ae06` and **restored on 2026-08-14**. A qualifying 3-candle rise no longer
fires regardless of the larger trend: the dead-cat bounce partway down a
sustained decline is excluded again, which is the case the filter exists for.

It is measured on the **signal candle only**. That is the difference from §4,
which checks its faster 9/21 pair on every candle of the window: here the
question is just "is the pair in a 21/50 uptrend at the moment this move
finished".

Restoring it costs fetch size. The 50 EMA's warmup makes 200 closed candles
the binding requirement again, so each pair requests **201** rows instead of
98 — the same size this analysis used before `5d7ae06`, and still well inside
Kraken's 720-row cap. §4 is unaffected (it needs 97), so turning this analysis
off takes the fetch straight back down.

`MOMENTUM_PRICE_CHANGE_PCT`, `MOMENTUM_MIN_AVG_SIGNAL_VOLUME` and the uptrend
filter are what change how many alerts arrive. Every other threshold this
analysis touches affects only what the email *says*.

#### Two different volume questions

Volume appears twice here, measuring different things over different windows,
and only one of them can block an alert.

| | Window | Asks | Can block? |
|---|---|---|---|
| **Move-volume floor** (`MOMENTUM_MIN_AVG_SIGNAL_VOLUME`, $5,000) | the 3 signal candles | did real money move *in this move*? | **yes** |
| **LIQUID/THIN badge** (`MIN_24H_QUOTE_VOLUME`, $50,000) | trailing 24h | is this pair liquid *in general*? | no |

They disagree on purpose, in both directions:

- A dormant pair that wakes up — $8,000 traded across the move, but under
  $50,000 over the day — **fires, badged THIN**. That's a real signal on a
  pair you should size carefully.
- A normally busy pair whose 5% move happened on $500 of trading is
  **filtered out**, despite being liquid on any daily measure. Nothing
  happened in that move worth alerting on.

The badge itself:

- <span title="green">**LIQUID**</span> (green) — 24h volume ≥ $50,000.
- <span title="red">**THIN**</span> (red) — below it. The move cleared the
  floor, so money did trade, but the pair is quiet enough that the size you
  can actually get in and out with is limited.

The relative-volume multiple is still printed (`volume 3.18x 20-candle
average`) as an unfiltered reading — useful for spotting a move that happened
on fading participation, which nothing here blocks.

**The $5,000 floor is what keeps genuinely dead pairs out; the badge is what
you triage the survivors with; the uptrend filter is what keeps the section to
moves worth reading in the first place.**

### Scenario A — the intended catch: a move already underway

ADA/USD grinds from $0.4200 to $0.4430 over 3 candles (+5.5%), closing above
both its 21 EMA ($0.4300) and 50 EMA ($0.4100) with the 21 above the 50, and
the 3-candle average quote volume is running above the 20-candle average — the
move isn't happening on fading interest. **Alert fires: UP, badged LIQUID.**
Useful as a "this is already moving, decide if you want in" signal, not an
early entry.

Two of the three things that make this the good case now qualify it: the size
of the move and the uptrend around it. The healthy volume is still only
reported — the $5,000 floor is absolute, so it does not care that this move
ran above its own baseline.

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

This is the cost of dropping the *relative* volume test. That condition was
previously "the one thing keeping this analysis from firing on random 5%+
chop," and it is gone — the $5,000 floor replaced it with an absolute bar,
which a busy pair clears easily even on a fading move. The `volume 0.8x
20-candle average` reading in the alert body is what now tells you this — a
multiple below ~1.0x means the move happened on thinning participation, and
it is worth reading before acting even when the badge is green.

### Scenario D — now fires: a thin pair below the 24h floor

ESP/USD's 2026-07-26 10:00 spike (+25% on the candle, $110k of volume and 438
trades on that candle alone) sat on a pair where **54 of the 96 trailing
candles had zero volume and zero trades** — trailing median quote volume of
exactly **$0**, far under the $1,000 per-candle minimum. Under the old rules
that failed the liquidity filter outright and produced **no alert on any of
the six analyses**.

Now `momentum_surge` **does alert** on it. The $110k that traded on the spike
candle puts the 3-candle average far above the $5,000 move-volume floor, and
its trailing 24h volume of $128,265 clears $50,000, so it is even badged
**LIQUID** despite half its recent candles being dead — a case where the
badge and the per-candle liquidity picture disagree, and the badge is the
less informative of the two.
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

### Scenario E — correctly does *not* fire: a bounce inside a downtrend

A token has bled −45% over two days. Price is well below both the 21 and 50
EMA, with the 21 EMA below the 50 EMA — a textbook downtrend. It then bounces
+5.4% over 3 candles on decent turnover, as falling assets regularly do.

**No alert.** The price and volume tests both pass; the uptrend filter is what
rejects it. Between `5d7ae06` and 2026-08-14 this fired, and the email
described a genuine +5.4% move on a liquid pair — every word true while the
pair was still in free-fall. §4 rejects the same candles independently, on its
own 9/21 test.

### Scenario F — correctly does *not* fire: a 5% move nobody traded

A microcap prints +6.8% over 3 candles, but only ~$400 of quote volume per
candle changed hands doing it — a handful of small orders walking a thin book
upward. The 3-candle average is under the **$5,000** floor. **No alert.**

This is the case the floor exists for, and it is the main thing standing
between you and a very noisy inbox: on a thin enough pair, a 5% move requires
almost no money at all. Note that the pair's *daily* volume is irrelevant
here — a pair with $2M of 24h volume is filtered out just the same if this
particular move traded $400 a candle.

---

## 4. Trend Momentum Surge (`trend_momentum_surge`)

**What it checks:** mostly the *shape* of a 3-candle move rather than its
size. Over the same window §3 measures, **every** candle must have closed
above its open and above the previous candle's close; then, on the **signal
candle** only, the 9 EMA must be above the 21 EMA with the close above the 21
EMA. It keeps §3's $5,000 volume floor and lowers its price bar from 5% to
**3%**, rather than dropping it.

Both momentum analyses now reject the §3 Scenario E dead-cat bounce — a pair
bleeding downhill fails the 9/21 test here for the same reason it fails the
21/50 test there. What separates them is everything else: this one has no
minimum move size, demands a candle-by-candle staircase, and reads a faster
EMA pair that turns days sooner than the 50 EMA does.

| Filter | Threshold |
|---|---|
| Price move over last 3 candles | **≥ 3.0%** (`MOMENTUM_TREND_PRICE_CHANGE_PCT`) — lower than §3's 5% |
| Average quote volume over those 3 candles | ≥ **$5,000** per candle (`MOMENTUM_MIN_AVG_SIGNAL_VOLUME`) |
| Consecutive up candles | all 3 must close above their open |
| Rising closes | each of the 3 must close above the previous candle's close |
| 9 EMA vs 21 EMA | 9 EMA above the 21 EMA **on the signal candle** |
| Price vs 21 EMA | close above the 21 EMA **on the signal candle** |
| Relative volume | **not checked** |
| Liquidity floor | **not checked** — reported only |
| 24h volume floor | **not checked** — drives the LIQUID/THIN badge |
| Cooldown | 4 candles per pair |

#### Green candles and rising closes are two different tests

Neither implies the other:

- A candle that **gaps up and fades** closes above the previous close while
  printing red — rising closes passes, green fails.
- A candle that **opens below the previous close and recovers only part of the
  way** is green while the sequence of closes steps *down* — green passes,
  rising closes fails.

Requiring both makes the window a staircase: each bar bullish in itself *and*
higher than the one before it. The "above the previous close" test compares
the window's first candle against the bar immediately **before** the window,
so "every candle" really is every candle rather than just the two comparisons
available inside it.

On the 12,060-evaluation sample above, the rising-closes rule cut this
analysis from **34 hits to 30** — the green runs whose closes did not actually
step up.

#### Not a subset of `momentum_surge`

`MOMENTUM_TREND_PRICE_CHANGE_PCT` (**3%**) is deliberately separate from
`MOMENTUM_PRICE_CHANGE_PCT` (**5%**), and lower, because the structural
conditions above carry more of the evidence here. The two analyses diverge in
both directions: in that same sample **8 of this analysis's 30 hits sat under
5%**, invisible to `momentum_surge`, while **17 of `momentum_surge`'s 36** were
not staircases and did not qualify here. The median hit here was +7.20%.

This threshold started at **0** — structure only, any size. At that setting it
could not reject anything at all (green candles with rising closes force the
last close above the first open, so the move is always positive) and the
section was dominated by sub-1% runs: the 3% floor removes **20 of the 50**
hits a 0% floor would have sent. Raise it toward 5% to converge on
`momentum_surge`'s alert set; lower it to trade more alerts for smaller
moves.

#### Per-candle for shape, signal candle for trend

The division of labour is deliberate. The staircase tests describe the *run*,
so they have to look at each bar. The EMA pair only places that run in a
trend, which is a question about where the pair stands when the run finishes —
the same reading §3 uses for its own 21/50 pair.

Both EMA comparisons originally ran across the whole window, which additionally
required the 9/21 cross to **predate** the run. That was the stricter claim,
and its cost was excluding the freshest crosses — often the better entries,
since a small first move off a new cross cleared neither this analysis's window
test nor §3's 5% bar. Relaxed on 2026-08-14; it added 6 hits in the sample
below (24 → 30). To restore it, zip the last 3 values of each EMA series
against the window in `evaluate_trend_momentum_surge_candles`.

There is no slope, separation or ATR test here, unlike the three pullback
analyses. Those need the EMAs meaningfully apart before a "touch" means
anything; here the evidence is the staircase itself, and the EMAs answer one
question only — with the trend, or against it?

The 9/21 periods are `MOMENTUM_TREND_FAST_PERIOD` and
`MOMENTUM_TREND_SLOW_PERIOD`, deliberately separate constants from
`ema9_pullback`'s `EMA9_*` pair even though both default to 9/21: an impulse
and a pullback ask different things of the same EMAs and should be tunable
apart.

### Scenario A — the intended win: an impulse with the trend behind it

SOL/USD has been grinding up for two hours — 9 EMA sitting above the 21 EMA
the whole time, price holding above both. Three candles then print green in a
row, $142.00 → $149.40 (**+5.2%**), on $48,000/candle of quote volume.

**Alert fires: UP trend momentum, badged LIQUID.** Every candle of the move
was green and closed above the one before it, all three above the 21 EMA, with
the 9/21 stack already in place before the first of them. At +5.2% it also
clears §3's bar, so it fires in the `momentum_surge` section too — a pair
appearing in both is the version of this signal worth looking at first.

### Scenario B — the other intended catch: a small run §3 never sees

LINK/USD is in the same shape — 9 EMA over the 21, price above both — and
prints three green candles, each closing above the last, for a total of
**+3.4%** on $30,000/candle.

**Alert fires here, and nowhere else** — it clears this analysis's 3% bar but
not §3's 5%. Sub-5% moves were 8 of the 30 hits in the sample, and it is the
practical point of the lower bar: you see clean trend continuation a leg
earlier than §3 would show it. Below 3% the same shape no longer qualifies, on
the view that a staircase that small is not worth an alert however tidy it
looks.

### Scenario C — the failure mode: a confirmed trend is still a late entry

The same SOL/USD move continues, and 90 minutes later another 3-candle +5%
leg prints — still green, still stacked, still above the 21 EMA. **Alert
fires again** (cooldown permitting). But this is now the third leg of an
extended run, and the trend filter has no concept of *where in a trend's life*
the move sits: a 9/21 stack looks identical on the first leg and the last one.
Adding the trend condition removes the counter-trend bounces; it does nothing
about buying an exhausted uptrend, and by construction the alert still only
arrives after the move has already happened.

### Scenario D — correctly does *not* fire: the dead-cat bounce

The §3 Scenario E token — down 45% over two days, price under both EMAs, 9 EMA
below the 21 EMA — bounces +5.4% over 3 candles on real turnover. **No alert**:
the EMAs are stacked the wrong way and price is below the 21 EMA on all three
candles. §3 rejects it too, on its own 21/50 filter, so this is now a case both
momentum analyses decline — but the 9/21 pair here declines it *sooner*, since
a 50 EMA takes far longer to roll over than a 21 EMA does.

### Scenario E — now fires: the move made its own trend

A pair drifts sideways-to-down for hours, then rips +15% in 3 green, rising
candles. That is violent enough to pull the 9 EMA above the 21 EMA — but only
by the *last* candle of the window; the first two were still stacked bearishly.

**Alert fires**, since the EMA pair is read on the signal candle. Before the
2026-08-14 relaxation it did not, on the argument that the move manufactured
its own trend rather than joining one. Both readings are defensible; this one
is the more permissive, and it is what makes the freshest 9/21 crosses
reachable. §3 will often take this candle too, so it is a case to check on the
chart rather than trust from the alert alone.

### Scenario F — correctly does *not* fire: a red candle mid-move

A pair prints +7% over 3 candles inside a clean 9/21 uptrend, but the middle
candle is red — it opened high, sold off, and the third candle made the gain
back and more. The +7% total, the EMA stack and the volume all pass. **No
alert**, because the "3 consecutive up candles" condition fails. This is
stricter than `momentum_surge`, which only measures the window's first open
against its last close and never looks at what happened between them.

### Scenario G — correctly does *not* fire: green candles, sagging closes

A pair inside the same clean uptrend prints three green candles, but the
second one opens well below the first one's close and recovers only part of
the way: green in itself, yet it closes *lower* than the candle before it. The
green-candle test passes and the EMA tests pass. **No alert**, because the
closes do not step up — this is chop inside a trend, not a run. It is the case
the rising-closes rule was added for, and it accounted for 4 of the 34 hits
this section would otherwise have sent in the sample.

---

## 5. 9 EMA Pullback (`ema9_pullback`)

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

## 6. 50 EMA Pullback (`ema50_pullback`)

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

All six analyses encode reasonable, standard technical-trading logic, but:

- **None of it executes trades.** There's no position sizing, stop-loss, or
  exit logic — these are alerts, not an automated strategy.
- **All six do better trending, worse chopping.** Crypto spends a lot of
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
