# -*- coding: utf-8 -*-
"""tpoint 交易日闸门 —— 供 bat 入口（run_daily_review.bat 等）调用。

exit 0 = 今天是交易日；exit 1 = 非交易日（调用方应静默跳过，不产出、不推送、不改生产配置）。

[2026-09-25 建立] 建立背景（中秋休市日事故）：
`scripts/run_daily_review.bat` 是一条 12 步流水线，但**整条链没有交易日闸门** ——
只有第 1 步 `daily_signal_review.py` 内部有 skip 判断，而它用的是一份**漏掉 2026-09-25 的本地
节假日表** ⇒ 2026-09-25（中秋休市）本该整条跳过，实际会跑完全程，其中：
  - `auto_tune.py`      完全没有交易日判断 → **会在休市日改写 monitor_config.json**
  - `push_tpoint_review.py` 无闸门 → 会推送「0 信号复盘」
  - `daily_iterate.py` / `daily_closed_loop.py` → 用休市空样本参与迭代
故在 bat 头部加本闸门，非交易日直接 `exit /b 77`（77 = 该流水线既有的 SKIPPED 约定）。

单一真源：`core/trading_calendar.py`。**任何新增的交易日判定都必须走这里，不要再复制节假日表。**
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE = os.path.join(ROOT, 'core')
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

try:
    from trading_calendar import is_trading_today
except Exception as e:  # noqa: BLE001
    # fail-closed：日历不可用时一律按非交易日处理。
    # 理由：本闸门守的是「生产改动流水线」（含 auto_tune 改写 monitor_config.json），
    # 误跑一次的代价远大于漏跑一次（漏跑可由 selfcheck / 次日补跑发现，误改参数不可逆）。
    sys.stderr.write('FAIL-CLOSED trading_calendar unavailable: %r\n' % (e,))
    sys.exit(1)

sys.exit(0 if is_trading_today() else 1)
