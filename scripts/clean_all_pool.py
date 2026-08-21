# -*- coding: utf-8 -*-
"""批量清洗 F:/keyfactor_data/1m 全部标的（按真实数据天数筛选 ≥5 天），输出到 1m_clean。"""
import os, csv, glob, sys

sys.path.insert(0, r'C:/Users/YZP/WorkBuddy/Claw/tpoint/scripts')
from clean_1m_data import clean_file

SRC = r'F:/keyfactor_data/1m'
DST = r'F:/keyfactor_data/1m_clean'
MIN_DAYS = 5
os.makedirs(DST, exist_ok=True)


def count_real_days(path):
    days = set()
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            try:
                ts = float(r['timestamp'])
            except Exception:
                continue
            if ts <= 1e12:
                try:
                    v = float(r['volume'])
                except Exception:
                    v = 0
                if 1 <= v <= 1e12:
                    days.add(r['trade_date'])
    return len(days)


files = glob.glob(SRC + '/*_1m.csv')
ok, skip = 0, 0
for p in files:
    sym = os.path.basename(p).replace('_1m.csv', '')
    try:
        nd = count_real_days(p)
    except Exception:
        nd = 0
    if nd < MIN_DAYS:
        skip += 1
        continue
    dst = os.path.join(DST, f'{sym}_1m.csv')
    try:
        st = clean_file(p, dst)
        ok += 1
        print(f'[{sym}] kept={st["kept"]} syn={st["synthetic"]} vol={st["badvol"]} sess={st["out_session"]} dup={st["dup"]} real_days={nd}')
    except Exception as e:
        print(f'ERR {sym}: {e}')
print(f'DONE cleaned={ok} skip(<{MIN_DAYS}d)={skip} total={len(files)}')
