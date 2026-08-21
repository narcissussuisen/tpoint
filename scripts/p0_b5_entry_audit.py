#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0 B5 入账价对账审计脚本

验证所有对账/复盘产物中 entry_price 是否严格使用信号 bar close 价
（而非 signal.txt / push_audit 推送价）：
  - data/roundtrip/*.jsonl（prod_vs_bt_reconcile 输出）
  - output/live_review_*.json（live_roundtrip_review 输出）

输出：stdout 摘要 + output/p0_b5_entry_audit.json
"""
import os, sys, json, glob
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output', 'p0_b5_entry_audit.json')

def audit_roundtrip_files():
    files = sorted(glob.glob(os.path.join(ROOT, 'data', 'roundtrip', '2026-*.jsonl')))
    max_slip_pct = 0.0
    max_record = None
    total = 0
    live_total = 0
    mismatches = []
    for fp in files:
        with open(fp, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                total += 1
                if r.get('source') != 'live':
                    continue
                live_total += 1
                ep = r.get('entry_price')
                ebc = r.get('entry_bar_close')
                if ep is None or ebc is None or ebc == 0:
                    continue
                slip_pct = abs((float(ep) - float(ebc)) / float(ebc) * 100.0)
                if slip_pct > max_slip_pct:
                    max_slip_pct = slip_pct
                    max_record = {'date': os.path.basename(fp)[:10], 'sym': r.get('sym'), 'entry_price': ep, 'entry_bar_close': ebc, 'slip_pct': slip_pct, 'entry_push_price': r.get('entry_push_price')}
                # 0.1% 容忍
                if slip_pct > 0.1:
                    mismatches.append({'date': os.path.basename(fp)[:10], 'sym': r.get('sym'), 'entry_price': ep, 'entry_bar_close': ebc, 'slip_pct': slip_pct, 'entry_push_price': r.get('entry_push_price')})
    return {
        'files': len(files),
        'total_records': total,
        'live_records': live_total,
        'max_slip_pct': round(max_slip_pct, 4),
        'max_record': max_record,
        'mismatches_over_0.1pct': mismatches,
        'mismatch_count': len(mismatches),
    }

def audit_reconcile_files():
    files = sorted(glob.glob(os.path.join(ROOT, 'output', 'reconcile_2026-*.json')))
    max_push_slip = 0.0
    max_rec = None
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        for sym, rec in data.get('symbols', {}).items():
            for slip in rec.get('live_push_slip_pct', []):
                if slip is None:
                    continue
                a = abs(float(slip))
                if a > max_push_slip:
                    max_push_slip = a
                    max_rec = {'date': data.get('date'), 'sym': sym, 'push_slip_pct': slip}
    return {
        'files': len(files),
        'max_abs_push_slip_pct': round(max_push_slip, 4),
        'max_record': max_rec,
    }

def audit_live_review_files():
    files = sorted(glob.glob(os.path.join(ROOT, 'output', 'live_review_2026-*.json')))
    max_slip_pct = 0.0
    max_record = None
    total = 0
    mismatches = []
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        for t in data.get('trips', []):
            total += 1
            ep = t.get('entry_price')
            ebc = t.get('entry_bar_close')
            if ep is None or ebc is None or ebc == 0:
                continue
            slip_pct = abs((float(ep) - float(ebc)) / float(ebc) * 100.0)
            if slip_pct > max_slip_pct:
                max_slip_pct = slip_pct
                max_record = {'date': data.get('date'), 'sym': t.get('sym'), 'entry_price': ep, 'entry_bar_close': ebc, 'slip_pct': slip_pct, 'entry_push_price': t.get('entry_push_price'), 'dir': t.get('dir')}
            if slip_pct > 0.1:
                mismatches.append({'date': data.get('date'), 'sym': t.get('sym'), 'entry_price': ep, 'entry_bar_close': ebc, 'slip_pct': slip_pct, 'entry_push_price': t.get('entry_push_price'), 'dir': t.get('dir')})
    return {
        'files': len(files),
        'total_trips': total,
        'max_slip_pct': round(max_slip_pct, 4),
        'max_record': max_record,
        'mismatches_over_0.1pct': mismatches,
        'mismatch_count': len(mismatches),
    }

def main():
    rt = audit_roundtrip_files()
    rc = audit_reconcile_files()
    lr = audit_live_review_files()
    pass_b5 = (rt['mismatch_count'] == 0 and rt['max_slip_pct'] <= 0.1 and
               lr['mismatch_count'] == 0 and lr['max_slip_pct'] <= 0.1)
    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'roundtrip_audit': rt,
        'reconcile_audit': rc,
        'live_review_audit': lr,
        'pass_b5': pass_b5,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print('P0 B5 入账价对账审计结果')
    print(f"  roundtrip 文件数: {rt['files']}")
    print(f"  roundtrip live 记录数: {rt['live_records']}")
    print(f"  roundtrip 最大 entry_price vs entry_bar_close 偏差: {rt['max_slip_pct']}%")
    print(f"  live_review 文件数: {lr['files']}")
    print(f"  live_review 总 trips: {lr['total_trips']}")
    print(f"  live_review 最大 entry_price vs entry_bar_close 偏差: {lr['max_slip_pct']}%")
    print(f"  >0.1% 不匹配数: roundtrip={rt['mismatch_count']}, live_review={lr['mismatch_count']}")
    print(f"  reconcile 最大 push_slip_pct: {rc['max_abs_push_slip_pct']}%")
    print(f"  B5 验收: {'PASS' if pass_b5 else 'FAIL'}")
    print(f"  详情: {os.path.relpath(OUT, ROOT)}")

if __name__ == '__main__':
    main()
