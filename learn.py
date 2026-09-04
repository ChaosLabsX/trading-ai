"""Distilled-journal learning pass — the long-horizon complement to the per-trade
journal in signal_checker.py.

The per-trade journal (`_trade_history_context`) shows the AI the last ~30 raw
trades. That window plateaus: trade #500 sees about as much history as trade #40.
This pass reads the ENTIRE graded history instead and turns it into compact,
conditional statistics ("entries with ATR > 4%: 31 trades, PF 0.81, 11 shakeouts"),
so the lesson keeps compounding past the point where the raw window flattens.

Division of labour, on purpose:
  * CODE computes every number. Cohort counts and profit factors are exact
    aggregations — never a model estimate. This is the same discipline the
    per-trade journal already follows (patterns computed in code, not inferred).
  * The MODEL only judges the pre-computed cohorts: which look like a real,
    actionable parameter problem versus ordinary noise, and what bounded change
    follows. That judgement is a small single-turn task, so it runs on the same
    CLAUDE_MODEL as the per-trade advisor rather than reaching for anything
    bigger or pricier.

Two outputs:
  * `learned_block` — statistics-only facts, stored and (once you opt in via
    LEARN_INJECT_INTO_PROMPT) injected into the live trade prompt.
  * `proposals` — parameter changes for you to approve and apply by hand. NEVER
    auto-applied. NEVER a coin blacklist. Whether these reach Telegram is governed
    by LEARN_TELEGRAM below (default 'off' — stored and logged, not pushed).

Runs at most once per worker run, and only after LEARN_TRIGGER_NEW_TRADES newly
graded trades have accumulated since the last pass — so at real trade volume it
fires roughly monthly, on evidence, not on a clock. Degrades silently if the
`learned_rules` migration (docs/ARCHITECTURE.md) has not been run.
"""

import html
import json

import requests

# How many newly-graded trades must accumulate since the last pass before it runs
# again. At this bot's volume that's roughly monthly — the pass fires on evidence,
# not on a schedule, so it never analyses five noisy data points and "learns" from
# them.
LEARN_TRIGGER_NEW_TRADES = 25

# A cohort thinner than this is never reported to the model or acted on. This is
# the single most important guard against manufacturing a false positive from a
# handful of trades — the same reason the trade prompt calls <10 trades "anecdote".
LEARN_MIN_COHORT = 25

# Headroom for adaptive thinking + the structured result. This pass returns more
# than the per-trade advisor (now 8000), so it gets more room again.
LEARN_MAX_TOKENS = 12000

# Where the learning pass sends its findings.
#
#   'off'       — never Telegram. Set on 2026-09-03 at the owner's request: the
#                 routine report is noise when the honest answer is almost always
#                 "no parameter changes". NOTHING is lost by this — the pass still
#                 runs, still computes its cohorts, still writes the distilled block
#                 and the full result to the `learned_rules` table, and still prints
#                 to C:\OKXAI\logs\okx-signal-checker.log. It just stops pushing.
#   'proposals' — Telegram ONLY when the pass actually wants something from you: a
#                 proposed parameter change, or a failed analysis.
#   'always'    — every pass reports, the original behaviour.
#
# Worth knowing before leaving this on 'off': surfacing a proposal is the whole
# reason this pass exists, and proposals are NEVER auto-applied — so on 'off' a real
# recommendation reaches nobody unless you go and read the log or the table.
# 'proposals' gives you the silence without that blind spot.
LEARN_TELEGRAM = 'off'

# OFF by default. The pass writes its distilled block and Telegrams its proposals
# from day one, but NOTHING is injected into a live trade decision until you flip
# this on — after you've read a couple of runs and trust the statistics. Add
# LEARN_INJECT=1 to C:\OKXAI\.env to enable; no code change needed.
import os
LEARN_INJECT_INTO_PROMPT = os.environ.get('LEARN_INJECT', '').lower() in ('1', 'true', 'yes')

