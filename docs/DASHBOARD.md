# Browser Dashboard (`index.html` + `app.js`)

A single-page vanilla-JS app, installable as a PWA. No build step — `index.html` loads `config.js` then `app.js`. All state lives in the global `state` object; persistence is `localStorage` (`LS` helper) plus optional encrypted Supabase cloud storage.

## Startup sequence (`init()`)

1. `loadSettings()` — pull API keys/prefs from `localStorage` into `CONFIG`.
2. Load `notifiedSignals` (alert dedup memory) and scanner symbol list (saved list merged with any new coins added to `CONFIG.DEFAULT_SCANNER`).
3. Register the service worker.
4. **If Supabase is configured** (it is, by default in `config.js`): try auto-unlock with a password cached in `sessionStorage`; otherwise show the **lock screen**. On successful unlock (`loadFromCloud`), decrypted settings overwrite `CONFIG` and `localStorage`, then `loadAppData()` runs.
5. `loadAppData()` — restore last-known USDT balance, fetch all market data, render, start auto-refresh timers, background-sync the OKX balance.

## Panels

### 1. Signal Scanner (primary panel)

- Watches the 38 symbols in `CONFIG.DEFAULT_SCANNER` (user-editable, persisted as `scanner` in localStorage).
- `fetchAllData()` pulls, per symbol: ticker + 1H + 4H + 30m candles — batched 2 symbols at a time with 250 ms gaps to avoid OKX 429s. Funding rate + open interest fetched separately in a non-blocking loop.
- `computeIndicators()` produces: RSI(14) 1H + previous, RSI 4H, MACD (12/26/9), Bollinger %B (20, 2σ), volume ratio (last candle vs 20-bar avg), 30m reversal inputs, and the **signal**.
- **Signal age**: `computeIndicators` walks back up to 10 1H candles re-running the signal to find when the current label began (`signalStartTs`) — so "In Signal" ages are derived from OKX candle timestamps and identical on every device. A 1 s ticker (`startAgeTicker`) keeps the age cells live.
- Table shows: price, 24h %, RSI 1H, RSI 4H, MACD state, BB%, volume ratio, signal badge with score, signal age. Hover filter chips (Buy/Sell), sort dropdown, "Top Pick" banner for the highest score ≥ 2.
- If OKX is unreachable a deterministic **Demo** dataset renders instead (`mockTicker`); demo rows are excluded from alerts and the scanner display.

### 2. Signal engine (`generateSignal()`)

Score components (identical to the Python worker — keep in sync!):

| Indicator | Contribution |
|---|---|
| RSI 1H | ≤20: **+3** · ≤30: **+2** · ≤40: 0 (reason only) · ≥60: **−1** · ≥70: **−2** · ≥80: **−3** |
| MACD | bullish cross: **+2** · bearish cross: **−2** · trend only: **±0.5** |
| Bollinger %B | ≤0.05: **+2** · ≤0.20: **+1** · ≥0.80: **−1** · ≥0.95: **−2** |
| Volume ratio | ≥2.0×: **+1** · ≥1.5×: 0 (reason only) |
| RSI 4H (confirmation) | score>0 & ≤40: **+1** · score<0 & ≥55: **−1** · caution counters: **∓0.5** |

**Order matters.** Volume is applied *before* the 4H term, because the 4H term keys off
the sign and size of the running score. Both implementations now evaluate RSI → MACD →
Bollinger → volume → 4H, in that order.

Labels: score ≥ `CONFIG.STRONG_BUY_SCORE` (4.5) → **STRONG BUY**, ≥ 2 → BUY, > −2 → HOLD, > −5 → SELL, else **STRONG SELL**. The BUY bar is a config value so it stays in step with `STRONG_BUY_SCORE` in `signal_checker.py`, which is what actually decides trades; the SELL bar stays hard-coded at −5 because this app is long-only and no sell label gates anything.

