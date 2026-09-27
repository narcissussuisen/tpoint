# -*- coding: utf-8 -*-
"""scripts/test_trading_calendar.py — 交易日历单一真源回归（2026-09-10）

建立原因：`is_trading_today()` 曾在 **4 处重复实现**（core/monitor.py、core/alert_engine.py、
scripts/selfcheck_daily.py 各抄一份相同的节假日表；scripts/watchdog.py 靠 import monitor 间接复用）。
节假日表每年 12 月都要更新，多份拷贝 ⇒ "改漏一处"是迟早的事，且后果分散在四个子系统里
（休市日 monitor 不退出 / 告警误报 service_down / 自检误判 / 基准采空数据污染统计）。

本测试锁三件事：
  ① `core/trading_calendar.py` 的休市表与规则正确（含关键日期逐日断言）；
  ② **无人再内联节假日字面量**（防再复制出第 5 份拷贝）——这是本测试的核心价值；
  ③ 四个消费方（monitor / alert_engine / selfcheck_daily / watchdog）委托后结果一致。

运行：venv/Scripts/python.exe scripts/test_trading_calendar.py
"""
import io
import os
import re
import subprocess
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import trading_calendar as TC          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


#: 2026 年 A 股休市日（独立于实现再写一遍：若实现被改动，这条断言会先炸）
#: [2026-09-25 按交易所官方公告重建] 上交所〔2025〕45号 /〔2026〕22号 + 深交所 2026-09-17 通知。
#: 只列**非周末**休市日（周末日由 wd<5 规则覆盖）。
#: ⚠️ 上一版把 9/28、9/29、9/30 当成休市（官方口径为照常开市）→ 会导致节后连续 3 天
#:    零扫描零信号；春节段也整体错位（写成 1/26–1/30、2/2–2/3，官方是 2/15–2/23）。
EXPECTED_2026 = {
    '2026-01-01', '2026-01-02',                                             # 元旦 1/1(四)–1/3(六)
    '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',   # 春节 2/15(日)–2/23(一)
    '2026-02-23',
    '2026-04-06',                                                           # 清明 4/4(六)–4/6(一)
    '2026-05-01', '2026-05-04', '2026-05-05',                               # 劳动 5/1(五)–5/5(二)
    '2026-06-19',                                                           # 端午 6/19(五)–6/21(日)
    '2026-09-25',                                                           # 中秋 9/25(五)–9/27(日)
    '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',   # 国庆 10/1(四)–10/7(三)
}