# Condition -> bands the cohorts are cut along. Bands are [lo, hi) so they never
# overlap and (within the natural range of each field) never leave a gap.
_BANDS = {
    'atr_pct':     [(0, 2, 'ATR <2%'), (2, 4, 'ATR 2-4%'), (4, 1e9, 'ATR >4%')],
    'rsi_4h':      [(0, 50, 'RSI4H <50'), (50, 60, 'RSI4H 50-60'), (60, 1e9, 'RSI4H >=60')],
    'rsi_1h':      [(0, 35, 'RSI1H <35'), (35, 50, 'RSI1H 35-50'), (50, 1e9, 'RSI1H >=50')],
    'funding_pct': [(-1e9, 0, 'funding <0'), (0, 0.04, 'funding 0-0.04%'), (0.04, 1e9, 'funding >0.04%')],
    'fear_greed':  [(0, 25, 'F&G <=25 (fear)'), (25, 75, 'F&G 25-75'), (75, 1e9, 'F&G >=75 (greed)')],
    'vol_ratio':   [(0, 1.5, 'vol <1.5x'), (1.5, 1e9, 'vol >=1.5x')],
    'score':       [(0, 5.5, 'score <5.5'), (5.5, 1e9, 'score >=5.5')],
}


def _esc(s):
    """Escape model-provided text before it goes into a Telegram HTML message."""
    return html.escape(str(s if s is not None else ''))


