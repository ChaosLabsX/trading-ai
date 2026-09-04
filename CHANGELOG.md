# Changelog

Every meaningful change to the app, newest first. Kept so a future developer (human or AI)
can trace what was done and why without digging through git history.

## 2026-09-03 (later) — The learning pass stops Telegramming

Owner's call: the routine report is noise. The honest answer from that pass is
almost always "no parameter changes" — its first real run said exactly that, at
length — and a message that never needs acting on trains you to ignore the channel
that also carries stop-loss and trade alerts.

- **`LEARN_TELEGRAM` added to `learn.py`, default `'off'`.** Three settings:
  `'off'` (never push), `'proposals'` (push only when it wants a parameter change
  approved, or when the analysis fails), `'always'` (the original behaviour).
- **Nothing is lost at `'off'`.** The pass still fires on its 25-newly-graded-trades
  trigger, still computes cohorts, still writes the distilled block and the full
  result to `learned_rules`, and still prints to the worker log. It stops pushing,
  not working.
- **The trade-off is written down next to the constant**, because it is not
  obvious: surfacing a proposal is the entire reason this pass exists, and proposals
  are never auto-applied — so at `'off'` a genuine recommendation reaches nobody
  unless someone reads the log or queries the table. `'proposals'` buys the silence
  without that blind spot, and is one word away.
- The failure notice ("analysis returned nothing") is gated too, but always prints
  to the log — this repo's own comment on that branch notes silent failures are the
  expensive kind, and that reasoning survives; only the transport changed.
- Verified against the real `_report()` across all three modes × proposals/none:
  `off` sends nothing either way, `proposals` sends only when there is one,
  `always` sends both.

**Context — the run that prompted this.** First real learning pass, triggered when
the DOGE trade closed as the 25th graded trade (`tp_trail`, +$2.02, graded
`well_timed`). It read 25 trades (15W/10L, PF 3.16) and correctly proposed nothing:
of ten losses only one was a demonstrable shakeout, and 11 of 15 winners exited at
or near the right moment, so neither stop nor trail width could be blamed. It also
refused to reason from any cohort below `LEARN_MIN_COHORT = 25`. The guards worked;
the message was simply not actionable.

## 2026-09-03 — Position sizes 15/22/30 → 45/50/55/60, and a volume gate to pay for it

Owner's decision to put substantially more capital behind each trade. Applied in
full. The sizing numbers are his; everything else here is what the change dragged
in behind it.

**What the ladder now is.** Caps on *available* USDT at decision time:

| recent profit factor | old cap | new cap |
|---|---|---|
| ≥ 2.0 | — | **60%** |
| 1.5–2.0 | 30% | **55%** |
| 1.0–1.5 | 22% | **50%** |
| < 1.0 | 15% | **45%** |
| unknown (< 30 trades) | win-rate fallback | 45% / 50% by the same fallback |

- **The SHAPE is the safety property, not the numbers.** Worse record → smaller
  bet, and "no record yet" still sits at the BOTTOM of the ladder. That is the
  2026-08-02 fix and it survives intact — a future edit that flattens these four
  values into one constant silently restores a 30%-cap-on-PF-0.32 situation.
- **The four values are now module constants** (`CAP_PF_POOR/FAIR/GOOD/STRONG`) and
  the advisor prompt interpolates them. It previously restated "30% / 22% / 15%"
  in prose next to a code path with different numbers — the exact defect that had
  Claude reasoning about an `slPct` bound the code did not have (2026-07-29). The
  score-based bands moved with it: 4.5–4.9 → 45–50%, 5.0–5.9 → 50–55%, 6.0+ → 55–60%.
- **Concurrency now approaches full deployment.** Because the cap is on *available*
  balance, three open trades compound down: 60% → 24% → 9.6% ≈ **94% of capital
  deployed**, against ≈ 66% under the old ladder. `MAX_OPEN_TRADES = 3` is what
  stands between the account and being all-in, and it matters more than it did.
  The daily circuit breaker's worst case moves with it — three stop-losses at a
  ~3% stop is ≈ **−4.4% of the account** now, against ≈ −1.9% before.

**New: `MIN_VOL_RATIO_TRADE = 2.0`.** A STRONG BUY is not traded unless 1H volume
is at least 2× its 20-bar average. Measured over the four independent 84-day
windows at score ≥ 4.5, both gates on:

| requirement | Dec-05 | Feb-27 | May-22 | Aug-14 | total | trades |
|---|---|---|---|---|---|---|
| none | −14.92 | +6.85 | +18.87 | −20.95 | **−10.15** | 88 |
| vol ≥ 1.75× | −2.81 | +0.11 | +13.71 | −8.99 | +2.02 | 58 |
| **vol ≥ 2.00×** | −2.81 | +0.27 | +13.71 | −7.52 | **+3.65** | 56 |
| vol ≥ 2.50× | −1.18 | +0.01 | +16.70 | −13.85 | +1.68 | 40 |

It trims **both** tails — smaller losses in the two down windows, smaller gains in
the two up windows — and nets positive because it removes more bad than good. Over
the most recent 90 days it takes the replay from 31 trades / −15.65 / PF 0.70 to
**15 trades / −2.21 / PF 0.92**, and roughly halves max drawdown (31.37 → 18.48).
Adopted for two reasons beyond the number: the sign does not flip by window (unlike
the tighter stop rejected earlier today), and the whole 1.75–2.50 neighbourhood is
positive rather than one lucky point. The mechanism is also principled rather than
mined — the engine already demands volume ≥ 1× average on the 30m reversal candle,
and the scoring table already pays +1 for ≥ 2×.

