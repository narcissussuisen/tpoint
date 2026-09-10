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
EXPECTED_2026 = {
    '2026-01-01', '2026-01-02',                                             # 元旦
    '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
    '2026-02-02', '2026-02-03',                                             # 春节
    '2026-04-06',                                                           # 清明
    '2026-05-01', '2026-05-04', '2026-05-05',                               # 劳动
    '2026-06-19',                                                           # 端午
    '2026-09-25', '2026-09-28', '2026-09-29', '2026-09-30',                 # 中秋
    '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',   # 国庆
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
        ('2026-09-10', True, '今天（周四，交易日）'),
        ('2026-09-11', True, '周五，交易日'),
        ('2026-09-12', False, '周六'),
        ('2026-09-13', False, '周日'),
        ('2026-09-25', False, '周五但中秋休市'),
        ('2026-09-28', False, '周一但中秋休市'),
        ('2026-10-01', False, '周四但国庆休市'),
        ('2026-10-07', False, '周三但国庆休市'),
        ('2026-10-08', True, '周四，节后首个交易日'),
        ('2026-05-01', False, '周五但劳动节休市'),
        ('2026-05-06', True, '周三，节后交易日'),
        ('2026-01-26', False, '周一但春节休市'),
        ('2026-02-04', True, '周三，春节后首个交易日'),
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
               'scripts/watchdog.py', 'scripts/datasource_benchmark.py']
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

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