def _pf(pnls):
    """Profit factor: gross win / gross loss. 99.0 for an all-wins cohort, 0.0 if
    there were no wins to divide."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss > 0:
        return gross_win / gross_loss
    return 99.0 if gross_win > 0 else 0.0


# --------------------------------------------------------------------- Supabase

def _last_run(headers, base):
    """(created_at of the most recent pass or None, table_exists)."""
    try:
        r = requests.get(
            f'{base}/rest/v1/learned_rules', headers=headers,
            params=[('select', 'created_at'), ('order', 'created_at.desc'), ('limit', '1')],
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json()
            return (rows[0]['created_at'] if rows else None), True
        return None, False
    except Exception:
        return None, False


def _count_new_graded(headers, base, since_ts):
    """Newly graded closed trades since the last pass (or all, on the first pass).
    None if the query fails."""
    # gt.<ts> already implies non-null, so no duplicate followup_at condition is
    # needed. requests URL-encodes the '+' in the timestamp — a literal '+' in a
    # query string would otherwise decode to a space.
    params = [('phase', 'eq.3'), ('exit_reason', 'not.is.null'), ('select', 'id')]
    params.append(('followup_at', f'gt.{since_ts}') if since_ts else ('followup_at', 'not.is.null'))
    try:
        r = requests.get(f'{base}/rest/v1/option3_trades', headers=headers, params=params, timeout=15)
        return len(r.json()) if r.status_code == 200 else None
    except Exception:
        return None


def _fetch_graded_trades(headers, base):
    """Every closed, graded trade with the conditions it was entered on. None on error."""
    try:
        r = requests.get(
            f'{base}/rest/v1/option3_trades', headers=headers,
            params=[('phase', 'eq.3'), ('followup_at', 'not.is.null'), ('exit_reason', 'not.is.null'),
                    ('select', 'symbol,net_pnl_usdt,exit_reason,entry_context,followup'),
                    ('order', 'closed_at.desc'), ('limit', '1000')],
            timeout=20,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _store_run(headers, base, trades_analyzed, cohorts, result):
    payload = {
        'trades_analyzed': trades_analyzed,
        'cohorts': cohorts,
        'summary': (result or {}).get('summary'),
        'learned_block': (result or {}).get('learned_block'),
        'proposals': (result or {}).get('proposals') or [],
    }
    try:
        requests.post(
            f'{base}/rest/v1/learned_rules',
            headers={**headers, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
            json=payload, timeout=15,
        )
    except Exception as e:
        print(f'  [Learn] failed to store run: {e}')


# ------------------------------------------------------------------- analysis

def _compute_cohorts(trades):
    """Cut the graded history into cohorts and compute exact stats for each. Only
    cohorts of at least LEARN_MIN_COHORT trades are returned — everything thinner
    is noise the model must never see."""
    rows = []
    for t in trades:
        pnl = t.get('net_pnl_usdt')
        if pnl is None:
            continue
        rows.append({
            'symbol': t.get('symbol'),
            'pnl': float(pnl),
            'verdict': (t.get('followup') or {}).get('verdict'),
            'ctx': t.get('entry_context') or {},
        })

    cohorts = []

    def summarize(name, dimension, members):
        if len(members) < LEARN_MIN_COHORT:
            return
        pnls = [m['pnl'] for m in members]
        verdicts = [m['verdict'] for m in members]
        cohorts.append({
            'name': name,
            'dimension': dimension,
            'n': len(members),
            'wins': sum(1 for p in pnls if p > 0),
            'losses': sum(1 for p in pnls if p < 0),
            'pf': round(_pf(pnls), 2),
            'avg_pnl': round(sum(pnls) / len(pnls), 2),
            'total_pnl': round(sum(pnls), 2),
            # All seven verdict classes _grade_exit() can produce. Only three were
            # counted until 2026-08-02, which dropped 9 of the 13 verdicts in the
            # live record — including partial_recovery, the most common one. A pass
            # that exists to find conditional patterns cannot do it on a third of
            # its own data.
            'shakeouts': verdicts.count('shakeout'),
            'partial_recoveries': verdicts.count('partial_recovery'),
            'flat_after_stop': verdicts.count('flat_after_stop'),
            'good_saves': verdicts.count('good_save'),
            'left_money': verdicts.count('left_money'),
            'well_timed': verdicts.count('well_timed'),
            'fair_exits': verdicts.count('fair_exit'),
        })

    summarize('Overall', 'overall', rows)

    for dim, bands in _BANDS.items():
        for lo, hi, label in bands:
            members = [
                m for m in rows
                if m['ctx'].get(dim) is not None and lo <= float(m['ctx'][dim]) < hi
            ]
            summarize(label, dim, members)

    # Per-coin cohorts are surfaced for sizing/exit tuning only. The model is told,
    # explicitly, that it may never turn one into a blacklist — a coin's small
    # sample is exactly the kind of thin evidence that manufactures a false rule.
    by_coin = {}
    for m in rows:
        by_coin.setdefault(m['symbol'], []).append(m)
    for sym, members in by_coin.items():
        summarize(sym, 'symbol', members)

    return cohorts


def _ask_opus(cohorts, params):
    """Hand the pre-computed cohorts to Opus for judgement only. Returns
    {summary, learned_block, proposals} or None on any failure (the caller then
    still records the run so the trigger advances)."""
    from signal_checker import CLAUDE_API_KEY, CLAUDE_MODEL
    if not CLAUDE_API_KEY:
        print('  [Learn] CLAUDE_API_KEY not set — recording stats without analysis')
        return None

    schema = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'summary': {'type': 'string'},
            'learned_block': {'type': 'string'},
            'proposals': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'parameter': {'type': 'string'},
                        'current': {'type': 'string'},
                        'proposed': {'type': 'string'},
                        'rationale': {'type': 'string'},
                    },
                    'required': ['parameter', 'current', 'proposed', 'rationale'],
                },
            },
        },
        'required': ['summary', 'learned_block', 'proposals'],
    }

    system = f"""You are a quantitative analyst reviewing an OKX spot-trading bot's OWN closed-trade history.

The statistics have ALREADY been computed for you in code — exact trade counts and
profit factors per cohort. You do NOT compute, estimate, or re-derive any number;
you interpret the ones you are given.

Your job:
1. Decide which cohorts show a REAL, actionable parameter problem versus ordinary noise.
2. Propose specific, bounded parameter changes, each backed by one cohort's numbers.
3. Write a short LEARNED block of statistics-only facts to inject into the trade prompt.

Hard rules:
- Reason ONLY from cohorts with n >= {LEARN_MIN_COHORT}. Ignore anything thinner.
- NEVER propose blacklisting, banning, or excluding a coin. Per-coin cohorts exist for
  sizing/exit tuning only; a single coin's sample never justifies dropping it.
- Proposals must stay within the stated bounds and touch only the parameters listed.
  Do not invent new parameters or rules.
- The LEARNED block is DESCRIPTIVE — what the data shows, no directives. Proposals are
  the ONLY place you prescribe.
- "No change" is the correct answer more often than not. A profit factor near 1.0, or a
  cohort barely over the size floor, is not evidence. If nothing is clearly actionable,
  return an empty proposals list and say so plainly.

