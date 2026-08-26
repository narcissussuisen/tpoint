# -*- coding: utf-8 -*-
"""core/exit_label.py — 出场信号标签映射（单一真源，P6 交付物）

背景：v10.5.0 及之前 `core/monitor.py` 把 STOP/TRAIL/TIME 三种性质完全不同的出场源
折叠成同一个「止损」标签，用户在飞书卡片上无法按优先级操作（分不清「判断错了走」的
STOP 与「赚到了走」的 TRAIL）。

本模块将 `core/exit_manager.py:172-228` 的全部 exit_reason 映射为「中文标签 + 配色」，
作为**唯一真源**；`monitor.emit_card` / `_append_signal_txt` 只引用本表，不再硬编码。

优先级语义（对齐 exit_manager 出场检查顺序）：
  P0 FIXSTOP  固定止损  —— 兜底断路器（-1.5%），最高风险，立即认错
  P1 STOP     破位止损  —— ATR 跌破 / 趋势翻空，主动认错
  P2 S        信号平仓  —— S 信号自然出场（中性）
  P3 TRAIL    移动止盈  —— 浮盈 ≥0.4% 激活，回撤 0.6% 锁利（好信号）
  P4 TIME     时间止损  —— 持仓 90 根 bar 无进展（反思信号）
  P5 EOD      收盘强平  —— 日终兜底（无需操作）

额外出口（非 EXIT_LABEL_MAP 覆盖）：
  - exit_reason == 'B' 且 sig_type=='X' → 空仓回补 = 买入（monitor 单独处理）
  - 其他空平（'空平' in level_type）   → 买入（monitor 单独处理）
"""
from typing import Dict, Tuple

#: exit_reason -> (中文标签, 飞书卡片 template 配色)
#: 配色取值：green/red/blue/orange/grey/purple（飞书 interactive card header.template）
EXIT_LABEL_MAP: Dict[str, Tuple[str, str]] = {
    'FIXSTOP': ('固定止损', 'red'),     # P0 最高优先，立即认错
    'STOP':    ('破位止损', 'orange'),   # P1 主动认错
    'S':       ('信号平仓', 'blue'),     # P2 自然出场
    'TRAIL':   ('移动止盈', 'green'),    # P3 浮盈保护成功（不是止损！）
    'TIME':    ('时间止损', 'grey'),     # P4 释放资金，反思信号
    'EOD':     ('收盘强平', 'grey'),     # P5 日终兜底，无需操作
}

#: 展示顺序（用于报告/文档的稳定排序；与出场检查优先级一致）
EXIT_LABEL_ORDER: Tuple[str, ...] = ('FIXSTOP', 'STOP', 'S', 'TRAIL', 'TIME', 'EOD')


def label_for(reason: str) -> Tuple[str, str]:
    """按 exit_reason 取 (中文标签, 配色)。未知 reason 返回中性兜底（不回退到旧「止损」坍缩）。"""
    if reason in EXIT_LABEL_MAP:
        return EXIT_LABEL_MAP[reason]
    return ('卖出', 'red')  # 未知 reason 保守按卖出处理，但不伪装成「止损」
