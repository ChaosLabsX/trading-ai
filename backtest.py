"""
OKX AI — Backtesting harness ("time machine" for the bot's rules)

Replays the PRODUCTION signal + Option 3 exit logic over historical OKX candles
and prints a report card: trades, win rate, profit factor, net P&L, drawdown,
per-coin breakdown. Imports the real functions from signal_checker.py, so what
is tested is exactly what trades — not a copy that can drift.

Usage (run locally, never touches OKX private APIs / places no orders):
  python backtest.py                                    # 90 days, all coins, production settings
  python backtest.py --days 60 --score 4.5              # try a looser STRONG BUY bar
  python backtest.py --no-regime                        # measure the BTC filter's effect
  python backtest.py --atr-sl 3.0                       # try wider stops
  python backtest.py --coins BTC-USDT,SOL-USDT --days 30
  python backtest.py --refresh                          # re-download candles (else cached)

Honest limitations (also printed with every report):
  - The AI layer isn't simulated — exits come from the deterministic ATR+structure
    baseline (suggest_exit_params), which is what the AI starts from.
  - Reversal confirmation uses 1H candles (production uses 30m — close approximation).
  - No funding/news/order-book history; entries fill at the NEXT candle's open;
    when TP and SL fall inside the same candle, SL is assumed first (conservative);
    0.1% taker fee is charged on every fill (ignores the maker saving).
  - A backtest is a report card on the past, not a promise about the future.
    Change few knobs, prefer settings that win across different periods.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests

import signal_checker as sc

CACHE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_cache')
FEE         = 0.001          # taker fee per fill side (conservative)
COOLDOWN_MS = 4 * 3600_000   # per-coin re-entry cooldown after a close (≈ REZONE_REMINDER)
HOUR_MS     = 3600_000


# ── Historical data (public endpoint, cached on disk) ─────────────────────────
def fetch_history(inst, bar, need, refresh=False):
    """Oldest→newest candles [{ts,o,h,l,c,v}] via /market/history-candles (paginated)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, f'{inst}_{bar}_{need}.json')
    if not refresh and os.path.exists(fp):
        with open(fp) as f:
            return json.load(f)
    rows, after, retries = [], '', 0
    while len(rows) < need:
        url = f'https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&limit=100'
        if after:
            url += f'&after={after}'
        try:
            d = requests.get(url, timeout=15).json()
        except Exception:
            retries += 1
            if retries > 3:
                break
            time.sleep(2)
            continue
        if d.get('code') != '0' or not d.get('data'):
            break
        batch = d['data']                      # newest-first
        rows.extend(batch)
        after = batch[-1][0]                   # page back from the oldest ts
        time.sleep(0.12)                       # stay under OKX rate limits
        if len(batch) < 100:
            break
    rows = rows[:need][::-1]                   # oldest first
    candles = [{'ts': int(c[0]), 'o': float(c[1]), 'h': float(c[2]),
                'l': float(c[3]), 'c': float(c[4]), 'v': float(c[5])} for c in rows]
    with open(fp, 'w') as f:
        json.dump(candles, f)
    return candles


# ── Trade simulation ──────────────────────────────────────────────────────────
def _net_frac(entry, exit_px):
    """Return fraction on one (half-)position incl. buy+sell fees."""
    return (exit_px - entry) / entry - FEE * (1 + exit_px / entry)


def step_trade(tr, cd, stake):
    """Advance one candle. Returns (exit_type, pnl, exit_px) when closed, else None."""
    e = tr['entry']
    if tr['phase'] == 1:
        tp_px = e * (1 + tr['tp'] / 100)
        # tr['sl'] is None under --no-sl: the position simply never stops out and
        # holds until TP, or until the replay ends and it is marked to market as
        # 'eod'. That force-close is what keeps the comparison honest — an
        # underwater position left open is a loss you have not booked, not a loss
        # you have avoided, and it must show up in the P&L either way.
        if tr['sl'] is not None:
            sl_px = e * (1 - tr['sl'] / 100)
            if cd['l'] <= sl_px:                               # SL first when ambiguous (conservative)
                fill = cd['o'] if cd['o'] <= sl_px else sl_px  # gap-down fills worse
                return 'sl', _net_frac(e, fill) * stake, fill
        if cd['h'] >= tp_px:                                   # partial TP fills
            fill = cd['o'] if cd['o'] >= tp_px else tp_px      # gap-up fills better
            tr['phase']  = 2
            tr['banked'] = _net_frac(e, fill) * (stake / 2)
            tr['peak']   = fill                                # trailing starts here (next candle on)
        return None
    # phase 2 — trailing stop on the second half (floor ≥ entry since trail < TP)
    floor = tr['peak'] * (1 - tr['trail'] / 100)
    if cd['l'] <= floor:
        fill = cd['o'] if cd['o'] <= floor else floor
        return 'tp+trail', tr['banked'] + _net_frac(e, fill) * (stake / 2), fill
    tr['peak'] = max(tr['peak'], cd['h'])
    return None