Tunable parameters (with current values and bounds):
- ATR_SL_MULT = {params['ATR_SL_MULT']}   (stop = N x ATR; widen on cohorts with high shakeout counts)
- ATR_TP_MULT = {params['ATR_TP_MULT']}   (take-profit = N x ATR)
- ATR_TRAIL_MULT = {params['ATR_TRAIL_MULT']}   (trailing callback = N x ATR; widen when winners leave money)
- SL_BOUNDS = {params['SL_BOUNDS']}, TP_BOUNDS = {params['TP_BOUNDS']}, TRAIL_BOUNDS = {params['TRAIL_BOUNDS']}  (hard % clamps)
- FUNDING_HARD_SKIP_PCT = {params['FUNDING_HARD_SKIP_PCT']}   (auto-skip above this 8h funding rate)

Verdict fields in the cohort data (every class the grader produces — a cohort's
stop-loss verdicts are shakeouts + partial_recoveries + flat_after_stop + good_saves):
- shakeouts          = stop-losses where price then reached our original target -> stop too tight.
- partial_recoveries = stop-losses where price bounced >=2% afterwards but never reached our
                       target -> weaker evidence in the same direction as a shakeout. Treat a
                       cohort as having a stop-width problem only when shakeouts alone are
                       substantial; partial_recoveries can corroborate that, never establish it
                       on their own (a 2% bounce after a stop is common noise).
- flat_after_stop    = price went nowhere after the stop -> the stop was neither a mistake nor
                       a save; it is the neutral baseline for how often stops simply fire.
- good_saves         = stop-losses where price kept falling -> the stop earned its keep.
- left_money         = winners where price ran well past our exit -> the trail was too tight.
- well_timed         = winners where price dropped after we exited -> the exit was correct.
- fair_exits         = winners where price drifted after our exit -> neutral.

