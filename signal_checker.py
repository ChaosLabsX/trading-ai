r"""
OKX AI — Background Signal Checker (24/7 mode)
Runs continuously on the VPS (C:\OKXAI, Task Scheduler task "OKX-SignalChecker",
launched via infra/run-okx.ps1 — see infra/VPS-SETUP.md).
Each invocation loops internally for ~4 minutes (one scan every 60 s), then
self-exits and is relaunched immediately by the wrapper, so coverage is gap-free.

Alert rules:
- BUY and STRONG BUY are the same zone — oscillating between them produces NO extra alert.
- SELL and STRONG SELL are the same zone — same rule.
- A new alert fires only when the zone CHANGES (entering BUY zone, flipping to SELL, etc.).
- 2-minute safety cooldown prevents false alerts from rapid back-and-forth oscillation.

Auto-trade (Claude, model set by CLAUDE_MODEL):
- When STRONG BUY + reversal confirmed, Claude decides whether to trade and sets
  parameters (USDT amount, TP%, SL%, trailing%) based on coin volatility + signal strength.
- Claude also sees recent live trade results (from Supabase) so it can skip/downsize
  setups that have been losing.
- If approved: places market buy + OCO (TP/SL on 50%) + conditional SL (other 50%) on
  OKX, so the FULL position is stop-loss-protected server-side 24/7. When the TP fires,
  the monitor swaps the 2nd-half SL for an immediately-active trailing stop.
- Safety rails (production only): BTC regime filter (no dip-buying while BTC is in a
  4H downtrend), max concurrent trades cap, and a daily stop-loss circuit breaker.
- Telegram notification includes "Trade Already Placed on OKX ✅" with full parameters.
- Requires CLAUDE_API_KEY in .env. Auto-trade silently skips if the key is missing.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests

# ── Config ────────────────────────────────────────────────────────────────────
# Coin universe — audited 2026-07-07 against live OKX data (see CHANGELOG).
# Keep in sync with DEFAULT_SCANNER in config.js.
# Criteria: OKX spot live + globally liquid + legitimate project + volatility
# suitable for swing TA. Removed: RUNE/TON (delisted from OKX), FLOKI/WIF
# (meme liquidity collapsed), STRK (unlock dilution), ATOM (structural decline).
SYMBOLS = [
    # Majors
    'BTC-USDT',  'ETH-USDT',  'BNB-USDT',  'SOL-USDT',  'XRP-USDT',
    'ADA-USDT',  'DOGE-USDT', 'TRX-USDT',  'LTC-USDT',  'BCH-USDT',
    'XLM-USDT',
    # L1 / L2 / infrastructure
    'AVAX-USDT', 'SUI-USDT',  'NEAR-USDT', 'APT-USDT',  'TIA-USDT',
    'SEI-USDT',  'OP-USDT',   'ARB-USDT',  'DOT-USDT',  'HBAR-USDT',
    'POL-USDT',  'MON-USDT',  'HYPE-USDT', 'ZEC-USDT',
    # DeFi / AI
    'LINK-USDT', 'UNI-USDT',  'AAVE-USDT', 'LDO-USDT',  'ENA-USDT',
    'ONDO-USDT', 'JUP-USDT',  'INJ-USDT',  'FET-USDT',  'TAO-USDT',
    'WLD-USDT',
    # Memes (high volume + volatility)
    'PEPE-USDT', 'BONK-USDT',
]

OKX_BASE           = 'https://www.okx.com'
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
OKX_API_KEY        = os.environ.get('OKX_API_KEY', '')
OKX_SECRET_KEY     = os.environ.get('OKX_SECRET_KEY', '')
OKX_PASSPHRASE     = os.environ.get('OKX_PASSPHRASE', '')
CLAUDE_API_KEY     = os.environ.get('CLAUDE_API_KEY', '')
# Adaptive thinking — top-tier decision quality for trade sizing and TP/SL/trail
# selection. Only runs in production, only for qualified STRONG BUY candidates,
# max 1 per scan, so the cost stays negligible. learn.py imports this value, so
# both AI calls stay on one model.
#
# THIS LINE IS THE ONLY PLACE THE MODEL VERSION IS NAMED. Everything else — the
# module docstring, the advisor header, learn.py, the docs — says "CLAUDE_MODEL"
# instead of a version number, because prose that restates a constant goes stale
# and this repo has already been bitten by exactly that (the system prompt said
# "slPct 2-12" while the constant said 8; see CHANGELOG 2026-07-29). Change the
# model here and nothing else needs touching.
CLAUDE_MODEL       = 'claude-opus-5'

# CryptoCompare News — free read-only key (news/polling scope only; same key ships
# publicly in config.js). Gives the AI each candidate coin's latest headlines so it
# can veto trades on hacks/lawsuits/delistings that indicators can't see.
CRYPTOCOMPARE_API_KEY = '9b260f1d70267786f07b9fc29fc785dae1f187863c7ae5466ede5e8a6f36b4a9'
CACHE_FILE         = 'signal_cache.json'

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

LOOP_DURATION  = 4 * 60   # seconds of scanning before the process self-exits
                          # (the wrapper relaunches it straight away)
CHECK_INTERVAL = 60        # seconds between scans

EMOJI = {'STRONG BUY': '🟢', 'BUY': '🔵', 'SELL': '🟠', 'STRONG SELL': '🔴'}

# Minimum seconds between any two alerts for the same coin.
# A genuine zone flip within this window is suppressed — rapid oscillation is noise.
FLIP_COOLDOWN  = 2 * 60
OKX_FEE_RATE   = 0.001   # 0.1% taker fee (adjust if your VIP tier is different)

# If a coin stays in the same BUY/SELL zone longer than this, send a reminder alert.
REZONE_REMINDER = 4 * 60 * 60  # 4 hours

# Set to True to force a STRONG BUY alert for BTC on the next run — bypasses all
# market logic so you can confirm scan → Telegram → auto-trade end-to-end.
# Delete C:\OKXAI\signal_cache.json first or the forced signal is suppressed as
# already-alerted, then set this back to False.
TEST_FORCE_SIGNAL = False

# ── TEST MODE ─────────────────────────────────────────────────────────────────
# Makes Option 3 trades trigger EASILY with a tiny fixed size — purely to test the
# trade → monitor → Telegram pipeline end to end. When True it:
#   • lowers the STRONG BUY bar from score ≥ 4.5 to score ≥ 1
#   • skips the 30-min reversal confirmation gate
#   • skips the Claude AI advisor and uses a fixed size + fixed TP/SL/trail
#   • keeps up to TEST_MAX_CONCURRENT (3) test trades alive at once
#
# ►►►  TO RESTORE NORMAL (PRODUCTION) BEHAVIOUR: set TEST_MODE = False  ◄◄◄
#      That one line reverts everything — all production values are preserved below.
TEST_MODE = False

# Test bar of 1.0 fires on very common conditions (e.g. bullish MACD trend +0.5
# plus price near the lower Bollinger Band +1) so test trades start quickly.
#
# PRODUCTION BAR: 5.0 → 4.5 on 2026-08-15, to raise trade frequency. Measured, not
# guessed — see the CHANGELOG entry of that date. Three things make 4.5 the right
# step and 5.0 a worse one than it looks:
#
#   1. THE SCALE IS A STAIRCASE, NOT A DIAL. generate_signal() only awards whole
#      and half points, so scores land on half-point values. Over 81,610 scored
#      coin-hours (38 coins × 90 days), exactly 26 landed on 5.0 and 938 on 5.5+.
#      A 5.0 bar therefore behaves as a 5.5 bar. There is no 4.7 or 4.8 to try;
#      4.5 is the next real step, and it admits exactly one new bucket.
#   2. IT ROUGHLY DOUBLES THE TRADES AT INDISTINGUISHABLE P&L. Replaying the real
#      rules over four NON-overlapping 84-day windows of the past year, with both
#      safety gates on: score 5.0 → 39 trades / −9.18 net, score 4.5 → 88 trades /
#      −10.15 net (2 of 4 windows profitable either way). Per trade that is −$0.24
#      vs −$0.12. Both are still losses; this buys evidence at roughly twice the
#      rate, it does not buy an edge.
#   3. THE NEW BUCKET SIZES ITSELF SMALLER. The advisor's sizing table already
#      carried an unreachable "Score 4.5–4.9 → 15–20% of capital" tier below the
#      "5.0+ → 20–30%" one. Lowering the bar activates it, so the weaker setups
#      this admits are automatically sized ~25% lighter than the ones traded today.
#
# What was tested and rejected: loosening btc_regime_ok() or reversal_confirmed()
# (both lose money in every window — they are carrying the strategy), and a tighter
# ATR_SL_MULT (wins only in the most recent window; fitted, not real).
STRONG_BUY_SCORE  =  1.0 if TEST_MODE else  4.5   # score needed to label STRONG BUY
# Deliberately NOT mirrored to -4.5. This worker is long-only spot: STRONG SELL
# never places, closes or blocks a trade — direction_zone() maps both SELL and
# STRONG SELL to the same 'down' zone, so the label is display text only. Moving it
# would change what the dashboard prints and nothing else.
STRONG_SELL_SCORE = -1.0 if TEST_MODE else -5.0   # score needed to label STRONG SELL
MIN_TRADE_USDT    = 10.0                           # balance gate: no trading below this USDT balance (test & prod)

# Performance-weighted position caps, as a fraction of AVAILABLE USDT at decision
# time. Defined here rather than inline so the advisor prompt can interpolate the
# SAME values it is being held to — the prompt used to restate "30% / 22% / 15%"
# in prose beside a code path that no longer used those numbers, which is the
# defect class that had Claude reasoning about an slPct bound the code did not
# have (2026-07-29). Ordered worst-record-first; see the cap_pct block for why
# "no record yet" must sit at the bottom of this ladder and not the top.
CAP_PF_POOR   = 0.45   # profit factor < 1.0
CAP_PF_FAIR   = 0.50   # profit factor 1.0–1.5  (also: unknown PF, win rate >= 50%)
CAP_PF_GOOD   = 0.55   # profit factor 1.5–2.0
CAP_PF_STRONG = 0.60   # profit factor >= 2.0

# Volume confirmation required before a STRONG BUY is TRADED (not before it is
# labelled — see run_scan). Added 2026-08-15 alongside the 45–60% position sizes:
# with half the account behind a trade, refusing the weakest entries is worth more
# than the extra trades it costs.
#
# Measured over four independent 84-day windows at score >= 4.5, both gates on:
#
#   requirement        Dec-05    Feb-27    May-22    Aug-14     total   trades
#   none               -14.92     +6.85    +18.87    -20.95    -10.15       88
#   vol >= 1.75x        -2.81     +0.11    +13.71     -8.99     +2.02       58
#   vol >= 2.00x        -2.81     +0.27    +13.71     -7.52     +3.65       56
#   vol >= 2.50x        -1.18     +0.01    +16.70    -13.85     +1.68       40
#
# It trims BOTH tails — smaller losses in the two down windows, smaller gains in
# the two up windows — and nets positive because it removes more bad than good.
# Two reasons to trust this more than the tighter-stop result that was rejected on
# 2026-08-15: the sign does not flip by window (the direction is the same in all
# four), and the whole 1.75–2.50 neighbourhood is positive rather than one lucky
# point. It is still a MODEST effect, roughly +0.18 per trade, and well inside
# noise on its own — it is adopted because the mechanism is principled (the engine
# already demands volume >= 1x average on the 30m reversal candle, and the scoring
# table already pays +1 for >= 2x) and because position sizes just tripled.
#
# To revert: set to 0.0. That restores the pre-2026-08-15 behaviour exactly.
MIN_VOL_RATIO_TRADE = 2.0
# Tight, cheap test parameters: trades resolve within minutes-to-hours instead of
# hours-to-days, and a worst-case (SL) test costs ≈ $0.10 + ~$0.01 fees.
# Each half-position sell is ≈ $2.50 — still above OKX minimum order sizes for
# every coin in SYMBOLS (largest minimum is BTC: 0.00001 BTC per order).
TEST_TRADE_USDT   =  5.0                           # fixed trade size used in TEST_MODE
TEST_TP_PCT       =  1.5                           # fixed take-profit % in TEST_MODE
TEST_SL_PCT       =  2.0                           # fixed stop-loss %  in TEST_MODE
TEST_TRAIL_PCT    =  1.0                           # fixed trailing %   in TEST_MODE

if TEST_MODE:
    # Shorten the anti-spam suppression so test trades re-trigger promptly
    # (production keeps the 4h reminder / 2min flip cooldown defined above).
    REZONE_REMINDER = 10 * 60   # 10 minutes instead of 4 hours
    FLIP_COOLDOWN   = 30        # 30 seconds instead of 2 minutes


# ── ATR + market-structure exits (production AI baseline) ─────────────────────
# Exits are sized in units of the coin's live volatility (ATR = average candle
# range) and anchored to nearby support/resistance, instead of fixed guesses.
ATR_TP_MULT    = 2.0            # partial TP = 2.0 × ATR above entry
ATR_SL_MULT    = 2.5            # stop loss  = 2.5 × ATR below entry (outside noise)
ATR_TRAIL_MULT = 1.0            # trailing   = 1.0 × ATR callback
TP_BOUNDS      = (1.5, 10.0)    # absolute % clamps whatever ATR/AI says
# The SL floor was raised to 8.0 on 2026-07-29 and reverted the same day. Both
# the change and the reversal are recorded here because the reasoning is the
# useful part.
#
# The case for widening: over 59 historical RSI<=30 + lower-BB signals, a -2.8%
# stop was hit on 24% of them, and most of those recovered afterwards. True, and
# still true. But that test only asked "does price reach +2% before -X%". It
# modelled none of the machinery that decides a real trade's P&L — the 50%
# partial TP, the trailing stop on the remainder, fees, or the BTC regime gate
# that blocks ~88% of signals before they are ever traded.
#
# Replaying the full engine over 90 days x 38 coins (scripts: backtest.py):
#
#     SL_BOUNDS      net P&L    PF     max DD   avg hold
#     (2, 12)         -6.50    0.63     11.27      26h
#     (8, 12)         -9.86    0.60     16.38      59h
#     no stop        -44.53    0.22     56.99     296h
#
# Wider stop, worse result. The reason is structural: wins exit on a partial TP
# plus a trail and average roughly +2, while an 8% stop loses -8.19. That is
# ~1:4 against, needing an ~80% win rate to break even where the replay got 70%.
# Widening a stop without widening the target does not give a trade room; it
# just makes every loss bigger.
#
# So: back to 2.0. Not because 10 trades prove 2 beats 8 — at that sample the
# gap is noise — but because the evidence used to justify the change did not
# survive the full replay, and absent a good reason to change, don't.
#
# The no-stop row is the one result here that is NOT sample-size dependent:
# 3 of 8 positions never exited, average age 30.5 days, marked to market at
# -56.99. That is a mechanism (capital locked while the scanner finds signals it
# cannot take), not a coin flip. Removing the stop also silently freezes the
# learning loop, since a trade that never closes never gets an exit_reason and
# so is never graded.
#
# Standing caveat: every configuration above LOSES money over those 90 days,
# PF 0.60-0.63. Stop placement is not the binding problem. Treat the strategy as
# unproven and keep sizes small.
SL_BOUNDS      = (2.0, 12.0)
TRAIL_BOUNDS   = (1.0, 5.0)
SR_TP_GAP_PCT  = 0.5            # sell this far below the nearest resistance
SR_SL_GAP_PCT  = 0.75           # stop this far below the nearest support

# ── Derivatives context ───────────────────────────────────────────────────────
FUNDING_HARD_SKIP_PCT = 0.10    # 8h funding above this → longs dangerously crowded, auto-skip

# ── Limit-order entries (maker-first, market fallback) ───────────────────────
LIMIT_ENTRY_OFFSET_PCT = 0.05   # place the buy 0.05% below market
LIMIT_ENTRY_WAIT_SEC   = 45     # cancel + market-buy fallback after this wait

# ── Option 3 order types ─────────────────────────────────────────────────────
# The partial-TP order carries BOTH a TP and an SL leg, which on OKX requires
# ordType='oco'. With ordType='conditional' OKX accepts the request but performs
# only the stop-loss logic and silently ignores the take-profit — leaving an
# SL-only order that can never take profit or reach phase 2.
# Trades placed before 2026-07-15 carry 'conditional' OCOs, so algo-history
# lookups for an OCO id try 'oco' first and fall back to 'conditional'.
OCO_ORD_TYPE  = 'oco'
OCO_ORD_TYPES = ('oco', 'conditional')

# ── Daily Telegram digest (heartbeat + performance report) ───────────────────
# Fires on the first run after this UTC hour, once per day. The message doubles
# as a dead-man switch: if it stops arriving, the pipeline is down.
DIGEST_UTC_HOUR = 8

# ── Trade journal (the learning loop) ────────────────────────────────────────
# Every trade AND every AI skip is stored with the market picture it was decided
# on; this many hours later the worker looks at what price actually did and
# records a verdict. Outcomes alone cannot teach: a stop-loss that was a shakeout
# and one that saved you from a crash are identical in a P&L column but imply
# opposite fixes. The graded history is fed back into the next AI decision.
JOURNAL_FOLLOWUP_HOURS = 24
JOURNAL_GRADE_BATCH    = 5      # rows graded per run — keeps the API cost trivial
JOURNAL_MIN_SAMPLES    = 10     # below this the prompt calls the history anecdotal

# A PATTERN line tells the model to CHANGE a money-affecting parameter, so it has
# to clear a significance bar first. Earlier this block fired on `if shakeouts:` —
# one shakeout in thirty trades emitted "consider a wider slPct". That is noise
# wearing the word PATTERN, and it is exactly the failure the sister MT5 project's
# research log is a monument to: across 48+ backtests it found FEWER significant
# results than chance predicts, so acting on thin evidence manufactures the false
# positive that later gets funded.
#
# The bar: the Wilson 95% lower bound on the observed rate must sit above a null
# rate that a well-tuned bot would show anyway. Some stops SHOULD get shaken out;
# the question is never "did this happen" but "does it happen more than normal".
# Counts are still reported either way — only the directive is gated, so the model
# keeps the data and loses only the instruction to act on too little of it.
JOURNAL_PATTERN_NULL_RATE = 0.15   # unremarkable background rate for a verdict class
JOURNAL_PATTERN_Z         = 1.96   # 95%, matching the CI convention used in backtests


def _wilson_lower(k, n, z=JOURNAL_PATTERN_Z):
    """
    Lower bound of the Wilson score interval for k successes in n trials.

    Wilson rather than the textbook normal approximation because it stays sane at
    the small n and extreme proportions this journal actually operates at (3 of 8),
    where the normal interval famously runs off the end of [0, 1). Hand-rolled and
    dependency-free on purpose — the worker's only dependency is `requests`.
    """
    if n <= 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, centre - margin)


def _pattern_is_real(k, n):
    """True when k-of-n is too frequent to be the background rate — the gate a
    prescriptive PATTERN line must pass. The absolute floor matters independently:
    without it 5-of-5 clears the bound, and five trades is an anecdote no matter
    how lopsided it looks."""
    return n >= JOURNAL_MIN_SAMPLES and _wilson_lower(k, n) > JOURNAL_PATTERN_NULL_RATE


# ── Zone helpers ──────────────────────────────────────────────────────────────
def direction_zone(label):
    if label in ('STRONG BUY', 'BUY'):   return 'up'
    if label in ('STRONG SELL', 'SELL'): return 'down'
    return 'neutral'


# ── OKX public data ───────────────────────────────────────────────────────────
def fetch_candles(symbol, bar='1H', limit=100):
    url = f'{OKX_BASE}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}'
    r   = requests.get(url, timeout=15)
    r.raise_for_status()
    d   = r.json()
    if d['code'] != '0' or not d.get('data'):
        return None
    rows = list(reversed(d['data']))
    return {
        'opens':   [float(c[1]) for c in rows],
        'highs':   [float(c[2]) for c in rows],
        'lows':    [float(c[3]) for c in rows],
        'closes':  [float(c[4]) for c in rows],
        'volumes': [float(c[5]) for c in rows],
    }


def fetch_candles_window(symbol, start_ms, hours=24, bar='15m'):
    """
    Candles covering [start_ms, start_ms + hours] — what price did after a moment
    in the past. OKX's `after` returns records EARLIER than the given ts, so anchor
    at the END of the window and let it page backwards, landing on exactly the
    window we want. Returns None if the data isn't available.
    """
    end_ms = int(start_ms) + int(hours * 3600 * 1000)
    limit  = max(1, min(100, int(hours * 60 // 15)))
    try:
        r = requests.get(
            f'{OKX_BASE}/api/v5/market/candles?instId={symbol}&bar={bar}'
            f'&after={end_ms}&limit={limit}',
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        if d.get('code') != '0' or not d.get('data'):
            return None
        rows = [c for c in reversed(d['data']) if int(c[0]) >= int(start_ms)]
        if not rows:
            return None
        return {
            'highs':  [float(c[2]) for c in rows],
            'lows':   [float(c[3]) for c in rows],
            'closes': [float(c[4]) for c in rows],
        }
    except Exception as e:
        print(f'  [Journal] {symbol}: follow-up candle fetch failed: {e}')
        return None


def fetch_ticker(symbol):
    url = f'{OKX_BASE}/api/v5/market/ticker?instId={symbol}'
    r   = requests.get(url, timeout=15)
    r.raise_for_status()
    d   = r.json()
    if d['code'] != '0' or not d.get('data'):
        return None
    t = d['data'][0]
    last, open24 = float(t['last']), float(t['open24h'])
    return {'price': last, 'change_pct': (last - open24) / open24 * 100 if open24 else 0}


# ── Technical indicators ──────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: avg_gain += d
        else:     avg_loss -= d
    avg_gain /= period
    avg_loss /= period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    return 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)


def ema_array(vals, period):
    k, out = 2 / (period + 1), [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def calc_macd(closes):
    if len(closes) < 35:
        return None
    ema12, ema26 = ema_array(closes, 12), ema_array(closes, 26)
    ml = [a - b for a, b in zip(ema12[25:], ema26[25:])]
    sl = ema_array(ml, 9)
    n  = len(ml) - 1
    return {
        'trend':         'bullish' if ml[n] > sl[n] else 'bearish',
        'bullish_cross': n > 0 and ml[n - 1] < sl[n - 1] and ml[n] >= sl[n],
        'bearish_cross': n > 0 and ml[n - 1] > sl[n - 1] and ml[n] <= sl[n],
    }


def calc_bb(closes, period=20):
    if len(closes) < period:
        return None
    sl   = closes[-period:]
    mean = sum(sl) / period
    std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
    upper, lower = mean + 2 * std, mean - 2 * std
    return {'pct_b': (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5}


def calc_vol_ratio(volumes):
    if len(volumes) < 21:
        return None
    avg = sum(volumes[-21:-1]) / 20
    return volumes[-1] / avg if avg > 0 else None


def calc_atr_pct(highs, lows, closes, period=14):
    """
    ATR(14) as a percentage of the last price — the coin's average candle range,
    i.e. how much it 'breathes' per candle. Wilder smoothing.
    """
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return (atr / closes[-1]) * 100 if closes[-1] > 0 else None


def find_support_resistance(highs, lows, closes, wing=3):
    """
    Nearest swing-low support below the current price and swing-high resistance
    above it. A swing point is a candle that is the extreme of its ±`wing`
    neighbours. Returns (support, resistance) — either may be None.
    """
    n = len(closes)
    if n < wing * 2 + 5:
        return None, None
    price = closes[-1]
    supports, resistances = [], []
    for i in range(wing, n - wing):
        if highs[i] >= max(highs[i - wing:i + wing + 1]) and highs[i] > price:
            resistances.append(highs[i])
        if lows[i] <= min(lows[i - wing:i + wing + 1]) and lows[i] < price:
            supports.append(lows[i])
    return (max(supports) if supports else None,
            min(resistances) if resistances else None)


def suggest_exit_params(atr_pct, support, resistance, price):
    """
    Volatility-adaptive exits anchored to market structure:
      baseline = ATR multiples (SL outside noise, TP within realistic reach),
      then TP is pulled just below the nearest resistance and SL pushed just
      below the nearest support when those levels are meaningfully placed.
    Returns {'tp','sl','trail'} in %, or None when there's no ATR data.
    """
    if not atr_pct or atr_pct <= 0 or not price or price <= 0:
        return None
    tp    = ATR_TP_MULT    * atr_pct
    sl    = ATR_SL_MULT    * atr_pct
    trail = ATR_TRAIL_MULT * atr_pct
    if resistance:
        res_dist = (resistance / price - 1) * 100        # % above entry
        if 1.5 <= res_dist <= 12.0:
            tp = min(tp, res_dist - SR_TP_GAP_PCT)       # sell just before the ceiling
    if support:
        sup_dist = (1 - support / price) * 100           # % below entry
        if 2.0 <= sup_dist <= 12.0:
            sl = max(sl, sup_dist + SR_SL_GAP_PCT)       # exit only if the floor breaks
    tp    = min(max(tp, TP_BOUNDS[0]), TP_BOUNDS[1])
    sl    = min(max(sl, SL_BOUNDS[0]), SL_BOUNDS[1])
    # trail must stay below TP — preserves the phase-2 break-even guarantee
    trail = max(TRAIL_BOUNDS[0], min(trail, TRAIL_BOUNDS[1], tp - 0.5))
    return {'tp': round(tp, 2), 'sl': round(sl, 2), 'trail': round(trail, 2)}


def fetch_funding_oi(symbol):
    """Funding rate (as %) and open interest from the coin's perpetual swap market."""
    swap = symbol.replace('-USDT', '-USDT-SWAP')
    funding = oi = None
    try:
        d = requests.get(f'{OKX_BASE}/api/v5/public/funding-rate?instId={swap}', timeout=10).json()
        if d.get('code') == '0' and d.get('data'):
            funding = float(d['data'][0]['fundingRate']) * 100
    except Exception:
        pass
    try:
        d = requests.get(f'{OKX_BASE}/api/v5/public/open-interest?instId={swap}', timeout=10).json()
        if d.get('code') == '0' and d.get('data'):
            oi = float(d['data'][0]['oiCcy'])
    except Exception:
        pass
    return funding, oi


