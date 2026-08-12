"""独立重放 2026-08-05 / 08-06 / 08-07：用生产同源 monitor.detect_for 重放 F 盘 tickflow 1m 数据，
判定 P0 跨日残留事故期间生产引擎本应产出多少信号（无首扫抑制、空仓起步 = 理想上界）。
打桩 emit 防止污染真实 signal.txt / 飞书。
输出：output/replay_0805_0807_inventory.json + 控制台表格。
交叉核对 data/push_audit.jsonl 当日实际推送 => 漏发 = 应产 - 实推。
"""
import sys, os, json
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = BASE + r'\core'
sys.path.insert(0, CORE)
sys.path.insert(0, BASE + r'\venv\Lib\site-packages')
os.chdir(CORE)

import pandas as pd
from miji_alpha import compute_miji_indicators
import monitor

# 打桩：防止副作用
monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None
monitor._append_signal_txt = lambda *a, **k: None
monitor.push_batch = lambda *a, **k: None

SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}
DATES = ['2026-08-05', '2026-08-06', '2026-08-07']
FROOT = r'F:\keyfactor_data\1m'

try:
    with open(f'{BASE}/data/monitor_config.json') as f:
        cfg_all = json.load(f)
except Exception as e:
    cfg_all = {}
    print('cfg load fail', e)

# 实际推送（来自 push_audit）
actual = {}  # (date,sym) -> {'B':n,'S':n,'X':n}
try:
    for line in open(f'{BASE}/data/push_audit.jsonl', encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        d = r.get('ts', '')[:10]
        s = r.get('sym')
        t = r.get('type')
        actual.setdefault((d, s), {'B': 0, 'S': 0, 'X': 0})
        if t in actual[(d, s)]:
            actual[(d, s)][t] += 1
except Exception as e:
    print('push_audit load fail', e)

inventory = []
for DATE in DATES:
    for sym, name in SYMS.items():
        csv = f'{FROOT}/{sym}_1m.csv'
        rec = {'date': DATE, 'sym': sym, 'name': name}
        if not os.path.exists(csv):
            rec['error'] = f'无 {csv}'
            inventory.append(rec)
            print(f'{name}: 无 {csv}')
            continue
        df = pd.read_csv(csv)
        df = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
        if len(df) < 10:
            rec['error'] = f'08 数据不足 {len(df)}'
            inventory.append(rec)
            print(f'{name}: {DATE} 数据不足 {len(df)}')
            continue
        prev = df[df['trade_date'] < DATE]
        if len(prev):
            pc = float(prev['close'].iloc[-1])
        else:
            pc_map = {'161129.SZ': 1.7, '513310.SH': 4.899, '300757.SZ': 488.18}
            pc = pc_map[sym]
        o = df['open'].values.astype(float); h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        data = compute_miji_indicators(o, h, lo, c, v, pc)
        data['df'] = df
        monitor.STATE[sym] = {'PC': pc, 'WARM': None}
        cfg = cfg_all.get(sym, {})
        atr_p = cfg.get('atr_min_pct'); mpr_e = cfg.get('mpr_enable'); mpr_p = cfg.get('mpr_periods')
        st = {}  # 无已处理标记 => 等价于无首扫抑制、空仓起步（理想上界）
        sigs = monitor.detect_for(sym, name, data, st,
                                  mpr_enable=mpr_e, mpr_periods=mpr_p, atr_min_pct=atr_p)
        nb = sum(1 for s in sigs if s[0] == 'B')
        ns = sum(1 for s in sigs if s[0] == 'S')
        nx = sum(1 for s in sigs if s[0] == 'X')
        act = actual.get((DATE, sym), {'B': 0, 'S': 0, 'X': 0})
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
        print(f'\n=== {name}({sym}) {DATE} pc={pc:.3f} bars={len(df)} atr={atr_p} mpr={mpr_e}/{mpr_p} ===')
        print(f'  应产 B={nb} S={ns} X={nx} | 实推 B={act["B"]} S={act["S"]} X={act["X"]} | 漏 B={nb-act["B"]} S={ns-act["S"]} X={nx-act["X"]}')
        for s in sigs:
            tt = s[12] if len(s) > 12 else '?'
            print(f'   {s[0]} @ {tt} px={s[1]:.3f} chg={s[2]:.2f}% reason={s[4]} size={s[-1]}')

# 汇总
tot_should = sum(r.get('should_B', 0) + r.get('should_S', 0) + r.get('should_X', 0) for r in inventory)
tot_actual = sum(r.get('actual_B', 0) + r.get('actual_S', 0) + r.get('actual_X', 0) for r in inventory)
tot_miss = tot_should - tot_actual
print(f'\n>>> 08-05~08-07 区间(3标的) 应产信号={tot_should} 实推={tot_actual} 漏发={tot_miss}')

out = {'dates': DATES, 'syms': SYMS, 'total_should': tot_should,
       'total_actual': tot_actual, 'total_miss': tot_miss, 'rows': inventory}
with open(f'{BASE}/output/replay_0805_0807_inventory.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('>>> 已写出 output/replay_0805_0807_inventory.json')
