# The Option 3 Trade System

"Option 3" is this app's name for its two-phase trade-management strategy: **take profit on half the position early, then let the other half ride a trailing stop with zero downside risk**. It is the only strategy the autonomous worker places, and the primary strategy the dashboard's AI Advisor recommends.

## The strategy in one picture

```
                                   Phase 2
  entry ──────────────► TP hit ───────────────► trailing stop exit (profit)
    │                     │
    │                     ├─ sell 50% at +TP%  (profit locked)
    │                     └─ 2nd-half SL swapped for an ACTIVE trailing stop
    │                        (floor = peak − trail% — above entry since trail% < TP%)
    │
    └──► SL hit (Phase 1) ── BOTH halves stopped out at −SL% on OKX (max loss capped)
```

Why it works: in phase 1 the **entire position** is stop-loss-protected server-side, so the max loss is capped even if the monitor is down. After the partial TP fires, the trade **can no longer lose money** — half the profit is banked and the trailing stop's floor sits above entry. The trailing stop lets the second half capture an extended run.

## Order placement (3 OKX orders)

Implemented twice with identical structure: `place_option3_trade()` in `signal_checker.py` (autonomous) and `executeTrade()` in `app.js` (manual, user-confirmed). Steps:

1. **Entry (worker: maker-first)** — `_limit_entry_buy()` places a limit buy 0.05% below market (cheaper maker fee, no spread cost), polls for up to 45 s, then cancels and falls back to a spot **market buy** (`tgtCcy: quote_ccy`) for whatever wasn't filled — partial fills and the cancel-races-a-fill case are handled, and the real average fill price becomes the trade's `entry_price` when known. The browser's manual path still uses a plain market buy. Wait ~1.5 s for the fill to register.
2. **OCO sell** on 50% of the coins: one algo order (`ordType: 'oco'`) carrying **both** `tpTriggerPx` (entry × (1 + TP%)) and `slTriggerPx` (entry × (1 − SL%)), market execution (`ordPx: -1`), trigger type `last`.
   *Why `oco` and not `conditional`:* with `ordType: 'conditional'` OKX accepts a two-legged request but **performs only the stop-loss logic and silently ignores the take-profit** — the order lands as SL-only, so the TP never fires and the trade can never reach phase 2. This shipped as a live bug until 2026-07-15; `orders-algo-history` lookups for an OCO id must likewise use `ordType=oco`.
   *Why one OCO instead of two orders:* OKX reserves the sell quantity per algo order. Separate TP + SL orders on the same half would try to reserve it twice and be rejected. The OCO's two legs are mutually exclusive, so OKX reserves that 50% only once.
3. **Conditional SL** on the remaining 50% at the **same trigger price** as the OCO's SL leg. This means the **full position is stop-loss-protected server-side on OKX 24/7** — a flash crash sells both halves at −SL% even if the worker is down (VPS offline, task stopped, network dead). The trailing stop is *not* placed at this point; the monitor swaps this SL for an immediately-active trailing stop once the TP fills (see phase 1 below). Because trail% < TP% in every parameter tier, the trailing floor sits above entry, so the swap never gives up break-even protection.

Quantity math: the worker computes `sz_coin` from **actual fills** — the limit order's filled size, plus `remaining / avgPx` for any market-bought remainder, where `avgPx` is looked up from the market order itself (`_get_order_fill_price`) rather than assumed to be the signal-time ticker. This matters: the market fallback runs ~45 s after the signal, so the ticker is stale by then, and `entry_price` anchors both the TP/SL triggers and every P&L figure. It also keeps `sz_half` honest — sizing off a too-low price overstates the coins bought, and the two half-sells can then exceed actual holdings and get rejected. `entry_price` is the spend-weighted average of all fills; only if nothing at all is known does it fall back to `amount_usdt / price`. The browser's manual path still estimates `amount_usdt / price`. Each half is then `sz_coin × 0.5 × 0.9985` — the haircut covers the buy fee having been taken in coin units, so the sells never exceed the actual holdings.

4. **Persist to Supabase** `option3_trades` (phase 1). The row's `id`, `partial_tp_id`, and `sl_id` are all the OCO algo ID; `sl2_id` is the 2nd-half SL; `trailing_id` starts empty. If the save fails, the orders still exist on OKX but the monitor can't manage the trade — the Telegram trade confirmation shouts about this explicitly. (Older rows without `sl2_id` are "legacy format" — the monitor still handles them with the original dormant-trailing + break-even-SL flow.)

### Parameter selection

- **Production (currently active — `TEST_MODE = False`):** Claude (`CLAUDE_MODEL`) picks the amount (45–60% of *available* balance by signal score, hard cap **performance-weighted** in code: 60% / 55% / 50% / 45% by the last-30-trades profit factor, an unknown PF treated as a weak one) and starts TP/SL/trail from a **volatility-adaptive baseline**: TP = 2×ATR(14), SL = 2.5×ATR, trail = 1×ATR, with TP pulled 0.5% below the nearest resistance and SL pushed 0.75% below the nearest support (`suggest_exit_params()`). Code-enforced clamps regardless of the AI's answer: TP 1.5–10%, SL 2–12%, trail 1–5% and always below TP (preserves the break-even guarantee). Funding rate, open interest, and order-book imbalance feed the decision; funding > +0.10% auto-skips the trade.
- **Test mode (`TEST_MODE = True` — currently OFF):** fixed $5 / TP 1.5% / SL 2% / trail 1%, up to `TEST_MAX_CONCURRENT` (3) test trades at once (worst-case cost ≈ $0.11 per test).
- **Manual (dashboard):** the AI's suggested values pre-fill the confirmation modal and every number is user-editable before placing.

