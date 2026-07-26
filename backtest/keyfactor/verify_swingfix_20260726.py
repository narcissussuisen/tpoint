# -*- coding: utf-8 -*-
"""验证 _is_new_low/_is_new_high 漏顶漏底修复。

两层验证:
  (A) 合成单元测试: 构造"反转收盘"极值 bar (BAR自身 high/low 创窗内极值, 但收盘回落),
      断言【修复后的生产模块】能捕获、而【旧逻辑】漏判。不依赖外部数据, 随处可跑。
  (B) 真实数据全量扫描(可选): 若 F:/keyfactor_data/1m 可达, 统计修复后新捕获的真实顶/底数量。

用法: python verify_swingfix_20260726.py   (cwd = tpoint 仓库根, 用 venv python)
"""
import os
import glob

import numpy as np
import pandas as pd

# ---- 旧逻辑(生产修复前, 用于对照) ----
W = 15


def old_new_low(c, lo, i, w=W):
    if i < 2:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(c[i]) < float(win.min())


def old_new_high(c, h, i, w=W):
    if i < 2:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(c[i]) > float(win.max())


def synthetic_unit_test():
    """构造反转收盘极值, 验证修复后模块行为正确。"""
    import sys
    _repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(_repo, 'core'))
    import miji_alpha as MA

    # 顶部反转: 前窗 high 全平=10, bar6 high  spike=10.5 但收盘=9.9(<前窗high最大)
    h_top = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.5])
    lo_top = np.array([9.9, 9.9, 9.9, 9.9, 9.9, 9.9, 9.8])
    c_top = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.9])

    # 底部反转: 前窗 low 全平=10, bar6 low spike=9.5 但收盘=10.1(>前窗low最小)
    lo_bot = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.5])
    h_bot = np.array([10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.2])
    c_bot = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.1])

    # 正常极值(收盘也越界): 两种逻辑都应捕获
    h_n = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0])
    lo_n = np.array([9.9] * 7)
    c_n = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0])

    i = 6
    res = {}
    # 顶部反转
    res['top_old'] = old_new_high(c_top, h_top, i)
    res['top_new'] = bool(MA._is_new_high(c_top, h_top, i))
    # 底部反转
    res['bot_old'] = old_new_low(c_bot, lo_bot, i)
    res['bot_new'] = bool(MA._is_new_low(c_bot, lo_bot, i))
    # 正常极值
    res['norm_old'] = old_new_high(c_n, h_n, i)
    res['norm_new'] = bool(MA._is_new_high(c_n, h_n, i))

    ok = (res['top_old'] is False and res['top_new'] is True and
          res['bot_old'] is False and res['bot_new'] is True and
          res['norm_old'] is True and res['norm_new'] is True)
    print('=== (A) 合成单元测试: 反转收盘极值 ===')
    print('  顶部反转(top):     旧=%s  修复后=%s  (期望 旧漏/新捕)' % (res['top_old'], res['top_new']))
    print('  底部反转(bottom):  旧=%s  修复后=%s  (期望 旧漏/新捕)' % (res['bot_old'], res['bot_new']))
    print('  正常极值(normal):  旧=%s  修复后=%s  (期望 两者都捕)' % (res['norm_old'], res['norm_new']))
    print('  >>> %s' % ('PASS: 修复后模块正确捕获反转收盘极值' if ok else 'FAIL'))
    return ok


def realdata_scan():
    if os.environ.get('RUN_REALDATA') != '1':
        print('\n=== (B) 真实数据扫描: 默认跳过 (设 RUN_REALDATA=1 且在宿主机 F: 可达时运行) ===')
        return
    DATA_DIR = 'F:/keyfactor_data/1m'
    if not os.path.isdir(DATA_DIR):
        print('\n=== (B) 真实数据扫描: 跳过 (F: 不可达, 请在宿主机运行) ===')
        return
    files = sorted(glob.glob(os.path.join(DATA_DIR, '*_1m.csv')))
    if not files:
        print('\n=== (B) 真实数据扫描: 无 CSV ===')
        return
    tot = {'old_low': 0, 'new_low': 0, 'old_high': 0, 'new_high': 0,
           'rec_low': 0, 'rec_high': 0}
    examples = []
    for p in files:
        fn = os.path.basename(p)
        df = pd.read_csv(p, encoding='utf-8-sig')
        for day, d in df.groupby('trade_date'):
            h = d['high'].values.astype(float)
            lo = d['low'].values.astype(float)
            c = d['close'].values.astype(float)
            for i in range(len(c)):
                ol, nl = old_new_low(c, lo, i), (float(lo[i]) < float(lo[max(0, i - W):i].min()) if i >= 1 and len(lo[max(0, i - W):i]) > 0 else False)
                oh, nh = old_new_high(c, h, i), (float(h[i]) > float(h[max(0, i - W):i].max()) if i >= 1 and len(h[max(0, i - W):i]) > 0 else False)
                tot['old_low'] += ol
                tot['new_low'] += nl
                tot['old_high'] += oh
                tot['new_high'] += nh
                if nl and not ol:
                    tot['rec_low'] += 1
                    if len([e for e in examples if e[0] == 'LOW']) < 8 and i > W:
                        examples.append(('LOW', fn, day, int(i), float(h[i]), float(lo[i]), float(c[i]), float(lo[max(0, i - W):i].min())))
                if nh and not oh:
                    tot['rec_high'] += 1
                    if len([e for e in examples if e[0] == 'HIGH']) < 8 and i > W:
                        examples.append(('HIGH', fn, day, int(i), float(h[i]), float(lo[i]), float(c[i]), float(h[max(0, i - W):i].max())))
    print('\n=== (B) 真实数据全量扫描 (窗口 W=%d) ===' % W)
    print('  新低: 旧=%d 新=%d 修复后新捕获=%d' % (tot['old_low'], tot['new_low'], tot['rec_low']))
    print('  新高: 旧=%d 新=%d 修复后新捕获=%d' % (tot['old_high'], tot['new_high'], tot['rec_high']))
    print('  样例(修复后新捕获的真实顶/底):')
    for e in examples:
        print('    %s %s %s bar#%d H=%.3f L=%.3f C=%.3f 前窗极=%.3f' % e)


if __name__ == '__main__':
    ok = synthetic_unit_test()
    realdata_scan()
    import sys
    sys.exit(0 if ok else 1)
