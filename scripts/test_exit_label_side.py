# -*- coding: utf-8 -*-
"""scripts/test_exit_label_side.py — 卡片标签 / 配色 / 文案回归（2026-09-10）

两次事故与决策驱动的口径，本测试一并锁定：

**A. 方向盲标签（事故①）**：2026-09-10 ST豆神当日 +5.7%、创新高，卡片却提示「破位止损」。
根因：`STOP` 在生产里的唯一来源是**反T空头**（被向上突破），却用了多头语义「向下跌破支撑」。
⇒ `label_for(reason, side)` 方向感知：空头 STOP =「突破止损」/ 多头 =「破位止损」。

**B. 配色统一（用户 2026-09-10 决策）**：**所有买入一色、所有卖出一色** —— 采用 A 股惯例
**买入=红 / 卖出=绿**（⚠️ 会翻转旧含义：旧卡片买入=绿、卖出=红，且出场按 exit_reason 分 5 色）。
⇒ 配色单一真源 `exit_label.color_for_action()`；卡片 header.template 只由**实际下单动作**决定。

**C. 文案最简（用户 2026-09-10 决策）**：标题只留「代码 + 买/卖 + 仓位」，原因移到正文行1，
去掉「正T平多 / 反T平空 / 买入回补 / 空头腿」等术语；方向（正T/反T）下沉到卡片底部灰显备注。
⚠️ 但 `signal.txt` 的 `🟢/🔴/🔵 + BUY/SELL/EXIT + [STOP]` 是**下游解析契约**
（`prod_vs_bt_reconcile.RE_SIG`、`shadow_v3_review`），**只许追加、不许改语义**。

运行：venv/Scripts/python.exe scripts/test_exit_label_side.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

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
               'ST豆神', TAG, '', 5.2, '2026-09-10T10:19:00', 'short', 2)
SIG_B_ENTRY = ('B', 5.16, 3.4, 5.10, '触及下轨', 48.0, 60.0, 1.4,
               'ST豆神', '[通用算法综合0.52[B] vwap, rsi]', '', 3.4,
               '2026-09-10T10:40:00', 'long', 2)


def _card(s, sym='300010.SZ'):
    return M.emit_card(s, sym=sym, sim=True)


def _card_text(s, sym='300010.SZ'):
    """把 emit_card 输出拍平成可断言的文本（title / 各 div / note）。
    div 的 line1 带 markdown 粗体 **…**（emit_card 内约定），断言前剥掉。
    ⚠️ 配色标记追加在**末尾**，避免顶掉正文行的下标（[0]=title, [1]=行1, [2]=行2, …）。"""
    card = _card(s, sym=sym)
    out = [card['card']['header']['title']['content']]
    for el in card['card'].get('elements', []):
        if el.get('tag') == 'div':
            out.append(el['text']['content'].replace('**', ''))
        elif el.get('tag') == 'note':
            out.append(el['elements'][0]['content'])
    out.append('[COLOR=' + card['card']['header']['template'] + ']')
    return out


def color_of(s, sym='300010.SZ'):
    return _card(s, sym=sym)['card']['header']['template']


def main():
    print('\n=== 1. exit_label 标签/动作映射 ===')
    check("label_for('STOP') 旧签名=破位止损（向后兼容）",
          EL.label_for('STOP') == ('破位止损', 'orange', 'action'), str(EL.label_for('STOP')))
    check("label_for('STOP','long')=破位止损",
          EL.label_for('STOP', 'long') == ('破位止损', 'orange', 'action'))
    check("label_for('STOP','short')=突破止损（向上突破，空头被打爆）",
          EL.label_for('STOP', 'short') == ('突破止损', 'orange', 'action'),
          str(EL.label_for('STOP', 'short')))
    check('动作只有「买入/卖出」两个词（去掉「回补」术语）',
          EL.action_for('long') == '卖出' and EL.action_for('short') == '买入',
          f"{EL.action_for('long')}/{EL.action_for('short')}")
    check("color_for_action 单一真源：买入=red / 卖出=green（A股惯例）",
          EL.color_for_action('买入') == 'red' and EL.color_for_action('卖出') == 'green',
          f"{EL.color_for_action('买入')}/{EL.color_for_action('卖出')}")

    print('\n=== 2. ★ 配色统一：买红卖绿，且不再按 exit_reason 分 5 色 ===')
    check('空头 STOP（实际动作=买入）→ 红色',
          color_of(SIG_SHORT_STOP) == 'red', color_of(SIG_SHORT_STOP))
    check('多头 STOP（实际动作=卖出）→ 绿色',
          color_of(SIG_LONG_STOP) == 'green', color_of(SIG_LONG_STOP))
    check('空头 TRAIL（买入）→ 红色（旧的 green 已弃用）',
          color_of(SIG_SHORT_TRAIL) == 'red', color_of(SIG_SHORT_TRAIL))
    check('B 入场（买入）→ 红色',
          color_of(SIG_B_ENTRY) == 'red', color_of(SIG_B_ENTRY))
    check('S 入场（卖出）→ 绿色',
          color_of(SIG_S_ENTRY) == 'green', color_of(SIG_S_ENTRY))
    check('全部卡片配色只出现 red/green（橙/蓝/灰已弃用）',
          all(color_of(s) in ('red', 'green')
              for s in (SIG_SHORT_STOP, SIG_LONG_STOP, SIG_SHORT_TRAIL, SIG_S_ENTRY, SIG_B_ENTRY)))

    print('\n=== 3. 标题只留「代码 + 买/卖 + 仓位」===')
    t = _card_text(SIG_SHORT_STOP)
    check('空头 STOP 标题 = 300010 买入 2成', t[0] == '300010 买入 2成', f'got={t[0]!r}')
    check('标题不含原因（原因移到正文）', '止损' not in t[0], f'got={t[0]!r}')
    check('标题不含「回补/反T/正T」术语',
          all(w not in t[0] for w in ('回补', '反T', '正T')), f'got={t[0]!r}')
    check('多头 STOP 标题 = 300010 卖出 2成', _card_text(SIG_LONG_STOP)[0] == '300010 卖出 2成')
    check('空头 TRAIL 标题 = 300010 买入 2成', _card_text(SIG_SHORT_TRAIL)[0] == '300010 买入 2成')

    print('\n=== 4. 正文：行1=原因、行2=本笔口径、方向下沉到备注 ===')
    check('行1 = ST豆神·突破止损', t[1].startswith('ST豆神·突破止损'), f'got={t[1]!r}')
    check('行1 不含方向术语（正T平多/反T平空已移除）',
          all(w not in t[1] for w in ('正T', '反T', '平多', '平空')), f'got={t[1]!r}')
    check('行2 浮盈口径为「本笔」', '本笔 -0.4%' in t[2], f'got={t[2]!r}')
    check('行2 不再出现「空头腿/多头腿」术语',
          '空头腿' not in t[2] and '多头腿' not in t[2], f'got={t[2]!r}')
    check('方向与 reason 下沉到底部备注（正T/反T + reason=STOP）',
          any('反T' in x and 'reason=STOP' in x for x in t),
          f'got={[x for x in t if "RSI=" in x]!r}')
    check('备注保留「距触发 +0.0%」（short STOP 时 price==stop_price）',
          any('距触发+0.0%' in x for x in t), f'got={[x for x in t if "RSI=" in x]!r}')

    print('\n=== 5. 入场卡文案（去掉方向前缀）===')
    ts = _card_text(SIG_S_ENTRY)
    check('S 入场卡行1 = ST豆神·卖出｜做T·2成', ts[1].startswith('ST豆神·卖出｜做T·2成'), f'got={ts[1]!r}')
    tb = _card_text(SIG_B_ENTRY)
    check('B 入场卡行1 = ST豆神·买入｜做T·2成', tb[1].startswith('ST豆神·买入｜做T·2成'), f'got={tb[1]!r}')

    print('\n=== 6. signal.txt 下游解析契约（只许追加，不许改语义）===')
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
    check('EXIT 行名称解析正确 = ST豆神', bool(m_sig) and m_sig.group(2) == 'ST豆神')
    check('现价行仍被 RE_PX 解析', bool(m_px), f'line={x_lines[2]!r}')
    check('EXIT [STOP] 原始 token 保留（对账/复盘依赖）', 'EXIT [STOP]' in x_lines[1], f'line={x_lines[1]!r}')
    check('signal.txt 动作词已统一为「买入」、口径为「本笔」',
          '买入' in x_lines[1] and '本笔' in x_lines[2] and '回补' not in x_lines[1],
          f'lines={x_lines[1:3]!r}')
    s_lines = [ln for ln in captured[1].splitlines() if ln.strip()]
    m_s2 = RE_SIG.match(s_lines[1])
    check('SELL 行仍被 RE_SIG 解析且名称正确',
          bool(m_s2) and m_s2.group(2) == 'ST豆神', f'line={s_lines[1]!r}')

    print('\n=== 7. emit() fallback 路径（15 元组）===')
    tmp = os.path.join(tempfile.gettempdir(), '_tpoint_emit_probe.txt')
    if os.path.exists(tmp):
        os.remove(tmp)
    _orig_sig = M.SIGNAL_FILE
    M.SIGNAL_FILE = tmp
    try:
        msg = M.emit(*SIG_SHORT_STOP)
        ok_emit = isinstance(msg, str) and '本笔' in msg and '买入' in msg
    except Exception as e:                       # noqa: BLE001
        ok_emit = False
        msg = f'{type(e).__name__}: {e}'
    finally:
        M.SIGNAL_FILE = _orig_sig
        if os.path.exists(tmp):
            os.remove(tmp)
    check('emit(*s) 15 元组不抛 TypeError 且口径一致', ok_emit, str(msg)[:160])

    print('\n=== 8. 元组口径与下游同步自检 ===')
    check('_mk_exit 返回 14 元组且 s[13]=side',
          (lambda r: len(r) == 14 and r[13] == 'short')(
              M._mk_exit('STOP', 'ST豆神', STOP_PRICE,
                         {'side': 'short', 'entry_price': 5.2461, 'max_fav': 5.2461,
                          'stop_price': STOP_PRICE, 'entry_reason': 'x', 'size_pct': 2},
                         [0.0] * 5, [0.01] * 5, [60.0] * 5, [70.0] * 5, [1.0] * 5,
                         3, 4.99, ['2026-09-10 10:25:00'] * 5)))
    src = open(os.path.join(ROOT, 'scripts', 'daily_signal_review.py'), encoding='utf-8').read()
    check('daily_signal_review 已同步：s[14]=仓位', 'pos_pct = s[14] if len(s) > 14' in src)
    check('daily_signal_review 已同步：_type_cn 按 side 归买/卖',
          "_type_cn(op, exit_reason, side)" in src and "return '买入' if side == 'short' else '卖出'" in src)
    check('daily_signal_review 买卖配色已翻转为 买红/卖绿',
          "{'买入': '#d4380d', '卖出': '#0a8f3c'}" in src)
    check('daily_signal_review 状态色未被误翻（有效=绿 仍是 #0a8f3c）',
          "vtag = '<span style=\"color:#0a8f3c;font-weight:700\">有效</span>'" in src)

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
