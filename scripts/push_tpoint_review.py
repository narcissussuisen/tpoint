#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""push_tpoint_review.py — tpoint 复盘报告推送（2026-08-04 晚）
- 只推 tpoint自迭代报告群 a35d7f52（用户指定，不再推 849577f5）
- 动态标题：含当日关键指标（配对/有效率/净盈亏/显著段捕获率/优化项数），不用固定名称
CLI：python push_tpoint_review.py <date>
"""
import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PUSH_PY = r'F:\Users\YZP\WorkBuddy\Claw\research\push_feishu_html.py'
HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'


def main():
    date = sys.argv[1]
    live = json.load(open(os.path.join(ROOT, 'output', f'live_review_{date}.json'), encoding='utf-8'))
    sm = live['summary']
    vol = live['volatility']
    sig_amp = sum(v.get('sig_amp_pct', 0) for v in vol.values() if 'sig_amp_pct' in v)
    cap = sum(v.get('captured_pct', 0) for v in vol.values() if 'captured_pct' in v)
    sig_rate = round(min(cap / sig_amp * 100, 100), 1) if sig_amp > 0 else 0
    vr = sm['valid_rate_pct']
    head = (f"📈 tpoint 复盘 {date}｜T单{sm['n_trips']} 有效{sm['n_valid']}"
            f"（{'—' if vr is None else str(vr) + '%'}）净{sm['net_sum_pct']:+.2f}%"
            f"｜显著段捕获{sig_rate}%｜优化项{len(live.get('opportunities', []))}")
    html_rel = f'output/review_{date}.html'   # push_feishu_html 的 lark-cli 要求相对路径（须 cwd=ROOT）
    r = subprocess.run([PY, PUSH_PY, html_rel, HOOK, '', head], capture_output=True, text=True,
                       encoding='utf-8', cwd=ROOT)
    print(r.stdout[-500:])
    if r.returncode != 0:
        print(r.stderr[-300:])
        sys.exit(1)


if __name__ == '__main__':
    main()
