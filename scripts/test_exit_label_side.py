# -*- coding: utf-8 -*-
"""scripts/test_exit_label_side.py — 出场标签方向感知回归（2026-09-10）

事故背景：2026-09-10 ST豆神(300010.SZ) 当日 +5.7%、日内最高 5.28 创新高，
飞书卡片却提示「ST豆神 破位止损 2成」，用户判定逻辑矛盾。

根因（非算法错误，是展示层语义错误）：
  `core/exit_label.py` 的 STOP 标签**方向盲**——「破位止损」是「向下跌破支撑」的
  多头语义，但生产的 STOP 唯一来源是**反T空头**（monitor.py short 腿 `c[i] >= stop_price`）。
  空头被打爆是**向上突破**，字面完全相反。叠加卡片不显方向、且「持仓 -0.4%」
  实为**空头腿**浮亏（多头口径应是 +0.4%），三处叠加 → 误读。

本测试锁定修复后口径：
  1. `label_for(reason)` 旧签名行为不变（向后兼容，无 side 仍走多头表）；
  2. `label_for('STOP','short')` = 突破止损 / `('STOP','long')` = 破位止损；
  3. 卡片标题 = 实际下单动作 + 仓位 + 原因（不再用 reason 顶替动作）；
     行1 带 正T/反T + 平多/平空；行2 浮盈口径为「多头腿/空头腿」而非「持仓」；
  4. 元组口径 s[13]=side、s[14]=仓位成数（_mk_exit / 内联 B·S 元组一致）；
  5. signal.txt 仍能被 scripts/prod_vs_bt_reconcile.py 的 RE_SIG / RE_PX 解析（格式兼容）；
  6. `emit(*s)` fallback 路径能吃下 15 元组（不引入 TypeError 半修复陷阱）。

运行：venv/Scripts/python.exe scripts/test_exit_label_side.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core'))

import exit_label as EL          # noqa: E402
import monitor as M              # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=''):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


# --------------------------------------------------------------------------- #
# 事故真实点位（2026-09-10 10:25 ST豆神，取自 data/push_audit.jsonl + signal.txt）
#   10:19 反T开空 @5.25(底仓卖出) → 10:25 c=5.2722 ≥ 5.25+1.5×1mATR → STOP 买回
#   chg=(5.2461-5.2671)/5.2461 = -0.4%；day_chg=+5.6%；卡面「距触发 +0.0%」
#   （short STOP 时 price 与 level_val 同为 stop_price，故 trigger_pct 恒为 0）
# --------------------------------------------------------------------------- #
TAG = '[通用算法综合-0.67[S] vwap, macd_div, rsi]'
STOP_PRICE = 5.2671
SIG_SHORT_STOP = ('X', STOP_PRICE, -0.4, STOP_PRICE, '硬止损线', 63.2, 70.0, 1.1,
                  'ST豆神', TAG, 'STOP', 5.6, '2026-09-10T10:25:00', 'short', 2)
SIG_LONG_STOP = ('X', STOP_PRICE, -0.4, STOP_PRICE, '硬止损线', 63.2, 70.0, 1.1,
                 'ST豆神', TAG, 'STOP', 5.6, '2026-09-10T10:25:00', 'long', 2)
SIG_SHORT_TRAIL = ('X', 5.24, 0.4, 5.2304, '移动止损线', 51.1, 61.0, 1.3,
                   'ST豆神', TAG, 'TRAIL', 5.0, '2026-09-10T10:31:00', 'short', 2)
SIG_S_ENTRY = ('S', 5.25, 5.2, 5.21, '触及上轨', 59.1, 66.0, 1.2,
               'ST豆神', '[通用算法综合-0.67[S] vwap, macd_div, rsi]', '', 5.2,
               '2026-09-10T10:19:00', 'short', 2)
SIG_B_ENTRY = ('B', 5.16, 3.4, 5.10, '触及下轨', 48.0, 60.0, 1.4,
               'ST豆神', '[通用算法综合0.52[B] vwap, rsi]', '', 3.4,
               '2026-09-10T10:40:00', 'long', 2)


def _card_text(s, sym='300010.SZ'):
    """把 emit_card 输出拍平成可断言的文本（title / 各 div / note）。
    div 的 line1 带 markdown 粗体 **…**（emit_card 内约定），断言前剥掉。"""
    card = M.emit_card(s, sym=sym, sim=True)
    out = [card['card']['header']['title']['content']]
    for el in card['card'].get('elements', []):
        if el.get('tag') == 'div':
            out.append(el['text']['content'].replace('**', ''))
        elif el.get('tag') == 'note':
            out.append(el['elements'][0]['content'])
    return out


def main():
    print('\n=== 1. exit_label 方向感知映射 ===')
    check("label_for('STOP') 旧签名=破位止损（向后兼容）",
          EL.label_for('STOP') == ('破位止损', 'orange', 'action'), str(EL.label_for('STOP')))
    check("label_for('STOP','long')=破位止损",
          EL.label_for('STOP', 'long') == ('破位止损', 'orange', 'action'))
    check("label_for('STOP','short')=突破止损（向上突破，空头被打爆）",
          EL.label_for('STOP', 'short') == ('突破止损', 'orange', 'action'),
          str(EL.label_for('STOP', 'short')))
    check("label_for('TRAIL','short') 仍=移动止盈（机制而非方向）",
          EL.label_for('TRAIL', 'short')[0] == '移动止盈')
    check("未知 side 归一化为 long（None/''→否不炸）",
          EL.label_for('STOP', None) == EL.label_for('STOP', 'long')
          and EL.label_for('STOP', '') == EL.label_for('STOP', 'long'))
    check('动作表：long=卖出 / short=买入回补',
          EL.action_for('long') == '卖出' and EL.action_for('short') == '买入回补')
    check('腿名表：多头腿 / 空头腿',
          EL.leg_for('long') == '多头腿' and EL.leg_for('short') == '空头腿')
    check('入场方向：B=正T / S=反T',
          EL.entry_direction_for('B') == '正T' and EL.entry_direction_for('S') == '反T')

    print('\n=== 2. 事故卡片：反T空头被向上突破止损 ===')
    t = _card_text(SIG_SHORT_STOP)
    check('标题含实际动作「买入回补」+ 原因「突破止损」',
          t[0] == '300010 买入回补 2成 · 突破止损', f'got={t[0]!r}')
    check('标题**不再**出现误导性的「破位止损」', '破位止损' not in t[0], f'got={t[0]!r}')
    check('行1 = ST豆神·反T平空｜突破止损', t[1].startswith('ST豆神·反T平空｜突破止损'), f'got={t[1]!r}')
    check('行2 浮盈口径为「空头腿」而非「持仓」',
          '空头腿 -0.4%' in t[2] and '持仓' not in t[2], f'got={t[2]!r}')
    check('行2 保留 [STOP] 原始 reason（复盘/对账可追溯）', '[STOP]' in t[2], f'got={t[2]!r}')
    check('备注距触发 +0.0%（short STOP 时 price==stop_price）',
          '距触发+0.0%' in t[-1], f'got={t[-1]!r}')

    print('\n=== 3. 多头对照：同一 STOP 仍是破位止损 ===')
    tl = _card_text(SIG_LONG_STOP)
    check('多头标题 = 卖出 · 破位止损', tl[0] == '300010 卖出 2成 · 破位止损', f'got={tl[0]!r}')
    check('多头行1 = 正T平多｜破位止损', tl[1].startswith('ST豆神·正T平多｜破位止损'), f'got={tl[1]!r}')
    check('多头行2 用「多头腿」', '多头腿 -0.4%' in tl[2], f'got={tl[2]!r}')

    print('\n=== 4. 其余出场/入场卡片方向标记 ===')
    tt = _card_text(SIG_SHORT_TRAIL)
    check('short TRAIL：买入回补 · 移动止盈 / 反T平空',
          tt[0] == '300010 买入回补 2成 · 移动止盈' and tt[1].startswith('ST豆神·反T平空｜移动止盈'),
          f'got={tt[0]!r} / {tt[1]!r}')
    ts = _card_text(SIG_S_ENTRY)
    check('S 入场卡显式标注反T（底仓卖出做T）',
          ts[0] == '300010 卖出 2成' and ts[1].startswith('ST豆神·反T卖出'), f'got={ts[0]!r} / {ts[1]!r}')
    tb = _card_text(SIG_B_ENTRY)
    check('B 入场卡标注正T', tb[0] == '300010 买入 2成' and tb[1].startswith('ST豆神·正T买入'),
          f'got={tb[0]!r} / {tb[1]!r}')

    print('\n=== 5. signal.txt 格式兼容（prod_vs_bt_reconcile 解析）===')
    # 与 scripts/prod_vs_bt_reconcile.py:95/99 完全同源的正则（改那边必须同步改这里）
    RE_SIG = re.compile(r'^(\U0001F7E2|\U0001F534|\U0001F535)\s+(.+?)\s+(BUY|SELL|EXIT)\b')
    RE_PX = re.compile(r'^现价\s+([0-9.]+)')
    captured = []
    _orig = M._buffered_append
    M._buffered_append = lambda path, text, label: captured.append(text)
    try:
        M._append_signal_txt(SIG_SHORT_STOP)
        M._append_signal_txt(SIG_S_ENTRY)
    finally:
        M._buffered_append = _orig
    check('_append_signal_txt 未写入生产文件（测试用捕获替身）', len(captured) == 2, f'got={len(captured)}')
    x_lines = [ln for ln in captured[0].splitlines() if ln.strip()]
    m_sig = RE_SIG.match(x_lines[1])
    m_px = RE_PX.match(x_lines[2])
    check('EXIT 行仍被 RE_SIG 解析（emoji+名称+EXIT）', bool(m_sig), f'line={x_lines[1]!r}')
    check('EXIT 行名称解析正确 = ST豆神', bool(m_sig) and m_sig.group(2) == 'ST豆神',
          f'got={m_sig.group(2) if m_sig else None!r}')
    check('现价行仍被 RE_PX 解析', bool(m_px), f'line={x_lines[2]!r}')
    check('EXIT [STOP] 原始 token 保留（对账/复盘依赖）', 'EXIT [STOP]' in x_lines[1], f'line={x_lines[1]!r}')
    check('signal.txt 也标注 反T平空·买回 + 空头腿',
          '反T平空·买回' in x_lines[1] and '空头腿' in x_lines[2],
          f'lines={x_lines[1:3]!r}')
    s_lines = [ln for ln in captured[1].splitlines() if ln.strip()]
    m_s2 = RE_SIG.match(s_lines[1])
    check('SELL 行仍被 RE_SIG 解析且名称正确',
          bool(m_s2) and m_s2.group(2) == 'ST豆神', f'line={s_lines[1]!r}')

    print('\n=== 6. emit() fallback 路径吃下 15 元组（半修复陷阱）===')
    tmp = os.path.join(tempfile.gettempdir(), '_tpoint_emit_probe.txt')
    if os.path.exists(tmp):
        os.remove(tmp)
    _orig_sig = M.SIGNAL_FILE
    M.SIGNAL_FILE = tmp
    try:
        msg = M.emit(*SIG_SHORT_STOP)
        ok_emit = isinstance(msg, str) and '空头腿' in msg and '买回' in msg
    except Exception as e:                       # noqa: BLE001
        ok_emit = False
        msg = f'{type(e).__name__}: {e}'
    finally:
        M.SIGNAL_FILE = _orig_sig
        if os.path.exists(tmp):
            os.remove(tmp)
    check('emit(*s) 15 元组不抛 TypeError 且口径一致', ok_emit, str(msg)[:160])

    print('\n=== 7. 元组口径自检 ===')
    check('_mk_exit 返回 14 元组且 s[13]=side',
          (lambda r: len(r) == 14 and r[13] == 'short')(
              M._mk_exit('STOP', 'ST豆神', STOP_PRICE,
                         {'side': 'short', 'entry_price': 5.2461, 'max_fav': 5.2461,
                          'stop_price': STOP_PRICE, 'entry_reason': 'x', 'size_pct': 2},
                         [0.0] * 5, [0.01] * 5, [60.0] * 5, [70.0] * 5, [1.0] * 5,
                         3, 4.99, ['2026-09-10 10:25:00'] * 5)))
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'scripts', 'daily_signal_review.py'), encoding='utf-8').read()
    check('daily_signal_review 已同步为 s[14]=仓位（防复盘 HTML 输出 long/short）',
          'pos_pct = s[14] if len(s) > 14' in src)

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