# ── The replay ────────────────────────────────────────────────────────────────
def run(args):
    coins = [c.strip() for c in args.coins.split(',')] if args.coins else list(sc.SYMBOLS)
    need_1h = args.days * 24 + 120                 # +warm-up
    need_4h = args.days * 6 + 70

    # Apply parameter overrides to the REAL module so the real functions use them
    sc.STRONG_BUY_SCORE  = args.score
    sc.STRONG_SELL_SCORE = -args.score
    if args.atr_tp:    sc.ATR_TP_MULT    = args.atr_tp
    if args.atr_sl:    sc.ATR_SL_MULT    = args.atr_sl
    if args.atr_trail: sc.ATR_TRAIL_MULT = args.atr_trail

    print(f'Fetching history: {len(coins)} coin(s) × {args.days} days (cached after first run)...')
    data, data4 = {}, {}
    for i, coin in enumerate(coins):
        c1 = fetch_history(coin, '1H', need_1h, args.refresh)
        c4 = fetch_history(coin, '4H', need_4h, args.refresh)
        if len(c1) >= 200:
            data[coin], data4[coin] = c1, c4
        else:
            print(f'  {coin}: only {len(c1)} 1H candles — skipped')
        print(f'\r  {i + 1}/{len(coins)} done', end='')
    print()
    btc4 = data4.get('BTC-USDT') or fetch_history('BTC-USDT', '4H', need_4h, args.refresh)
    if not data:
        print('No usable data.'); return

    # Fast lookups
    arr   = {c: {k: [cd[k] for cd in v] for k in ('ts', 'o', 'h', 'l', 'c', 'v')} for c, v in data.items()}
    tsmap = {c: {t: i for i, t in enumerate(a['ts'])} for c, a in arr.items()}
    master = arr.get('BTC-USDT', arr[list(arr)[0]])['ts']

    p4      = {c: 0 for c in data}      # per-coin 4H pointer
    rsi4    = {c: None for c in data}
    pbtc    = 0
    regime_ok, regime_cache_idx = True, -1
    open_tr, closed, cooldown = {}, [], {}
    stats = {'signals': 0, 'blocked_regime': 0, 'blocked_slots': 0, 'no_reversal': 0,
             'no_atr': 0, 'low_volume': 0}

    for mts in master:
        # 1. advance open trades
        for coin in list(open_tr):
            tr  = open_tr[coin]
            idx = tsmap[coin].get(mts)
            if idx is None or idx < tr['entry_idx']:
                continue
            cd = {k: arr[coin][k][idx] for k in ('o', 'h', 'l', 'c')}
            out = step_trade(tr, cd, args.stake)
            if out:
                etype, pnl, _ = out
                closed.append({'coin': coin, 'type': etype, 'pnl': pnl,
                               'hours': (mts - tr['entry_ts']) / HOUR_MS})
                del open_tr[coin]
                cooldown[coin] = mts + COOLDOWN_MS

        # 2. BTC regime (recompute only when a new 4H candle appears)
        if not args.no_regime:
            ts4 = [c['ts'] for c in btc4]
            while pbtc + 1 < len(ts4) and ts4[pbtc + 1] <= mts:
                pbtc += 1
            if pbtc != regime_cache_idx and pbtc >= 60:
                closes = [c['c'] for c in btc4[:pbtc + 1]][-100:]
                ema50  = sc.ema_array(closes, 50)[-1]
                r      = sc.calc_rsi(closes)
                regime_ok = not (closes[-1] < ema50 and r is not None and r < 45)
                regime_cache_idx = pbtc

        # 3. new entries
        slots = args.max_open - len(open_tr)
        if slots <= 0:
            continue
        cands = []
        for coin, a in arr.items():
            if coin in open_tr or cooldown.get(coin, 0) > mts:
                continue
            i = tsmap[coin].get(mts)
            if i is None or i < 100 or i + 1 >= len(a['ts']):
                continue
            closes, opens_, vols = a['c'][max(0, i - 99):i + 1], a['o'][max(0, i - 99):i + 1], a['v'][max(0, i - 99):i + 1]
            highs,  lows         = a['h'][max(0, i - 99):i + 1], a['l'][max(0, i - 99):i + 1]
            # per-coin 4H RSI (advance pointer, recompute on new candle)
            c4 = data4.get(coin) or []
            ts4c = [c['ts'] for c in c4]
            moved = False
            while p4[coin] + 1 < len(ts4c) and ts4c[p4[coin] + 1] <= mts:
                p4[coin] += 1; moved = True
            if (moved or rsi4[coin] is None) and p4[coin] >= 15:
                rsi4[coin] = sc.calc_rsi([c['c'] for c in c4[:p4[coin] + 1]][-50:])

            rsi  = sc.calc_rsi(closes)
            macd = sc.calc_macd(closes)
            bb   = sc.calc_bb(closes)
            volr = sc.calc_vol_ratio(vols)
            sig  = sc.generate_signal(rsi, macd, bb, volr, rsi4[coin])
            if sig['label'] != 'STRONG BUY':
                continue
            stats['signals'] += 1
            if not args.no_regime and not regime_ok:
                stats['blocked_regime'] += 1
                continue
            if not args.no_reversal and not sc.reversal_confirmed(opens_[-30:], closes[-30:], vols[-30:], 'up'):
                stats['no_reversal'] += 1
                continue
            # Mirrors the production volume trade-gate. --min-vol overrides it so the
            # gate itself can be measured; without this line the replay would quietly
            # report a looser strategy than the one actually running.
            if args.min_vol > 0 and (volr is None or volr < args.min_vol):
                stats['low_volume'] += 1
                continue
            atr = sc.calc_atr_pct(highs, lows, closes)
            sup, res = sc.find_support_resistance(highs, lows, closes)
            ex = sc.suggest_exit_params(atr, sup, res, closes[-1])
            if not ex:
                stats['no_atr'] += 1
                continue
            cands.append((sc._rank_candidate(sig, rsi, rsi4[coin], volr), coin, i, ex))

        cands.sort(reverse=True)
        for rank, coin, i, ex in cands[:min(args.per_scan, slots)]:
            open_tr[coin] = {'entry': arr[coin]['o'][i + 1], 'entry_idx': i + 1,
                             'entry_ts': arr[coin]['ts'][i + 1], 'phase': 1,
                             'tp': ex['tp'], 'sl': None if args.no_sl else ex['sl'],
                             'trail': ex['trail']}
        if len(cands) > min(args.per_scan, slots):
            stats['blocked_slots'] += len(cands) - min(args.per_scan, slots)

    # force-close whatever is still open at the end of data
    for coin, tr in open_tr.items():
        last = arr[coin]['c'][-1]
        pnl  = (_net_frac(tr['entry'], last) * args.stake if tr['phase'] == 1
                else tr['banked'] + _net_frac(tr['entry'], last) * (args.stake / 2))
        closed.append({'coin': coin, 'type': 'eod', 'pnl': pnl,
                       'hours': (arr[coin]['ts'][-1] - tr['entry_ts']) / HOUR_MS})

    report(args, coins, closed, stats)


