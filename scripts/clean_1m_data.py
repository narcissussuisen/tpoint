# -*- coding: utf-8 -*-
"""
clean_1m_data.py —— tickflow 1m 数据清洗器（2026-08-20）

背景：F:/keyfactor_data/1m 部分标的混入「合成/占位」脏数据，必须清洗后才能用于回测：
  1. **MS 合成段**：timestamp > 1e12（毫秒级）且价格带 14-15 位小数（如 301.59932860813467）、
     volume 为个位数（93/77/173）—— 判定为合成假数据（非真实 tick），整行剔除。
     （688111/300058/600570 在 2025-12~2026-07 中旬整段如此，真实段仅 2026-07-31 起。）
  2. **垃圾 volume**：volume < 1 或 volume > 1e12（占位值 5.877471754111438e-39）→ 整行剔除。
     （513310 有 5248 行、161129 688 行、603039 每日 14:59 各 1 行。）
  3. **时段外 bar**：trade_time 不在 09:30-11:30 / 13:00-15:00 → 剔除（688111 有 149 个时段外）。
  4. 按 (trade_date, trade_time) 去重 + 排序。

输出：F:/keyfactor_data/1m_clean/<sym>_1m.csv（同列格式，utf-8-sig，仅保留真实 tick）。

用法：python scripts/clean_1m_data.py --syms 603039.SH,688111.SH,...   （缺省=watchlist+老5标的）
"""
import argparse, csv, os, sys


def is_synthetic(r):
    """合成判定：ms 级时间戳 且 价格含 >4 位小数（真实 A 股 tick 网格 ≤0.001/0.01）。"""
    try:
        ts = float(r['timestamp'])
    except (TypeError, ValueError):
        return True
    if ts > 1e12:  # ms 级
        return True
    for k in ('open', 'high', 'low', 'close'):
        v = r.get(k, '')
        if '.' in v and len(v.split('.')[-1]) > 4:
            return True
    return False


def bad_volume(r):
    try:
        v = float(r['volume'])
    except (TypeError, ValueError):
        return True
    # 真实 A 股 1m 最小成交也 > 1 手（ETF/LOF 亦如此）；<1 或 >1e12 均为占位/异常
    return not (1 <= v <= 1e12)


def in_session(t):
    try:
        hh, mm = int(t[11:13]), int(t[14:16])
    except (ValueError, IndexError):
        return False
    if hh == 9 and mm >= 30: return True
    if 10 <= hh <= 10: return True
    if hh == 11 and mm <= 30: return True
    if hh == 13: return True
    if 14 <= hh <= 14: return True
    return False


def clean_file(src, dst):
    kept, dropped_syn, dropped_vol, dropped_sess, dropped_dup = 0, 0, 0, 0, 0
    seen = set()
    rows_out = []
    with open(src, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        for r in reader:
            if is_synthetic(r):
                dropped_syn += 1
                continue
            if bad_volume(r):
                dropped_vol += 1
                continue
            if not in_session(r.get('trade_time', '')):
                dropped_sess += 1
                continue
            key = (r.get('trade_date', ''), r.get('trade_time', ''))
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)
            rows_out.append(r)
            kept += 1
    rows_out.sort(key=lambda r: (r.get('trade_date', ''), r.get('trade_time', '')))
    with open(dst, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)
    return dict(kept=kept, synthetic=dropped_syn, badvol=dropped_vol,
                out_session=dropped_sess, dup=dropped_dup, total=kept + dropped_syn + dropped_vol + dropped_sess + dropped_dup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=r'F:/keyfactor_data/1m')
    ap.add_argument('--out', default=r'F:/keyfactor_data/1m_clean')
    ap.add_argument('--syms', default='')
    a = ap.parse_args()
    if not a.syms:
        import json
        wl = json.load(open(r'C:/Users/YZP/WorkBuddy/Claw/tpoint/data/watchlist.json', encoding='utf-8'))
        a.syms = ','.join(list(wl.keys()) + ['300058.SZ', '600570.SH', '161129.SZ', '513310.SH'])
    os.makedirs(a.out, exist_ok=True)
    for sym in [s.strip() for s in a.syms.split(',') if s.strip()]:
        src = os.path.join(a.root, f'{sym}_1m.csv')
        if not os.path.exists(src):
            print(f'[{sym}] NO FILE'); continue
        dst = os.path.join(a.out, f'{sym}_1m.csv')
        st = clean_file(src, dst)
        pct = 100.0 * st['synthetic'] / st['total'] if st['total'] else 0
        print(f'[{sym}] kept={st["kept"]} 剔除: 合成={st["synthetic"]}({pct:.0f}%) 垃圾vol={st["badvol"]} '
              f'时段外={st["out_session"]} 重复={st["dup"]}')
    print('[done] clean ->', a.out)


if __name__ == '__main__':
    main()
