"""
OKX AI — signal parity check (browser vs worker)

The scoring engine exists TWICE: `generate_signal()` in signal_checker.py, which
decides what actually gets traded, and `generateSignal()` in app.js, which decides
what the dashboard shows you. They are supposed to be the same function.

They were not. Until 2026-08-15 the browser applied the volume term after the 4H
term instead of before, awarded its point from 1.5x volume where the worker
requires 2.0x, gated it on an existing +/-2 score where the worker does not gate,
and carried a -1 "selling pressure" branch the worker has never had. Ordering
alone is enough to change the answer, because the 4H term keys off the sign and
size of the running score. Across the grid below the two disagreed on 257 scores
and 80 LABELS - the dashboard could show STRONG BUY for a coin the worker scored a
full point lower and would never trade. docs/DASHBOARD.md had described this as a
"minor asymmetry ... same intent".

A duplicated function with no test drifts silently, and this one drifts into
showing you a trade signal the bot does not have. So: run this after touching
either implementation.

  python parity_check.py            # exits non-zero on any mismatch

Reason strings are compared too, not just scores. They are not cosmetic - the
worker pastes them into the Claude prompt verbatim as "Confirmed by: ...", which
is what made the 2026-07-29 4H mislabelling a trading bug and not a typo.

Requires `node` on PATH. signal_checker.py is the source of truth: if this fails,
the fix belongs in app.js unless you have decided otherwise on purpose.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signal_checker as sc  # noqa: E402

# Deliberately straddles every branch boundary in both files: the RSI cuts
# (20/30/40/60/70/80), both %B tails, the volume steps at 1.5 and 2.0, and 4H
# values either side of 30/40/55/70 - including the sign flips that make the 4H
# term order-dependent.
RSI_1H = [15, 18, 25, 27, 35, 45, 50, 62, 72, 85]
MACD = {
    'cross': ({'bullishCross': True,  'bearishCross': False, 'trend': 'bullish'},
              {'bullish_cross': True, 'bearish_cross': False, 'trend': 'bullish'}),
    'bear_x': ({'bullishCross': False,  'bearishCross': True,  'trend': 'bearish'},
               {'bullish_cross': False, 'bearish_cross': True, 'trend': 'bearish'}),
    'bull': ({'bullishCross': False,  'bearishCross': False, 'trend': 'bullish'},
             {'bullish_cross': False, 'bearish_cross': False, 'trend': 'bullish'}),
    'bear': ({'bullishCross': False,  'bearishCross': False, 'trend': 'bearish'},
             {'bullish_cross': False, 'bearish_cross': False, 'trend': 'bearish'}),
}
PCT_B = [0.02, 0.15, 0.5, 0.85, 0.97]
RSI_4H = [22, 26, 33, 42, 50, 58, 72]
VOL = [0.8, 1.0, 1.4, 1.6, 1.9, 2.0, 2.4, 3.5]

# Loads config.js (for CONFIG.STRONG_BUY_SCORE) and lifts generateSignal out of
# app.js by source slice, so the browser file is exercised as it actually ships -
# no second copy of the logic to drift in its own right.
JS_DRIVER = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const [here, gridPath, outPath] = process.argv.slice(2);

const cfg = fs.readFileSync(path.join(here, 'config.js'), 'utf8');
// Split on /\r?\n/, not '\n'. On Windows these files are routinely CRLF in the
// working tree (git normalises to LF only on commit), and a bare '\n' split
// leaves a trailing '\r' on every line — which silently breaks the `line === '}'`
// scan below and makes this check fail closed with 'could not find the end of
// generateSignal'. A parity test that cannot run is worse than no parity test.
const lines = fs.readFileSync(path.join(here, 'app.js'), 'utf8').split(/\r?\n/);

const start = lines.findIndex(l => l.startsWith('function generateSignal('));
if (start < 0) { console.error('generateSignal not found in app.js'); process.exit(2); }
let end = -1;
for (let i = start + 1; i < lines.length; i++) { if (lines[i] === '}') { end = i; break; } }
if (end < 0) { console.error('could not find the end of generateSignal'); process.exit(2); }
const fn = lines.slice(start, end + 1).join('\n');

const probe = `(() => grid.map(([rsi, macd, pctB, r4, vr]) => {
  const s = generateSignal(rsi, macd, { pctB }, r4, vr);
  return [s.score, s.label, s.reasons];
}))()`;

const ctx = { grid: JSON.parse(fs.readFileSync(gridPath, 'utf8')), console };
vm.createContext(ctx);
fs.writeFileSync(outPath, JSON.stringify(vm.runInContext(cfg + '\n' + fn + '\n' + probe, ctx)));
"""


def main():
    node = shutil.which('node')
    if not node:
        print('SKIP: node not on PATH - cannot exercise app.js')
        return 2

    cases = [(rsi, mk, pctb, r4, vr)
             for rsi in RSI_1H for mk in MACD for pctb in PCT_B
             for r4 in RSI_4H for vr in VOL]

    tmp = tempfile.mkdtemp(prefix='parity_')
    grid_fp, out_fp = os.path.join(tmp, 'grid.json'), os.path.join(tmp, 'out.json')
    with open(grid_fp, 'w') as f:
        json.dump([[rsi, MACD[mk][0], pctb, r4, vr] for rsi, mk, pctb, r4, vr in cases], f)

    driver_fp = os.path.join(tmp, 'driver.js')
    with open(driver_fp, 'w') as f:
        f.write(JS_DRIVER)

    r = subprocess.run([node, driver_fp, HERE, grid_fp, out_fp],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('FAIL: could not run app.js generateSignal')
        print(r.stderr.strip())
        return 2
    js = json.load(open(out_fp))

    bad_score = bad_label = bad_reasons = 0
    examples = []
    for (rsi, mk, pctb, r4, vr), (js_score, js_label, js_reasons) in zip(cases, js):
        py = sc.generate_signal(rsi, MACD[mk][1], {'pct_b': pctb}, vr, r4)
        s_ok = abs(py['score'] - js_score) < 1e-9
        l_ok = py['label'] == js_label
        r_ok = py['reasons'] == js_reasons
        bad_score += not s_ok
        bad_label += not l_ok
        bad_reasons += not r_ok
        if not (s_ok and l_ok and r_ok) and len(examples) < 10:
            examples.append(
                f'  RSI {rsi:<3} macd {mk:<6} %B {pctb:<5} 4H {r4:<3} vol {vr:<4}\n'
                f'      worker    {py["score"]:>5}  {py["label"]:<11} {py["reasons"]}\n'
                f'      dashboard {js_score:>5}  {js_label:<11} {js_reasons}')

    n = len(cases)
    print(f'Compared {n:,} cases  (signal_checker.py vs app.js)')
    print(f'  STRONG_BUY_SCORE: worker {sc.STRONG_BUY_SCORE:g}')
    print(f'  score  mismatches: {bad_score}')
    print(f'  label  mismatches: {bad_label}')
    print(f'  reason mismatches: {bad_reasons}')
    if examples:
        print('\nFirst mismatches:')
        print('\n'.join(examples))
        print('\nFAIL - the dashboard is not showing what the worker trades.')
        print('signal_checker.py is the source of truth; fix app.js to match.')
        return 1
    print('\nOK - the two implementations agree on every case.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