- **It is a TRADE gate, not a labelling rule**, sitting beside `reversal_confirmed`
  in `run_scan`. Folding it into `generate_signal()` would mean changing app.js too
  and re-proving parity; as a trade gate the coin still reads STRONG BUY on the
  dashboard and the worker simply declines it, which is how the reversal gate
  already behaves. `backtest.py` mirrors it (`--min-vol`, defaulting to the live
  constant) so the replay does not quietly measure a looser strategy than the one
  running.
- **Net effect on frequency, stated plainly:** the 4.5 threshold alone took the
  90-day replay from 11 trades to 31; the volume gate gives most of that back, to
  15. Still ~36% more than before today, not the 2.3× the threshold change alone
  implied. `MIN_VOL_RATIO_TRADE = 0.0` reverts exactly that trade-off in one line.

**Rejected on the same evidence.** `1H RSI ≤ 25` scored better in total (+3.91,
3 of 4 windows up) but is not believable: `RSI ≤ 28` is −18.08 and 1 of 4. A real
effect does not invert between 25 and 28, and the combination `vol ≥ 2 AND
RSI ≤ 28` (−18.24) is worse than either part alone. Requiring a MACD bullish cross
leaves 2 trades in 336 days, and "MACD not bearish" leaves 6 — both unusable, and
exactly the "too rare" outcome the owner asked to avoid. Requiring the lower
Bollinger band (−21.67) or 4H RSI ≤ 35 (−19.54) makes things actively worse, which
is notable given those are the conditions the current scoring rewards most.

**The standing caveat, with current numbers.** Sizing multiplies an edge; it does
not create one. The live record as of this change is **24 closed trades, 14W/10L,
58% win rate, net +$7.89, PF 2.72** — and, unlike a month ago, **+$5.17 with the
single largest winner removed**, so it no longer rests on one trade. Average win
+2.57% against average loss −3.02% (R:R 0.85, break-even 54%) — the payoff geometry
is still upside-down, the win rate is simply carrying it at the moment.

Two reasons to keep the caveat anyway: 24 trades is below the 30 the profit-factor
guard itself requires before it trusts a PF, and every backtested configuration over
the four independent windows is still net negative. Position size was roughly
tripled at the owner's explicit direction on a record that is encouraging but not
yet established.

**Data cutoff.** The candle cache backing every window in this entry ends
**2026-08-14**, so the measurements exclude the ~3 weeks before this change — a
stretch in which the bot went 5W/1L live. The backtested figures are therefore
neither confirmed nor contradicted by the most recent live results.

## 2026-08-15 — The STRONG BUY bar goes 5.0 → 4.5, and nothing else moves

The bot was placing too few trades to learn from. A full review of the entry gate
found the frequency problem is real, found that **none of the obvious fixes
survive testing**, and found exactly one that does.

**Method matters here, because it changed the answer.** The first pass compared
30-, 60- and 90-day windows — all ending today, each containing the last. That is
one window counted three times, and it agreed with itself. Re-run over **four
non-overlapping 84-day windows** of the past year (candles pulled for all 38 coins
back to 2025-08-14), several conclusions reversed.

- **`STRONG_BUY_SCORE` 5.0 → 4.5.** Across the four independent windows with both
  safety gates on: 5.0 → 39 trades / −9.18 net, 4.5 → **88 trades / −10.15 net**,
  2 of 4 windows profitable either way. Per trade that is −$0.24 → −$0.12. It
  roughly doubles the sample rate at a P&L difference well inside noise. It does
  **not** make the bot profitable and is not claimed to.
- **5.0 was really 5.5.** `generate_signal()` awards only whole and half points, so
  the scale is a staircase. Of 81,610 scored coin-hours, **26 landed on 5.0** and
  938 on 5.5+. There was never a 4.7 or 4.8 to try; 4.5 is the next real step and
  admits exactly one new bucket (1,221 bars vs 964), which is why the change is a
  ~2.3× step and cannot be made gentler.
- **The new bucket sizes itself down, for free.** The advisor's sizing table already
  carried a `Score 4.5–4.9 → 15–20% of capital` tier that was unreachable under a
  5.0 bar. It now activates, so the weaker setups this admits are sized ~25% lighter
  than today's. The table's dead `4.0–4.4` row was removed (a rule the model can
  never apply is noise) and the remaining bands interpolate `STRONG_BUY_SCORE`
  rather than restating it.
- **`STRONG_SELL_SCORE` deliberately NOT mirrored to −4.5.** This worker is long-only
  spot; `direction_zone()` maps SELL and STRONG SELL to the same `down` zone, so the
  sell label never places, closes or blocks anything. Moving it would change
  dashboard text and nothing else. The asymmetry is now commented at both sites.
- **`config.js` gains `STRONG_BUY_SCORE`,** and `app.js` reads it instead of a
  hard-coded `5`. A dashboard printing "BUY" for a coin the worker just bought is
  the confusing half of a drifted pair. The **manual** AI Advisor keeps its stricter
  ≥ 5.0 bar on purpose — it has no automatic size reduction — and now says so in the
  prompt instead of claiming 5.0 is where the STRONG BUY zone starts.
- **Found while verifying that: the two `generateSignal()` implementations had never
  actually agreed.** Matching the threshold is necessary but not sufficient if the
  two sides compute different scores to compare against it — and they did. `app.js`
  awarded its volume point from `volRatio ≥ 1.5` where the worker requires `≥ 2.0`,
  gated it on an existing `score >= 2` where the worker does not gate at all, ran a
  `−1` "selling pressure" branch the worker has never had, and applied the whole
  block **after** the 4H term instead of before. That last one is not cosmetic: the
  4H term keys off the sign and size of the running score, so position changes the
  result. Over a 720-case grid the two disagreed on **257 scores and 80 labels
  (11%)**, in both directions — the dashboard could show STRONG BUY for a coin the
  worker scored a full point lower and would never trade. `app.js` was aligned to
  the worker (source of truth: it places the orders) and re-verified over an
  8,400-case grid: **0 mismatches on score, label and reason strings**. The reason
  strings matter too — Python pastes them into the advisor prompt verbatim as
  `Confirmed by: …`, the same coupling that made the 2026-07-29 4H mislabelling a
  trading bug rather than a cosmetic one.
