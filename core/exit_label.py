# -*- coding: utf-8 -*-
"""core/exit_label.py — 出场信号标签映射（单一真源，P6 交付物）

背景：v10.5.0 及之前 `core/monitor.py` 把 STOP/TRAIL/TIME 三种性质完全不同的出场源
折叠成同一个「止损」标签，用户在飞书卡片上无法按优先级操作（分不清「判断错了走」的
STOP 与「赚到了走」的 TRAIL）。

本模块将 `core/exit_manager.py:172-228` 的全部 exit_reason 映射为「中文标签 + 配色」，
作为**唯一真源**；`monitor.emit_card` / `_append_signal_txt` / `emit` 只引用本表，不再硬编码。

⚠️ 方向感知（2026-09-10 修复）
    原实现**方向盲**：`STOP` 只有一个标签「破位止损」（= 向下跌破支撑，多头语义），
    但生产的 `STOP` 唯一来源是**反T空头**（见 monitor.py:1374-1376，short 腿
    `c[i] >= stop_price` 触发）。空头被打爆是**向上突破**，用「破位止损」字面完全相反，
    造成"个股创新高却提示破位止损"的误读事故（2026-09-10 ST豆神 300010）。
    ⇒ 本模块所有公开函数新增 `side` 参数（'long'|'short'），标签/动作/腿名按方向取表；
      `side` 缺省 'long'，保持旧调用完全兼容。

优先级语义（对齐 exit_manager 出场检查顺序）+ P12 推送分级：
  P0 FIXSTOP  固定止损  —— 兜底断路器（生产 EXIT_CFG 已关 use_fixed_stop=False，v10.10.0 起不再产生；
                            保留映射供 reintroduce 时用；如需尾部保护经 monitor_config 热重载 3.0 档）
  P1 STOP     long  破位止损（向下跌破支撑） / short 突破止损（向上突破，空头被打爆）—— 强提示（必推）
  P2 S        信号平仓  —— 反向信号自然出场（中性，照常推送）
  P3 TRAIL    移动止盈  —— 浮盈 ≥0.4% 激活，回撤 0.6% 锁利（正反馈，必推）
  P4 TIME     时间止损  —— 持仓 90 根 bar 无进展（信息级）
  P5 EOD      收盘强平  —— 日终兜底（结算信息，照常推送）

推送分级（P12 落地，2026-08-26 A/B 验证驱动）：
  level='action'   —— 用户必须处理的指令级信号（反T STOP）
  level='remind'   —— 提醒/风险告知（FIXSTOP 若 reintroduce）
  level='info'     —— 状态/结算信息（S/TIME/EOD）
  level='positive' —— 正反馈锁利（TRAIL）

额外出口（非 EXIT_LABEL_MAP 覆盖）：
  - exit_reason == 'B' 且 side=='short' → 空仓回补 = 买入（monitor 单独处理，标签「信号回补」）
  - exit_reason == 'S' 且 side=='long'  → 平多 = 卖出（标签「信号平仓」）
"""
from typing import Dict, Optional, Tuple

#: 多头出场标签（原 v10.6.0 语义，side 缺省时即此表）
#: exit_reason -> (中文标签, 飞书卡片 template 配色, 推送级别)
#: 配色取值：green/red/blue/orange/grey/purple（飞书 interactive card header.template）
#: level 取值：action(指令级，必推) / remind(提醒) / positive(正反馈) / info(信息级)
EXIT_LABEL_MAP: Dict[str, Tuple[str, str, str]] = {
    'FIXSTOP': ('固定止损', 'red', 'remind'),      # 生产已关（v10.10.0），保留映射
    'STOP':    ('破位止损', 'orange', 'action'),    # 多头硬止损：向下跌破支撑（必推）
    'S':       ('信号平仓', 'blue', 'info'),        # 自然出场：反向 S 信号平多
    'TRAIL':   ('移动止盈', 'green', 'positive'),   # 浮盈保护成功（不是止损！）
    'TIME':    ('时间止损', 'grey', 'info'),        # 释放资金，反思信号
    'EOD':     ('收盘强平', 'grey', 'info'),        # 日终兜底
}

#: 空头（反T）出场标签。仅 STOP 的措辞与多头镜像（向上突破 vs 向下跌破），
#: 其余 reason 描述的是**机制**而非方向，故与多头表一致（避免无意义的分叉）。
EXIT_LABEL_MAP_SHORT: Dict[str, Tuple[str, str, str]] = {
    'FIXSTOP': ('固定止损', 'red', 'remind'),
    'STOP':    ('突破止损', 'orange', 'action'),    # 空头硬止损：向上突破，空头被打爆（必推）
    'S':       ('信号平仓', 'blue', 'info'),
    'TRAIL':   ('移动止盈', 'green', 'positive'),
    'TIME':    ('时间止损', 'grey', 'info'),
    'EOD':     ('收盘强平', 'grey', 'info'),
}