> **The two implementations are now byte-identical in behaviour, and there is a test for it.**
> Until 2026-08-15 the browser applied volume *after* the 4H term, awarded its point from
> 1.5× rather than 2.0×, gated it on an existing ±2 score, and carried a −1 "selling
> pressure" branch the worker never had. This was documented here as a "minor asymmetry";
> it was not. Across an 8,400-case grid the two disagreed on 257 scores and 80 labels —
> the dashboard could show **STRONG BUY for a coin the worker scored a full point lower
> and would never trade**. `signal_checker.py` is the source of truth; it places the
> orders. If you change one side, change the other and run `python parity_check.py`
> from the repo root — it lifts `generateSignal` straight out of `app.js`, runs it against
> `generate_signal()` over 11,200 cases, and compares scores, labels **and** reason strings.

### 3. Alerts (`checkSignalAlerts()`)

Browser alerts are **toasts only** — Telegram for STRONG BUY is handled exclusively by the Python worker to avoid duplicates. On the first scan after page load all currently-STRONG-BUY coins are silently marked as already-notified (warm-up). A STRONG BUY without 30m reversal confirmation shows a "waiting for reversal" toast with the failing conditions (`reversalWhyFailed`). `sendTelegramAlert()` exists in the code but is currently unused.

### 4. AI Advisor (`runAiAnalysis()`)

- Calls the Anthropic Messages API **directly from the browser** (`anthropic-dangerous-direct-browser-access` header) with model `CONFIG.CLAUDE_MODEL` (`claude-opus-5` — set in `config.js`; this is a static page, so that line is the only place it can come from).
- Before prompting, it fetches the **live** OKX balance and holdings so the AI sees real capital, then builds:
  - `buildSystemPrompt()` — strict trading rules (only recommend BUY with score ≥ 5.0 — deliberately stricter than the 4.5 STRONG BUY label, since this manual path has no automatic size reduction — and ≥ 2 confirmations, explicit SKIP conditions, confidence levels), risk-profile position sizing (conservative 10% / moderate 20% / aggressive 30% of capital), and the **TRADE tag contract** with Option 3 parameter guidance per volatility tier.
  - `buildPrompt()` — per-coin technical snapshot, derivatives context (funding rate, open interest), portfolio with live P&L, news headlines + sentiment %.
- The response is rendered as markdown; any `[TRADE:{...}]` tags are parsed (`parseTradeActions`) into **action buttons** ("⚡ Option 3 Trade · 🟢 BUY AVAX · TP50% +5% · Trail 3% · SL −8%").

### 5. Trade execution (`showTradeConfirmation()` → `executeTrade()`)

Clicking an action button opens a confirmation modal where amount, partial-TP %, trailing %, and SL % are all **editable** with live price previews. Confirming runs the same Option 3 placement sequence as the worker:

1. Spot market **buy** (`tgtCcy: 'quote_ccy'` — size in USDT).
2. **OCO** conditional sell for 50% (TP trigger + SL trigger in one order — one order so OKX reserves the coin balance once; two separate orders would try to reserve 150% of holdings and get rejected).
3. **Trailing stop** (`move_order_stop`) for the other 50%, activation price = TP price.
4. `saveOption3Trade()` inserts the row into Supabase so the Python worker's monitor takes over from there (break-even SL move + exit Telegram messages happen server-side even with the browser closed).

A non-Option-3 `[TRADE]` sell tag falls back to a simple market sell. See [OPTION3-TRADE-SYSTEM.md](OPTION3-TRADE-SYSTEM.md) for the full lifecycle.

### 6. News panel