- **`docs/DASHBOARD.md` had documented this as a "minor asymmetry … same intent,
  slightly different code paths".** It was neither minor nor the same intent. The
  note now states what actually differed, and the scoring table lists the components
  in evaluation order with an explicit warning that the order is load-bearing.
- **New: `parity_check.py`.** A duplicated function with no test drifts silently, and
  this one drifted into showing a trade signal the bot did not have — for long enough
  that the divergence got written down as intentional. The script lifts
  `generateSignal` out of `app.js` by source slice (so the shipped file is what runs,
  not a third copy) and compares it against `generate_signal()` over 11,200 cases
  straddling every branch boundary in both files. It was verified to actually fail:
  reintroducing just the 1.5×-vs-2.0× volume difference produces 2,800 score and
  **948 label** mismatches and exit 1. Run it after touching either implementation.
- **`backtest.py --score` now defaults to the live constant.** It said `5.0`, so
  after this change an unflagged run would have quietly measured a threshold the bot
  no longer uses and reported it as the baseline — the same defect class as a prompt
  naming a bound the code doesn't have (2026-07-29).

**Tested and rejected — deliberately not shipped:**

| Change | Result across the 4 independent windows |
|---|---|
| Relax `btc_regime_ok()` | 174 trades, **−77.26** (0 of 4 windows profitable) |
| Relax `reversal_confirmed()` | 159 trades, **−51.00** (0 of 4) |
| Relax both | 538 trades, **−290.12** (0 of 4) |
| `ATR_SL_MULT` 2.5 → 1.0/1.5 | wins **only** in the newest window; wide stop wins 2 of 4 |

The two gates are carrying the strategy — they are not costing opportunity, they
are the reason the loss is small. The tighter stop is the cautionary one: it turned
the last 90 days from −$19.17 (PF 0.72) to +$10.21 (PF 1.20) and improved
monotonically across the *nested* windows, which is exactly what a setting fitted to
one market looks like. Not shipped.

**Where the frequency actually goes** (90-day replay, production rules): 940 STRONG
BUY signals → 815 killed by the BTC regime filter (87%) → 114 killed by the 30m
reversal gate (91% of the remainder) → **11 trades**. The regime filter is bearish
44% of clock time but removes 87% of signals, because this engine buys oversold dips
and dips cluster when BTC is weak. `MAX_OPEN_TRADES` and `MAX_TRADES_PER_SCAN` never
bound once in 90 days at the old bar, so raising them is a no-op.

**The finding that outranks this change:** the payoff geometry is upside-down. Option
3 takes profit on *half* the position and stops *the whole* position, so TP must beat
SL — and in **16 of 18 live trades the stop was wider than the target**. Live record
to date: 9W/9L, average win +2.38%, average loss −3.08%, reward-to-risk 0.77, which
needs a 56% win rate against the 50% the entry delivers. Net is +$1.11, but −$0.96
excluding the single largest position. Also measured, from the `entry_context`
snapshots: the AI spends its ±30% latitude widening **both** exit legs (median TP
+20%, SL +25%, same direction in 14 of 15 trades), never closing the gap. No stop
setting tested fixes this reliably, which points at the entry — the same place the
2026-08-02 review landed from the other direction.

## 2026-08-02 — The sizing guard was off during the only 30 trades it was for

Triggered by reviewing the ADA shakeout of 07-27. The trade itself was graded
correctly; the review found four defects around it, none of them in the trade
logic.

**The record this was found against:** 13 closed trades, 4 wins, net −$2.35,
**PF 0.32**. All 13 graded.

- **`cap_pct` treated "no profit factor yet" as "good profit factor".**
  `_trade_history_context()` returns `pf = None` until 30 closed trades exist,
  and the cap block only shrank the cap when `pf is not None` — so the full 30%
  cap applied for exactly the first 30 trades, the stretch with the least
  evidence the bot works at all. On the live record the cap in force was **30%
  while the real PF was 0.32**. An unknown profit factor is not a good one. Below
  30 trades the cap now falls back to the win rate over whatever history exists
  (< 50% → 15%, otherwise 22%); with genuinely no history the old 30% default
  stands. On the current record this moves the cap **30% → 15%**. Verified by
  exercising the real function against the real 13 rows plus a case table.
- **9 of 13 graded verdicts were computed, stored, then never counted.**
  `_grade_exit()` produces seven verdict classes; `_trade_history_context()` and
  `learn.py._compute_cohorts()` both aggregated only three (`shakeout`,
  `good_save`, `left_money`). The most common verdict in the live record —
  `partial_recovery`, 4 of 13 — reached no aggregate anywhere. Both now count all
  seven, and the prompt carries a full verdict breakdown line.
- **`partial_recovery` is deliberately NOT folded into the shakeout count.** It
  points the same direction, and combining them would take the observed rate from
  1-of-13 to 5-of-13 and clear the Wilson gate. That would be clearing the bar by
  redefining the class, not by evidence: `JOURNAL_PATTERN_NULL_RATE = 0.15` was
  chosen for the narrow shakeout class, and a ≥2% bounce after a stop is common
  noise. It ships as context with an explicit "not tested against a null rate"
  label. Same reasoning as the 2026-07-27 gate; the fix there was to stop acting
  on thin evidence, not to find a wider class that looks thicker.
