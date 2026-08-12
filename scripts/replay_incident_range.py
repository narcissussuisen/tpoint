"""完整重放 2026-08-05 ~ 08-12（含 08-10/11/12，跳过周末 08-08/09）：
用生产同源 monitor.detect_for 重放 F 盘 tickflow 1m 数据，逐日逐标的给出
「本应产出信号」（无首扫抑制、空仓起步 = 理想上界），并与 data/push_audit.jsonl
当日实际推送交叉核对 => 漏发 = 应产 - 实推。
打桩 emit 防污染。输出 output/replay_incident_inventory.json + 表格。
注：空仓起步重放对「入场 B」信号是干净代理（入场不需前置持仓）；
「出场 S/X」信号依赖持仓结转，空仓重放会低估，故 S/X 漏发仅作参考，主指标用 B。
"""
import sys, os, json, collections
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = BASE + r'\core'
sys.path.insert(0, CORE)
sys.path.insert(0, BASE + r'\venv\Lib\site-packages')
os.chdir(CORE)

import pandas as pd
from miji_alpha import compute_miji_indicators
import monitor

monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None
monitor._append_signal_txt = lambda *a, **k: None
monitor.push_batch = lambda *a, **k: None

SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}
DATES = ['2026-08-05', '2026-08-06', '2026-08-07', '2026-08-10', '2026-08-11', '2026-08-12']
FROOT = r'F:\keyfactor_data\1m'

try:
    with open(f'{BASE}/data/monitor_config.json') as f:
        cfg_all = json.load(f)
except Exception as e:
    cfg_all = {}
    print('cfg load fail', e)

# 实际推送（来自 push_audit）
actual = collections.defaultdict(lambda: {'B': 0, 'S': 0, 'X': 0})
for line in open(f'{BASE}/data/push_audit.jsonl', encoding='utf-8'):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    d, s, t = r.get('ts', '')[:10], r.get('sym'), r.get('type')
    if t in actual[(d, s)]:
        actual[(d, s)][t] += 1

inventory = []
for DATE in DATES:
    for sym, name in SYMS.items():
        csv = f'{FROOT}/{sym}_1m.csv'
        rec = {'date': DATE, 'sym': sym, 'name': name}
        if not os.path.exists(csv):
            rec['error'] = f'无 {csv}'
            inventory.append(rec)
            continue
        df = pd.read_csv(csv)
        df = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
        if len(df) < 10:
            rec['error'] = f'数据不足 {len(df)}'
            inventory.append(rec)
            print(f'{name}({sym}) {DATE}: 数据不足 {len(df)} (无法评估)')
            continue
        prev = df[df['trade_date'] < DATE]
        pc = float(prev['close'].iloc[-1]) if len(prev) else {'161129.SZ': 1.7, '513310.SH': 4.899, '300757.SZ': 488.18}[sym]
        o = df['open'].values.astype(float); h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        data = compute_miji_indicators(o, h, lo, c, v, pc)
        data['df'] = df
        monitor.STATE[sym] = {'PC': pc, 'WARM': None}
        cfg = cfg_all.get(sym, {})
        atr_p = cfg.get('atr_min_pct'); mpr_e = cfg.get('mpr_enable'); mpr_p = cfg.get('mpr_periods')
        st = {}
        sigs = monitor.detect_for(sym, name, data, st,
                                  mpr_enable=mpr_e, mpr_periods=mpr_p, atr_min_pct=atr_p)
        nb = sum(1 for s in sigs if s[0] == 'B')
        ns = sum(1 for s in sigs if s[0] == 'S')
        nx = sum(1 for s in sigs if s[0] == 'X')
        act = actual[(DATE, sym)]
        rec.update({
            'pc': round(pc, 4), 'bars': len(df),
            'atr_min': atr_p, 'mpr': f'{mpr_e}/{mpr_p}',
            'should_B': nb, 'should_S': ns, 'should_X': nx,
            'actual_B': act['B'], 'actual_S': act['S'], 'actual_X': act['X'],
            'miss_B': nb - act['B'], 'miss_S': ns - act['S'], 'miss_X': nx - act['X'],
            'signals': [{'t': s[0], 'px': round(float(s[1]), 3),
                         'chg': round(float(s[2]), 2) if s[2] is not None else None,
                         'reason': s[4], 'tt': s[12] if len(s) > 12 else '?',
                         'size': s[-1]} for s in sigs],
        })
        inventory.append(rec)
        print(f'{name}({sym}) {DATE}: 应B={nb} S={ns} X={nx} | 实B={act["B"]} S={act["S"]} X={act["X"]} | 漏B={nb-act["B"]} S={ns-act["S"]} X={nx-act["X"]}')

# 汇总（主指标 B）
agg = {}
for sym, name in SYMS.items():
    sb = sum(r.get('should_B', 0) for r in inventory if r.get('sym') == sym and 'should_B' in r)
    ab = sum(r.get('actual_B', 0) for r in inventory if r.get('sym') == sym and 'should_B' in r)
    agg[sym] = {'should_B': sb, 'actual_B': ab, 'miss_B': sb - ab}
print('\n=== 汇总（主指标：入场 B 信号漏发）===')
for sym, a in agg.items():
    print(f'  {sym}: 应产B={a["should_B"]} 实推B={a["actual_B"]} 漏发B={a["miss_B"]}')
tot_miss_B = sum(a['miss_B'] for a in agg.values())
print(f'  >>> 区间(08-05~08-12, 3标的) 入场信号漏发合计 = {tot_miss_B}')

out = {'dates': DATES, 'syms': SYMS, 'agg_by_sym': agg, 'total_miss_B': tot_miss_B, 'rows': inventory}
with open(f'{BASE}/output/replay_incident_inventory.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('>>> 已写出 output/replay_incident_inventory.json')