def fetch_orderbook_imbalance(symbol, depth=20):
    """Bid/ask volume ratio over the top N order-book levels. >1 = buy-side depth."""
    try:
        d = requests.get(f'{OKX_BASE}/api/v5/market/books?instId={symbol}&sz={depth}', timeout=10).json()
        if d.get('code') == '0' and d.get('data'):
            book    = d['data'][0]
            bid_vol = sum(float(b[1]) for b in book.get('bids', []))
            ask_vol = sum(float(a[1]) for a in book.get('asks', []))
            if ask_vol > 0:
                return bid_vol / ask_vol
    except Exception:
        pass
    return None


def fetch_fear_greed():
    """
    Crypto Fear & Greed Index (alternative.me — free, no key). Market-wide mood
    0–100: low = panic (contrarian dip-buy conditions), high = euphoria (late-
    cycle risk). Returns (value, label) or (None, '').
    """
    try:
        d   = requests.get('https://api.alternative.me/fng/?limit=1', timeout=10).json()
        row = (d.get('data') or [{}])[0]
        return int(row.get('value')), row.get('value_classification', '')
    except Exception:
        return None, ''


def fetch_coin_news(symbol, limit=5):
    """
    Latest headlines tagged with this coin (CryptoCompare News API — every article
    carries coin tags, so the filter is server-side). Formatted for the AI prompt;
    returns '' when no news or the API is unavailable — never blocks a trade.
    """
    coin = symbol.replace('-USDT', '')
    try:
        r = requests.get(
            'https://min-api.cryptocompare.com/data/v2/news/',
            params={'lang': 'EN', 'categories': coin, 'api_key': CRYPTOCOMPARE_API_KEY},
            timeout=10,
        )
        arts = r.json().get('Data') or []
        now  = time.time()
        lines = []
        for a in arts:
            # The API pads thin categories with general news — keep only articles
            # whose own tags (or title) genuinely mention this coin.
            tags  = (a.get('categories') or '').upper().split('|')
            title = (a.get('title') or '').strip()
            if not title:
                continue
            if coin.upper() not in tags and coin.upper() not in title.upper():
                continue
            age_h = max(0.0, (now - float(a.get('published_on') or now)) / 3600)
            lines.append(f'- [{age_h:.0f}h ago] {title}')
            if len(lines) >= limit:
                break
        return '\n'.join(lines)
    except Exception as e:
        print(f'  [News] {symbol}: fetch failed: {e}')
        return ''


def reversal_confirmed(opens, closes, volumes, zone):
    """
    Guards against alerting into a falling knife (BUY) or rising knife (SELL).
    Uses 30min candle data for earlier entry detection, falls back to 1H.
    Requires ALL three on the most recent candle:
      BUY  → green candle AND RSI turning up AND volume ≥ 1× avg
      SELL → red candle  AND RSI turning down AND volume ≥ 1× avg
    """
    if len(closes) < 16 or len(opens) < 2:
        return True
    rsi_now  = calc_rsi(closes)
    rsi_prev = calc_rsi(closes[:-1])
    if rsi_now is None or rsi_prev is None:
        return True
    vol_ok = True
    if volumes and len(volumes) >= 21:
        avg_vol = sum(volumes[-21:-1]) / 20
        vol_ok  = avg_vol > 0 and volumes[-1] >= avg_vol * 1.0
    if zone == 'up':
        return closes[-1] >= opens[-1] and rsi_now >= rsi_prev and vol_ok
    if zone == 'down':
        return closes[-1] <= opens[-1] and rsi_now <= rsi_prev and vol_ok
    return True


def generate_signal(rsi, macd, bb, vol_ratio=None, rsi_4h=None):
    score, reasons = 0.0, []

    if rsi is not None:
        if   rsi <= 20: score += 3; reasons.append(f'RSI {rsi:.0f} — extremely oversold')
        elif rsi <= 30: score += 2; reasons.append(f'RSI {rsi:.0f} — oversold')
        elif rsi <= 40:             reasons.append(f'RSI {rsi:.0f} — below neutral')   # no score bonus
        elif rsi >= 80: score -= 3; reasons.append(f'RSI {rsi:.0f} — extremely overbought')
        elif rsi >= 70: score -= 2; reasons.append(f'RSI {rsi:.0f} — overbought')
        elif rsi >= 60: score -= 1; reasons.append(f'RSI {rsi:.0f} — above neutral')

    if macd is not None:
        if   macd['bullish_cross']: score += 2; reasons.append('MACD bullish crossover')
        elif macd['bearish_cross']: score -= 2; reasons.append('MACD bearish crossover')
        elif macd['trend'] == 'bullish': score += 0.5
        else:                            score -= 0.5

    if bb is not None:
        if   bb['pct_b'] <= 0.05: score += 2; reasons.append('Price at lower Bollinger Band')
        elif bb['pct_b'] <= 0.20: score += 1; reasons.append('Price near lower BB')
        elif bb['pct_b'] >= 0.95: score -= 2; reasons.append('Price at upper Bollinger Band')
        elif bb['pct_b'] >= 0.80: score -= 1; reasons.append('Price near upper BB')

    if vol_ratio is not None:
        if   vol_ratio >= 2.0: score += 1; reasons.append(f'Volume {vol_ratio:.1f}× avg — strong buying interest')
        elif vol_ratio >= 1.5:             reasons.append(f'Volume {vol_ratio:.1f}× avg — elevated')

    # 4H RSI confirmation — mirrors the browser dashboard logic (max ±1 point).
    #
    # The first two branches used to be labelled "higher-TF uptrend/downtrend
    # confirmed". Both were backwards: rsi_4h <= 40 is the 4H being WEAK, not
    # trending up, and rsi_4h >= 55 is the 4H being STRONG, not trending down.
    # That text is not cosmetic — the reasons list is pasted verbatim into the
    # AI prompt as "Confirmed by: ...", so the model was being told the higher
    # timeframe agreed when it said the opposite. FET/POL/ADA all carried
    # "4H RSI 34 — higher-TF uptrend confirmed" into their entry decisions on
    # 2026-07-29 and all three stopped out.
    #
    # Only the labels changed. The +1/-1 is deliberately left alone: whether
    # stacked-oversold is confluence or a falling knife is an empirical
    # question, and rsi_4h is recorded in every entry snapshot so the journal
    # can answer it around 30 graded trades. Do not flip the sign on a hunch.
    if rsi_4h is not None:
        if   score > 0 and rsi_4h <= 40:
            score += 1;   reasons.append(f'4H RSI {rsi_4h:.0f} — 4H oversold as well')
        elif score < 0 and rsi_4h >= 55:
            score -= 1;   reasons.append(f'4H RSI {rsi_4h:.0f} — 4H still elevated')
        elif score > 0 and rsi_4h >= 70:
            score -= 0.5; reasons.append(f'4H RSI {rsi_4h:.0f} — caution: overbought on 4H')
        elif score < 0 and rsi_4h <= 30:
            score += 0.5; reasons.append(f'4H RSI {rsi_4h:.0f} — caution: oversold on 4H')

    label = ('STRONG BUY'  if score >= STRONG_BUY_SCORE  else
             'BUY'         if score >= 2  else
             'STRONG SELL' if score <= STRONG_SELL_SCORE else
             'SELL'        if score <= -2 else 'HOLD')
    return {'label': label, 'score': score, 'reasons': reasons}


def _returns(closes):
    """
    Bar-to-bar returns. A non-positive prior close yields 0.0 rather than being
    dropped — dropping would shorten one series and silently misalign the two
    being compared, which is worse than one dead bar.
    """
    out = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        out.append((closes[i] / prev - 1) if prev > 0 else 0.0)
    return out


def _pearson(a, b):
    """Pearson r over two equal-length series. Hand-rolled — this worker has no numpy."""
    n = len(a)
    if n < 2 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va  = sum((x - ma) ** 2 for x in a)
    vb  = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:      # a flat series has no correlation to measure
        return None
    return cov / math.sqrt(va * vb)


def _downside_corr(a_closes, b_closes):
    """
    Correlation of two coins on the bars that matter — the ones where they fell.

    Computed in both directions (bars where A fell, bars where B fell) and
    reported as the max, so the answer does not depend on which coin happens to
    be held and which is the candidate. Returns None when neither direction has
    CORRELATION_MIN_DOWN bars to work with.
    """
    n = min(len(a_closes), len(b_closes))
    ra, rb = _returns(a_closes[-n:]), _returns(b_closes[-n:])

    best = None
    for ref, other in ((ra, rb), (rb, ra)):
        idx = [i for i, v in enumerate(ref) if v < 0]
        if len(idx) < CORRELATION_MIN_DOWN:
            continue
        r = _pearson([ref[i] for i in idx], [other[i] for i in idx])
        if r is not None and (best is None or r > best):
            best = r
    return best


def correlation_block(symbol, closes_by_symbol, active_symbols):
    """
    Is this candidate just another copy of a position we already hold?

    Returns (blocking_symbol, r) when downside correlation reaches
    CORRELATION_MAX against some open position, else (None, highest_r_seen).

    Fails OPEN when a series is missing or too short: MAX_OPEN_TRADES is still
    the hard cap, and a coin leaving the watchlist must not halt trading. Every
    fail-open prints, so "no correlation data" can never pass silently. Series
    are the same scan's 1H candles, so bars line up; a gap on one side would
    understate r, which errs toward allowing the trade — the same direction as
    the explicit fail-open.
    """
    mine = closes_by_symbol.get(symbol)
    if not mine or len(mine) < CORRELATION_MIN_BARS:
        print(f'  [Corr] {symbol}: no usable candle history — guard skipped')
        return None, None

    worst_sym, worst_r = None, None
    for other in sorted(active_symbols):
        theirs = closes_by_symbol.get(other)
        if not theirs or len(theirs) < CORRELATION_MIN_BARS:
            print(f'  [Corr] {other}: open position has no candle history — not compared')
            continue
        r = _downside_corr(mine, theirs)
        if r is None:
            print(f'  [Corr] {symbol} vs {other}: too few down bars to judge — not compared')
            continue
        if worst_r is None or r > worst_r:
            worst_sym, worst_r = other, r

    if worst_r is not None and worst_r >= CORRELATION_MAX:
        return worst_sym, worst_r
    return None, worst_r