- **`learn.py` can only tune exits, and the exits are not the binding problem.**
  Replaying all 13 trades on real 15m candles reproduced the recorded exit reason
  **13/13**, then varied only the exits: every configuration still loses (wider
  stop −$3.31 to −$3.70, wider trail −$2.42 to −$2.82, wider TP −$2.08 to −$2.73,
  as-traded −$2.25). Entry diagnostics: median best-case gain before exit 1.77%,
  5 of 13 never gained 1%, price above entry 4h after entry on 5 of 13. The
  learning pass's tunable list is `ATR_*_MULT` + bounds + funding — all exits, no
  entry rule — so it is searching a space that does not contain the problem. Its
  system prompt now names that asymmetry and tells it to say so rather than
  propose an exit change that cannot fix an entry problem.
- **Model version is named in exactly one place now.** `CLAUDE_MODEL` is
  `claude-opus-5`; the module docstring, advisor header, `learn.py` and four docs
  said "Opus 4.8". They now say `CLAUDE_MODEL` instead of a version number —
  prose that restates a constant goes stale, which is the same defect class as
  the prompt saying `slPct 2–12` while the constant said 8 (2026-07-29). Historic
  CHANGELOG entries keep their original wording on purpose; they are a record of
  what was true then.
- **Stale docs corrected:** `OPTION3-TRADE-SYSTEM.md` said test mode was
  "currently active" (it is off, and has been since 2026-07-13) and "one live
  trade at a time" (`TEST_MAX_CONCURRENT = 3` since 2026-07-08);
  `SIGNAL-CHECKER.md` showed `TEST_MODE = True` and described the cache as
  persisted via `actions/cache`.
- **`LEARN_INJECT` added to `infra/.env.example`**, which claims to be "the
  canonical list of everything the worker needs" and was missing it.

## 2026-07-29 — The stop floor was widened, measured properly, and put back

`SL_BOUNDS` went 2.0 → 8.0 and back to 2.0 the same day. Net code change is a
comment. The reasoning is the deliverable.

**The case for widening**, from 59 historical `RSI<=30 + at lower BB` signals
across 6 coins: a −2.8% stop was hit on 24% of them, and most recovered
afterwards. ADA on 2026-07-27 was exactly that — stopped at −2.9%, back above
its original target within a day.

**Why that was not enough.** The test only asked *does price reach +2% before
−X%*. It modelled none of the machinery that actually determines a trade's P&L:
the 50% partial TP, the trailing stop on the remainder, fees, or the BTC regime
gate that blocks ~88% of signals before they are traded. Replaying the full
engine over 90 days × 38 coins via `backtest.py`:

| `SL_BOUNDS` | net P&L | PF | max DD | avg hold |
|---|---|---|---|---|
| **(2, 12)** | **−6.50** | **0.63** | 11.27 | 26h |
| (8, 12) | −9.86 | 0.60 | 16.38 | 59h |
| no stop | **−44.53** | 0.22 | 56.99 | 296h |

Wider stop, worse result — and the reason is structural, not incidental. Wins
exit on a partial TP plus a trail and average about +2; an 8% stop loses −8.19.
That is ~1:4 against, needing an ~80% win rate to break even where the replay
managed 70%. Widening a stop without widening the target does not give a trade
room, it just makes every loss bigger.

Reverted not because 10 trades prove 2 beats 8 — at that sample the gap is
noise — but because the evidence used to justify the change did not survive the
full replay, and absent a good reason to change, don't.

- **The no-stop row is the one result here that is not sample-size dependent.**
  3 of 8 positions never exited, average age 30.5 days, marked to market at
  −56.99. That is a mechanism — capital locked while the scanner finds signals it
  cannot take — not a coin flip. It also freezes the journal: a trade that never
  closes never gets an `exit_reason` and is never graded, so the learning loop
  built over the last two releases goes silent.
- **Bounds are now interpolated into the system prompt** rather than restated by
  hand. The prompt said `slPct 2–12` while the constant said 8, which would have
  had Claude reasoning about a stop that did not exist — the same defect class as
  the 4H label below. It now tracks the constants automatically, which is what
  made this revert a one-line change instead of two.
- **`backtest.py --no-sl`** added: places trades with no stop and holds to TP or
  end of data, marked to market. Positions still open at the end are reported
  with their count and average age, because an underwater position left open is
  a loss you have not booked, not a loss you have avoided.
- **Fixed: `backtest.py` crashed at the report line on a default Windows
  console.** cp1252 cannot encode `≥`, `·`, `→` or `⚠`, so the process died
  *after* the fetch and the entire replay had run. `sys.stdout.reconfigure(...)`
  in `main()` widens the stream once instead of hunting glyphs.
- **The finding that outranks stop placement: every configuration loses money**
  over those 90 days, PF 0.60–0.63, 10 trades. Ten trades cannot establish that
  either, but it is a reason to treat the strategy as unproven and keep sizes
  small rather than tuning exits on a system whose edge has not been shown.

## 2026-07-29 — Two coins fell together and the bot called it two opinions

Three trades (FET, POL, ADA) opened on the same setup within one session and all
three hit their stops for a combined −$1.25. Two separate defects surfaced.

- **The AI was being told the opposite of the truth.** `generate_signal()`
  labelled `rsi_4h <= 40` as *"higher-TF uptrend confirmed"* and awarded +1.
  RSI 40 on the 4H is the higher timeframe being **weak**. The mirrored branch
  had the same inversion (`rsi_4h >= 55` → *"downtrend confirmed"*). This was not
  cosmetic: the reasons list is pasted verbatim into the Opus prompt as
  `Confirmed by: ...`, so every one of the three losers told the model the 4H
  agreed while it was falling. Labels are now `4H oversold as well` /
  `4H still elevated`, in `signal_checker.py` and the dashboard's `app.js` copy.
  **Scoring is deliberately unchanged** — whether stacked-oversold is confluence
  or a falling knife is an empirical question, `rsi_4h` is in every entry
  snapshot, and the journal can answer it at ~30 graded trades. Reproducing the
  published `[+5.5]` score for the real FET alert is the regression test.
