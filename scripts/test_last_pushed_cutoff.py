"""P2 修复回归：首扫抑制改用 LAST_PUSHED_TS(最后推送信号时间戳)。

验证两类正确性：
1) 边界语义 —— 仅抑制 <= last_pushed 的信号(确已推送)，严格晚于它的全部放行(含缺口实时信号)；
2) 防跨重启重复推送 —— 信号时间 == last_pushed 必被抑制(<=)；
3) 长断线兜底 —— last_pushed 过旧(>REPLAY_MAX_AGE_S)时回落 REPLAY floor；
4) 跨日裁剪 —— 加载时丢弃非今日条目；
5) _upd_last_pushed 取最大值且不回退。
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
import monitor as M
from datetime import datetime

def dt(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')

NOW = dt('2026-08-12 10:00:00')
REPLAY = M.REPLAY_MAX_AGE_S  # 600

def decide(sig_ts, cutoff):
    """复刻 emit 侧判定：信号时间严格晚于 cutoff 才放行。"""
    sd = datetime.fromisoformat(sig_ts[:16] if len(sig_ts) >= 16 else sig_ts)
    return sd > cutoff

results = []
def check(name, cond):
    results.append((name, cond))
    print(('PASS' if cond else 'FAIL'), name)

# A: pushed_ts 近期(09:55) → cutoff=09:55；09:55 抑制、09:56(下1分钟) 放行
# 注：emit 侧按分钟分辨率比较(s_ts[:16])，故用分钟对齐时间戳验证。
c = M._first_scan_cutoff(NOW, pushed_ts='2026-08-12 09:55:00')
check('A1 cutoff==pushed', c == dt('2026-08-12 09:55:00'))
check('A2 边界09:55抑制', not decide('2026-08-12 09:55:00', c))
check('A3 09:56(下1分钟)放行', decide('2026-08-12 09:56:00', c))

# B: 无 pushed_ts 且无游标 → 回退 now-3min(保守窗口，与 v10.1.2 旧回归一致)
c = M._first_scan_cutoff(NOW, pushed_ts=None)
check('B1 cutoff==now-3min', c == dt('2026-08-12 09:57:00'))
check('B2 09:57抑制', not decide('2026-08-12 09:57:00', c))
check('B3 09:58放行', decide('2026-08-12 09:58:00', c))

# C: pushed_ts 过旧(08:00，>600s前) → cutoff=floor(09:50) 而非 08:00
c = M._first_scan_cutoff(NOW, pushed_ts='2026-08-12 08:00:00')
check('C1 过旧用floor', c == dt('2026-08-12 09:50:00'))

# D: 跨重启不重复推送 —— 信号==last_pushed 必被抑制(<=)
c = M._first_scan_cutoff(NOW, pushed_ts='2026-08-12 09:55:00')
check('D 等于last_pushed抑制', not decide('2026-08-12 09:55:00', c))

# E: 缺口恢复 —— last_pushed=09:55，其后实时信号放行
c = M._first_scan_cutoff(NOW, pushed_ts='2026-08-12 09:55:00')
check('E1 缺口09:56放行', decide('2026-08-12 09:56:00', c))
check('E2 缺口10:00放行', decide('2026-08-12 10:00:00', c))

# F: 加载跨日裁剪 —— 昨日条目应被丢弃，今日保留
td = tempfile.mkdtemp()
fp = os.path.join(td, 'lp.json')
json.dump({'161129.SZ': '2026-08-11 15:00:00',
           '300757.SZ': '2026-08-12 09:30:00'}, open(fp, 'w'))
M._LAST_PUSHED_FILE = fp
M._load_last_pushed()
check('F1 昨日条目被裁剪', '161129.SZ' not in M.LAST_PUSHED_TS)
check('F2 今日条目保留', M.LAST_PUSHED_TS.get('300757.SZ') == '2026-08-12 09:30:00')

# G: _upd_last_pushed 取最大值且持久化、不回退
M.LAST_PUSHED_TS = {}
M._upd_last_pushed('X.SZ', '2026-08-12 09:00:00')
M._upd_last_pushed('X.SZ', '2026-08-12 09:30:00')
check('G1 取最大', M.LAST_PUSHED_TS['X.SZ'] == '2026-08-12 09:30:00')
M._upd_last_pushed('X.SZ', '2026-08-12 08:00:00')
check('G2 不回退', M.LAST_PUSHED_TS['X.SZ'] == '2026-08-12 09:30:00')

# H: 回退旧口径(游标)仍等价旧测试 —— use_cursor 路径不受 pushed_ts=None 影响
c = M._first_scan_cutoff(NOW, cursor='2026-08-12 09:57:00', use_cursor=True)
check('H 游标路径=min(now-3min,游标)', c == min(NOW - __import__('datetime').timedelta(minutes=3), dt('2026-08-12 09:57:00')))

print('\n=== %d/%d PASS ===' % (sum(1 for _, c in results if c), len(results)))
sys.exit(0 if all(c for _, c in results) else 1)