def btc_regime_ok():
    """
    BTC regime filter: the signal engine buys oversold dips, which bleeds money when
    the whole market is trending down (alts are ~80% correlated with BTC). Block new
    buys only when BTC's higher timeframe is clearly bearish:
        price below the 4H EMA-50  AND  4H RSI < 45.
    Fails OPEN (allows trading) if BTC data can't be fetched — logged loudly.
    Returns (ok, reason_string).
    """
    try:
        c = fetch_candles('BTC-USDT', bar='4H', limit=100)
        if not c or len(c['closes']) < 60:
            return True, 'BTC 4H data unavailable — regime check skipped'
        closes = c['closes']
        price  = closes[-1]
        ema50  = ema_array(closes, 50)[-1]
        rsi4h  = calc_rsi(closes)
        if price < ema50 and rsi4h is not None and rsi4h < 45:
            return False, (f'BTC ${price:,.0f} < 4H EMA50 ${ema50:,.0f} and '
                           f'4H RSI {rsi4h:.0f} — bearish regime, dip-buys blocked')
        rsi_s = f'{rsi4h:.0f}' if rsi4h is not None else 'N/A'
        return True, f'BTC ${price:,.0f} vs 4H EMA50 ${ema50:,.0f}, RSI {rsi_s} — OK to trade'
    except Exception as e:
        return True, f'regime check error ({e}) — allowing trades'


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_price(p):
    return f'${p:,.2f}' if p >= 10000 else f'${p:.4f}' if p >= 1 else f'${p:.6f}'


def fmt_usdt(v):
    """Signed USDT amount, e.g. +$1.23 / −$0.0450 (4 decimals for tiny values)."""
    sign = '+' if v >= 0 else '−'
    a = abs(v)
    return f'{sign}${a:.2f}' if a >= 0.10 else f'{sign}${a:.4f}'


def _exit_pnl(entry_px, fill_px, sz):
    """
    Calculate net P&L and fee breakdown for a single exit (one half-position).
    Returns (net_pnl, total_fees, buy_fee, sell_fee) all in USDT.
    buy_fee  = proportional share of the original entry fee for this quantity.
    sell_fee = OKX fee charged on this exit.
    net_pnl  = gross price gain minus both fees.
    """
    gross_pnl  = (fill_px - entry_px) * sz
    buy_fee    = entry_px * sz * OKX_FEE_RATE
    sell_fee   = fill_px  * sz * OKX_FEE_RATE
    total_fees = buy_fee + sell_fee
    net_pnl    = gross_pnl - total_fees
    return net_pnl, total_fees, buy_fee, sell_fee


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('  [Telegram] credentials not set — skipping.')
        return
    r = requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
        json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
        timeout=15,
    )
    print(f'  [Telegram] {"sent OK" if r.status_code == 200 else f"error {r.status_code}"}')


def format_alert(symbol, sig, ticker, trade_result=None):
    """
    Build the Telegram message for a signal alert.
    trade_result: None = auto-trade not configured
                  dict  = trade placed successfully (contains amount_usdt, tp_pct, etc.)
                  'skip'  = Claude decided not to trade
                  'error' = trade placement failed
    """
    coin    = symbol.replace('-USDT', '')
    emoji   = EMOJI.get(sig['label'], '⚪')
    reasons = ' · '.join(sig['reasons']) if sig['reasons'] else 'Multiple indicators aligned'
    score   = sig['score']
    msg = (
        f"{emoji} <b>{sig['label']}: {coin}</b>  [{score:+.1f}]\n"
        f"💰 {fmt_price(ticker['price'])} ({ticker['change_pct']:+.2f}% 24h)\n"
        f"📊 {reasons}"
    )
    if isinstance(trade_result, dict):
        msg += (
            f"\n\n✅ <b>Trade Already Placed on OKX</b>\n"
            f"💵 ${trade_result['amount_usdt']:.2f} USDT\n"
            f"🎯 TP +{trade_result['tp_pct']}%  ·  "
            f"🛡️ SL −{trade_result['sl_pct']}%  ·  "
            f"🔄 Trail {trade_result['trail_pct']}%"
        )
        if not trade_result.get('saved', True):
            msg += (
                f"\n⚠️ <b>NOT saved to tracking DB</b> — break-even SL move & exit "
                f"alerts are OFF for this trade. Fix Supabase secrets (see Actions log)."
            )
    elif trade_result == 'skip':
        msg += '\n\n⏭️ <i>AI skipped — setup not optimal right now</i>'
    elif trade_result == 'error':
        msg += '\n\n⚠️ <i>Auto-trade failed — place manually on OKX if desired</i>'
    elif trade_result == 'cap':
        msg += '\n\n⏩ <i>Signal qualified but ranked below the top pick this scan — no trade placed</i>'
    return msg


# ── Cache ─────────────────────────────────────────────────────────────────────
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            # Migrate old format {symbol: label_str} → {symbol: dict}
            result = {}
            for k, v in data.items():
                if isinstance(v, str):
                    old_zone = direction_zone(v)
                    result[k] = {
                        'label':        v,
                        'alerted_zone': old_zone,
                        'alerted_at':   time.time() - FLIP_COOLDOWN,
                    }
                else:
                    result[k] = v
            return result
        except Exception:
            pass
    return {}


def save_cache(data):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# ── OKX private API ───────────────────────────────────────────────────────────
def _okx_sign(method, path, body=None):
    ts  = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    pre = ts + method + path + (json.dumps(body, separators=(',', ':')) if body else '')
    sig = base64.b64encode(
        hmac.new(OKX_SECRET_KEY.encode(), pre.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        'OK-ACCESS-KEY':        OKX_API_KEY,
        'OK-ACCESS-SIGN':       sig,
        'OK-ACCESS-TIMESTAMP':  ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type':         'application/json',
    }


def _okx_get(path):
    r = requests.get(OKX_BASE + path, headers=_okx_sign('GET', path), timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get('code') != '0':
        raise Exception(f"OKX {d.get('code')}: {d.get('msg', '')}")
    return d


def _okx_post(path, body):
    # Must send the exact same compact JSON that was used to compute the HMAC signature.
    # requests' json= kwarg adds spaces which would produce a different byte sequence → 401.
    body_str = json.dumps(body, separators=(',', ':'))
    r = requests.post(
        OKX_BASE + path,
        headers=_okx_sign('POST', path, body),
        data=body_str,
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    if d.get('code') != '0':
        raise Exception(f"OKX {d.get('code')}: {d.get('msg', '')}")
    return d


def _fetch_usdt_balance():
    """Return available USDT balance from OKX spot account."""
    try:
        d = _okx_get('/api/v5/account/balance?ccy=USDT')
        for item in d.get('data', [{}])[0].get('details', []):
            if item.get('ccy') == 'USDT':
                return float(item.get('availBal', 0) or 0)
    except Exception as e:
        print(f'  [OKX] Balance fetch error: {e}')
    return 0.0


# ── Claude — AI trade advisor (model: CLAUDE_MODEL) ──────────────────────────
def ai_trade_params(symbol, sig, ticker, usdt_balance, rsi_1h, rsi_4h, macd_data, bb_data, vol_ratio,
                    extra=None, evidence_out=None):
    """
    Ask Claude (CLAUDE_MODEL, adaptive thinking) whether this STRONG BUY is worth trading
    and what parameters to use. `extra` carries the rich decision context built by
    _build_trade_context(): ATR, support/resistance, suggested exits, funding rate,
    open interest, order-book imbalance, BTC regime, and the coin's latest headlines.
    Returns (params_dict, None) on TRADE, or (None, reason) on SKIP — the reason is
    logged to the journal so skips can be graded later like trades.

    Pass a dict as `evidence_out` to receive the provenance of the decision: how much
    history the prompt carried, which PATTERN directives fired, whether the distilled
    learn.py block was injected. Stored on the resulting trade or skip so the feedback
    loop's own effect can be measured later rather than assumed.
    """
    coin     = symbol.replace('-USDT', '')
    rsi_1h_s = f'{rsi_1h:.1f}' if rsi_1h is not None else 'N/A'
    rsi_4h_s = f'{rsi_4h:.1f}' if rsi_4h is not None else 'N/A'

    if macd_data:
        if macd_data['bullish_cross']:        macd_s = 'Bullish crossover ✓'
        elif macd_data['bearish_cross']:      macd_s = 'Bearish crossover'
        elif macd_data['trend'] == 'bullish': macd_s = 'Bullish trend'
        else:                                 macd_s = 'Bearish trend'
    else:
        macd_s = 'N/A'

    if bb_data:
        zone_s = 'oversold zone' if bb_data['pct_b'] < 0.2 else 'overbought zone' if bb_data['pct_b'] > 0.8 else 'mid-range'
        bb_s   = f'{bb_data["pct_b"] * 100:.0f}% B — {zone_s}'
    else:
        bb_s = 'N/A'

    vol_s = f'{vol_ratio:.1f}× average' if vol_ratio is not None else 'N/A'

    history, pf, ev_trades = _trade_history_context(symbol)
    history_s   = f'\n\nTRADE JOURNAL — this bot\'s real closed trades:\n{history}' if history else ''
    skips, ev_skips = _skip_history_context(symbol)
    skips_s     = f'\n\nYOUR PAST SKIPS (graded {JOURNAL_FOLLOWUP_HOURS}h later):\n{skips}' if skips else ''
    # Distilled statistics over the FULL history (the recent-30 journal above only
    # sees a rolling window). Empty unless the learning pass has run AND injection
    # has been deliberately enabled (LEARN_INJECT=1) — see learn.py.
    try:
        from learn import _learned_rules_context
        learned = _learned_rules_context()
    except Exception:
        learned = ''
    learned_s   = f'\n\nLEARNED FROM FULL HISTORY (auto-distilled statistics):\n{learned}' if learned else ''

    # Performance-weighted sizing: shrink the hard cap when the system is cold.
    #
    # `pf` is None until 30 closed trades exist. This block used to treat that as
    # the BEST case and hand out the full cap — so the guard was disabled for
    # exactly the first 30 trades, the stretch where the bot has least evidence
    # that it works at all. On 2026-08-02 the live record was 13 trades at PF 0.32
    # and the cap in force was 30%.
    #
    # An unknown profit factor is not a good profit factor. When there is no PF
    # yet, fall back to the win rate over whatever trades DO exist, and only use
    # the optimistic default when there is genuinely nothing to go on.
    #
    # LADDER RAISED 15/22/30 → 45/50/55/60 on 2026-08-15 at the owner's direction,
    # to put more capital behind each trade. Read this before changing it again:
    #
    #   • The SHAPE is the safety property, not the numbers. Worse record → smaller
    #     bet, and "no record yet" sits at the BOTTOM of the ladder, never the top.
    #     That is the whole point of the 2026-08-02 fix. A future edit that flattens
    #     these four values into one constant silently restores the bug that had a
    #     30% cap running against a live PF of 0.32.
    #   • These are caps on AVAILABLE USDT at decision time, not on starting equity,
    #     so concurrent trades compound down rather than over-allocating: at 60%,
    #     three open trades deploy 60% → 24% → 9.6% ≈ 94% of capital. At the old 30%
    #     it was ≈ 66%. Near-full deployment is the intended effect of this change
    #     and also its main new risk — MAX_OPEN_TRADES (3) is now what stands
    #     between the account and being all-in.
    #   • The measured backing at the time of the change: 18 closed live trades,
    #     9W/9L, PF 1.32, net +$1.11 — but −$0.96 with the single largest winner
    #     removed, and every backtested configuration over four independent 84-day
    #     windows was net negative. Sizing multiplies whatever the edge is; it does
    #     not create one. Nothing here has demonstrated a positive expectancy yet.
    cap_pct = CAP_PF_STRONG
    if pf is not None:
        if pf < 1.0:
            cap_pct = CAP_PF_POOR
        elif pf < 1.5:
            cap_pct = CAP_PF_FAIR
        elif pf < 2.0:
            cap_pct = CAP_PF_GOOD
    elif ev_trades.get('journal_n'):
        # Below 30 trades: an all-loss or mostly-loss record still shrinks the cap.
        # Deliberately coarse — this is a risk cap, not a tuned parameter.
        cap_pct = CAP_PF_POOR if ev_trades.get('journal_win_rate', 1.0) < 0.5 else CAP_PF_FAIR

    # Provenance of THIS decision: exactly which evidence the model was shown.
    # Filled through an out-parameter rather than a third return value on purpose —
    # this function has six return points and a scar from the last arity change
    # (see the `no text block` branch below, where a bare `return None` took down a
    # whole run). Widening all six is the same trap; filling a dict is not.
    if evidence_out is not None:
        evidence_out.update({
            **ev_trades, **ev_skips,
            'learned_injected': bool(learned),
            'cap_pct':          cap_pct,
            'model':            CLAUDE_MODEL,
        })

    # Market structure & derivatives context (ATR, S/R, funding, OI, order book, BTC regime)
    extra = extra or {}
    price = ticker['price']
    struct_lines = []
    if extra.get('atr_pct') is not None:
        struct_lines.append(f"ATR(14) 1H: {extra['atr_pct']:.2f}% of price (avg candle range)")
    if extra.get('support'):
        struct_lines.append(f"Nearest support: {fmt_price(extra['support'])} "
                            f"({(1 - extra['support'] / price) * 100:.1f}% below)")
    if extra.get('resistance'):
        struct_lines.append(f"Nearest resistance: {fmt_price(extra['resistance'])} "
                            f"({(extra['resistance'] / price - 1) * 100:.1f}% above)")
    if extra.get('exits'):
        e = extra['exits']
        struct_lines.append(f"SUGGESTED EXITS (ATR + structure): TP +{e['tp']}%  ·  "
                            f"SL −{e['sl']}%  ·  trail {e['trail']}%")
    if extra.get('funding_pct') is not None:
        struct_lines.append(f"Funding rate (8h): {extra['funding_pct']:+.4f}%")
    if extra.get('oi') is not None:
        struct_lines.append(f"Open interest: {extra['oi']:,.0f} coins")
    if extra.get('book_ratio') is not None:
        struct_lines.append(f"Order book bid/ask ratio (top 20 levels): {extra['book_ratio']:.2f}")
    if extra.get('regime'):
        struct_lines.append(f"BTC regime: {extra['regime']}")
    fg_val, fg_label = extra.get('fear_greed') or (None, '')
    if fg_val is not None:
        struct_lines.append(f"Fear & Greed Index: {fg_val}/100 — {fg_label}")
    struct_s = ('\n\nMARKET STRUCTURE & DERIVATIVES:\n' + '\n'.join(struct_lines)) if struct_lines else ''

    news_s = f"\n\nRECENT NEWS for {coin} (newest first):\n{extra['news']}" if extra.get('news') else ''

    system = f"""You are an expert crypto trading advisor for OKX spot markets (no leverage, no futures).
A STRONG BUY signal has been confirmed with reversal on 30-minute candle. Decide if this trade is worth placing and output the optimal Option 3 parameters.

CAPITAL & POSITION SIZING:
Available USDT: ${usdt_balance:.2f}
- Score {STRONG_BUY_SCORE:g}–4.9, 2–3 confirmations → 45–50% of capital
- Score 5.0–5.9, 3+ confirmations → 50–55% of capital
- Score 6.0+, 3+ confirmations → 55–60% of capital
({STRONG_BUY_SCORE:g} is the lowest score that reaches you — anything weaker is filtered out
before this prompt, so treat a {STRONG_BUY_SCORE:g} as the WEAKEST setup you will ever see,
not as a middling one, and size it at the bottom of its band.)
These are large fractions of the whole account and they are deliberate. Because the
cap applies to AVAILABLE USDT rather than starting equity, taking the top of a band
leaves little room for the next signal — so spend the top of a band on genuine
confluence, not on a setup that merely cleared the bar.
PERFORMANCE-WEIGHTED CAP: the hard cap for THIS trade is {cap_pct * 100:.0f}% of capital
({CAP_PF_STRONG * 100:.0f}% at profit factor ≥ 2.0, {CAP_PF_GOOD * 100:.0f}% at 1.5–2.0, {CAP_PF_FAIR * 100:.0f}% at 1.0–1.5,
{CAP_PF_POOR * 100:.0f}% below 1.0 — losing streaks get smaller bets, and an UNKNOWN profit
factor is treated as a weak one, not a good one). Never exceed it. Minimum $10 USDT.

EXIT PARAMETERS — volatility-adaptive (ATR) + market structure:
The market data includes SUGGESTED EXITS computed from the coin's live ATR(14) and
nearby support/resistance (TP just below the ceiling, SL just below the floor and
outside normal noise). Rules:
- Start from the suggested TP/SL/trail; adjust within ±30% only when the data justifies it
- Stronger setups (score ≥ 5, deep 1H+4H oversold, MACD cross) → push TP toward the upper end
- Absolute bounds: partialTpPct {TP_BOUNDS[0]:g}–{TP_BOUNDS[1]:g}, slPct {SL_BOUNDS[0]:g}–{SL_BOUNDS[1]:g}, trailingCallbackPct {TRAIL_BOUNDS[0]:g}–{TRAIL_BOUNDS[1]:g} (values outside are clamped, so stay inside)
- trailingCallbackPct must stay BELOW partialTpPct (protects the break-even guarantee)

DERIVATIVES & ORDER BOOK RULES:
- Funding +0.05% to +0.10%: longs crowded — cut position size by 50%
- Funding negative: short-squeeze fuel — TP may go up to 1% higher
- Order-book bid/ask ratio > 1.3: buy-side support; < 0.7: weak book — halve size or SKIP
(Funding above +{FUNDING_HARD_SKIP_PCT}% is auto-skipped before you are consulted.)

NEWS RULES (RECENT NEWS section, when provided):
- Hack, exploit, lawsuit, SEC enforcement, delisting, or insolvency headlines → SKIP
  regardless of how good the indicators look (the "dip" has a reason)
- Clearly negative news-flow → halve size or SKIP; a strong positive catalyst supports
  normal-to-upper sizing
- No headlines for the coin is NEUTRAL — never penalize missing news

MARKET SENTIMENT (Fear & Greed Index, when provided):
- ≤ 25 (Extreme Fear): panic pricing — favorable contrarian dip-buy conditions; upper-end
  sizing is allowed when the other signals agree
- ≥ 75 (Extreme Greed): euphoric late-cycle market — cut size by 25–50% and favor a tighter TP

DO NOT TRADE ([SKIP]) if:
- RSI 4H > 65 (higher timeframe overbought — bad risk/reward entry)
- Fewer than 2 indicator confirmations in the reasons list
- Available USDT < $10

TRADE JOURNAL (when provided) — this bot's real results. Each past trade lists the
conditions it was ENTERED on and what price did AFTER the exit. Use it:
- SHAKEOUT verdict = the stop sat inside normal noise and price recovered to our target
  anyway → widen slPct on setups like that one (this is a losing trade worth learning from)
- GOOD_SAVE verdict = the stop correctly avoided a deeper fall → keep that stop distance
- LEFT_MONEY verdict = price ran well past our exit → widen trailingCallbackPct
- This coin repeatedly stopping out on similar conditions → require a stronger setup or SKIP
- Overall results negative → size toward the LOWER end of the capital range
- YOUR PAST SKIPS: MISSED_WIN means you were too cautious in those conditions;
  GOOD_SKIP means the caution was right. Weigh both — refusing good trades is also a mistake.
EVIDENCE DISCIPLINE: when the journal shows fewer than {JOURNAL_MIN_SAMPLES} closed trades it
is anecdote, not statistics. Make only obvious, small parameter corrections; never blacklist a
coin or jump position size over one or two results. Reason from the CONDITIONS, not the coin name.
Only a line beginning "PATTERN:" is actionable. Those have already passed a statistical test in
code (the 95% lower bound on the rate clears the level a well-tuned bot shows anyway). A bare
count — "2 of 14 graded trades were shakeouts" — is CONTEXT, not an instruction: it did not clear
that bar, and retuning on it would be fitting noise. Do not treat a raw count as a weak PATTERN
and split the difference; treat it as no evidence at all.

Respond with EXACTLY ONE line — nothing else:
Trade:  [TRADE:{{"side":"buy","symbol":"{symbol}","amountUsdt":0,"partialTpPct":0,"trailingCallbackPct":0,"slPct":0}}]
Skip:   [SKIP: one-line reason]"""

    user_msg = f"""STRONG BUY confirmed — {coin}

Price: {fmt_price(ticker['price'])} ({ticker['change_pct']:+.2f}% 24h)
Signal score: {sig['score']:+.1f}
Confirmed by: {', '.join(sig['reasons']) if sig['reasons'] else 'multiple indicators'}

RSI 1H:  {rsi_1h_s}
RSI 4H:  {rsi_4h_s}
MACD:    {macd_s}
BB:      {bb_s}
Volume:  {vol_s}
USDT available: ${usdt_balance:.2f}{struct_s}{news_s}{history_s}{skips_s}{learned_s}

Place this trade?"""

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         CLAUDE_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      CLAUDE_MODEL,
                # Adaptive thinking: Opus reasons internally before answering.
                # Thinking tokens count against max_tokens, so leave headroom.
                # Raised 2000 -> 8000: the answer is one line, but at 2000 the
                # reasoning could consume the budget and return no text block —
                # the exact failure the handler below was written for.
                'max_tokens': 8000,
                'thinking':   {'type': 'adaptive'},
                'system':     system,
                'messages':   [{'role': 'user', 'content': user_msg}],
            },
            timeout=60,
        )
        r.raise_for_status()
        # With thinking enabled the first content block can be a thinking block —
        # pick the text block explicitly instead of assuming content[0].
        blocks = r.json().get('content', [])
        text   = next((b.get('text', '') for b in blocks if b.get('type') == 'text'), '').strip()
        if not text:
            # Must stay a 2-tuple: the caller does `params, skip_reason = ...`, so a
            # bare `return None` raised TypeError and killed the whole run
            # (before monitor_option3_trades), leaving open trades untracked that
            # cycle. Most likely trigger: adaptive thinking consuming max_tokens and
            # leaving no text block. Treat it as a skip, not a crash.
            print(f'  [Claude] {coin}: no text block in response — skipping')
            return None, 'no text block in AI response (thinking may have consumed max_tokens)'
        print(f'  [Claude] {coin}: {text[:150]}')

        # Parse TRADE tag
        m = re.search(r'\[TRADE:(\{.*?\})\]', text, re.DOTALL)
        if m:
            p      = json.loads(m.group(1))
            amount = float(p.get('amountUsdt', 0))
            if amount < 10:
                print(f'  [Claude] Amount too small (${amount:.2f}) — skipping')
                return None, f'AI sized it at ${amount:.2f}, below the $10 minimum'
            # Safety cap: performance-weighted (30% / 22% / 15% by profit factor)
            cap = usdt_balance * cap_pct
            if amount > cap:
                print(f'  [Claude] Amount ${amount:.2f} exceeds {cap_pct * 100:.0f}% cap — capped at ${cap:.2f}')
                amount = cap
            # Hard bounds + break-even invariant (trail < TP), whatever the AI said
            tp    = min(max(float(p.get('partialTpPct', 5)),        TP_BOUNDS[0]),    TP_BOUNDS[1])
            sl    = min(max(float(p.get('slPct', 7)),               SL_BOUNDS[0]),    SL_BOUNDS[1])
            trail = max(TRAIL_BOUNDS[0],
                        min(float(p.get('trailingCallbackPct', 3)), TRAIL_BOUNDS[1], round(tp - 0.5, 2)))
            return {
                'amount_usdt':    round(amount, 2),
                'partial_tp_pct': round(tp, 2),
                'trailing_pct':   round(trail, 2),
                'sl_pct':         round(sl, 2),
            }, None

        # Parse SKIP tag
        if re.search(r'\[SKIP', text, re.IGNORECASE):
            m2 = re.search(r'\[SKIP[:\s]*(.*?)\]', text, re.IGNORECASE)
            reason = m2.group(1).strip() if m2 else 'no reason given'
            print(f'  [Claude] SKIP — {reason}')
            return None, reason

        print(f'  [Claude] Unexpected response format — skipping trade')
        return None, 'unexpected AI response format'

    except Exception as e:
        print(f'  [Claude] API error: {e}')
        return None, f'AI call failed: {e}'


def _build_trade_context(cand, regime_msg):
    """
    Rich decision context for one trade candidate (called only for the top
    candidates, so the extra API calls stay tiny): ATR, support/resistance,
    suggested exits, funding rate, open interest, order-book imbalance,
    BTC regime, and the coin's latest headlines.
    """
    symbol = cand['symbol']
    price  = cand['ticker']['price']
    highs, lows, closes = cand.get('highs', []), cand.get('lows', []), cand.get('closes', [])
    atr_pct             = calc_atr_pct(highs, lows, closes)
    support, resistance = find_support_resistance(highs, lows, closes)
    funding, oi         = fetch_funding_oi(symbol)
    book                = fetch_orderbook_imbalance(symbol)
    return {
        'atr_pct':     atr_pct,
        'support':     support,
        'resistance':  resistance,
        'exits':       suggest_exit_params(atr_pct, support, resistance, price),
        'funding_pct': funding,
        'oi':          oi,
        'book_ratio':  book,
        'regime':      regime_msg,
        'news':        fetch_coin_news(symbol),
        'fear_greed':  fetch_fear_greed(),
    }


# ── Trade journal: capture, grade, recall ────────────────────────────────────
def _rnd(v, n):
    """Round unless None — keeps snapshot JSON compact without a guard at every field."""
    return round(float(v), n) if v is not None else None


def _sb_headers(extra=None):
    h = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}
    h.update(extra or {})
    return h