Note the asymmetry to watch for: if a cohort's winners are mostly fair_exits and well_timed
while its losers are mostly good_saves, the exits are working and any problem is upstream in
the ENTRY rule — which is NOT in your list of tunable parameters. Say so in the summary rather
than proposing an exit change that cannot fix it."""

    user = ('Computed cohorts (each already meets the minimum sample size):\n'
            + json.dumps(cohorts, indent=2)
            + '\n\nAnalyze and return your structured result.')

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': CLAUDE_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': CLAUDE_MODEL,
                'max_tokens': LEARN_MAX_TOKENS,
                'thinking': {'type': 'adaptive'},
                'system': system,
                'messages': [{'role': 'user', 'content': user}],
                'output_config': {'format': {'type': 'json_schema', 'schema': schema}},
            },
            timeout=120,
        )
        r.raise_for_status()
        blocks = r.json().get('content', [])
        # With thinking enabled the first block may be a thinking block — take the
        # text block explicitly, and never assume it is present.
        text = next((b.get('text', '') for b in blocks if b.get('type') == 'text'), '').strip()
        if not text:
            print('  [Learn] no text block from Claude (thinking may have consumed max_tokens) — recording stats only')
            return None
        return json.loads(text)
    except Exception as e:
        print(f'  [Learn] analysis call failed: {e}')
        return None


def _report(result, n):
    from signal_checker import send_telegram
    proposals = result.get('proposals') or []
    if LEARN_TELEGRAM == 'off' or (LEARN_TELEGRAM == 'proposals' and not proposals):
        print(f'  [Learn] report suppressed (LEARN_TELEGRAM={LEARN_TELEGRAM!r}, '
              f'{len(proposals)} proposal(s)) — stored in learned_rules')
        return
    lines = [
        f'🧠 <b>Learning pass</b> — {n} graded trades analyzed',
        '',
        _esc(result.get('summary', '').strip()),
    ]
    if proposals:
        lines += ['', '<b>Proposed changes</b> (nothing applied automatically):']
        for p in proposals:
            lines.append(f"• <b>{_esc(p.get('parameter'))}</b> {_esc(p.get('current'))} → {_esc(p.get('proposed'))}")
            lines.append(f"  {_esc(p.get('rationale'))}")
        lines += ['', 'Edit <code>signal_checker.py</code> to apply.']
    else:
        lines += ['', 'No parameter changes proposed — cohorts look like noise or already well-tuned.']
    lines += ['', f"Prompt injection: {'ON' if LEARN_INJECT_INTO_PROMPT else 'OFF (report-only)'}"]
    send_telegram('\n'.join(lines))


# --------------------------------------------------------------------- entry points

def run_learning_pass():
    """Trigger-gated full-history learning pass. Safe to call on every run —
    it does nothing until enough new trades have accumulated, and never raises into
    the caller (the trade loop must not die because analysis hiccuped)."""
    from signal_checker import (
        SUPABASE_URL, SUPABASE_KEY, _sb_headers,
        ATR_SL_MULT, ATR_TP_MULT, ATR_TRAIL_MULT,
        SL_BOUNDS, TP_BOUNDS, TRAIL_BOUNDS, FUNDING_HARD_SKIP_PCT,
    )
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    headers = _sb_headers()

    last_ts, table_ok = _last_run(headers, SUPABASE_URL)
    if not table_ok:
        print('  [Learn] learned_rules table missing — run the SQL migration '
              '(docs/ARCHITECTURE.md). Learning pass inactive.')
        return

    new_count = _count_new_graded(headers, SUPABASE_URL, last_ts)
    if new_count is None:
        print('  [Learn] could not count new graded trades — skipping this run')
        return
    if new_count < LEARN_TRIGGER_NEW_TRADES:
        print(f'  [Learn] {new_count}/{LEARN_TRIGGER_NEW_TRADES} newly-graded trades since '
              f'last pass — not enough yet, skipping')
        return

    trades = _fetch_graded_trades(headers, SUPABASE_URL)
    if not trades:
        print('  [Learn] no graded trades to analyze')
        return

    cohorts = _compute_cohorts(trades)
    if not cohorts:
        # Record the run anyway so the trigger advances and we don't recompute
        # every scan until the next trade lands.
        print(f'  [Learn] {len(trades)} trades but no cohort reaches n>={LEARN_MIN_COHORT} yet')
        _store_run(headers, SUPABASE_URL, len(trades), [], None)
        return

    params = {
        'ATR_SL_MULT': ATR_SL_MULT, 'ATR_TP_MULT': ATR_TP_MULT, 'ATR_TRAIL_MULT': ATR_TRAIL_MULT,
        'SL_BOUNDS': list(SL_BOUNDS), 'TP_BOUNDS': list(TP_BOUNDS), 'TRAIL_BOUNDS': list(TRAIL_BOUNDS),
        'FUNDING_HARD_SKIP_PCT': FUNDING_HARD_SKIP_PCT,
    }
    result = _ask_opus(cohorts, params)
    _store_run(headers, SUPABASE_URL, len(trades), cohorts, result)
    if result:
        _report(result, len(trades))
    else:
        # The stats are stored, but analysis didn't return — say so rather than go
        # silent (this repo has a history of silent failures being the expensive kind).
        print(f'  [Learn] analysis returned nothing — cohorts stored, no proposals')
        if LEARN_TELEGRAM != 'off':
            try:
                from signal_checker import send_telegram
                send_telegram(f'🧠 Learning pass ran on {len(trades)} trades but the analysis '
                              f'call returned nothing — cohorts stored, no proposals this run.')
            except Exception:
                pass
    print(f'  [Learn] pass complete — {len(trades)} trades, {len(cohorts)} cohorts, '
          f"{len(result['proposals']) if result else 0} proposal(s)")


def _learned_rules_context():
    """The latest distilled LEARNED block, for injection into the trade prompt.

    Returns '' unless LEARN_INJECT_INTO_PROMPT is enabled — the pass reports to
    Telegram and stores its block from day one, but nothing touches a live decision
    until you deliberately turn injection on."""
    if not LEARN_INJECT_INTO_PROMPT:
        return ''
    from signal_checker import SUPABASE_URL, SUPABASE_KEY, _sb_headers
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ''
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/learned_rules', headers=_sb_headers(),
            params=[('select', 'learned_block'), ('learned_block', 'not.is.null'),
                    ('order', 'created_at.desc'), ('limit', '1')],
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0].get('learned_block') or ''
    except Exception:
        pass
    return ''