def main():
    print('\n=== 1. 休市表与规则 ===')
    check('HOLIDAYS_2026 是 frozenset（防运行期被改）', isinstance(TC.HOLIDAYS_2026, frozenset))
    check('休市日集合与预期逐项一致',
          set(TC.HOLIDAYS_2026) == EXPECTED_2026,
          f'多={sorted(set(TC.HOLIDAYS_2026) - EXPECTED_2026)} 少={sorted(EXPECTED_2026 - set(TC.HOLIDAYS_2026))}')
    check('HOLIDAYS 是 HOLIDAYS_2026 的兼容别名', TC.HOLIDAYS == TC.HOLIDAYS_2026)

    print('\n=== 2. 关键日期逐日断言（节假日 / 周末 / 工作日）===')
    cases = [
        ('2026-09-10', True, '周四，交易日'),
        ('2026-09-11', True, '周五，交易日'),
        ('2026-09-12', False, '周六'),
        ('2026-09-13', False, '周日'),
        ('2026-09-24', True, '周四，中秋节前最后一个交易日'),
        ('2026-09-25', False, '周五但中秋休市'),
        ('2026-09-26', False, '周六'),
        ('2026-09-27', False, '周日'),
        ('2026-09-28', True, '★ 周一，官方口径节后首个交易日（旧表误列为休市）'),
        ('2026-09-29', True, '★ 周二，交易日（旧表误列为休市）'),
        ('2026-09-30', True, '★ 周三，交易日（旧表误列为休市）'),
        ('2026-10-01', False, '周四但国庆休市'),
        ('2026-10-07', False, '周三但国庆休市'),
        ('2026-10-08', True, '周四，节后首个交易日'),
        ('2026-05-01', False, '周五但劳动节休市'),
        ('2026-05-06', True, '周三，节后交易日'),
        ('2026-02-13', True, '周五，春节前最后一个交易日'),
        ('2026-02-16', False, '★ 周一但春节休市（旧表未列，会空转）'),
        ('2026-02-23', False, '★ 周一，春节最后一天休市（旧表未列）'),
        ('2026-02-24', True, '周二，春节后首个交易日'),
    ]
    for ds, want, note in cases:
        got = TC.is_trading_day(ds)
        check(f'{ds} → {want}（{note}）', got is want, f'got={got}')

    print('\n=== 3. 入参类型兼容（date / datetime / str）===')
    check("str '2026-09-10' 可用", TC.is_trading_day('2026-09-10') is True)
    check('date(2026,9,12) 周六 → False', TC.is_trading_day(date(2026, 9, 12)) is False)
    check('datetime(2026,10,1) → False', TC.is_trading_day(datetime(2026, 10, 1, 10, 0)) is False)
    check('带时分秒的 str 只取日期部分', TC.is_trading_day('2026-09-10 14:30:00') is True)
    try:
        TC.is_trading_day(20260910)
        check('非法类型抛 TypeError', False, '未抛')
    except TypeError:
        check('非法类型抛 TypeError', True)

    print('\n=== 4. ★ 防再复制：无人内联节假日字面量 ===')
    # 用休市表里最具辨识度的几个日期做特征串（避免把注释里的说明误判为拷贝）
    probes = ("'2026-01-26'", '"2026-01-26"', "'2026-05-01'", '"2026-05-01"',
              "'2026-10-05'", '"2026-10-05"', 'holidays_2026')
    targets = ['core/monitor.py', 'core/alert_engine.py', 'scripts/selfcheck_daily.py',
               'scripts/watchdog.py', 'scripts/datasource_benchmark.py',
               # [2026-09-25 补] 这三个原本各内联一份**不含 2026-09-25** 的表，是"第 5 份拷贝"
               # 家族（其中 fdisk 版还凭空多了 2026-12-25）；现已统一 import trading_calendar。
               'scripts/daily_signal_review.py', 'scripts/daily_report_push.py',
               'scripts/fdisk_daily_update.py']
    for rel in targets:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            check(f'{rel} 存在', False, '文件缺失'); continue
        src = io.open(p, encoding='utf-8').read()
        hits = [t for t in probes if t in src]
        check(f'{rel} 无内联节假日表（已委托 trading_calendar）', not hits, f'命中={hits}')

    print('\n=== 5. 四个消费方委托后结果一致 ===')
    import monitor
    import alert_engine
    import selfcheck_daily
    base = TC.is_trading_today()
    check('monitor.is_trading_today() 与交易日历一致', monitor.is_trading_today() == base)
    check('alert_engine.is_trading_today() 与交易日历一致', alert_engine.is_trading_today() == base)
    check('selfcheck_daily.is_trading_today() 与交易日历一致', selfcheck_daily.is_trading_today() == base)

    # ------------------------------------------------------------------
    # [2026-09-25 新增] 6. watchdog 必须在【真实运行环境】下判定正确
    #
    # 为什么必须隔离子进程：本节第 5 部分是在 **core 已进 sys.path** 的上下文里跑的，
    # 而 watchdog 运行时 sys.path 里**没有 core** ——
    #   launch_watchdog.py:9 给的 PYTHONPATH = venv\Lib\site-packages;venv\Lib;BASE，
    #   且 sys.path[0] = BASE\scripts。
    # 旧的 watchdog 用 `from core.monitor import is_trading_today`，该导入在这里必然
    # ModuleNotFoundError（core/monitor.py 模块级就 `from datasource import ...`，
    # datasource.py 只在 core/ 里），被 except 静默降级成「仅周末判断」
    # ⇒ 2026-09-25（中秋，周五）被判为交易日，全天空转 spawn 705 次。
    # 第 5 部分那种"同路径对比"测不出它 —— 这正是本次缺陷潜伏一整天的测试盲区。
    # ------------------------------------------------------------------
    print('\n=== 6. watchdog 在真实 sys.path（不含 core）下的判定（隔离子进程）===')
    probe = (
        "import importlib.util as u, sys, datetime\n"
        "BASE = r'%s'\n"
        "sys.path.insert(0, BASE + r'\\scripts')\n"
        "sys.path.append(BASE)\n"            # 刻意不插 BASE\core —— 复刻真实环境
        "spec = u.spec_from_file_location('wd_under_test', BASE + r'\\scripts\\watchdog.py')\n"
        "m = u.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"       # __name__ != '__main__' ⇒ 不会启动守护循环
        "import datetime as _dt\n"
        "for ds in ['2026-09-24','2026-09-25','2026-09-26','2026-09-27',\n"
        "           '2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-08']:\n"
        "    y, mo, d = (int(x) for x in ds.split('-'))\n"
        "    try:\n"
        "        v = m.is_trading_today(_dt.date(y, mo, d))\n"          # 现代签名（可注入日期）
        "    except TypeError:\n"
        "        v = m.is_trading_today()\n"                            # 旧签名（无注入点）→ 只能给"今天"的答案
        "    print(ds, v)\n"
    ) % ROOT
    r = subprocess.run([sys.executable, '-c', probe], capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    got = {}
    for _ln in (r.stdout or '').splitlines():
        _p = _ln.split()
        if len(_p) == 2 and _p[1] in ('True', 'False'):
            got[_p[0]] = (_p[1] == 'True')
    check('隔离环境下可加载 watchdog 并取得判定（复刻真实 sys.path）', bool(got),
          f'rc={r.returncode} err={(r.stderr or "")[-400:]}')
    for ds in ('2026-09-24', '2026-09-25', '2026-09-26', '2026-09-27',
               '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-08'):
        want = TC.is_trading_day(ds)
        check(f'watchdog[{ds}] == trading_calendar == {want}', got.get(ds) is want,
              f'got={got.get(ds)}（旧实现会在此报 2026-09-25 为 True）')

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