def _build_entry_snapshot(cand, extra=None, params=None, evidence=None):
    """
    Freeze the market picture a decision was made on. Costs nothing extra — every
    value here was already computed to make the call, and is otherwise thrown away
    — but without it a closed trade records only THAT it lost, never the conditions
    that produced the loss, which is precisely what turns a loss into a lesson.

    `evidence` records the second half of that story: not what the MARKET looked
    like, but what the model was TOLD about its own past when it decided. Without
    it the journal can never answer the only question that validates it — whether
    decisions made with more history turned out better than decisions made with
    less. Every trade would be evidence about the market and none about the loop.
    """
    sig  = cand.get('sig') or {}
    macd = cand.get('macd_data') or {}
    bb   = cand.get('bb_data') or {}
    snap = {
        'score':      _rnd(sig.get('score'), 2),
        'reasons':    sig.get('reasons') or [],
        'rsi_1h':     _rnd(cand.get('rsi_1h'), 1),
        'rsi_4h':     _rnd(cand.get('rsi_4h'), 1),
        'macd_trend': macd.get('trend'),
        'macd_cross': bool(macd.get('bullish_cross')),
        'bb_pct_b':   _rnd(bb.get('pct_b'), 3),
        'vol_ratio':  _rnd(cand.get('vol_ratio'), 2),
        'change_24h': _rnd((cand.get('ticker') or {}).get('change_pct'), 2),
    }
    if extra:
        fg_val, fg_label = extra.get('fear_greed') or (None, '')
        snap.update({
            'atr_pct':     _rnd(extra.get('atr_pct'), 2),
            'funding_pct': _rnd(extra.get('funding_pct'), 4),
            'book_ratio':  _rnd(extra.get('book_ratio'), 2),
            'support':     extra.get('support'),
            'resistance':  extra.get('resistance'),
            'fear_greed':  fg_val,
            'fg_label':    fg_label or None,
            'btc_regime':  extra.get('regime'),
            'suggested':   extra.get('exits'),
            'had_news':    bool(extra.get('news')),
        })
    if params:
        snap['chosen'] = {
            'amount_usdt': params.get('amount_usdt'),
            'tp_pct':      params.get('partial_tp_pct'),
            'sl_pct':      params.get('sl_pct'),
            'trail_pct':   params.get('trailing_pct'),
        }
    if evidence:
        snap['evidence'] = evidence
    return {k: v for k, v in snap.items() if v is not None and v != [] and v != ''}


def _log_skipped_setup(symbol, price, reason, snapshot):
    """
    Record a setup the AI declined. Mistakes come in two kinds — trades that lost,
    and trades never taken that would have won. A skip otherwise vanishes without
    a trace, so the AI can never learn it is being too cautious (or rightly careful).
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/skipped_setups',
            headers=_sb_headers({'Content-Type': 'application/json', 'Prefer': 'return=minimal'}),
            json={'symbol': symbol, 'price': price,
                  'reason': (reason or 'no reason given')[:300], 'entry_context': snapshot},
            timeout=10,
        )
        if r.status_code in (200, 201, 204):
            print(f'  [Journal] {symbol}: skip logged for grading in {JOURNAL_FOLLOWUP_HOURS}h ✓')
        else:
            print(f'  [Journal] skip log failed (HTTP {r.status_code}) — is the '
                  f'skipped_setups table created? See docs/ARCHITECTURE.md')
    except Exception as e:
        print(f'  [Journal] skip log error: {e}')


def _patch_journal(table, row_id, payload):
    try:
        requests.patch(
            f'{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}',
            headers=_sb_headers({'Content-Type': 'application/json', 'Prefer': 'return=minimal'}),
            json=payload, timeout=10,
        )
    except Exception as e:
        print(f'  [Journal] patch error ({table} {row_id}): {e}')


def _grade_exit(exit_reason, exit_px, tp_px, c):
    """
    Verdict on a closed trade: what did price do after we left? This is where a
    losing trade earns its keep — 'shakeout' and 'good_save' are both stop-losses
    but they teach opposite lessons, and P&L alone cannot tell them apart.
    """
    peak, trough, last = max(c['highs']), min(c['lows']), c['closes'][-1]
    up   = (peak / exit_px - 1) * 100
    down = (trough / exit_px - 1) * 100

    if exit_reason in ('sl', 'tp_then_sl'):
        if tp_px and peak >= tp_px:
            v, note = 'shakeout', 'price reached our original TP after stopping us out — SL sat inside the noise'
        elif up >= 2.0:
            v, note = 'partial_recovery', 'price bounced after our stop — SL may be slightly tight'
        elif down <= -3.0:
            v, note = 'good_save', 'price kept falling — the stop did its job'
        else:
            v, note = 'flat_after_stop', 'price went nowhere after the stop'
    elif exit_reason in ('tp_trail', 'break_even'):
        if up >= 3.0:
            v, note = 'left_money', 'price ran well past our exit — trail was too tight'
        elif down <= -3.0:
            v, note = 'well_timed', 'price dropped after we exited — good timing'
        else:
            v, note = 'fair_exit', 'price drifted after our exit'
    else:
        return None
    return {'verdict': v, 'note': note, 'hours': JOURNAL_FOLLOWUP_HOURS,
            'peak_pct': round(up, 2), 'trough_pct': round(down, 2),
            'end_pct': round((last / exit_px - 1) * 100, 2)}


def _grade_skip(price, tp_pct, sl_pct, c):
    """
    Would the declined setup have won? Walk the candles in order and see which of
    the suggested exits would have triggered first. SL wins an ambiguous candle —
    the same conservative assumption backtest.py makes.
    """
    tp_px, sl_px = price * (1 + tp_pct / 100), price * (1 - sl_pct / 100)
    peak = (max(c['highs']) / price - 1) * 100
    trough = (min(c['lows']) / price - 1) * 100
    base = {'hours': JOURNAL_FOLLOWUP_HOURS, 'peak_pct': round(peak, 2),
            'trough_pct': round(trough, 2), 'tp_pct': tp_pct, 'sl_pct': sl_pct}
    for h, l in zip(c['highs'], c['lows']):
        if l <= sl_px:
            return {**base, 'verdict': 'good_skip',
                    'note': f'would have hit the −{sl_pct}% stop — correctly avoided'}
        if h >= tp_px:
            return {**base, 'verdict': 'missed_win',
                    'note': f'would have hit the +{tp_pct}% target — too cautious here'}
    return {**base, 'verdict': 'neutral_skip', 'note': 'neither target nor stop was reached'}


def grade_journal_followups():
    """
    Grade trades that closed (and setups skipped) JOURNAL_FOLLOWUP_HOURS ago by
    looking at what price actually did next. Each row is graded exactly once.
    Silently inactive until the journal migration is run (docs/ARCHITECTURE.md).
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=JOURNAL_FOLLOWUP_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Closed trades ────────────────────────────────────────────────────────
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/option3_trades'
            f'?phase=eq.3&followup_at=is.null&exit_reason=not.is.null'
            f'&closed_at=not.is.null&closed_at=lt.{cutoff}&limit={JOURNAL_GRADE_BATCH}'
            f'&select=id,symbol,entry_price,exit_price,exit_reason,partial_tp_pct,closed_at',
            headers=_sb_headers(), timeout=10,
        )
        if r.status_code == 200:
            for t in r.json():
                entry_px = float(t.get('entry_price') or 0)
                exit_px  = float(t.get('exit_price') or 0)
                if entry_px <= 0 or exit_px <= 0:
                    _patch_journal('option3_trades', t['id'],
                                   {'followup': {'verdict': 'ungradable', 'note': 'no exit price recorded'},
                                    'followup_at': now_iso})
                    continue
                closed_ms = int(datetime.fromisoformat(t['closed_at']).timestamp() * 1000)
                c = fetch_candles_window(t['symbol'], closed_ms, JOURNAL_FOLLOWUP_HOURS)
                if not c:
                    continue   # try again next run
                tp_px = entry_px * (1 + float(t.get('partial_tp_pct') or 0) / 100)
                v = _grade_exit(t['exit_reason'], exit_px, tp_px, c)
                if v:
                    _patch_journal('option3_trades', t['id'], {'followup': v, 'followup_at': now_iso})
                    print(f"  [Journal] {t['symbol']} {t['exit_reason']} → {v['verdict']} ({v['note']})")
        elif r.status_code != 200:
            print(f'  [Journal] trade grading inactive (HTTP {r.status_code}) — run the SQL migration')
    except Exception as e:
        print(f'  [Journal] trade grading error: {e}')

    # ── Skipped setups ───────────────────────────────────────────────────────
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/skipped_setups'
            f'?followup_at=is.null&created_at=lt.{cutoff}&limit={JOURNAL_GRADE_BATCH}'
            f'&select=id,symbol,price,entry_context,created_at',
            headers=_sb_headers(), timeout=10,
        )
        if r.status_code != 200:
            return
        for s in r.json():
            px = float(s.get('price') or 0)
            if px <= 0:
                _patch_journal('skipped_setups', s['id'],
                               {'followup': {'verdict': 'ungradable'}, 'followup_at': now_iso})
                continue
            created_ms = int(datetime.fromisoformat(s['created_at']).timestamp() * 1000)
            c = fetch_candles_window(s['symbol'], created_ms, JOURNAL_FOLLOWUP_HOURS)
            if not c:
                continue
            sug = (s.get('entry_context') or {}).get('suggested') or {}
            v = _grade_skip(px, float(sug.get('tp') or 3.0), float(sug.get('sl') or 4.0), c)
            _patch_journal('skipped_setups', s['id'], {'followup': v, 'followup_at': now_iso})
            print(f"  [Journal] {s['symbol']} skip → {v['verdict']} ({v['note']})")
    except Exception as e:
        print(f'  [Journal] skip grading error: {e}')


# ── Option 3 auto-trade placement ────────────────────────────────────────────
def _save_option3_trade(trade_data):
    """Persist Option 3 trade to Supabase so the monitor can track it. Returns True on success."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print('  [Supabase] URL or key not set — trade NOT saved (monitor cannot track it). '
              'Check SUPABASE_URL / SUPABASE_KEY in .env.')
        return False
    try:
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/option3_trades',
            headers={
                'apikey':        SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'Content-Type':  'application/json',
                'Prefer':        'resolution=merge-duplicates,return=minimal',
            },
            json=trade_data,
            timeout=10,
        )
        if r.status_code in (200, 201, 204):
            print(f'  [Supabase] Trade saved ✓ (id={trade_data.get("id")})')
            return True
        # If an optional column doesn't exist yet (migration not run), retry without
        # them so the trade is at least tracked. Losing the journal or 2nd-half
        # tracking is bad; losing the whole row would be worse.
        optional = [c for c in ('sl2_id', 'entry_context') if c in trade_data]
        if optional:
            slim = {k: v for k, v in trade_data.items() if k not in optional}
            r2 = requests.post(
                f'{SUPABASE_URL}/rest/v1/option3_trades',
                headers={
                    'apikey':        SUPABASE_KEY,
                    'Authorization': f'Bearer {SUPABASE_KEY}',
                    'Content-Type':  'application/json',
                    'Prefer':        'resolution=merge-duplicates,return=minimal',
                },
                json=slim,
                timeout=10,
            )
            if r2.status_code in (200, 201, 204):
                print(f'  [Supabase] Trade saved WITHOUT {", ".join(optional)} — run the SQL '
                      f'migration in docs/ARCHITECTURE.md to enable full tracking + journal!')
                return True
        # Loud failure: exact status + body + which keys we sent, so the log pinpoints the cause
        print(f'  [Supabase] Save FAILED: HTTP {r.status_code} — {r.text[:300]}')
        print(f'  [Supabase] Payload keys sent: {list(trade_data.keys())}')
        return False
    except Exception as e:
        print(f'  [Supabase] Save error: {e}')
        return False


def _fetch_instrument_spec(symbol):
    """Return (tickSz, lotSz, minSz) as strings for a spot instrument, or (None, None, None)."""
    try:
        d = requests.get(
            f'{OKX_BASE}/api/v5/public/instruments?instType=SPOT&instId={symbol}',
            timeout=10,
        ).json()
        if d.get('code') == '0' and d.get('data'):
            it = d['data'][0]
            return it.get('tickSz'), it.get('lotSz'), it.get('minSz')
    except Exception as e:
        print(f'  [Trade] {symbol}: instrument spec fetch failed: {e}')
    return None, None, None


def _fmt_step(value, step_str):
    """Round value DOWN to the instrument's step size and format to match it."""
    step     = float(step_str)
    decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
    return f'{math.floor(value / step) * step:.{decimals}f}'