#: 展示顺序（用于报告/文档的稳定排序；与出场检查优先级一致）
EXIT_LABEL_ORDER: Tuple[str, ...] = ('FIXSTOP', 'STOP', 'S', 'TRAIL', 'TIME', 'EOD')

#: 方向 → 平仓动作。EXIT 一律是「平掉当前这条腿」；多头平仓=卖出，空头平仓=买入。
#: [2026-09-10] 统一为**买入/卖出**两个词（用户决策：不要「回补」这类术语，
#: 指令必须一眼看懂是买还是卖）；方向语义（正T/反T）移入卡片底部灰显备注。
ACTION_BY_SIDE: Dict[str, str] = {'long': '卖出', 'short': '买入'}

#: 方向 → 腿名（卡片里标注"本腿浮盈"，与底仓浮盈区分开——
#: 2026-09-10 误读事故的第二来源：卡片只写「持仓 -0.4%」，用户以为是底仓亏）
LEG_BY_SIDE: Dict[str, str] = {'long': '多头腿', 'short': '空头腿'}

#: 方向 → 做T方向标记（正T=先买后卖 / 反T=先卖后买，与用户口径一致）
DIRECTION_BY_SIDE: Dict[str, str] = {'long': '正T', 'short': '反T'}

#: EXIT → 卡片副标题用的「平仓腿」短描述
CLOSE_LEG_BY_SIDE: Dict[str, str] = {'long': '平多', 'short': '平空'}

#: ─────────────────────────────────────────────────────────────────────────────
#: 卡片配色**单一真源**（用户 2026-09-10 决策）：
#:   「所有买入一种颜色、所有卖出一种颜色」，不按 exit_reason 分散配色。
#: 采用 **A 股惯例：买入=红 / 卖出=绿**（与行情涨跌色一致；用户明确拍板）。
#: ⚠️ 这 ≠ 国际惯例的「买绿卖红」，也不是本模块旧行为（旧行为买卖=绿/红 + 出场 5 色）。
#: ⇒ 下列 EXIT_LABEL_MAP / EXIT_LABEL_MAP_SHORT 的**第 2 列（配色）已停用**：
#:   仅为向后兼容保留字段（`label_for()` 的调用方仍在解包三元组），
#:   卡片配色一律走 `color_for_action()`。
#: ─────────────────────────────────────────────────────────────────────────────
ACTION_COLOR: Dict[str, str] = {'买入': 'red', '卖出': 'green'}


def color_for_action(action: str) -> str:
    """实际下单动作 → 飞书卡片 header 配色（买红/卖绿，A 股惯例）。单一真源。"""
    return ACTION_COLOR.get(action, 'grey')


def _norm_side(side: Optional[str]) -> str:
    """归一化方向；None/未知 → 'long'（保持 v10.6.0 行为，向后兼容）。"""
    return 'short' if side == 'short' else 'long'


def label_for(reason: str, side: Optional[str] = None) -> Tuple[str, str, str]:
    """按 exit_reason + side 取 (中文标签, 配色, 推送级别)。

    side='short' → STOP 走「突破止损」（向上突破）；其余 reason 与多头同表。
    side 缺省 → 多头语义（与 v10.6.0 完全一致）。未知 reason 返回保守兜底。
    """
    table = EXIT_LABEL_MAP_SHORT if _norm_side(side) == 'short' else EXIT_LABEL_MAP
    if reason in table:
        return table[reason]
    return ('卖出', 'red', 'info')  # 未知 reason 保守按卖出处理，但不伪装成「止损」


def action_for(side: Optional[str]) -> str:
    """平仓腿对应的**实际下单动作**：多头平仓=卖出，空头平仓=买入。"""
    return ACTION_BY_SIDE[_norm_side(side)]


def leg_for(side: Optional[str]) -> str:
    """腿名（多头腿/空头腿），用于标注卡片里的浮盈归属，避免与底仓混淆。"""
    return LEG_BY_SIDE[_norm_side(side)]


def direction_for(side: Optional[str]) -> str:
    """做T方向标记：正T / 反T。"""
    return DIRECTION_BY_SIDE[_norm_side(side)]


def close_leg_for(side: Optional[str]) -> str:
    """平仓腿短描述：平多 / 平空。"""
    return CLOSE_LEG_BY_SIDE[_norm_side(side)]


def entry_direction_for(sig_type: str) -> str:
    """入场信号的方向标记：B(开多/加多)=正T，S(开空/加空)=反T。

    底仓模型下 S 入场 = 卖底仓做反T，卡片必须显式标出「反T」，否则会被读成普通卖出。
    """
    return '正T' if sig_type == 'B' else '反T'