## Exit monitoring (`monitor_option3_trades()`)

Runs on every worker scan (~every 60 s while the Action is alive). Fetches all Supabase rows with `phase < 3` and checks OKX algo-order history (`/trade/orders-algo-history`, state `effective` = triggered, `canceled`/`order_failed` = manually killed).

### Phase 1 — waiting for TP or SL (one OCO order covers both)

Because TP and SL share one algo ID, the monitor tells them apart by **fill price vs entry price**:

- **Fill > entry → partial TP fired** — three sub-cases:
  1. *Normal:* `_swap_sl2_to_trailing()` cancels the 2nd-half SL and places an **immediately-active trailing stop** (price is at/above TP, so it starts tracking the peak right away). Supabase: `phase = 2`, `trailing_id` = new algo ID. Telegram: exact USDT profit locked + "trailing stop now active".
  2. *Whipsaw:* if the 2nd-half SL **also** already fired (price hit TP then crashed through the SL before this monitor run), the trade is fully closed — Telegram reports both halves and the whole-trade net (`exit_reason = tp_then_sl`).
  3. *Fallbacks:* if the trailing placement fails, a break-even SL is placed instead; if that also fails, the trade is closed as `error` with an urgent "manage manually" Telegram. Legacy rows (no `sl2_id`) instead get the original break-even-SL move beside their dormant trailing stop.
- **Fill ≤ entry (or fill unknown) → SL fired:** `_close_full_position_at_sl()`:
  1. New format: the 2nd-half SL shares the trigger price, so OKX normally sold **both halves server-side already** — the monitor just collects both fills (with a 2 s grace re-check). Market-sell is only a fallback (after cancelling the 2nd SL so it can't double-sell), and legacy rows always use it.
  2. Mark closed (`exit_reason = sl`, whole-trade net P&L recorded) and send Telegram: exact total USDT loss across both halves incl. fees, entry → exit average.
- **OCO manually cancelled on OKX:** the 2nd-half SL and any trailing stop are cancelled too, trade marked closed (`cancelled`), Telegram notice (fresh signals will open a new trade for that coin).
- A legacy branch handles old rows where TP and SL were separate algo IDs (`is_oco == False`) — same flow.

### Phase 2 — trailing stop riding (break-even SL only in fallback/legacy trades)

- **Trailing stop triggered:** fetch fill, compute 2nd-half net USDT gain, recover the phase-1 TP fill from OKX history (`_phase1_pnl()`) and report the **whole-trade net result**. Any leftover break-even SL is cancelled. Close (`exit_reason = tp_trail`).
- **Break-even SL triggered** (fallback/legacy trades only): the 2nd half exited at ≈ entry (net ≈ −fees); report that plus phase-1 profit and whole-trade net. The never-activated trailing stop is cancelled. Close (`exit_reason = break_even`).
- **Either order manually cancelled:** close the trade (`cancelled`), Telegram notice.

In both exit branches the *counterpart* order is explicitly cancelled so no dangling algo order is left on OKX with no coins behind it. Every close records `exit_reason`, `exit_price`, `net_pnl_usdt`, and `closed_at` in Supabase — this history powers the daily stop-loss circuit breaker and the AI advisor's "recent live results" context (see [SIGNAL-CHECKER.md](SIGNAL-CHECKER.md)).

### Fill-price robustness

OKX frequently leaves `avgPx` empty on algo-history rows. `_get_fill_price()` therefore tries, in order: `avgPx` → `actualPx` → the `avgPx` of the child market order via `/trade/order?instId&ordId` (`_get_order_fill_price()`). If everything fails, the Telegram P&L is **estimated from the trigger price and prefixed with `~`** — a USDT figure is always shown, never just a percentage.

### P&L formula (`_exit_pnl()`)

```
gross    = (fill − entry) × size
buy_fee  = entry × size × 0.001      # proportional share of the entry fee
sell_fee = fill  × size × 0.001
net      = gross − buy_fee − sell_fee
```

`fmt_usdt()` renders signed amounts with 2 decimals (4 decimals under $0.10, which matters for $10 test trades).

## Invariants and gotchas for future development

- **The Supabase row is the single source of truth for "is a trade running".** A symbol with an open row (`phase < 3`) will not be re-traded; a row stuck at phase < 3 with no live OKX orders blocks new trades for that coin until marked closed (the manual-cancel detection normally handles this).
- The monitor only distinguishes TP from SL by fill-vs-entry comparison — if OKX returns **no** fill price at all, phase 1 assumes the SL side. The multi-fallback fill lookup makes this rare.
- `sz_half` is stored as the string OKX accepted; both later sells (break-even SL, emergency market sell) reuse it verbatim.
- All timing assumes the monitor runs every ~60 s within a 4-minute Action window with up to ~5-minute gaps between Actions — exits are detected minutes after they happen on OKX, which is fine because the protective orders themselves live on OKX 24/7.
- Keep the browser (`executeTrade`) and worker (`place_option3_trade`) placement logic in sync — same order types, same haircut, same Supabase columns.