def _limit_entry_buy(symbol, amt_usdt, price):
    """
    Maker-first entry: limit buy LIMIT_ENTRY_OFFSET_PCT below market — cheaper
    maker fee and no spread cost. Cancels and reports back for the market-order
    fallback after LIMIT_ENTRY_WAIT_SEC (accounting for partial fills, including
    the race where the order fills during cancellation).
    Returns (filled_coins, avg_px) — (0.0, None) when nothing filled.
    """
    tick, lot, _ = _fetch_instrument_spec(symbol)
    if not tick or not lot:
        return 0.0, None
    limit_px_s = _fmt_step(price * (1 - LIMIT_ENTRY_OFFSET_PCT / 100), tick)
    sz_s       = _fmt_step(amt_usdt / float(limit_px_s), lot)
    if float(sz_s) <= 0 or float(limit_px_s) <= 0:
        return 0.0, None
    try:
        resp   = _okx_post('/api/v5/trade/order', {
            'instId': symbol, 'tdMode': 'cash', 'side': 'buy',
            'ordType': 'limit', 'px': limit_px_s, 'sz': sz_s,
        })
        ord_id = resp.get('data', [{}])[0].get('ordId', '')
    except Exception as e:
        print(f'  [Trade] {symbol}: limit entry rejected ({e}) — using market buy')
        return 0.0, None
    print(f'  [Trade] {symbol}: limit buy {sz_s} @ {limit_px_s} — waiting up to {LIMIT_ENTRY_WAIT_SEC}s for a maker fill...')

    deadline = time.time() + LIMIT_ENTRY_WAIT_SEC
    while time.time() < deadline:
        time.sleep(5)
        try:
            order = _okx_get(f'/api/v5/trade/order?instId={symbol}&ordId={ord_id}').get('data', [{}])[0]
            if order.get('state') == 'filled':
                px = float(order.get('avgPx') or limit_px_s)
                print(f'  [Trade] {symbol}: limit entry filled @ {px} ✓ (maker fee + spread saved)')
                return float(order.get('accFillSz') or sz_s), px
        except Exception:
            pass

    # Timeout — cancel, then re-check (the cancel can race a fill / partial fill)
    try:
        _okx_post('/api/v5/trade/cancel-order', {'instId': symbol, 'ordId': ord_id})
    except Exception:
        pass
    try:
        order   = _okx_get(f'/api/v5/trade/order?instId={symbol}&ordId={ord_id}').get('data', [{}])[0]
        fill_sz = float(order.get('accFillSz', '0') or 0)
        avg_px  = float(order.get('avgPx', '0') or 0)
        if fill_sz > 0 and avg_px > 0:
            state = 'filled during cancel' if order.get('state') == 'filled' else 'partially filled'
            print(f'  [Trade] {symbol}: limit entry {state} — {fill_sz} @ {avg_px}')
            return fill_sz, avg_px
    except Exception:
        pass
    print(f'  [Trade] {symbol}: limit entry not filled in {LIMIT_ENTRY_WAIT_SEC}s — falling back to market buy')
    return 0.0, None


class Option3Preflight(Exception):
    """
    Trade rejected BEFORE any order was placed — no money was spent, nothing to
    clean up. Distinct from a mid-flight failure, which can leave real coins on
    the books and must never be reported as a harmless skip.
    """


def _abort_unprotected(symbol, sz_coin, algo_ids, reason):
    """
    The entry filled but a protective order was rejected — the position is live
    with NO stop loss. Never leave it that way silently: cancel whatever did get
    placed, sell the coins straight back, and tell the user either way.
    Returns a short note for the raised exception.
    """
    coin = symbol.replace('-USDT', '')
    for aid in [a for a in algo_ids if a]:
        _cancel_algo(symbol, aid)

    sold = False
    try:
        _okx_post('/api/v5/trade/order', {
            'instId': symbol, 'tdMode': 'cash', 'side': 'sell',
            'ordType': 'market', 'sz': f'{sz_coin * 0.9985:.8f}',
        })
        sold = True
        print(f'  [Option3] {symbol}: position unwound after protection failure ✓')
    except Exception as e:
        print(f'  [Option3] {symbol}: EMERGENCY — unwind sell failed: {e}')

    if sold:
        send_telegram(
            f"🚨 <b>Trade Aborted — {coin}</b>\n"
            f"⚠️ OKX rejected the protective orders, so the position was sold "
            f"straight back — you are flat (cost ≈ fees only).\n"
            f"📋 {reason}"
        )
    else:
        send_telegram(
            f"🚨 <b>URGENT — Unprotected {coin} Position</b>\n"
            f"⚠️ The buy filled, OKX rejected the protective orders, and the "
            f"automatic sell-back ALSO failed.\n"
            f"👉 You are holding ~{sz_coin:.8f} {coin} with NO stop loss — "
            f"close it manually on OKX now.\n"
            f"📋 {reason}"
        )
    return 'position unwound' if sold else 'UNWIND FAILED — manual action needed'


def place_option3_trade(symbol, params, ticker, snapshot=None):
    """
    Execute a full Option 3 trade on OKX:
      0. Pre-flight: both halves must clear OKX's minimum order size (see below)
      1. Entry: maker-first limit buy slightly below market (cheaper fee, no
         spread), with a market-buy fallback so the signal is never missed
      2. OCO — TP + SL on first 50% (single order avoids balance reservation issue)
      3. Conditional SL on remaining 50% at the same trigger price — the FULL position
         is stop-loss-protected server-side 24/7. The monitor swaps this for an
         immediately-active trailing stop once the partial TP fills.
      4. Save to Supabase for monitoring

    Raises Option3Preflight (nothing bought) or Exception (see _abort_unprotected)
    so the caller can send the appropriate Telegram message.
    Returns a dict with trade summary on success.
    """
    price     = ticker['price']
    amt_usdt  = params['amount_usdt']
    tp_pct    = params['partial_tp_pct']
    sl_pct    = params['sl_pct']
    trail_pct = params['trailing_pct']

    # 0. Pre-flight — the position is sold in two halves, so EACH half must clear
    #    the instrument's minSz. Checked before a single order goes out: OKX
    #    accepts the buy and only rejects the protective orders afterwards, which
    #    would strand an unprotected position (this stranded two real HYPE buys —
    #    $10 buys 0.16 HYPE, but each half is 0.08 vs HYPE's 0.1 minimum).
    _, _, min_sz = _fetch_instrument_spec(symbol)
    if min_sz:
        min_viable = float(min_sz) * price * 2 / 0.9985
        if amt_usdt < min_viable:
            raise Option3Preflight(
                f'${amt_usdt:.2f} is too small for {symbol}: each half would be '
                f'{amt_usdt / price * 0.5 * 0.9985:.8f} vs OKX minimum {min_sz} — '
                f'needs ${min_viable:.2f}+. No order placed.'
            )

    # 1. Entry — maker-first limit buy, market fallback (cuts fees + slippage ~half)
    filled_coins, limit_avg = _limit_entry_buy(symbol, amt_usdt, price)
    filled_usdt  = filled_coins * limit_avg if (filled_coins and limit_avg) else 0.0
    remaining    = amt_usdt - filled_usdt
    market_coins = 0.0
    if remaining >= 1.0:
        mkt_resp = _okx_post('/api/v5/trade/order', {
            'instId':  symbol,
            'tdMode':  'cash',
            'side':    'buy',
            'ordType': 'market',
            'sz':      f'{remaining:.4f}',
            'tgtCcy':  'quote_ccy',
        })
        print(f'  [Trade] {symbol}: market buy ${remaining:.2f} USDT ✓')
        # Price the fill off the real average, not the signal-time ticker: the
        # market fallback runs ~45 s after the signal, so `price` can be stale by
        # then — and entry_price drives the TP/SL triggers and every P&L figure.
        mkt_ord_id = mkt_resp.get('data', [{}])[0].get('ordId', '')
        time.sleep(1.0)   # let OKX register the fill before asking for its price
        mkt_px = _get_order_fill_price(symbol, mkt_ord_id) if mkt_ord_id else None
        if not mkt_px:
            mkt_px = price
            print(f'  [Trade] {symbol}: market fill price unavailable — estimating entry at {fmt_price(price)}')
        market_coins = remaining / mkt_px
    else:
        remaining = 0.0

    # Give OKX time to register the fill before placing algo orders
    time.sleep(1.5)

    # Coin quantity from actual fills (exact for limit, estimated for market)
    sz_coin = filled_coins + market_coins
    spent   = filled_usdt + remaining
    if sz_coin <= 0 or spent <= 0:
        sz_coin, spent = amt_usdt / price, amt_usdt   # safety fallback
    entry_px = spent / sz_coin
    half_sz  = sz_coin * 0.5 * 0.9985  # 50% with OKX fee haircut

    tp_price  = entry_px * (1 + tp_pct  / 100)
    sl_price  = entry_px * (1 - sl_pct  / 100)
    base_algo = {'instId': symbol, 'tdMode': 'cash', 'side': 'sell'}

    # 2+3. Protective orders. From here the coins are REAL: any failure means an
    #      unprotected position, so both placements share one guard that unwinds
    #      the trade rather than leaving it naked.
    oco_id = sl2_id = ''
    try:
        # 2. OCO: TP and SL on first 50% (one order — OKX only reserves balance once).
        #    ordType MUST be 'oco' — see OCO_ORD_TYPE: 'conditional' would silently
        #    drop the TP leg and leave an SL-only order.
        oco_resp = _okx_post('/api/v5/trade/order-algo', {
            **base_algo,
            'ordType':          OCO_ORD_TYPE,
            'sz':               f'{half_sz:.8f}',
            'tpTriggerPx':      f'{tp_price:.8f}',
            'tpOrdPx':          '-1',
            'tpTriggerPxType':  'last',
            'slTriggerPx':      f'{sl_price:.8f}',
            'slOrdPx':          '-1',
            'slTriggerPxType':  'last',
        })
        oco_id = oco_resp.get('data', [{}])[0].get('algoId', '')
        print(f'  [Trade] {symbol}: OCO TP +{tp_pct}% / SL −{sl_pct}% (ID: {oco_id}) ✓')

        # 3. Conditional SL on remaining 50% at the same trigger price — full position
        #    protected on OKX even if the monitor is down. Swapped for a trailing stop
        #    by the monitor when the TP fires.
        sl2_resp = _okx_post('/api/v5/trade/order-algo', {
            **base_algo,
            'ordType':         'conditional',
            'sz':              f'{half_sz:.8f}',
            'slTriggerPx':     f'{sl_price:.8f}',
            'slOrdPx':         '-1',
            'slTriggerPxType': 'last',
        })
        sl2_id = sl2_resp.get('data', [{}])[0].get('algoId', '')
        print(f'  [Trade] {symbol}: 2nd-half SL −{sl_pct}% (ID: {sl2_id}) ✓')
    except Exception as e:
        note = _abort_unprotected(symbol, sz_coin, [oco_id, sl2_id], f'Protective order rejected: {e}')
        raise Exception(f'protective orders failed ({e}) — {note}')

    # 4. Save to Supabase for monitor_option3_trades() to track
    saved = _save_option3_trade({
        'id':             oco_id,
        'symbol':         symbol,
        'entry_price':    entry_px,
        'partial_tp_id':  oco_id,
        'sl_id':          oco_id,   # same ID — monitor uses fill px vs entry to tell TP vs SL
        'sl2_id':         sl2_id,   # 2nd-half SL — swapped for a trailing stop after TP
        'trailing_id':    '',       # set by the monitor when the TP fires
        'amount_usdt':    amt_usdt,
        'sz_half':        half_sz,
        'partial_tp_pct': tp_pct,
        'sl_pct':         sl_pct,
        'trailing_pct':   trail_pct,
        'phase':          1,
        # Journal: the market picture this decision was made on, graded after the exit
        'entry_context':  snapshot or {},
    })

    return {
        'amount_usdt': amt_usdt,
        'tp_pct':      tp_pct,
        'sl_pct':      sl_pct,
        'trail_pct':   trail_pct,
        'entry_price': entry_px,
        'saved':       saved,   # False → monitor can't track it (no break-even move / exit alerts)
    }


