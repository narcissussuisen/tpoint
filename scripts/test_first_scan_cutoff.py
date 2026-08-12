# -*- coding: utf-8 -*-
"""P1 回归测试：首扫白名单下界与「抑制标记侧」口径对齐（2026-08-12）。

被测缺陷（08-07 修复只做了一半留下的口径不一致）：
  · 抑制标记侧（run() 首扫块）走游标路径时，只把 <= 游标 的 bar 标记为已处理，
    游标之后的 bar 会被 detect_for 真正评估；
  · 但 emit 侧白名单仍硬编码 recent_cutoff = now - 3min → 落在 (游标, now-3min)
    区间内的真实信号被当"历史重扫"丢弃 → 形成「detect_for 已计入 _b/_s_count
    却从未推送」的幽灵计数。
  实证：08-12 罗博特科(300757) S@09:34（游标 09:30、进程 09:40 起来）
        _s_count_300757.SZ_20260812=1 而 push_audit.jsonl 无任何 08-12 记录。

修复：monitor._first_scan_cutoff()
  走游标路径 → cutoff = clamp(min(now-3min, 游标), 下界=now-REPLAY_MAX_AGE_S)
  非游标路径 → cutoff = now-3min（保守语义不变）

用法：python scripts/test_first_scan_cutoff.py     退出码 0=全过，1=有失败。
"""
import sys
import os
from datetime import datetime, timedelta

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = os.path.join(BASE, 'core')
sys.path.insert(0, CORE)
sys.path.insert(0, os.path.join(BASE, 'venv', 'Lib', 'site-packages'))
os.chdir(CORE)

import monitor  # noqa: E402

F = monitor._first_scan_cutoff
D = lambda s: datetime.strptime(s, '%Y-%m-%d %H:%M:%S')  # noqa: E731

fails = []


def chk(name, got, want, note=''):
    ok = got == want
    print(f"{'✅ PASS' if ok else '❌ FAIL'} {name}: got={got.strftime('%m-%d %H:%M:%S')} "
          f"want={want.strftime('%m-%d %H:%M:%S')} {note}")
    if not ok:
        fails.append(name)
    return ok


print('=' * 78)
print('P1 回归：首扫白名单下界 _first_scan_cutoff（REPLAY_MAX_AGE_S=%ds）'
      % monitor.REPLAY_MAX_AGE_S)
print('=' * 78)

# ---- A 今日事故复现：游标 09:30、09:40 重启，S@09:34 必须放行 ----
now_a = D('2026-08-12 09:40:00')
cut_a = F(now_a, cursor='2026-08-12 09:30:00', use_cursor=True)
chk('A 事故复现·游标对齐', cut_a, D('2026-08-12 09:30:00'), '(旧逻辑=09:37 会吞掉 09:34)')
sig = D('2026-08-12 09:34:00')
passed_new = sig >= cut_a
passed_old = sig >= (now_a - timedelta(minutes=3))
print(f"   → S@09:34 新逻辑{'放行' if passed_new else '抑制'} / "
      f"旧逻辑{'放行' if passed_old else '抑制'}")
if not (passed_new and not passed_old):
    fails.append('A 信号放行断言')
    print('   ❌ 期望：新逻辑放行 且 旧逻辑抑制（证明本修复确有行为差异）')
else:
    print('   ✅ 期望满足：新放行 + 旧抑制（修复行为差异被证明）')

# ---- B 非游标路径（长时间死亡/跨日/无游标）保持 now-3min 保守语义 ----
chk('B 非游标路径保守窗口', F(now_a, cursor='2026-08-12 09:30:00', use_cursor=False),
    D('2026-08-12 09:37:00'), '(不得放宽：死亡期历史 bar 不重发)')
chk('B2 无游标回退', F(now_a, cursor=None, use_cursor=True),
    D('2026-08-12 09:37:00'))

# ---- C 游标很新（1min 前重启）：保留更宽松的 3min 窗口，不因游标而收紧 ----
chk('C 新游标不收紧窗口', F(now_a, cursor='2026-08-12 09:39:00', use_cursor=True),
    D('2026-08-12 09:37:00'), '(min(now-3min, 游标)=09:37)')

# ---- D 游标异常陈旧：REPLAY_MAX_AGE_S 兜底，不重发过期信号（价格已失真） ----
chk('D 陈旧游标被 age 兜底', F(now_a, cursor='2026-08-12 09:00:00', use_cursor=True),
    D('2026-08-12 09:30:00'), '(floor=now-600s=09:30)')

# ---- E 游标格式异常 → 回退保守窗口，不得抛异常 ----
chk('E 脏游标安全回退', F(now_a, cursor='not-a-timestamp', use_cursor=True),
    D('2026-08-12 09:37:00'))

# ---- F 跨日边界（23:58 重启 → 00:01）：datetime 比较不得错乱 ----
now_f = D('2026-08-13 00:01:00')
chk('F 跨日边界', F(now_f, cursor='2026-08-12 23:58:00', use_cursor=True),
    D('2026-08-12 23:58:00'), '(字符串比较会误判，必须 datetime)')

print('=' * 78)
if fails:
    print(f"❌ 失败 {len(fails)} 项：{fails}")
    sys.exit(1)
print('✅ 全部通过（6 组断言）')
sys.exit(0)
