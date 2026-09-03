# Background Worker (`signal_checker.py`)

The autonomous half of OKX AI. Runs **on the VPS** (`C:\OKXAI`, Task Scheduler
task `OKX-SignalChecker` — see [`../infra/VPS-SETUP.md`](../infra/VPS-SETUP.md));
each invocation loops for ~4 minutes doing one full scan per 60 s
(`LOOP_DURATION` / `CHECK_INTERVAL`), then exits and is relaunched immediately by
the wrapper, so coverage is continuous.

> It used to run on GitHub Actions triggered by cron-job.org. That path is
> **disabled** and retained only as a fallback ([CRON-JOB-ORG.md](CRON-JOB-ORG.md)).
> The self-exit-after-4-minutes design is a leftover from it — harmless on the
> VPS, where the wrapper just starts the script again.

```
main()
 └─ loop every 60s for ~4min:
     ├─ run_scan(cache)            # signals → filters → ranking → auto-trades → Telegram
     ├─ monitor_option3_trades()   # exit detection for all open trades (see OPTION3 doc)
     └─ save_cache(cache)          # persists alert-dedup state to signal_cache.json
```

## Scan pipeline (`run_scan()`)

**Pass 1 — collect.** For each of the 38 `SYMBOLS`: fetch 1H candles (100), ticker, 30m candles (50, reversal check), 4H candles (50, RSI confirmation); compute RSI/MACD/BB/volume ratio; run `generate_signal()` (same scoring table as the browser — see [DASHBOARD.md](DASHBOARD.md#2-signal-engine-generatesignal); `parity_check.py` enforces that they stay identical). A coin survives to trade-candidacy only if **all** of these pass:

1. Label is **STRONG BUY** (`score ≥ STRONG_BUY_SCORE` — 4.5 production, 1.0 test mode).
2. **Reversal confirmed** on 30m candles (skipped in test mode): latest candle green **and** RSI rising **and** volume ≥ 1× the 20-bar average (`reversal_confirmed()`). Guards against buying a falling knife.
3. **Volume confirmed**: the 1H volume ratio is at least `MIN_VOL_RATIO_TRADE` (2.0×) of the 20-bar average. A *trade* gate, not a labelling rule — the coin still reads STRONG BUY on the dashboard, the worker just declines to buy it, exactly as with the reversal gate. Set to `0.0` to disable.
4. Not suppressed by the **zone/cooldown rules** (below).
5. No active Option 3 trade already running for this symbol (then it's logged but not re-traded).

**Pass 2 — safety rails, then rank & trade.** Before any trade is placed, three safety rails run (**enforced in production, logged-only in TEST_MODE** so the test pipeline keeps flowing):

1. **BTC regime filter** (`btc_regime_ok()`): the engine buys oversold dips, which loses money when the whole market trends down. New buys are blocked while BTC is clearly bearish on the higher timeframe (price below the 4H EMA-50 **and** 4H RSI < 45). Fails open with a loud log if BTC data is unavailable.
2. **Open-trade cap** (`MAX_OPEN_TRADES = 3`): never more than 3 concurrent Option 3 trades.
3. **Daily circuit breaker** (`MAX_SL_PER_DAY = 3`): if 3 stop-loss exits landed in the last 24 h (counted from the `exit_reason`/`closed_at` columns in Supabase), new trades pause until the window clears — a one-per-day "⏸️ Auto-Trading Paused" Telegram announces it.

In TEST_MODE, candidates are first capped to whatever concurrent-trade slots are free (`TEST_MAX_CONCURRENT = 3` minus currently-open test trades — best-ranked kept, rest wait for a slot). Surviving candidates are ranked by `_rank_candidate()` (signal score + up to +1.0 for 1H RSI depth below 30 + up to +0.5 for 4H RSI depth + up to +0.5 for volume surge). Only the top `MAX_TRADES_PER_SCAN` (1 — test & production) is traded per scan; lower-ranked signals wait for a later scan. The winner goes through:

- **Production:** `ai_trade_params()` — Claude Opus 5 (`claude-opus-5`, adaptive thinking enabled; set in `signal_checker.py`) receives the full technical picture, the live balance, this bot's recent live results (`_trade_history_context()`), **and rich decision context** built by `_build_trade_context()` for the top candidates only: **ATR(14)** (live volatility), nearest **support/resistance** (swing highs/lows), **suggested exits** (`suggest_exit_params()`: TP = 2×ATR pulled 0.5% below resistance, SL = 2.5×ATR pushed 0.75% below support, trail = 1×ATR; clamps TP 1.5–10% / SL 2–12% / trail 1–5%, trail always < TP), **funding rate + open interest** (funding > +0.10% auto-skips before the AI is even asked; +0.05–0.10% → halve size; negative → squeeze fuel), **order-book bid/ask imbalance** (top 20 levels; < 0.7 → halve or skip), the **BTC regime values**, the **Fear & Greed Index** (alternative.me, keyless — ≤ 25 Extreme Fear favors contrarian dip-buys, ≥ 75 Extreme Greed cuts size 25–50%), and the coin's **latest headlines** (`fetch_coin_news()` — CryptoCompare News filtered to articles genuinely tagged with the coin; hack/exploit/lawsuit/SEC/delisting/insolvency headlines mean SKIP regardless of indicators, no headlines is neutral). It must reply with exactly one line: `[TRADE:{...}]` (amountUsdt, partialTpPct, trailingCallbackPct, slPct) or `[SKIP: reason]`. **Performance-weighted sizing**: the hard position cap scales with the profit factor of the last 30 closed trades — PF ≥ 2.0 → 60%, 1.5–2.0 → 55%, 1.0–1.5 → 50%, < 1.0 → 45%; below 30 closed trades an *unknown* PF falls back to the win rate (< 50% → 45%, else 50%) rather than being treated as good. Raised from 15/22/30 on 2026-08-15 at the owner's direction. The cap applies to **available** USDT, so concurrent trades compound down (60% → 24% → 9.6% ≈ 94% deployed at three open trades, vs ≈ 66% under the old ladder) — `MAX_OPEN_TRADES` is what bounds total exposure now. Enforced both in the prompt and in code, along with the exit-parameter clamps, whatever the AI answers. Minimum $10; mandatory SKIP if 4H RSI > 65 or < 2 confirmations.
- **Test mode:** the AI is bypassed; fixed `$5 / TP 1.5% / SL 2% / trail 1%`.
- Then `place_option3_trade()` — see [OPTION3-TRADE-SYSTEM.md](OPTION3-TRADE-SYSTEM.md).

**Pass 3 — notify.** Telegram is sent **only when a trade was actually placed** (`format_alert()` with the trade parameters appended). Signal-only, AI-skip, rank-capped, and error outcomes update the dedup cache silently — the user chose to only hear about confirmed new trades. If the Supabase save failed, the message carries a loud "NOT saved to tracking DB" warning because break-even moves and exit alerts won't happen for that trade.

## Alert deduplication (zone system + cache)

Labels collapse into **zones**: BUY + STRONG BUY = `up`, SELL + STRONG SELL = `down`, HOLD = `neutral`. Rules:

- Oscillating between BUY and STRONG BUY (same zone) never re-alerts.
- A genuine zone flip within `FLIP_COOLDOWN` (2 min production / 30 s test) is suppressed as noise.
- Staying in the same zone longer than `REZONE_REMINDER` (4 h production / 10 min test) re-arms one reminder alert.

State lives in `signal_cache.json` — `{symbol: {label, zone, alerted_zone, alerted_at}}` — persisted on the VPS at `C:\OKXAI\signal_cache.json` between the wrapper's relaunches (the retired Actions fallback carried it with `actions/cache`). `load_cache()` migrates an older string-only format. When the cache is empty (first run / cache evicted), the first scan is a **warm-up**: it records state but sends no alerts and places no trades, preventing an alert storm after every cache loss.

## Test mode

**Currently `TEST_MODE = False` — the bot is in production mode and trades real money.**
The flag is the one line that switches everything:

```python
TEST_MODE = False   # ►►► set to True for $5 test trades ◄◄◄
```

One flag flips everything (all production values are preserved in the same file):

| Behavior | Production | Test mode |
|---|---|---|
| STRONG BUY threshold | score ≥ 4.5 | score ≥ 1.0 (fires on common conditions, e.g. bullish MACD + price near lower BB) |
| 30m reversal confirmation | required | skipped |
| Volume trade-gate (`MIN_VOL_RATIO_TRADE`) | 2.0× average required | skipped |
| Claude advisor (`CLAUDE_MODEL`) | decides trade + sizing | bypassed |
| Trade size | AI-chosen, 45–60% of available balance | fixed $5 USDT (worst-case SL test ≈ $0.11 incl. fees) |
| TP / SL / trail | AI-chosen by volatility tier | fixed 1.5% / 2% / 1% (tight → fast full-lifecycle tests) |
| Max trades per scan | 1 | 1 |
| Concurrent test trades | — (`MAX_OPEN_TRADES = 3`) | up to `TEST_MAX_CONCURRENT = 3` at once — a slow trade no longer blocks new ones |
| Flip cooldown / re-zone reminder | 2 min / 4 h | 30 s / 10 min |
| Maker-first limit entries | active | active — live-tested by the $5 test trades |
| ATR/S&R exits, news veto, rich context, PF sizing | active (feed the AI decision) | not exercised (AI bypassed) but fully unit-tested |

There is also `TEST_FORCE_SIGNAL` (normally `False`): forces a fake BTC STRONG BUY on the next run to verify the scan → Telegram → auto-trade pipeline end-to-end. Delete the alert-dedup cache first, or the forced signal may be suppressed as already-alerted — on the VPS that is `C:\OKXAI\signal_cache.json` (on the disabled Actions fallback it was the `actions/cache` entry).

## Telegram messages

`send_telegram()` posts HTML-mode messages to the configured chat. **No message contains a timestamp line** — Telegram's native message time is the timestamp. The catalogue:

| Event | Sent by | Content highlights |
|---|---|---|
| New trade placed | `format_alert()` | Signal, score, price, reasons + "✅ Trade Already Placed on OKX" with $ amount, TP/SL/trail |
| Partial TP hit | monitor | Exact USDT profit locked (net of fees), fee breakdown, "trailing stop now active" |
| Stop loss hit | monitor | **Exact total USDT loss** (both halves, incl. fees), entry → exit prices, "Full position closed" |
| Fast reversal (whipsaw) | monitor | TP profit + 2nd-half SL loss + whole-trade net (price hit TP then crashed within one monitor window) |
| Trailing stop exit | monitor | Exact USDT gain on 2nd half + recovered phase-1 profit + **whole-trade net result** |
| Break-even exit | monitor | (fallback/legacy trades) 2nd-half result (≈ −fees) + phase-1 profit + whole-trade net result |
| Auto-trading paused | circuit breaker | 3 stop-losses in 24 h — new trades resume automatically; sent at most once per day |
| **Daily Report** (heartbeat) | `maybe_send_daily_digest()` | Once per UTC day (first run after 08:00 UTC), deliberately minimal — two lines: `💓 Daily Report — OKX Trading` + `📈 Open trades: N (COIN, COIN…)`. **Dead-man switch: if this message stops arriving, the pipeline is down** — on the VPS, check `Get-ScheduledTask -TaskName "OKX-SignalChecker"` and the tail of `C:\OKXAI\logs\okx-signal-checker.log`. Dedup state in the cache (`_daily_digest`). Full performance stats live in the dashboard's 📊 Bot Performance panel instead. |
| Orders cancelled manually on OKX | monitor | Trade marked closed, fresh signals will re-trade the coin |

P&L math (`_exit_pnl()`): `net = (fill − entry) × size − entry×size×fee − fill×size×fee` with `fee = 0.001`. When OKX won't return an exact fill price even after the fallback lookups, the message shows an **estimate marked with `~`** (computed from the trigger price) rather than omitting the USDT figure.

## Operational notes

- All OKX/Supabase/Claude failures are caught per-coin/per-trade and logged to the Actions console — one bad symbol never kills the scan.
- `time.sleep(0.3)` pacing between symbols keeps OKX rate limits happy.
- If `CLAUDE_API_KEY` is missing in production mode, auto-trade silently does nothing (signals still tracked). If OKX keys are missing, `monitor_option3_trades()` exits immediately.
- The available-USDT fetch happens once per scan (plus refreshes between multiple trades); if it fails, auto-trading is disabled for that run.