- **`correlation_block()`: concurrency is exposure.** `MAX_OPEN_TRADES = 3`
  counts positions, not bets. The cap was never reached, so nothing stopped one
  bet being placed three times.
- **Average correlation was built, measured, and thrown out.** The first version
  used Pearson r on hourly returns at a 0.75 cap. Against the real candles it
  would have waved all three trades through — FET/POL is only +0.394 on average.
  But on hours BTC fell >0.4% that same pair is **+0.844**, and 83–100% of
  hard-down hours had both coins of every pair red. Average r measures the market
  you are not afraid of. The shipped guard conditions on down bars (~50 of 99
  survive; conditioning on hard-down hours only leaves ~6, which is noise) and
  takes the max of both directions so the verdict is order-independent. All six
  FET/POL/ADA orderings now block.
- **`CORRELATION_MAX = 0.50` is a judgement call and is documented as one.** In
  the current watchlist nearly every pair clears it, so the guard effectively
  means "one open position at a time". Recorded as the finding it is rather than
  tuned until it looked comfortable.
- Fails **open** on missing or short history, printing why — `MAX_OPEN_TRADES`
  is still the hard backstop and a watchlist change must not halt trading.
  Logged-only in `TEST_MODE`, matching the existing rails.

## 2026-07-27 — The journal now has to prove a pattern before prescribing one

The learning loop was telling the AI to retune stops on a single data point. Its
pattern block fired on `if shakeouts:` — **one** shakeout in thirty trades emitted
`PATTERN: ... Consider a wider slPct on similar setups`. A feedback loop that acts
on n=1 doesn't get better over time; it chases noise while looking like it's
learning, and it does so with real money.

- **Significance gate** (`_wilson_lower()`, `_pattern_is_real()`): a prescriptive
  `PATTERN:` line now requires the Wilson 95% lower bound on the rate to clear
  `JOURNAL_PATTERN_NULL_RATE` (0.15), over at least `JOURNAL_MIN_SAMPLES` graded
  trades. At n=30 that's 9 shakeouts, not 1. Hand-rolled, no new dependency —
  the worker still needs only `requests`. `learn.py` already held this line with
  `LEARN_MIN_COHORT = 25`; the per-trade journal did not.
- **Counts survive, directives don't.** Below the bar the model still sees
  "2 of 14 graded trades were shakeouts" as context, and the system prompt now
  says plainly that a bare count is *no evidence at all* — not a weak pattern to
  half-act on. Without that line the gate would have been cosmetic.
- **Bug found while gating it: rates were diluted by ungraded trades.** The
  denominator was every closed trade, including those closed inside the last 24 h
  that have no verdict yet — counting "not graded" as "not a shakeout". Rates are
  now over graded trades only.
- **Skips judged as a two-way split.** `missed >= 3 and missed > good` called
  3-vs-2 a PATTERN. Now: among skips that actually *resolved* (`neutral_skip`
  decided nothing either way), the missed/good split must be distinguishable from
  a coin flip. `_skip_history_context()` returns `(text, evidence)`.
- **`evidence`: the decision's provenance** — journal sample size, which
  directives fired, whether the `learn.py` block was injected, the PF and cap in
  force. Stored inside the existing `entry_context` jsonb on both trades and
  skips, so **no migration**. This is what makes "did the journal help?"
  answerable later instead of permanently unknowable: you cannot compare
  decisions made with more history against ones made with less unless you
  recorded which was which.
- `ai_trade_params()` takes an `evidence_out` dict rather than returning a third
  value — it has six return points and a scar from the last arity change (the
  `no text block` branch, where a bare `return None` took down a whole Actions
  run). Widening all six is the same trap.
- Verified by exercising the real functions against fabricated Supabase
  responses: 1-of-30 stays silent, 12-of-30 fires, 5-of-5 never fires, ungraded
  rows don't dilute, and every degraded path still returns the right arity.

## 2026-07-17 — Trade journal: the AI can now learn from its losses (and its skips)

Goal: let Claude Opus learn from earlier mistakes. It already saw *that* a trade lost
(`_trade_history_context`), never *why* — and the conditions were computed at decision
time and then thrown away. Three additions close that loop:

- **Entry snapshot** (`entry_context` jsonb, `_build_entry_snapshot()`): freezes the market
  picture every decision was made on — score/reasons, RSI 1H+4H, MACD, BB %B, volume, ATR,
  funding, order-book ratio, S/R, BTC regime, Fear & Greed, chosen TP/SL/trail. ~600 bytes,
  zero extra API calls.
- **Post-exit verdict** (`followup` jsonb, `grade_journal_followups()`): ~24 h after a trade
  closes the worker fetches the candles since the exit and grades it — `shakeout` (stopped us
  out then hit our TP anyway → SL too tight) · `good_save` (kept falling → stop earned its
  keep) · `left_money` (ran past our trailing exit) · `well_timed` · `fair_exit` etc.
  **This is what makes a loss teachable**: `shakeout` and `good_save` are identical in a P&L
  column and imply opposite fixes.
- **Skip ledger** (`skipped_setups` table): every AI `[SKIP]` logged with the same snapshot and
  graded the same way (`missed_win` / `good_skip` / `neutral_skip`) — because refusing a good
  trade is a mistake that never shows up in P&L. Mechanical `Option3Preflight` size rejections
  are deliberately not logged (limits, not judgments). `ai_trade_params()` now returns
  `(params, skip_reason)`.