# ── Supabase Option 3 trade helpers ──────────────────────────────────────────
def _fetch_option3_trades():
    """Fetch all active Option 3 trades (phase < 3) from Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print('  [Supabase] URL or key not set — skipping trade fetch')
        return []
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/option3_trades?phase=lt.3&select=*',
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        print(f'  [Supabase] Fetch error: HTTP {r.status_code} — {r.text[:300]}')
    except Exception as e:
        print(f'  [Supabase] Fetch failed: {e}')
    return []


def _algo_history(algo_id, ord_type):
    """
    Algo-history rows for one algo ID. ord_type is an OKX ordType, or a tuple of
    candidates to try in order (OCO_ORD_TYPES — an OCO id is 'oco' on new trades
    but 'conditional' on ones placed before the OCO fix). Returns [] if not found.
    """
    for t in ((ord_type,) if isinstance(ord_type, str) else ord_type):
        try:
            rows = [o for o in _okx_get(
                        f'/api/v5/trade/orders-algo-history?ordType={t}&algoId={algo_id}'
                    ).get('data', []) if o.get('algoId') == algo_id]
            if rows:
                return rows
        except Exception as e:
            print(f'  [Option3] Algo history error ({algo_id}, ordType={t}): {e}')
    return []


def _is_algo_triggered(algo_id, ord_type='conditional'):
    """Return True if an OKX algo order has triggered (state=effective in history)."""
    return any(o.get('state') == 'effective' for o in _algo_history(algo_id, ord_type))


def _get_order_fill_price(symbol, ord_id):
    """Return the average fill price of a regular OKX order, or None."""
    try:
        d = _okx_get(f'/api/v5/trade/order?instId={symbol}&ordId={ord_id}')
        for order in d.get('data', []):
            px = float(order.get('avgPx', '0') or '0')
            if px > 0:
                return px
    except Exception as e:
        print(f'  [Option3] Order fill lookup error ({ord_id}): {e}')
    return None


def _get_fill_price(algo_id, ord_type='conditional', symbol=None):
    """
    Return the actual fill price of a triggered OKX algo order.
    OKX often leaves avgPx empty on algo-history rows, so fall back to actualPx,
    then to the avgPx of the child market order the algo triggered (ordId).
    """
    try:
        for order in _algo_history(algo_id, ord_type):
            if order.get('state') == 'effective':
                for key in ('avgPx', 'actualPx'):
                    px = float(order.get(key, '0') or '0')
                    if px > 0:
                        return px
                ord_id = order.get('ordId', '')
                inst   = symbol or order.get('instId', '')
                if ord_id and inst:
                    return _get_order_fill_price(inst, ord_id)
    except Exception:
        return None
    return None


def _is_algo_cancelled(algo_id, ord_type='conditional'):
    """Return True if an OKX algo order was manually cancelled or failed (not triggered)."""
    return any(o.get('state') in ('canceled', 'order_failed')
               for o in _algo_history(algo_id, ord_type))


def _rank_candidate(sig, rsi_1h, rsi_4h, vol_ratio):
    """
    Composite ranking score for a STRONG BUY candidate.
    Used when multiple signals fire in the same scan to pick the best one.

    Components (higher = better opportunity):
      sig.score  (4.5–9): aggregate RSI/MACD/BB/4H/vol indicator checks — dominant factor
      RSI depth (+0–1) : how far 1H RSI is below 30 (RSI 15 beats RSI 29)
      4H depth  (+0–0.5): higher-timeframe alignment depth (multi-TF conviction)
      Volume    (+0–0.5): relative volume surge above average (confirms buying pressure)
    """
    score = float(sig['score'])
    if rsi_1h and rsi_1h < 30:
        score += (30 - rsi_1h) / 30.0        # max +1.0 when RSI→0
    if rsi_4h and rsi_4h < 40:
        score += (40 - rsi_4h) / 80.0        # max +0.5 when 4H RSI→0
    if vol_ratio and vol_ratio > 1.0:
        score += min(vol_ratio - 1.0, 2.0) / 4.0  # max +0.5 at vol_ratio=3
    return score


def _update_sl_to_breakeven(trade):
    """Cancel original SL and place a new conditional SL at entry price."""
    symbol   = trade['symbol']
    entry_px = float(trade['entry_price'])
    sl_id    = trade['sl_id']
    sz_half  = trade['sz_half']

    try:
        _okx_post('/api/v5/trade/cancel-algos', [{'algoId': sl_id, 'instId': symbol}])
        print(f'  [Option3] {symbol}: original SL ({sl_id}) canceled ✓')
    except Exception as e:
        print(f'  [Option3] {symbol}: SL cancel warning (may already be gone): {e}')

    try:
        resp      = _okx_post('/api/v5/trade/order-algo', {
            'instId':          symbol,
            'tdMode':          'cash',
            'side':            'sell',
            'ordType':         'conditional',
            'sz':              f'{float(sz_half):.8f}',
            'slTriggerPx':     f'{float(entry_px):.8f}',
            'slOrdPx':         '-1',
            'slTriggerPxType': 'last',
        })
        new_sl_id = resp.get('data', [{}])[0].get('algoId', '')
        print(f'  [Option3] {symbol}: break-even SL placed at {entry_px} (ID: {new_sl_id}) ✓')
        return new_sl_id
    except Exception as e:
        print(f'  [Option3] {symbol}: break-even SL placement failed: {e}')
        return None


def _mark_phase2(trade_id, updates):
    """Update Supabase: set phase=2 plus the phase-2 protection order IDs
    (e.g. {'trailing_id': ..., 'sl_id': ...})."""
    try:
        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/option3_trades?id=eq.{trade_id}',
            headers={
                'apikey':        SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'Content-Type':  'application/json',
            },
            json={'phase': 2, **updates},
            timeout=10,
        )
        if r.status_code in (200, 204):
            print(f'  [Option3] Trade {trade_id}: marked phase 2 ✓')
        else:
            print(f'  [Option3] Supabase phase update failed: {r.status_code}')
    except Exception as e:
        print(f'  [Option3] Supabase phase update error: {e}')


def _cancel_algo(symbol, algo_id):
    """Cancel an OKX algo order silently — already-gone is fine."""
    try:
        _okx_post('/api/v5/trade/cancel-algos', [{'algoId': algo_id, 'instId': symbol}])
        print(f'  [Option3] {symbol}: cancelled algo {algo_id} ✓')
    except Exception as e:
        print(f'  [Option3] {symbol}: cancel algo {algo_id} warning: {e}')


def _mark_trade_closed(trade_id, exit_reason=None, exit_price=None, net_pnl=None):
    """
    Mark a trade as phase=3 (fully closed) in Supabase, recording the outcome
    (exit_reason / exit_price / net_pnl_usdt / closed_at) when known. The outcome
    columns feed the daily circuit breaker and the AI's trade-history context.
    Falls back to a plain phase=3 update if the columns don't exist yet.
    """
    def _patch(payload):
        return requests.patch(
            f'{SUPABASE_URL}/rest/v1/option3_trades?id=eq.{trade_id}',
            headers={
                'apikey':        SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'Content-Type':  'application/json',
            },
            json=payload,
            timeout=10,
        )

    payload = {'phase': 3}
    if exit_reason:
        payload['exit_reason'] = exit_reason
        payload['closed_at']   = datetime.now(timezone.utc).isoformat()
        if exit_price is not None:
            payload['exit_price'] = exit_price
        if net_pnl is not None:
            payload['net_pnl_usdt'] = round(net_pnl, 4)
    try:
        r = _patch(payload)
        if r.status_code in (200, 204):
            print(f'  [Option3] Trade {trade_id}: marked closed ✓ ({exit_reason or "no outcome data"})')
            return
        if exit_reason:
            print(f'  [Option3] Close-with-outcome failed ({r.status_code}) — outcome columns '
                  f'missing? Run the SQL migration in docs/ARCHITECTURE.md. Retrying plain close...')
            r2 = _patch({'phase': 3})
            if r2.status_code in (200, 204):
                print(f'  [Option3] Trade {trade_id}: marked closed ✓ (outcome NOT recorded)')
                return
        print(f'  [Option3] Close update failed: {r.status_code}')
    except Exception as e:
        print(f'  [Option3] Close update error: {e}')


def _swap_sl2_to_trailing(trade):
    """
    After the partial TP fills (new-format trades): cancel the 2nd-half stop-loss and
    replace it with an immediately-active trailing stop. As long as trail% < TP%, the
    trailing floor sits above entry, so the 2nd half stays break-even-or-better.
    Returns ('trailing', algo_id) on success,
            ('be_sl',    algo_id) if the trailing failed but a break-even SL was placed,
            (None, '')            if the 2nd half could not be protected at all.
    """
    symbol    = trade['symbol']
    sz_half   = f"{float(trade['sz_half']):.8f}"   # never scientific notation — OKX rejects it
    trail_pct = float(trade.get('trailing_pct', 0) or 0)
    entry_px  = float(trade.get('entry_price', 0) or 0)

    _cancel_algo(symbol, trade['sl2_id'])

    try:
        resp = _okx_post('/api/v5/trade/order-algo', {
            'instId': symbol, 'tdMode': 'cash', 'side': 'sell',
            'ordType':       'move_order_stop',
            'sz':            sz_half,
            'callbackRatio': f'{trail_pct / 100:.4f}',
        })
        tid = resp.get('data', [{}])[0].get('algoId', '')
        if tid:
            print(f'  [Option3] {symbol}: trailing stop {trail_pct}% now active (ID: {tid}) ✓')
            return 'trailing', tid
    except Exception as e:
        print(f'  [Option3] {symbol}: trailing stop placement failed: {e}')

    # Fallback: at least pin the 2nd half to break-even
    try:
        resp = _okx_post('/api/v5/trade/order-algo', {
            'instId': symbol, 'tdMode': 'cash', 'side': 'sell',
            'ordType':         'conditional',
            'sz':              sz_half,
            'slTriggerPx':     f'{entry_px:.8f}',
            'slOrdPx':         '-1',
            'slTriggerPxType': 'last',
        })
        sid = resp.get('data', [{}])[0].get('algoId', '')
        if sid:
            print(f'  [Option3] {symbol}: fallback break-even SL placed (ID: {sid}) ✓')
            return 'be_sl', sid
    except Exception as e:
        print(f'  [Option3] {symbol}: fallback break-even SL failed: {e}')
    return None, ''


def _count_recent_sl(hours=24):
    """
    Circuit-breaker input: number of stop-loss exits in the last N hours.
    Returns 0 (fail-open, logged) if the outcome columns don't exist yet or
    Supabase is unreachable.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    # 'Z' suffix instead of '+00:00' — a literal '+' in a URL query decodes as a space
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/option3_trades'
            f'?select=id&exit_reason=eq.sl&closed_at=gte.{since}',
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            timeout=10,
        )
        if r.status_code == 200:
            return len(r.json())
        print(f'  [Safety] SL-count query failed (HTTP {r.status_code}) — outcome columns '
              f'missing? Run the SQL migration in docs/ARCHITECTURE.md. Circuit breaker inactive.')
    except Exception as e:
        print(f'  [Safety] SL-count query error: {e} — circuit breaker inactive')
    return 0


def _fmt_journal_trade(t):
    """One closed trade as: outcome | the conditions it was entered on | what price did after."""
    ctx  = t.get('entry_context') or {}
    fu   = t.get('followup') or {}
    coin = t['symbol'].replace('-USDT', '')
    parts = [f"{coin} {t.get('exit_reason')} {float(t.get('net_pnl_usdt') or 0):+.2f}"]

    ins = []
    if ctx.get('score')       is not None: ins.append(f"score {ctx['score']:+.1f}")
    if ctx.get('rsi_1h')      is not None: ins.append(f"RSI {ctx['rsi_1h']:.0f}")
    if ctx.get('rsi_4h')      is not None: ins.append(f"4H {ctx['rsi_4h']:.0f}")
    if ctx.get('vol_ratio')   is not None: ins.append(f"vol {ctx['vol_ratio']:.1f}x")
    if ctx.get('atr_pct')     is not None: ins.append(f"ATR {ctx['atr_pct']:.1f}%")
    if ctx.get('fear_greed')  is not None: ins.append(f"F&G {ctx['fear_greed']}")
    if ctx.get('funding_pct') is not None: ins.append(f"fund {ctx['funding_pct']:+.3f}%")
    ch = ctx.get('chosen') or {}
    if ch.get('sl_pct'):    ins.append(f"SL -{ch['sl_pct']}%")
    if ch.get('trail_pct'): ins.append(f"trail {ch['trail_pct']}%")
    if ins:
        parts.append('entered on: ' + ', '.join(ins))

    if fu.get('verdict') and fu['verdict'] != 'ungradable':
        parts.append(f"{fu.get('hours', JOURNAL_FOLLOWUP_HOURS)}h after exit: "
                     f"peak {fu.get('peak_pct', 0):+.1f}% / trough {fu.get('trough_pct', 0):+.1f}% "
                     f"→ {fu['verdict'].upper()} ({fu.get('note', '')})")
    return ' | '.join(parts)


def _trade_history_context(symbol):
    """
    The trade journal, rendered for the Claude prompt: every recent closed trade with
    the conditions it was ENTERED on and — once graded — what price did AFTER the exit,
    plus the PROFIT FACTOR (last 30 trades) that drives performance-weighted sizing.

    The follow-up verdicts are what make losses useful: 'shakeout' and 'good_save' are
    both stop-losses, look identical in a P&L column, and imply opposite fixes.

    Returns (text, profit_factor, evidence). `evidence` is the provenance record of
    what this prompt was actually shown — sample size, which directives fired — so a
    decision can later be attributed to the evidence behind it instead of leaving
    "did the journal help?" permanently unanswerable. profit_factor stays None until
    30 closed trades have accumulated.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return '', None, {}
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/option3_trades'
            f'?phase=eq.3&exit_reason=not.is.null&order=closed_at.desc&limit=30'
            f'&select=symbol,exit_reason,net_pnl_usdt,closed_at,entry_context,followup',
            headers=_sb_headers(), timeout=10,
        )
        if r.status_code != 200:
            # Journal columns missing (migration not run) — fall back to bare outcomes
            r = requests.get(
                f'{SUPABASE_URL}/rest/v1/option3_trades'
                f'?phase=eq.3&exit_reason=not.is.null&order=closed_at.desc&limit=30'
                f'&select=symbol,exit_reason,net_pnl_usdt,closed_at',
                headers=_sb_headers(), timeout=10,
            )
            if r.status_code != 200:
                return '', None, {}
        rows = r.json()
        if not rows:
            return '', None, {}

        pnls  = [float(t.get('net_pnl_usdt') or 0) for t in rows]
        wins  = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        lines = [f'Overall: {len(rows)} closed trades, {wins} wins / {len(rows) - wins} losses, '
                 f'net {total:+.2f} USDT']

        pf = None
        if len(rows) >= 30:
            gross_win  = sum(p for p in pnls if p > 0)
            gross_loss = -sum(p for p in pnls if p < 0)
            if gross_loss > 0:
                pf = gross_win / gross_loss
                lines.append(f'Profit factor (last 30 trades): {pf:.2f}')
            elif gross_win > 0:
                pf = 99.0
                lines.append('Profit factor (last 30 trades): no losses')

        # Patterns worth acting on — computed in code, not inferred by the model,
        # and only PRESCRIPTIVE once the rate clears _pattern_is_real(). Note the
        # denominator: rates are over GRADED trades, not all closed ones. A trade
        # that closed in the last JOURNAL_FOLLOWUP_HOURS has no verdict yet, and
        # counting it as "not a shakeout" silently dilutes every rate here.
        verdicts  = [(t.get('followup') or {}).get('verdict') for t in rows]
        graded    = sum(1 for v in verdicts if v)
        shakeouts = verdicts.count('shakeout')
        left      = verdicts.count('left_money')
        saves     = verdicts.count('good_save')
        fired     = []

        # Every verdict class, not just the three that have a directive attached.
        # _grade_exit() produces seven, and until 2026-08-02 only three of them
        # reached the model in aggregate — on the live record that silently dropped
        # 9 of 13 verdicts, including the single most common one. A verdict that is
        # computed, stored, and then never counted is work the journal did and threw
        # away. These are COUNTS ONLY and carry no directive: see the note below on
        # why partial_recovery is not folded into the shakeout gate.
        tally = {v: verdicts.count(v) for v in
                 ('shakeout', 'partial_recovery', 'flat_after_stop', 'good_save',
                  'left_money', 'well_timed', 'fair_exit') if verdicts.count(v)}
        if tally:
            lines.append('Verdict breakdown over ' + str(graded) + ' graded trades: '
                         + ', '.join(f'{k} {n}' for k, n in tally.items()))

        if _pattern_is_real(shakeouts, graded):
            fired.append('shakeout')
            lines.append(f'PATTERN: {shakeouts} of {graded} graded trades were SHAKEOUTS — the stop '
                         f'sat inside normal noise and price recovered to our target afterwards. '
                         f'Consider a wider slPct on similar setups.')
        elif shakeouts:
            lines.append(f'{shakeouts} of {graded} graded trades were shakeouts — below the level '
                         f'that separates a real problem from ordinary stop noise. Do not retune on it.')

        # partial_recovery ("price bounced >=2% after our stop but never reached our
        # target") points the same direction as shakeout, and it is the most common
        # verdict in the live record. It is deliberately NOT added to the shakeout
        # count above. The Wilson gate compares against JOURNAL_PATTERN_NULL_RATE =
        # 0.15, a rate chosen for the narrow shakeout class; widening the class
        # without re-deriving the null rate would clear the bar by redefinition
        # rather than by evidence — the exact false positive this gate exists to
        # stop. It is surfaced as context so the model can see it, and left for
        # learn.py to judge properly once a cohort is big enough to say anything.
        pr = verdicts.count('partial_recovery')
        if pr:
            lines.append(f'{pr} of {graded} graded stops saw price bounce afterwards without '
                         f'reaching our target (partial_recovery). Context only — this has not '
                         f'been tested against a null rate, so do not retune on it.')

        if _pattern_is_real(left, graded):
            fired.append('left_money')
            lines.append(f'PATTERN: {left} of {graded} graded exits left significant money on the '
                         f'table — consider a wider trailingCallbackPct so winners run further.')
        elif left:
            lines.append(f'{left} of {graded} graded exits left money on the table — not yet a pattern.')

        if saves:
            lines.append(f'{saves} stop-loss exit(s) correctly avoided a deeper fall.')

        # This coin first — most relevant — then the rest of the journal
        coin_rows  = [t for t in rows if t.get('symbol') == symbol][:5]
        other_rows = [t for t in rows if t.get('symbol') != symbol][:8]
        if coin_rows:
            lines.append(f'\n{symbol} history:')
            lines += [f'  - {_fmt_journal_trade(t)}' for t in coin_rows]
        else:
            lines.append(f'\n{symbol}: no closed trades yet')
        if other_rows:
            lines.append('\nOther recent trades:')
            lines += [f'  - {_fmt_journal_trade(t)}' for t in other_rows]

        if len(rows) < JOURNAL_MIN_SAMPLES:
            lines.append(f'\nNOTE: only {len(rows)} closed trade(s) so far — this is ANECDOTE, not '
                         f'statistics. Use it for obvious parameter problems only. Do NOT blacklist '
                         f'a coin or make large sizing jumps from one or two results.')

        evidence = {
            'journal_n':   len(rows),
            'journal_graded': graded,
            'journal_pf':  round(pf, 2) if pf is not None else None,
            # Win rate over whatever history exists. Feeds the sizing cap before
            # 30 trades make a profit factor available (see ai_trade_params).
            'journal_win_rate': round(wins / len(rows), 3),
            'shakeouts':   shakeouts,
            'left_money':  left,
            'good_saves':  saves,
            'verdicts':    tally,
            'patterns':    fired,
        }
        return '\n'.join(lines), pf, evidence
    except Exception:
        return '', None, {}


def _skip_history_context(symbol):
    """
    Graded record of setups the AI declined — the other half of the mistake ledger.
    Being too cautious never shows up in P&L, so it is surfaced explicitly.

    Returns (text, evidence). The "too cautious / correctly cautious" call is a
    two-way split, so it is judged as one: among skips that actually resolved
    (neutral_skip decided nothing either way), is the missed/good split far enough
    from a coin flip to be worth telling the model about? The old test — `missed >= 3
    and missed > good` — called 3-vs-2 a PATTERN, which is a coin flip with a label.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return '', {}
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/skipped_setups'
            f'?followup_at=not.is.null&order=created_at.desc&limit=20'
            f'&select=symbol,reason,followup,created_at',
            headers=_sb_headers(), timeout=10,
        )
        if r.status_code != 200:
            return '', {}
        rows = r.json()
        if not rows:
            return '', {}
        verdicts = [(s.get('followup') or {}).get('verdict') for s in rows]
        missed, good = verdicts.count('missed_win'), verdicts.count('good_skip')
        decisive = missed + good      # neutral_skip resolved nothing — it is not evidence either way
        lines = [f'Last {len(rows)} graded skips: {good} correctly avoided a loss, '
                 f'{missed} would have hit target ({verdicts.count("neutral_skip")} went nowhere)']

        fired = []
        if decisive >= JOURNAL_MIN_SAMPLES and _wilson_lower(missed, decisive) > 0.5:
            fired.append('too_cautious')
            lines.append(f'PATTERN: {missed} of {decisive} resolved skips would have WON — you are '
                         f'being too cautious; do not skip setups that meet the rules without a '
                         f'concrete reason.')
        elif decisive >= JOURNAL_MIN_SAMPLES and _wilson_lower(good, decisive) > 0.5:
            fired.append('caution_working')
            lines.append(f'PATTERN: {good} of {decisive} resolved skips correctly avoided losses — '
                         f'your caution is working.')
        elif decisive:
            lines.append(f'The {decisive} resolved skips split {missed} missed / {good} avoided — '
                         f'too close to a coin flip to conclude anything about your caution level.')

        coin_rows = [s for s in rows if s.get('symbol') == symbol][:3]
        for s in coin_rows:
            fu = s.get('followup') or {}
            lines.append(f"  - {symbol} skipped ({(s.get('reason') or '')[:60]}) → "
                         f"{fu.get('verdict', '?').upper()}: {fu.get('note', '')}")
        return '\n'.join(lines), {'skips_n': len(rows), 'skips_decisive': decisive,
                                  'missed_win': missed, 'good_skip': good, 'skip_patterns': fired}
    except Exception:
        return '', {}


def _phase1_pnl(trade):
    """
    Net USDT profit of the phase-1 partial TP exit, recovered from the OKX fill
    history (nothing is stored in Supabase). None if the fill can't be found.
    """
    entry_px = float(trade.get('entry_price', 0) or 0)
    sz_half  = float(trade.get('sz_half', '0') or 0)
    tp_id    = trade.get('partial_tp_id')
    if not tp_id or entry_px <= 0 or sz_half <= 0:
        return None
    tp_fill = _get_fill_price(tp_id, OCO_ORD_TYPES, trade.get('symbol'))
    if tp_fill and tp_fill > entry_px:
        net, _, _, _ = _exit_pnl(entry_px, tp_fill, sz_half)
        return net
    return None