def report(args, coins, closed, stats):
    print('\n' + '=' * 62)
    print(f'BACKTEST — {args.days} days · {len(coins)} coin(s) · score ≥ {args.score} · '
          f'stake ${args.stake}/trade')
    print(f'ATR mults TP/SL/trail: {sc.ATR_TP_MULT}/{sc.ATR_SL_MULT}/{sc.ATR_TRAIL_MULT} · '
          f'regime filter: {"OFF" if args.no_regime else "ON"} · '
          f'reversal gate: {"OFF" if args.no_reversal else "ON"}')
    print('=' * 62)
    if not closed:
        print('No trades triggered. Try --score lower, more --days, or more coins.')
        print(f"(signals seen: {stats['signals']}, regime-blocked: {stats['blocked_regime']}, "
              f"no-reversal: {stats['no_reversal']})")
        return
    pnls   = [t['pnl'] for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net    = sum(pnls)
    pf     = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float('inf')
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd   = max(dd, peak - cum)
    by_type = {}
    for t in closed:
        by_type[t['type']] = by_type.get(t['type'], 0) + 1
    print(f'Trades: {len(closed)}   Wins: {len(wins)}   Losses: {len(losses)}   '
          f'Win rate: {len(wins) / len(closed) * 100:.0f}%')
    print(f'Net P&L: {net:+.2f} USD on ${args.stake} stakes  '
          f'({net / args.stake * 100:+.1f}% of one stake; avg {net / len(closed):+.2f}/trade)')
    print(f'Profit factor: {pf:.2f}   Max drawdown: {dd:.2f} USD   '
          f'Avg hold: {sum(t["hours"] for t in closed) / len(closed):.0f}h')
    print(f'Exits: {by_type}')
    # 'eod' = never reached an exit; still open when the data ran out. Under
    # --no-sl these are the "waiting for the pump" positions, valued at the last
    # close. Their count and age is the real cost of the strategy: capital that
    # was locked and could not take the next signal.
    eod = [t for t in closed if t['type'] == 'eod']
    if eod:
        held = sum(t['hours'] for t in eod) / len(eod)
        print(f'Never exited: {len(eod)} of {len(closed)} ({len(eod) / len(closed) * 100:.0f}%) '
              f'still open at end, avg age {held:.0f}h ({held / 24:.1f} days), '
              f'marked to market at {sum(t["pnl"] for t in eod):+.2f} USD')
    print(f"Gates: {stats['signals']} STRONG BUY signals → regime-blocked {stats['blocked_regime']}, "
          f"no-reversal {stats['no_reversal']}, low-volume {stats['low_volume']}, "
          f"slot-capped {stats['blocked_slots']}, no-ATR {stats['no_atr']}")
    by_coin = {}
    for t in closed:
        by_coin.setdefault(t['coin'], [0.0, 0])
        by_coin[t['coin']][0] += t['pnl']
        by_coin[t['coin']][1] += 1
    print('\nPer coin (net / trades):')
    for coin, (p, n) in sorted(by_coin.items(), key=lambda kv: -kv[1][0]):
        print(f'  {coin.replace("-USDT", ""):>6}: {p:+8.2f}  ({n})')
    print('\n⚠ Past performance is a report card, not a promise. Prefer settings that')
    print('  win across DIFFERENT periods; treat small differences as noise.')


def main():
    # The report uses >=, mid-dot, arrow and warning glyphs. A default Windows
    # console is cp1252 and raises UnicodeEncodeError on the first of them —
    # after the fetch and the entire replay have already run, so the whole run
    # is lost at the print. Widen the stream rather than hunting the glyphs;
    # errors='replace' means a stranger console degrades instead of crashing.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(description='Replay TradingAI production rules over OKX history.')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--coins', default='')
    # Default tracks the live constant instead of restating it. When production
    # moved 5.0 → 4.5 this argument still said 5.0, so an unflagged run would have
    # quietly measured a threshold the bot no longer uses — and reported it as the
    # baseline. Same defect class as a prompt naming a bound the code doesn't have.
    ap.add_argument('--score', type=float, default=sc.STRONG_BUY_SCORE,
                    help=f'STRONG BUY threshold (production {sc.STRONG_BUY_SCORE:g})')
    ap.add_argument('--atr-tp', type=float, default=None)
    ap.add_argument('--atr-sl', type=float, default=None)
    ap.add_argument('--atr-trail', type=float, default=None)
    ap.add_argument('--stake', type=float, default=100.0)
    ap.add_argument('--max-open', type=int, default=3)
    ap.add_argument('--per-scan', type=int, default=1)
    ap.add_argument('--no-sl', action='store_true',
                    help='place trades with NO stop-loss and hold until TP (or end of data, '
                         'marked to market). Tests "it will pump eventually" against history.')
    ap.add_argument('--min-vol', type=float, default=sc.MIN_VOL_RATIO_TRADE,
                    help=f'volume-ratio trade gate (production {sc.MIN_VOL_RATIO_TRADE:g}; '
                         f'0 disables)')
    ap.add_argument('--no-regime', action='store_true')
    ap.add_argument('--no-reversal', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    run(ap.parse_args())


if __name__ == '__main__':
    main()