- Both histories are rendered back into every prompt with **code-computed** patterns ("3 of 8
  were SHAKEOUTS → widen slPct"), never model-inferred ones. Under `JOURNAL_MIN_SAMPLES` (10)
  closed trades the prompt labels the data **anecdote, not statistics** and forbids blacklisting
  a coin or jumping size over one or two results — the main over-fitting risk at this sample size.
- Grading runs once per Actions run, ≤5 rows, public candles only. Degrades silently if the
  migration isn't run (`_save_option3_trade` now strips any missing optional column).
- **Requires the updated SQL migration in docs/ARCHITECTURE.md** (new columns + `skipped_setups`).

## 2026-07-17 — CRITICAL: small trades left unprotected positions, silently

User found HYPE on their OKX account they never authorised: two buys (~$10 each,
07/17 00:43 and 05:46), no TP/SL orders, no Telegram, no Supabase row. Not a
compromise — the bot's own worker, failing in the worst possible way.

- **Root cause: an Option 3 position is sold in two halves, and each half must clear
  the instrument's `minSz`.** A $10 HYPE trade buys 0.1617 HYPE; each half is 0.0808,
  under HYPE's 0.1 minimum. OKX **accepts the buy** and only rejects the protective
  orders afterwards → `_okx_post` raised → the coins were already bought → **naked
  position with no stop loss**. Only HYPE ($11.79 min) and ZEC ($10.63) breach this at
  the $10 floor; the other 36 coins are unaffected.
- **Three failures compounded it:** (1) no size pre-check, (2) `run_scan` swallowed the
  exception into `trade_result = 'error'` and Pass 3 only ever notified on success, so
  the user was never told, and (3) no Supabase row meant the symbol never entered
  `active_symbols` — so the coin stayed eligible and **re-bought on the next signal**,
  which is why HYPE was purchased twice.
- **Fixes:** a pre-flight `minSz` check (`Option3Preflight`) rejects an unviable trade
  **before any money moves**, treated as a skip; if a protective order fails *after* the
  entry fills, `_abort_unprotected()` cancels any placed algos, market-sells the position
  straight back, and sends a Telegram either way; `'error'` outcomes now always notify.
  Same pre-check added to the browser's `executeTrade()`. `_fetch_instrument_spec()` now
  also returns `minSz`.
- Verified with mocked-API tests covering all four paths: $10 HYPE blocked pre-buy, $15
  HYPE trades normally, post-buy rejection unwinds + alerts, and a failed unwind escalates.

## 2026-07-15 — CRITICAL: the OCO take-profit leg was never placed

Caught by inspecting the first real production trade (TAO, $16.50 @ 194.6): OKX showed
**both** algo orders as SL-only at 189.1, no TP anywhere, and price had already run
through the intended TP (199.85) without selling.

- **Root cause:** the TP+SL order was sent with `ordType: 'conditional'`. Per OKX's API
  docs, a `conditional` order given both TP and SL params performs *"only stop-loss logic
  … take-profit logic will be ignored"* — accepted with a success response, TP silently
  dropped. Both legs require **`ordType: 'oco'`** (same parameters otherwise).
  **Impact:** no trade could ever take partial profit, arm the trailing stop, or reach
  phase 2 — every Option 3 trade could only ever exit at its stop loss. Present in
  `place_option3_trade()` (worker) *and* the mirrored `executeTrade()` (browser).
- `orders-algo-history` is queried per `ordType`, so OCO lookups now use `oco`, with a
  `conditional` fallback (`OCO_ORD_TYPES`) so trades placed before this fix stay
  monitorable. Also fixed `_phase1_pnl()`, which looks up the OCO id and would otherwise
  have dropped the "phase 1 profit / whole-trade net" lines from phase-2 Telegrams.
- **Entry price now comes from the real market fill.** The market fallback set
  `entry_price` to the *signal-time ticker* (`remaining / price`), but it runs ~45 s
  later, so entry was recorded as exactly 194.6 on the TAO trade — an estimate, not a
  fill. It now reads the order's `avgPx` (falling back to the ticker only if OKX returns
  nothing). This anchors the TP/SL triggers and all P&L to reality, and closes a latent
  bug where a worse-than-ticker fill made the two half-sells exceed actual holdings and
  get rejected.
- Verified with mocked-API tests: OCO body carries `ordType=oco` + both legs, the 2nd-half
  SL stays single-leg `conditional`, legacy `conditional` OCOs still resolve, entry tracks
  the real fill, and `2 × sz_half` fits inside actual holdings.

## 2026-07-13 — PRODUCTION MODE ON + max 1 trade per scan

- **`TEST_MODE = False`** — testing finished and confirmed working. Production behavior
  now active: STRONG BUY needs score ≥ 5.0, the 30-min reversal gate is required,
  Claude Opus 4.8 decides and sizes every trade (10–30% of balance,
  performance-weighted cap), and the safety rails (BTC regime filter, max 3 open
  trades, 3-SL/24h circuit breaker) are enforced instead of logged-only.
  Fully reversible: setting `TEST_MODE = True` restores all test behavior unchanged.
- **`MAX_TRADES_PER_SCAN = 1`** (was 2 in production) — per user preference, only the
  single best-ranked STRONG BUY is traded per scan to keep things simple; lower-ranked
  signals wait for a later scan. The now-dead "refresh balance before the 2nd trade"
  block was removed; `backtest.py --per-scan` default updated 2 → 1 to match.

## 2026-07-08 — Daily Report simplified to a minimal heartbeat

Per user preference, the daily Telegram digest is now exactly two lines:
`💓 Daily Report — OKX Trading` + `📈 Open trades: N (COIN, COIN…)`. All performance
stats (win rate, profit factor, 7-day slice, best/worst coins, Fear & Greed, mode
line, dead-man hint) were removed from the message — that information lives in the
dashboard's 📊 Bot Performance panel instead. The once-per-UTC-day schedule, 08:00
gate, and dead-man-switch role are unchanged. Removed the now-unused
`_fetch_closed_trades()` helper (`fetch_fear_greed()` stays — the AI context uses it).

## 2026-07-08 — Test mode: fix the real bottleneck on test-trade cadence