Three sources fetched **in parallel and merged** (deduped by normalized title, sorted newest-first): the **CryptoCompare News API** (primary — direct JSON, CORS-friendly, no proxy, keyed via `CONFIG.CRYPTOCOMPARE_API_KEY`), **CryptoPanic** (only when `CONFIG.CRYPTOPANIC_API_KEY` is set — its community votes replace regex sentiment for its articles), and two RSS feeds (CoinTelegraph, CoinDesk) via CORS proxies for breadth. Keyword search filters the merged list by topic. `guessSentiment()` regex-classifies non-voted headlines and an aggregate "% bullish" sentiment bar renders on top. Demo articles if every source fails. (The worker separately fetches coin-tagged CryptoCompare headlines for AI trade decisions — see [SIGNAL-CHECKER.md](SIGNAL-CHECKER.md).)

### 7. Fear & Greed (summary bar)

`fetchFearGreed()` shows the market-wide Fear & Greed Index (alternative.me, free/keyless) in the summary bar, color-coded (≤ 40 red, 41–59 amber, ≥ 60 green), refreshed on the news cadence (page load + every 10 min). The worker independently feeds the same index to AI trade decisions.

### 8. Balance sync

`fetchOKXBalance()` merges the **unified trading account** and the **funding account** (on Classic accounts spot coins often sit in Funding), dedupes per coin, and keeps the higher USDT figure. Shown in the summary bar; cached in `localStorage` for instant display on next load.

### 9. Bot Performance panel (header 📊 button — lazy-loaded)

Hidden by default; the bar-chart button in the header slides it in above the scanner. **Nothing is fetched at page load** (coin data stays fast): the first open runs one Supabase query for all closed trades with recorded outcomes (`loadPerfData()`, cached — range switching is instant afterwards; "↻ Refresh data" re-fetches). **Period control:** a stepper (`‹ September 2026 ›`) plus mode buttons **Month / Year / Last 6 months / All**, and custom from→to date pickers. The arrows walk one month (or one year) at a time — by click, or with the ← / → keys while the panel is open — and **disable themselves at the edges of the data**, so you cannot page off into empty periods in either direction. Month and year bounds are built from calendar `Date` parts rather than by subtracting days, so a month means that month however long it is and DST cannot shift an edge; the per-month totals partition thehistory exactly. Opening the panel snaps the anchor to the newest traded month (`perfSnapToData()`), so it never opens blank after a quiet spell. The 7D / 30D / 90D buttons were removed on 2026-09-03 — unused. Shows: **Net P&L after OKX fees** (headline — the real result), before-fees P&L and estimated fees (0.2% round trip on `amount_usdt`), trades W/L, win rate, profit factor, avg win/loss, a cumulative equity-curve SVG, per-coin net table, and exit-type counts. Only trades closed after the P&L-tracking migration appear (older rows have no recorded outcome).

### 10. Portrait-only lock

Three layers: `"orientation": "portrait"` in both manifests (locks installed PWAs on Android), a best-effort `screen.orientation.lock('portrait')` at init, and a CSS overlay (`#rotateOverlay`) that covers the app with a "rotate back" prompt whenever a phone-sized screen goes landscape (`orientation: landscape` + `max-height: 500px` — desktops are unaffected).

### 11. Settings modal

Sections: Supabase config, Claude API key, Telegram (token/chat ID), OKX keys (Read + Trade, no Withdraw), **Cloud Security** (password + "Save All to Cloud" → encrypt to Supabase `app_settings`), scanner export/import JSON, full reset. Risk profile (`aggressive`) and auto-refresh interval (1 minute) are fixed in `config.js` and intentionally absent from the UI.

## Refresh behavior

- Market data auto-refreshes every `CONFIG.AUTO_REFRESH_INTERVAL` (default 60 s), news every 10 min.
- The `visibilitychange` handler **pauses all timers when the tab is hidden** (saves API calls; the worker covers background monitoring) and refreshes immediately on return.

## PWA (`sw.js`, `site.webmanifest`)

Network-first with cache fallback for GET requests; OKX/Anthropic/API calls are never cached. Static assets pre-cached at install (`okx-ai-v1`). This makes the dashboard installable on mobile and usable (read-only, last data) offline.