def _close_full_position_at_sl(trade, sl_fill_px):
    """
    After an SL trigger, close out the whole trade and report the exact USDT loss
    (both halves, incl. fees).
      New-format trades: the 2nd half has its own SL at the same trigger price, so
      OKX normally sells BOTH halves server-side — we just collect the fills.
      Legacy trades: cancel the dormant trailing stop and market-sell the 2nd half.
    sl_fill_px may be None — falls back to the SL trigger price as an estimate.
    """
    symbol   = trade['symbol']
    coin     = symbol.replace('-USDT', '')
    entry_px = float(trade.get('entry_price', 0) or 0)
    sz_half  = float(trade.get('sz_half', '0') or 0)
    sl_pct   = float(trade.get('sl_pct', 0) or 0)
    sl2_id   = trade.get('sl2_id') or ''

    trailing_id = trade.get('trailing_id')
    if trailing_id:
        _cancel_algo(symbol, trailing_id)   # legacy dormant trailing stop

    second_half_sold = False
    half2_px         = None

    if sl2_id:
        # New format: the 2nd-half SL shares the trigger price and should have fired
        # on OKX at (almost) the same moment. Give it a short grace period.
        if not _is_algo_triggered(sl2_id, 'conditional'):
            time.sleep(2)
        if _is_algo_triggered(sl2_id, 'conditional'):
            second_half_sold = True
            half2_px = _get_fill_price(sl2_id, 'conditional', symbol)
            print(f'  [Option3] {symbol}: 2nd-half SL fired server-side ✓')
        else:
            _cancel_algo(symbol, sl2_id)   # cancel first so the market sell can't double-sell

    if not second_half_sold:
        half2_ord_id = ''
        try:
            resp = _okx_post('/api/v5/trade/order', {
                'instId': symbol, 'tdMode': 'cash',
                'side': 'sell', 'ordType': 'market', 'sz': f'{sz_half:.8f}',
            })
            half2_ord_id     = resp.get('data', [{}])[0].get('ordId', '')
            second_half_sold = True
            print(f'  [Option3] {symbol}: remaining 50% market-sold ✓')
        except Exception as e:
            print(f'  [Option3] {symbol}: could not sell remaining 50%: {e}')
        if second_half_sold and half2_ord_id:
            time.sleep(1.0)   # let OKX register the market-sell fill
            half2_px = _get_order_fill_price(symbol, half2_ord_id)

    estimated = False
    if not sl_fill_px and entry_px > 0 and sl_pct > 0:
        sl_fill_px = entry_px * (1 - sl_pct / 100)   # SL trigger price
        estimated  = True
    if half2_px is None:
        half2_px = sl_fill_px

    net_total = avg_exit = None
    is_profit = False
    if sl_fill_px and entry_px > 0:
        exit_prices = [sl_fill_px] + ([half2_px] if second_half_sold else [])
        net_total = fee_total = 0.0
        for px in exit_prices:
            net, fees, _, _ = _exit_pnl(entry_px, px, sz_half)
            net_total += net
            fee_total += fees
        avg_exit  = sum(exit_prices) / len(exit_prices)
        pct       = (avg_exit / entry_px - 1) * 100
        approx    = '~' if estimated else ''
        is_profit = net_total >= 0
        if is_profit:
            # Rare (e.g. SL triggered right at/above entry) — show gross profit
            # (excluding fees) plus a separate fee line so the two together give
            # the exact net result, instead of a redundant fees-already-included line.
            gross      = net_total + fee_total
            result_str = f'{approx}{fmt_usdt(gross)} USDT ({pct:+.1f}%, excl. fees)'
            detail     = (f"📉 OKX fees paid: ${fee_total:.4f} USDT\n"
                          f"📍 Entry: {fmt_price(entry_px)} → Exit: {approx}{fmt_price(avg_exit)}")
        else:
            result_str = f'{approx}{fmt_usdt(net_total)} USDT ({pct:+.1f}% incl. fees)'
            detail     = f"📍 Entry: {fmt_price(entry_px)} → Exit: {approx}{fmt_price(avg_exit)}"
    else:
        result_str = f'−{sl_pct}% on full position'
        detail      = f"📍 Entry: {fmt_price(entry_px)}"

    _mark_trade_closed(trade['id'], 'sl', avg_exit, net_total)
    extra = '' if second_half_sold else '\n⚠️ Could not auto-sell remaining 50% — check OKX'
    icon         = '🟢' if is_profit else '🔴'
    result_label = 'Total profit' if is_profit else 'Total loss'
    send_telegram(
        f"{icon} <b>Stop Loss Hit — {coin}</b>\n"
        f"💸 {result_label}: {result_str}\n"
        f"{detail}\n"
        f"✅ Full position closed (both halves){extra}"
    )