User reported "still too slow" waiting for test trades. Root-caused with a live Supabase
query: it was **not** the STRONG_BUY_SCORE threshold — NEAR-USDT had been sitting open in
phase 1, and a rule blocked **every** new test trade while *any* test trade was active,
regardless of signal quality. Three changes were made, then two were rolled back at the
user's request as unnecessary once the actual fix proved sufficient on its own:

- **Kept:** replaced the "only ONE test trade at a time" block with `TEST_MAX_CONCURRENT = 3`
  (matches the production `MAX_OPEN_TRADES` cap) — a slow-moving trade no longer stalls
  every other signal. When slots are tight, the best-ranked candidates are kept (candidates
  are sorted by rank before slicing, not truncated arbitrarily). Verified via simulation:
  the exact "1 active trade + 2 new candidates" scenario that was silently dropping
  everything now correctly lets new trades through.
- **Reverted:** `STRONG_BUY_SCORE` (test mode) tried at 0.5, restored to **1.0**.
- **Reverted:** `MAX_TRADES_PER_SCAN` tried unified at 2, restored to **1 in test mode**
  (2 in production, unchanged).

## 2026-07-07 — Coin universe audit (33 → 38 coins)

Full audit against **live OKX spot data** (volume ranks, listing status, perp availability).
`SYMBOLS` (worker) and `DEFAULT_SCANNER` (browser) updated in sync; the browser now also
**drops removed coins** from saved localStorage lists (previously removals never synced).

