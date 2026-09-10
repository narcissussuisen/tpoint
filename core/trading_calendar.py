# -*- coding: utf-8 -*-
"""core/trading_calendar.py — A 股交易日历（**单一真源**）

[2026-09-10 建立] 建立原因：`is_trading_today()` 曾在 **4 处重复实现** ——
`core/monitor.py`、`core/alert_engine.py`、`scripts/selfcheck_daily.py` 各抄了一份完全相同的
节假日表，`scripts/watchdog.py` 靠 `import core.monitor` 间接复用（委托模式是对的）。
节假日表**每年都要更新**，多份拷贝意味着"改漏一处"是迟早的事；漏改的后果很实在：
休市日 monitor 不退出 / 告警误报 service_down / 自检误判 / 基准采到空数据污染统计。

现三处已改为委托本模块。**任何新增的交易日判定都必须用这里，不要再复制节假日表**
（回归 `scripts/test_trading_calendar.py` 会检查无人再抄表）。

⚠️ 维护约定：每年 12 月按国务院办公厅次年放假安排更新 `HOLIDAYS`。
   本表只列**休市日**；若某年出现"调休上班的周末"（周末开市），当前规则会误判为非交易日，
   届时需改为**显式交易日白名单**模式（并在本文件与回归里同步）。
"""
from datetime import datetime, date, timezone, timedelta

CST = timezone(timedelta(hours=8))

#: 2026 年 A 股休市日（元旦/春节/清明/劳动/端午/中秋+国庆）
HOLIDAYS_2026 = frozenset({
    '2026-01-01', '2026-01-02',
    '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
    '2026-02-02', '2026-02-03',
    '2026-04-06',
    '2026-05-01', '2026-05-04', '2026-05-05',
    '2026-06-19',
    '2026-09-25', '2026-09-28', '2026-09-29', '2026-09-30',
    '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
})
HOLIDAYS = HOLIDAYS_2026          # 兼容别名


def is_trading_day(d) -> bool:
    """交易日判定（周一~周五 且 不在休市表）。接受 date / datetime / 'YYYY-MM-DD'。

    ⚠️ 与旧的三处内联实现语义完全一致（同样的周末规则 + 同样的休市集合）；
    唯一差异是**统一按 CST 取当前时间**（旧 alert_engine 用 naive local，本机为 CST 故等价，
    但显式带时区更不易在跨时区/容器环境里出错）。
    """
    if isinstance(d, str):
        s = d[:10]
        wd = datetime.strptime(s, '%Y-%m-%d').weekday()
    elif isinstance(d, datetime):
        s, wd = d.strftime('%Y-%m-%d'), d.weekday()
    elif isinstance(d, date):
        s, wd = d.strftime('%Y-%m-%d'), d.weekday()
    else:
        raise TypeError('is_trading_day 需要 date/datetime/str，得到 %r' % type(d))
    return wd < 5 and s not in HOLIDAYS


def is_trading_today(now=None) -> bool:
    """今天是否交易日。now 可显式传入（便于测试）。"""
    return is_trading_day(now or datetime.now(CST))
