# -*- coding: utf-8 -*-
import os, sys, csv, glob, traceback

def main():
    root = r'F:/keyfactor_data/1m'
    print('root exists:', os.path.exists(root), flush=True)
    files = glob.glob(root + '/*_1m.csv')
    print('total files: %d' % len(files), flush=True)
    cands = []
    n = 0
    for p in files:
        n += 1
        if n % 500 == 0:
            print('...%d' % n, flush=True)
        sym = os.path.basename(p).replace('_1m.csv', '')
        real_days = set()
        try:
            with open(p, encoding='utf-8-sig') as f:
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
                            real_days.add(r['trade_date'])
        except Exception as e:
            print('ERR %s: %s' % (sym, e), flush=True)
            continue
        if len(real_days) >= 5:
            cands.append((sym, len(real_days)))
    cands.sort(key=lambda x: -x[1])
    print('real-data >=5d symbols: %d' % len(cands), flush=True)
    for s, nn in cands:
        print('  %s: %dd' % (s, nn), flush=True)
    print('DONE', flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
