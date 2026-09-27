#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""push_tpoint_review.py — tpoint 复盘报告推送（2026-08-04 晚）
- 只推 tpoint自迭代报告群 a35d7f52（用户指定，不再推 849577f5）
- 动态标题：含当日关键指标（配对/有效率/净盈亏/显著段捕获率/优化项数），不用固定名称
CLI：python push_tpoint_review.py <date>
"""
import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# [2026-09-25] 交易日历单一真源 + 非交易日守卫：本脚本此前无任何交易日判断，
# 休市日会照推「0 信号复盘」到 a35d7f52 群。
sys.path.insert(0, os.path.join(ROOT, 'core'))
from trading_calendar import is_trading_day as _is_trading_day  # noqa: E402
PY = sys.executable
# 2026-09-01 修复：原硬编码路径 C:\...\方法论与研究文档\研究报告\ 已不存在（自 08-2x 起 step5 一直
# 静默失败 → 复盘 HTML 从未真正推送）。改为多候选探测，任一存在即用，避免再次因路径漂移静默丢推。
_PUSH_CANDIDATES = [
    r'F:\Users\YZP\WorkBuddy\Claw\research\push_feishu_html.py',
    r'C:\Users\YZP\WorkBuddy\Claw\research\push_feishu_html.py',
    os.path.join(ROOT, 'scripts', 'push_feishu_html.py'),
    r'C:\Users\YZP\WorkBuddy\Claw\方法论与研究文档\研究报告\push_feishu_html.py',
]
PUSH_PY = next((p for p in _PUSH_CANDIDATES if os.path.exists(p)), _PUSH_CANDIDATES[0])
HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'


def main():
    date = sys.argv[1]
    # [2026-09-25] 非交易日不推送复盘（休市日只有 0 信号，推出去是噪声且会污染群里的复盘序列）
    if not _is_trading_day(date):
        print(f'[skip] {date} 非交易日，不推送复盘')
        return
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