- **Removed (6):**
  - RUNE, TON — **delisted from OKX spot** (confirmed via instruments API; the scanner had
    been burning 4 requests/scan each on dead pairs)
  - FLOKI — OKX volume collapsed to ~$0.1M/24h (rank #178): manipulation-prone candles
  - WIF — meme peak long past, OKX liquidity migrated away (~$0.4M, rank #111)
  - STRK — persistent unlock dilution, fading traction (~$0.8M)
  - ATOM — multi-year structural decline; the classic dip-buyer trap (~$0.4M, rank #124)
- **Added (11), all verified live on OKX with full candle history + perps:**
  - Majors: BNB, LTC, BCH, XLM (deep global liquidity, clean TA)
  - Blue-chip DeFi: UNI, AAVE
  - AI: TAO (Bittensor), WLD (Worldcoin)
  - High-momentum 2025-26 leaders: HYPE (Hyperliquid), MON (Monad), ZEC (Zcash revival)
- **Watch list (kept but monitor via profit-factor data):** TIA, INJ, POL, JUP, FET —
  legitimate projects with weak current OKX volume; prune if the digest shows chronic losses.
- **Considered and rejected:** TRUMP/PUMP/WLFI/PI (event-driven/manipulation-prone),
  OKB (exchange-token idiosyncrasy), XAUT/PAXG (gold, wrong asset class), SHIB (meme cohort
  already covered), ORDI/BLUR/ETC/ICP/PYTH (fading sectors), TRB (notorious manipulation),
  plus the new-listing churn at the top of the volume table (NES/RE/DATA/LIT/…).
- Docs updated (counts 33→38); manual AI advisor volatility-tier examples refreshed.
- Scan cost: ~152 OKX requests/scan (from ~132) — still comfortably inside the 60s cycle.

## 2026-07-07 — Bot Performance panel + portrait lock (dashboard)

- **Bot Performance panel**: new bar-chart button in the header slides in a P&L dashboard
  above the scanner. Lazy-loaded — zero requests at page load (coin speed preserved); the
  first open fetches all closed trades from Supabase once and caches them, so range
  switching (7D / 30D / 90D / All / custom from→to dates) is instant. Shows net P&L
  **after OKX fees** as the headline plus before-fees and estimated-fees columns, trades
  W/L, win rate, profit factor, avg win/loss, a cumulative equity-curve chart, per-coin
  net table, and exit-type counts. Verified live in a browser (Supabase query 200,
  lazy-load confirmed, rendering checked with sample data).
- **Portrait-only lock**: manifests set `"orientation": "portrait"` (installed PWA),
  best-effort `screen.orientation.lock()` at init, and a full-screen "rotate back"
  overlay for phone-sized landscape (desktops unaffected). Verified in the browser.

## 2026-07-07 — Backtesting harness

- New `backtest.py`: replays the PRODUCTION signal + Option 3 exit logic over historical
  OKX candles (free public endpoint, disk-cached, no keys, no orders). Imports the real
  functions from `signal_checker.py` so the tested logic can't drift from the traded logic.
  Flags to A/B any knob: `--score`, `--atr-tp/-sl/-trail`, `--no-regime`, `--no-reversal`,
  `--days`, `--coins`, `--stake`. Conservative fill model (next-candle-open entries,
  SL-first on ambiguous candles, taker fees). Full guide in docs/BACKTESTING.md.
- First real finding (6 majors × 45 days): all 124 STRONG BUY signals were regime-blocked;
  with the filter off they would have netted −$4.35 at PF 0.62 — i.e. the BTC regime
  filter demonstrably saved money during this bear stretch.

## 2026-07-07 — Daily digest + Fear & Greed

- **Daily Telegram report** (`maybe_send_daily_digest`, fires once per UTC day after 08:00):
  bot-alive heartbeat + mode, Fear & Greed, open trades, win rate & net P&L over the last
  100 closed trades, profit factor → current sizing tier, 7-day slice, best/worst coins.
  Doubles as a **dead-man switch** — if the report stops arriving, the pipeline is down
  (expired cron-job.org PAT, broken workflow, etc.). Dedup via `_daily_digest` cache key.
- **Fear & Greed Index** (alternative.me — free, keyless): shown color-coded in the dashboard
  summary bar (refreshes with the news cadence), added to the AI trade-decision prompt with
  contrarian rules (≤ 25 Extreme Fear → dip-buy conditions; ≥ 75 Extreme Greed → cut size
  25–50%, tighter TP), and included in the daily report. Free replacement for the
  CryptoPanic idea after their API went paid.

## 2026-07-06 — Major upgrade round (still in TEST_MODE)

### Telegram messages
- Every "sold" message now shows the **exact USDT profit/loss net of OKX fees** (never just a
  percentage). When OKX won't return a fill price, an estimate marked `~` is shown instead.
- Fill-price lookup made robust: `avgPx` → `actualPx` → the child market order's `avgPx`.
- Removed the `⏰ HH:MM UTC` line from **all** messages (Telegram's native timestamp is used).
- New message types: Fast Reversal (whipsaw), Auto-Trading Paused (circuit breaker).
- Phase-2 exits report the recovered phase-1 profit and the **whole-trade net result**.

### Trade structure (Option 3 hardening)
- **Full-position stop-loss protection**: the 2nd half now gets its own conditional SL at
  placement (instead of a dormant trailing stop) — in a crash both halves stop out server-side
  on OKX even if the worker is down. When the TP fills, the monitor swaps that SL for an
  immediately-active trailing stop (`_swap_sl2_to_trailing`). New Supabase column: `sl2_id`.
- Whipsaw handling: TP fills then price crashes through the 2nd-half SL within one monitor
  window → detected, closed, reported as `tp_then_sl`.
- Phase-2 exits cancel the counterpart order (no dangling algo orders on OKX).
- Honest failure reporting: if the break-even SL can't be placed, Telegram says so.
- Trade outcomes recorded on close: `exit_reason`, `exit_price`, `net_pnl_usdt`, `closed_at`
  (Supabase migration in docs/ARCHITECTURE.md — **must be run once in the SQL editor**).

### Safety rails (enforced in production, logged-only in TEST_MODE)
- **BTC regime filter**: no dip-buying while BTC is below its 4H EMA-50 with 4H RSI < 45.
- **Open-trade cap**: max 3 concurrent Option 3 trades (`MAX_OPEN_TRADES`).
- **Daily circuit breaker**: 3 stop-loss exits in 24h pauses new trades until the window
  clears (`MAX_SL_PER_DAY`), with a once-per-day Telegram notice.

### AI decision-maker
- Model upgraded Haiku → **Claude Opus 4.8** (`claude-opus-4-8`) with **adaptive thinking**;
  response parser handles thinking blocks; `max_tokens` 2000; ~$0.01–0.02 per decision.
- **ATR-based exits**: TP = 2×ATR(14), SL = 2.5×ATR, trail = 1×ATR from live 1H candles.
- **Support/resistance**: TP pulled 0.5% below the nearest swing-high ceiling, SL pushed
  0.75% below the nearest swing-low floor (`suggest_exit_params`).
- **Code-enforced clamps** regardless of the AI's answer: TP 1.5–10%, SL 2–12%, trail 1–5%
  and always < TP (protects the phase-2 break-even guarantee).
- **Rich context in the prompt**: funding rate & open interest (funding > +0.10% auto-skips
  before the AI is consulted), order-book bid/ask imbalance (top 20 levels), BTC regime values.
- **News veto**: the coin's latest CryptoCompare headlines (verified genuinely coin-tagged)
  go into the prompt — hack/exploit/lawsuit/SEC/delisting/insolvency → SKIP regardless of
  indicators; no headlines is neutral (`fetch_coin_news`).
- **Performance-weighted sizing**: position cap scales with the last-30-trades profit factor
  (PF ≥ 1.5 → 30%, 1.0–1.5 → 22%, < 1.0 → 15%), enforced in code and prompt.
- The AI also sees the bot's recent win/loss record overall and for the specific coin.

### Trade execution
- **Maker-first limit entries**: limit buy 0.05% below market (rounded to the instrument's
  official tick/lot size), 45s wait, then cancel + market fallback — cuts fees + slippage
  roughly in half; partial fills and cancel-races-a-fill handled; `entry_price` now comes
  from real fills when known. Active in test mode too.

### Test mode (current state)
- STRONG BUY bar lowered to score ≥ 1 (production stays ≥ 5) so tests trigger fast.
- Test trades shrunk to $5 with TP 1.5% / SL 2% / trail 1% — worst case ≈ $0.11 per test.
- `TEST_MODE = False` still reverts everything to production behavior in one line.

### Dashboard
- News: three sources fetched in parallel and merged (CryptoCompare News API primary — direct,
  no proxy, keyed; CryptoPanic community-voted sentiment when `CRYPTOPANIC_API_KEY` is set;
  CoinTelegraph + CoinDesk RSS), deduped by title, newest first.
- Risk profile permanently `aggressive`, auto-refresh permanently 1 minute — both removed from
  the Settings UI (fixed in config.js). Also fixed a pre-existing crash in `saveSettings()`
  (referenced a form field that doesn't exist).

### Bug fixes found during verification
- Circuit-breaker Supabase query: timestamp `+00:00` URL-decoded as a space → query always
  failed silently. Now uses `Z` format (verified against the live table).
- Coin quantities could serialize in scientific notation for high-priced coins (BTC) → OKX
  rejection. All monitor order sizes now use fixed 8-decimal formatting.
- CryptoCompare pads thin coin categories with general news → headlines are now verified
  against each article's own tags before reaching the AI.

### Keys / config added
- `CRYPTOCOMPARE_API_KEY` (free, read-only, news scope) in config.js + signal_checker.py.
- `CRYPTOPANIC_API_KEY` placeholder in config.js — **left empty on purpose**: CryptoPanic's
  API turned out to be paid (~$50/week, rejected as not worth it). The integration code stays
  dormant; keyword sentiment is used and trading is unaffected (the AI judges raw headlines).

## Earlier (pre-changelog)
- Initial system: browser dashboard (scanner/AI advisor/news/PWA), Python worker on GitHub
  Actions (signals → Option 3 auto-trades → monitor → Telegram), Supabase persistence,
  cron-job.org scheduling. Documented across README.md and docs/.