def maybe_send_daily_digest(cache):
    """
    Once per UTC day (first run after DIGEST_UTC_HOUR): minimal Telegram
    heartbeat — just the header and the currently open trades. Still doubles as
    a dead-man switch: if this message stops arriving, the pipeline is down.
    (Full performance stats live in the dashboard's 📊 Bot Performance panel.)
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour < DIGEST_UTC_HOUR:
        return
    today = now_utc.strftime('%Y-%m-%d')
    if cache.get('_daily_digest', {}).get('date') == today:
        return

    open_trades = _fetch_option3_trades()
    if open_trades:
        syms      = ', '.join(t['symbol'].replace('-USDT', '') for t in open_trades)
        open_line = f'📈 Open trades: {len(open_trades)} ({syms})'
    else:
        open_line = '📈 Open trades: none'

    send_telegram(f'💓 <b>Daily Report — OKX Trading</b>\n{open_line}')
    cache['_daily_digest'] = {'date': today}
    print('  [Digest] Daily report sent ✓')


# ── Option 3 exit monitor ─────────────────────────────────────────────────────
def monitor_option3_trades():
    """
    Poll all active Option 3 trades and send Telegram on every exit event.

    Phase 1: partial TP hit  → lock profit, move SL to break-even, advance to phase 2.
             SL hit (OCO)    → sell remaining 50%, report full loss, close trade.
    Phase 2: trailing stop   → report profit with exact USDT gain, close trade.
             break-even SL   → report zero-loss exit, close trade.
    """
    if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
        return

    trades = _fetch_option3_trades()
    if not trades:
        return

    print(f'\n  [Option3] Monitoring {len(trades)} active trade(s)...')

    for trade in trades:
        symbol   = trade['symbol']
        phase    = trade.get('phase', 1)
        entry_px = float(trade.get('entry_price', 0) or 0)
        sz_half  = float(trade.get('sz_half', '0') or 0)
        ptp_pct  = float(trade.get('partial_tp_pct', 0) or 0)
        coin     = symbol.replace('-USDT', '')

        try:
            if phase == 1:
                tp_id  = trade['partial_tp_id']
                sl_id  = trade.get('sl_id')
                is_oco = (tp_id and sl_id and tp_id == sl_id)
                # An OCO id lives under ordType 'oco'; a legacy separate TP order is
                # a plain 'conditional'.
                tp_types = OCO_ORD_TYPES if is_oco else 'conditional'

                if _is_algo_triggered(tp_id, tp_types):
                    fill_px  = _get_fill_price(tp_id, tp_types, symbol)
                    tp_fired = (not is_oco) or (fill_px is not None and fill_px > entry_px)

                    if tp_fired:
                        # ── Partial TP filled ──────────────────────────────────
                        print(f'  [Option3] {symbol}: partial TP triggered ✓')
                        estimated = False
                        if not fill_px and entry_px > 0 and ptp_pct > 0:
                            fill_px   = entry_px * (1 + ptp_pct / 100)   # TP trigger price
                            estimated = True
                        tp_pnl = None
                        approx = ''
                        if fill_px and entry_px > 0:
                            tp_pnl, total_fees, buy_fee, sell_fee = _exit_pnl(entry_px, fill_px, sz_half)
                            approx      = '~' if estimated else ''
                            profit_line = f'{approx}{fmt_usdt(tp_pnl)} USDT (after fees)'
                            fee_line    = f'📉 OKX fees: ${total_fees:.4f} USDT (entry ${buy_fee:.4f} + exit ${sell_fee:.4f})'
                        else:
                            profit_line = f'+{ptp_pct}% on 50% of position'
                            fee_line    = None

                        sl2_id = trade.get('sl2_id') or ''
                        if sl2_id and _is_algo_triggered(sl2_id, 'conditional'):
                            # ── Whipsaw: TP filled, then price reversed and the 2nd-half
                            #    SL also fired before this monitor run — trade fully closed.
                            print(f'  [Option3] {symbol}: 2nd-half SL also fired (whipsaw) — closing...')
                            half2_px = _get_fill_price(sl2_id, 'conditional', symbol)
                            sl_pct_v = float(trade.get('sl_pct', 0) or 0)
                            est2 = estimated
                            if not half2_px and entry_px > 0 and sl_pct_v > 0:
                                half2_px, est2 = entry_px * (1 - sl_pct_v / 100), True
                            msg = [f"🔄 <b>Fast Reversal — {coin}</b>",
                                   f"✅ TP hit on first 50%: {profit_line}"]
                            whole = None
                            if half2_px and entry_px > 0 and tp_pnl is not None:
                                pnl2, _, _, _ = _exit_pnl(entry_px, half2_px, sz_half)
                                a2 = '~' if est2 else ''
                                whole = tp_pnl + pnl2
                                msg.append(f"🔴 Price reversed — 2nd half stopped out: {a2}{fmt_usdt(pnl2)} USDT")
                                msg.append(f"📊 Whole trade net result: {a2}{fmt_usdt(whole)} USDT")
                            else:
                                msg.append(f"🔴 Price reversed — 2nd half stopped out")
                            _mark_trade_closed(trade['id'], 'tp_then_sl', half2_px, whole)
                            send_telegram('\n'.join(msg))
                        else:
                            # ── Normal TP: protect the 2nd half for phase 2 ────────
                            if sl2_id:
                                kind, oid = _swap_sl2_to_trailing(trade)
                            else:
                                # Legacy trade (pre-sl2 format): dormant trailing already
                                # armed at the TP price — add a break-even SL beside it.
                                oid  = _update_sl_to_breakeven(trade)
                                kind = 'legacy_be' if oid else 'legacy_none'
                            trail_v = float(trade.get('trailing_pct', 0) or 0)
                            if kind == 'trailing':
                                _mark_phase2(trade['id'], {'trailing_id': oid, 'sl_id': ''})
                                guard_lines = [f"🔄 Trailing stop now active on remaining 50% — "
                                               f"exits on first {trail_v}% pullback from the peak"]
                            elif kind == 'be_sl':
                                _mark_phase2(trade['id'], {'trailing_id': '', 'sl_id': oid})
                                guard_lines = [f"⚠️ Trailing stop failed — 2nd half protected by a "
                                               f"break-even SL instead (check OKX)"]
                            elif kind == 'legacy_be':
                                _mark_phase2(trade['id'], {'sl_id': oid})
                                guard_lines = [f"🛡️ SL moved to entry (break-even) — 2nd half is now risk-free",
                                               f"🔄 Trailing stop protecting remaining 50%"]
                            elif kind == 'legacy_none':
                                _mark_phase2(trade['id'], {'sl_id': ''})
                                guard_lines = [f"⚠️ Break-even SL could not be placed — check OKX",
                                               f"🔄 Trailing stop protecting remaining 50%"]
                            else:
                                # New-format trade with BOTH protections failed — don't
                                # leave a stuck row; tell the user to manage it manually.
                                _mark_trade_closed(trade['id'], 'error')
                                guard_lines = [f"🚨 Could not place trailing stop OR break-even SL — "
                                               f"2nd half is UNPROTECTED. Manage it manually on OKX!"]
                            msg_parts = [
                                f"✅ <b>Partial TP Hit — {coin}</b>",
                                f"💰 Profit locked: {profit_line}",
                            ]
                            if fee_line:
                                msg_parts.append(fee_line)
                            msg_parts += guard_lines
                            send_telegram('\n'.join(msg_parts))
                    else:
                        # ── SL side of OCO fired ───────────────────────────────
                        print(f'  [Option3] {symbol}: SL triggered (OCO) — closing full position...')
                        _close_full_position_at_sl(trade, fill_px)

                # Old format: separate SL order (backwards compat)
                elif not is_oco and sl_id and _is_algo_triggered(sl_id, 'conditional'):
                    fill_px = _get_fill_price(sl_id, 'conditional', symbol)
                    _cancel_algo(symbol, tp_id)
                    _close_full_position_at_sl(trade, fill_px)

                elif _is_algo_cancelled(tp_id, tp_types):
                    # ── OCO order manually cancelled on OKX ───────────────────────
                    print(f'  [Option3] {symbol}: OCO cancelled — marking trade closed...')
                    trailing_id = trade.get('trailing_id')
                    if trailing_id:
                        _cancel_algo(symbol, trailing_id)
                    sl2_id = trade.get('sl2_id') or ''
                    if sl2_id:
                        _cancel_algo(symbol, sl2_id)
                    _mark_trade_closed(trade['id'], 'cancelled')
                    send_telegram(
                        f"⚠️ <b>Orders Cancelled — {coin}</b>\n"
                        f"📋 OCO order was manually cancelled on OKX\n"
                        f"🔄 Remaining protective orders also cancelled\n"
                        f"📌 Trade marked closed — new {coin} signals will trigger fresh trades"
                    )
                else:
                    print(f'  [Option3] {symbol}: phase 1 — waiting for TP or SL')

            elif phase == 2:
                trailing_id = trade.get('trailing_id')
                be_sl_id    = trade.get('sl_id')  # updated to break-even SL in _mark_phase2

                if trailing_id and _is_algo_triggered(trailing_id, 'move_order_stop'):
                    fill_px   = _get_fill_price(trailing_id, 'move_order_stop', symbol)
                    estimated = False
                    if not fill_px:
                        t = fetch_ticker(symbol)   # last resort: current market price
                        if t:
                            fill_px, estimated = t['price'], True
                    fee_line    = total_line = None
                    phase1_line = "✅ Phase 1 profit already secured"
                    whole       = None
                    if fill_px and entry_px > 0:
                        net_pnl, total_fees, buy_fee, sell_fee = _exit_pnl(entry_px, fill_px, sz_half)
                        gain_pct   = (fill_px / entry_px - 1) * 100
                        approx     = '~' if estimated else ''
                        profit_str = f'{approx}{fmt_usdt(net_pnl)} USDT ({gain_pct:+.1f}%) after fees'
                        fee_line   = f'📉 OKX fees: ${total_fees:.4f} USDT (entry ${buy_fee:.4f} + exit ${sell_fee:.4f})'
                        p1_pnl     = _phase1_pnl(trade)
                        whole      = net_pnl + p1_pnl if p1_pnl is not None else net_pnl
                        if p1_pnl is not None:
                            phase1_line = f"✅ Phase 1 profit already secured: {fmt_usdt(p1_pnl)} USDT"
                            total_line  = f"📊 Whole trade net result: {approx}{fmt_usdt(whole)} USDT"
                    else:
                        profit_str = 'exited via trailing stop'
                    if be_sl_id:
                        _cancel_algo(symbol, be_sl_id)   # remove the now-orphaned break-even SL
                    _mark_trade_closed(trade['id'], 'tp_trail', fill_px, whole)
                    msg_parts = [
                        f"🏁 <b>Trade Closed — {coin}</b>",
                        f"🔄 Trailing stop exit: {profit_str}",
                    ]
                    if fee_line:
                        msg_parts.append(fee_line)
                    msg_parts.append(phase1_line)
                    if total_line:
                        msg_parts.append(total_line)
                    send_telegram('\n'.join(msg_parts))

                elif be_sl_id and _is_algo_triggered(be_sl_id, 'conditional'):
                    fill_px   = _get_fill_price(be_sl_id, 'conditional', symbol)
                    estimated = False
                    if not fill_px and entry_px > 0:
                        fill_px, estimated = entry_px, True   # BE SL triggers at entry price
                    half_line   = "🛡️ Break-even SL hit — 2nd half exited at entry price"
                    phase1_line = "✅ Phase 1 profit is secured — net result is positive"
                    total_line  = None
                    whole       = None
                    if fill_px and entry_px > 0:
                        net_pnl, _, _, _ = _exit_pnl(entry_px, fill_px, sz_half)
                        approx    = '~' if estimated else ''
                        half_line = f"🛡️ Break-even SL hit — 2nd half exited at entry: {approx}{fmt_usdt(net_pnl)} USDT (after fees)"
                        p1_pnl    = _phase1_pnl(trade)
                        whole     = net_pnl + p1_pnl if p1_pnl is not None else net_pnl
                        if p1_pnl is not None:
                            phase1_line = f"✅ Phase 1 profit secured: {fmt_usdt(p1_pnl)} USDT"
                            total_line  = f"📊 Whole trade net result: {approx}{fmt_usdt(whole)} USDT"
                    if trailing_id:
                        _cancel_algo(symbol, trailing_id)   # trailing never activated — remove it
                    _mark_trade_closed(trade['id'], 'break_even', fill_px, whole)
                    msg_parts = [
                        f"⚪ <b>Break-Even Exit — {coin}</b>",
                        half_line,
                        phase1_line,
                    ]
                    if total_line:
                        msg_parts.append(total_line)
                    send_telegram('\n'.join(msg_parts))

                elif (trailing_id and _is_algo_cancelled(trailing_id, 'move_order_stop')) or \
                     (be_sl_id and _is_algo_cancelled(be_sl_id, 'conditional')):
                    # ── Phase 2 orders manually cancelled on OKX ──────────────────
                    print(f'  [Option3] {symbol}: phase 2 orders cancelled — marking trade closed...')
                    _mark_trade_closed(trade['id'], 'cancelled')
                    send_telegram(
                        f"⚠️ <b>Orders Cancelled — {coin}</b>\n"
                        f"📋 Trailing stop / break-even SL was manually cancelled on OKX\n"
                        f"📌 Trade marked closed — new {coin} signals will trigger fresh trades"
                    )
                else:
                    print(f'  [Option3] {symbol}: phase 2 — waiting for trailing stop or break-even SL')

        except Exception as e:
            print(f'  [Option3] {symbol}: error — {e}')


# ── Single scan ───────────────────────────────────────────────────────────────
MAX_TRADES_PER_SCAN = 1  # hard cap: never place more than ONE trade per scan (test & production)
# TEST_MODE previously allowed only ONE test trade alive at a time — a single
# slow-moving trade (e.g. sitting flat for hours) silently blocked every new
# signal regardless of score. Now allows several concurrent test trades, same
# as the production open-trade cap, so testing isn't hostage to one trade.
TEST_MAX_CONCURRENT = 3

# Safety rails — enforced in production, logged-only in TEST_MODE so tests keep flowing.
MAX_OPEN_TRADES = 3   # never hold more than this many Option 3 trades at once
MAX_SL_PER_DAY  = 3   # circuit breaker: pause new trades after this many stop-losses in 24h

# Correlation guard. MAX_OPEN_TRADES counts POSITIONS; it cannot see EXPOSURE.
# On 2026-07-29 FET, POL and ADA were each opened on the same oversold + lower-BB
# setup and all three stopped out together for a combined -$1.25. Three
# "independent" trades that were one market move. The cap of 3 was never
# reached, so nothing blocked it, and the journal records it as three data
# points when it is really one.
#
# WHY DOWNSIDE CORRELATION AND NOT PLAIN CORRELATION. The obvious guard —
# Pearson r on hourly returns — was built first and measured against the real
# candles for those three coins. It does not work:
#
#     pair       all bars   hours BTC fell >0.4%
#     FET/POL      +0.394           +0.844
#     ADA/POL      +0.412           +0.598
#     FET/ADA      +0.643           +0.616
#
# On average these coins look only loosely related, and a plain-r guard at any
# threshold loose enough to permit normal trading would have waved all three
# through. But correlations converge in drawdowns — that is the whole
# phenomenon — and 83-100% of hard-down hours saw both coins of every pair red
# together. Average r measures the market you are not afraid of.
#
# So r is computed only over the bars where one side actually fell, and taken as
# the max of both directions so the verdict does not depend on which coin
# happened to be bought first. ~50 of 99 bars survive that filter, which is
# enough to estimate; conditioning on hard-down hours only would be truer still
# but leaves ~6 bars, which is noise.
#
# CORRELATION_MAX is a judgement call, not a derived number: half the downside
# variance shared is enough to call two positions one bet. Be aware that in the
# current universe nearly every pair clears it, so in practice this guard means
# "one open position at a time" until the watchlist holds genuinely unrelated
# assets. That is the finding, not a bug.
CORRELATION_MAX      = 0.50   # refuse a new trade this co-exposed with an open one
CORRELATION_MIN_BARS = 50     # fewer bars than this and the estimate is noise
CORRELATION_MIN_DOWN = 20     # ...and this many must be down bars to condition on


def run_scan(cache, warm_up=False):
    """
    warm_up=True → cache was empty on this run (fresh process, no signal_cache.json).
    Populate state without sending alerts or placing any trades.

    Trade placement uses a two-pass approach when multiple STRONG BUY signals fire:
      Pass 1 — scan all coins, collect every qualified STRONG BUY into `candidates`.
      Pass 2 — rank by composite score, place only the single best trade
               (MAX_TRADES_PER_SCAN = 1); lower-ranked signals wait for a later scan.
      Pass 3 — send all Telegram alerts (including 'cap' notices for skipped coins).
    """
    now = time.time()

    active_trades  = _fetch_option3_trades()
    active_symbols = {t['symbol'] for t in active_trades}

    usdt_balance = 0.0
    if OKX_API_KEY and (CLAUDE_API_KEY or TEST_MODE) and not warm_up:
        usdt_balance = _fetch_usdt_balance()
        if usdt_balance > 0:
            print(f'  [AutoTrade] Available USDT: ${usdt_balance:.2f}')
        else:
            print(f'  [AutoTrade] Balance unavailable — auto-trade disabled this run')

    # ── Pass 1: scan all coins, collect signal data ──────────────────────────
    pending_alerts = []  # (symbol, sig, ticker, trade_result, cache_update)
    candidates     = []  # STRONG BUY coins eligible for auto-trade, to be ranked
    # symbol → 1H closes, kept for the correlation guard. Filled for EVERY scanned
    # coin, not just candidates — the open positions we compare against are exactly
    # the ones that get filtered out of `candidates` further down.
    closes_by_symbol = {}

    for symbol in SYMBOLS:
        try:
            candle_data = fetch_candles(symbol)
            ticker      = fetch_ticker(symbol)
            if not candle_data or not ticker:
                print(f'  {symbol}: no data')
                continue

            opens     = candle_data['opens']
            closes    = candle_data['closes']
            volumes   = candle_data['volumes']
            vol_ratio = calc_vol_ratio(volumes)

            closes_by_symbol[symbol] = closes

            candle_30m = fetch_candles(symbol, bar='30m', limit=50)
            if candle_30m:
                r_opens, r_closes, r_volumes = candle_30m['opens'], candle_30m['closes'], candle_30m['volumes']
            else:
                r_opens, r_closes, r_volumes = opens, closes, volumes

            candle_4h = fetch_candles(symbol, bar='4H', limit=50)
            rsi_4h    = calc_rsi(candle_4h['closes']) if candle_4h and len(candle_4h['closes']) > 14 else None

            rsi_1h    = calc_rsi(closes)
            macd_data = calc_macd(closes)
            bb_data   = calc_bb(closes)
            sig       = generate_signal(rsi_1h, macd_data, bb_data, vol_ratio, rsi_4h)

            if TEST_FORCE_SIGNAL and symbol == 'BTC-USDT':
                sig = {'label': 'STRONG BUY', 'score': 5.0, 'reasons': ['TEST — forced signal, not real']}

            label = sig['label']
            zone  = direction_zone(label)

            prev         = cache.get(symbol, {})
            alerted_zone = prev.get('alerted_zone')
            alerted_at   = prev.get('alerted_at', 0)

            cache[symbol] = {**prev, 'label': label, 'zone': zone}

            if warm_up:
                cache[symbol] = {'label': label, 'zone': zone, 'alerted_zone': zone, 'alerted_at': now}
                print(f'  {symbol}: {label} — warm-up, state saved, no alert')
                time.sleep(0.1)
                continue

            print(f'  {symbol}: {label} (zone={zone}, last_alerted={alerted_zone or "—"})')

            if zone == 'neutral':
                time.sleep(0.3)
                continue

            if label != 'STRONG BUY':
                time.sleep(0.3)
                continue

            if not TEST_MODE and not reversal_confirmed(r_opens, r_closes, r_volumes, zone):
                print(f'  {symbol}: STRONG BUY — no 30min reversal confirmation yet')
                time.sleep(0.3)
                continue

            # Volume confirmation. Deliberately a TRADE gate and not a labelling
            # rule: generate_signal() is mirrored in app.js (parity_check.py keeps
            # them identical), so folding this into the score would mean changing
            # both engines and re-proving parity. Sitting here it behaves like the
            # reversal gate above — the coin still reads STRONG BUY on the
            # dashboard, the worker simply declines to buy it, which the dashboard
            # already communicates for the reversal case.
            if not TEST_MODE and MIN_VOL_RATIO_TRADE > 0 and (
                    vol_ratio is None or vol_ratio < MIN_VOL_RATIO_TRADE):
                shown = f'{vol_ratio:.1f}x' if vol_ratio is not None else 'unknown'
                print(f'  {symbol}: STRONG BUY — volume {shown} below '
                      f'{MIN_VOL_RATIO_TRADE:g}x average, not traded')
                time.sleep(0.3)
                continue

            if zone == alerted_zone:
                secs_in_zone = now - alerted_at
                if secs_in_zone < REZONE_REMINDER:
                    print(f'  {symbol}: still in {zone} zone ({int(secs_in_zone/60)}m) — suppressed')
                    time.sleep(0.3)
                    continue
                print(f'  {symbol}: still in {zone} zone for {int(secs_in_zone/3600)}h — reminder alert')

            if now - alerted_at < FLIP_COOLDOWN:
                secs_left = int(FLIP_COOLDOWN - (now - alerted_at))
                print(f'  {symbol}: zone flip within cooldown ({secs_left}s left) — suppressed')
                time.sleep(0.3)
                continue

            # Coin passed all filters — build cache update once
            cache_update = {**cache[symbol], 'alerted_zone': zone, 'alerted_at': now}

            if symbol in active_symbols:
                # Already has a running trade — signal alert only, no new trade
                print(f'  {symbol}: STRONG BUY — active trade already running, signal only')
                pending_alerts.append((symbol, sig, ticker, None, cache_update))
            else:
                # Eligible for auto-trade — defer to ranking pass
                candidates.append({
                    'symbol':       symbol,
                    'sig':          sig,
                    'ticker':       ticker,
                    'rsi_1h':       rsi_1h,
                    'rsi_4h':       rsi_4h,
                    'macd_data':    macd_data,
                    'bb_data':      bb_data,
                    'vol_ratio':    vol_ratio,
                    'highs':        candle_data['highs'],   # for ATR + support/resistance
                    'lows':         candle_data['lows'],
                    'closes':       closes,
                    'rank_score':   _rank_candidate(sig, rsi_1h, rsi_4h, vol_ratio),
                    'cache_update': cache_update,
                })

        except Exception as e:
            print(f'  {symbol}: ERROR — {e}')

    # ── Pass 2: rank candidates, place top MAX_TRADES_PER_SCAN auto-trades ───
    # TEST_MODE safety: cap concurrent test trades (TEST_MAX_CONCURRENT) instead
    # of blocking on ANY active trade — one slow trade no longer stalls testing.
    if TEST_MODE and candidates:
        free_slots = max(0, TEST_MAX_CONCURRENT - len(active_symbols))
        if free_slots < len(candidates):
            candidates.sort(key=lambda x: x['rank_score'], reverse=True)  # keep the best, drop the rest
            print(f'  [TEST MODE] {len(active_symbols)} active trade(s) — '
                  f'only {free_slots} slot(s) free (cap {TEST_MAX_CONCURRENT})')
            for cand in candidates[free_slots:]:
                pending_alerts.append((cand['symbol'], cand['sig'], cand['ticker'], None, cand['cache_update']))
            candidates = candidates[:free_slots]

    # ── Safety rails: BTC regime, open-trade cap, daily SL circuit breaker ────
    # Enforced in production; in TEST_MODE they are evaluated and logged only,
    # so the test pipeline keeps producing trades regardless of market regime.
    regime_msg = ''
    if candidates:
        regime_ok, regime_msg = btc_regime_ok()
        print(f'  [Regime] {regime_msg}')

        block_reason = None
        if not regime_ok:
            block_reason = 'bearish BTC regime'
        elif len(active_trades) >= MAX_OPEN_TRADES:
            block_reason = f'{len(active_trades)} trades already open (cap: {MAX_OPEN_TRADES})'
        else:
            recent_sl = _count_recent_sl(24)
            if recent_sl >= MAX_SL_PER_DAY:
                block_reason = f'{recent_sl} stop-losses in the last 24h (circuit breaker)'
                cb = cache.get('_circuit_breaker', {})
                if not TEST_MODE and now - cb.get('alerted_at', 0) > 24 * 3600:
                    send_telegram(
                        f"⏸️ <b>Auto-Trading Paused</b>\n"
                        f"🔴 {recent_sl} stop-losses hit in the last 24h — circuit breaker active\n"
                        f"▶️ New trades resume automatically once the 24h window clears"
                    )
                    cache['_circuit_breaker'] = {'alerted_at': now}

        if block_reason:
            if TEST_MODE:
                print(f'  [Safety] {block_reason} — TEST MODE: logged only, trading anyway')
            else:
                print(f'  [Safety] No new trades this scan: {block_reason}')
                for cand in candidates:
                    pending_alerts.append((cand['symbol'], cand['sig'], cand['ticker'], None, cand['cache_update']))
                candidates = []

    # ── Correlation guard: refuse a second copy of a bet we already hold ──────
    # Runs after the rails above, so it only ever filters candidates that were
    # otherwise going to trade. Blocked coins still get their Telegram alert —
    # the signal was real, we just already own that exposure.
    if candidates and active_symbols:
        kept = []
        for cand in candidates:
            blocker, r = correlation_block(cand['symbol'], closes_by_symbol, active_symbols)
            if blocker and TEST_MODE:
                print(f'  [Corr] {cand["symbol"]}: r={r:.2f} vs open {blocker} — '
                      f'TEST MODE: logged only, trading anyway')
                kept.append(cand)
            elif blocker:
                print(f'  [Corr] {cand["symbol"]}: r={r:.2f} vs open {blocker} '
                      f'(cap {CORRELATION_MAX}) — same bet, not a new one')
                pending_alerts.append((cand['symbol'], cand['sig'], cand['ticker'], None, cand['cache_update']))
            else:
                if r is not None:
                    print(f'  [Corr] {cand["symbol"]}: max r={r:.2f} vs open positions — independent enough')
                kept.append(cand)
        candidates = kept

    if candidates:
        candidates.sort(key=lambda x: x['rank_score'], reverse=True)

        top_candidates     = candidates[:MAX_TRADES_PER_SCAN]
        skipped_candidates = candidates[MAX_TRADES_PER_SCAN:]

        print(f'\n  [AutoTrade] {len(candidates)} signal(s) qualified — '
              f'trading top {len(top_candidates)}, skipping {len(skipped_candidates)}')
        for i, c in enumerate(candidates):
            print(f'    #{i+1}  {c["symbol"]}  score={c["rank_score"]:.2f}'
                  f'  ({"TRADE" if i < MAX_TRADES_PER_SCAN else "skip"})')

        for rank_i, cand in enumerate(top_candidates):
            symbol = cand['symbol']

            trade_result = None
            if TEST_MODE and OKX_API_KEY and usdt_balance >= MIN_TRADE_USDT:
                # Test mode: bypass the AI advisor, place a fixed tiny trade so the
                # full Option 3 pipeline (buy → OCO → trailing → monitor → Telegram) runs.
                params = {
                    'amount_usdt':    min(TEST_TRADE_USDT, usdt_balance),
                    'partial_tp_pct': TEST_TP_PCT,
                    'sl_pct':         TEST_SL_PCT,
                    'trailing_pct':   TEST_TRAIL_PCT,
                }
                print(f'  {symbol}: [TEST MODE] placing fixed ${params["amount_usdt"]:.2f} trade (AI bypassed)...')
                try:
                    trade_result = place_option3_trade(symbol, params, cand['ticker'],
                                                       _build_entry_snapshot(cand, None, params))
                    print(f'  {symbol}: [TEST MODE] Option 3 trade placed ✓')
                except Option3Preflight as e:
                    print(f'  {symbol}: trade not viable — {e}')
                    trade_result = 'skip'
                except Exception as e:
                    print(f'  {symbol}: trade placement failed — {e}')
                    trade_result = 'error'
            elif not TEST_MODE and OKX_API_KEY and CLAUDE_API_KEY and usdt_balance >= MIN_TRADE_USDT:
                # Rich decision context: ATR, S/R, suggested exits, funding, OI, order book
                extra = _build_trade_context(cand, regime_msg)
                if extra.get('funding_pct') is not None and extra['funding_pct'] > FUNDING_HARD_SKIP_PCT:
                    reason = (f'funding {extra["funding_pct"]:+.3f}% above '
                              f'+{FUNDING_HARD_SKIP_PCT}% — longs dangerously crowded')
                    print(f'  {symbol}: {reason}, auto-skip')
                    trade_result = 'skip'
                    _log_skipped_setup(symbol, cand['ticker']['price'], reason,
                                       _build_entry_snapshot(cand, extra))
                else:
                    print(f'  {symbol}: asking Claude for trade params '
                          f'(rank #{rank_i + 1}/{len(top_candidates)}, score={cand["rank_score"]:.2f})...')
                    # Filled in place by ai_trade_params — the record of what this
                    # decision was actually shown, stored on the trade or the skip.
                    evidence = {}
                    params, skip_reason = ai_trade_params(
                        symbol, cand['sig'], cand['ticker'], usdt_balance,
                        cand['rsi_1h'], cand['rsi_4h'], cand['macd_data'], cand['bb_data'], cand['vol_ratio'],
                        extra=extra, evidence_out=evidence,
                    )
                    if params:
                        try:
                            trade_result = place_option3_trade(symbol, params, cand['ticker'],
                                                               _build_entry_snapshot(cand, extra, params, evidence))
                            print(f'  {symbol}: Option 3 trade placed ✓  (rank #{rank_i + 1})')
                        except Option3Preflight as e:
                            # A mechanical size limit, not a judgment call — keep it out
                            # of the skip ledger so the AI's record stays honest.
                            print(f'  {symbol}: trade not viable — {e}')
                            trade_result = 'skip'
                        except Exception as e:
                            print(f'  {symbol}: trade placement failed — {e}')
                            trade_result = 'error'
                    else:
                        trade_result = 'skip'
                        _log_skipped_setup(symbol, cand['ticker']['price'], skip_reason,
                                           _build_entry_snapshot(cand, extra, None, evidence))

            pending_alerts.append((symbol, cand['sig'], cand['ticker'], trade_result, cand['cache_update']))

        for i, cand in enumerate(skipped_candidates):
            rank_num = len(top_candidates) + i + 1
            print(f'  {cand["symbol"]}: STRONG BUY ranked #{rank_num} — skipped (cap={MAX_TRADES_PER_SCAN})')
            pending_alerts.append((cand['symbol'], cand['sig'], cand['ticker'], 'cap', cand['cache_update']))

    # ── Pass 3: notify ────────────────────────────────────────────────────────
    # Placed trades and FAILED placements are both reported. skip, cap, and
    # signal-only (None) stay silent — the user only wants to hear about
    # confirmed new trades — but 'error' means an order may have reached OKX,
    # and a money-touching failure must never be silent.
    for symbol, sig, ticker, trade_result, cache_update in pending_alerts:
        if isinstance(trade_result, dict) or trade_result == 'error':
            send_telegram(format_alert(symbol, sig, ticker, trade_result))
        cache[symbol] = cache_update
        time.sleep(0.3)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    cache    = load_cache()
    fresh    = not bool(cache)
    start    = time.time()
    scan_num = 0

    if fresh:
        print('Cache is empty — first scan will populate state only (no alerts, no trades).')

    while True:
        scan_num += 1
        elapsed = time.time() - start
        print(f'\n=== Scan #{scan_num} | +{elapsed:.0f}s | {time.strftime("%H:%M:%S UTC")} ===')

        run_scan(cache, warm_up=(fresh and scan_num == 1))
        monitor_option3_trades()
        if scan_num == 1:
            grade_journal_followups()   # once per run is plenty — nothing here is urgent
            # Long-horizon learning pass: reads the FULL graded history, not just the
            # recent-30 window the per-trade prompt sees. Trigger-gated (fires only
            # after enough new trades) and wrapped so a hiccup here can never take
            # down the trading loop.
            try:
                from learn import run_learning_pass
                run_learning_pass()
            except Exception as e:
                print(f'  [Learn] pass errored (non-fatal): {e}')
        maybe_send_daily_digest(cache)
        save_cache(cache)

        elapsed   = time.time() - start
        remaining = LOOP_DURATION - elapsed

        if remaining <= CHECK_INTERVAL:
            print(f'\nLoop complete — {scan_num} scan(s) in {elapsed:.0f}s.')
            break

        print(f'Next scan in {CHECK_INTERVAL}s...')
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
