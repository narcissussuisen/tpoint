# -*- coding: utf-8 -*-
"""scripts/test_exit_card_push.py — 出场卡片推送验收（P6 建，2026-09-10 扩到方向感知）

覆盖 exit_reason × side（多头/空头）的真实信号卡片，用 monitor.emit_card 构造后
推送到信号群 webhook（1d241455）。标题统一加 [TEST] 前缀，不写 signal.txt（sim=True），
不污染实盘流水。

⚠️ 本脚本**会真实推送**，请人工确认后再运行；纯口径回归请跑 scripts/test_exit_label_side.py。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core'))

from monitor import emit_card, WEBHOOK_URL, _push_retry  # noqa: E402

# 用例：exit_reason × side（s = 15 元组：(sig_type, price, chg, level_val, level_type, rsi, temp,
#        vol_r, name, tag, exit_reason, day_chg, bar_tt, side, pos_pct)）
#        [2026-09-10] 尾部新增 side（s[13]）与 pos_pct（s[14]）——方向感知修复：
#        short 的 STOP 现在显示「突破止损 + 买入回补 + 反T平空 + 空头腿」，不再是多头语义的
#        「破位止损」（事故：2026-09-10 ST豆神创新高却提示破位止损）。
# 数据以 600721.SH 百花医药 2026-08-26 当日为底，模拟不同出场场景
CASES = [
    {'label': '空头 STOP 突破止损（09-10 事故场景）', 'reason': 'STOP', 'sym': '300010.SZ',
     's': ('X', 5.2671, -0.4, 5.2671, '硬止损线', 63.2, 70.0, 1.1, 'ST豆神',
           '通用算法综合-0.67[S] vwap, macd_div, rsi', 'STOP', 5.6, '2026-09-10T10:25:00', 'short', 2)},
    {'label': '空头 TRAIL 移动止盈（买回）', 'reason': 'TRAIL', 'sym': '300010.SZ',
     's': ('X', 5.24, 0.4, 5.2304, '移动止损线', 51.1, 61.0, 1.3, 'ST豆神',
           '通用算法综合-0.57[S] vwap, macd_div, rsi', 'TRAIL', 5.0, '2026-09-10T10:31:00', 'short', 2)},
    {'label': 'S 反T开空（卖底仓）', 'reason': '', 'sym': '300010.SZ',
     's': ('S', 5.25, 5.2, 5.21, '触及上轨', 59.1, 66.0, 1.2, 'ST豆神',
           '通用算法综合-0.67[S] vwap, macd_div, rsi', '', 5.2, '2026-09-10T10:19:00', 'short', 2)},
    {'label': 'FIXSTOP 固定止损（多头）', 'reason': 'FIXSTOP',
     's': ('X', 15.16, -1.62, 15.20, '固定止损线', 65.0, 70.0, 3.2, '百花医药',
           '通用算法综合-0.56[S] vwap, rsi', 'FIXSTOP', 1.3, '2026-08-26T14:26:00', 'long', 2)},
    {'label': '多头 STOP 破位止损', 'reason': 'STOP',
     's': ('X', 15.14, -0.40, 15.14, '硬止损线', 62.0, 68.0, 2.5, '百花医药',
           '通用算法综合-0.49[S] vwap, rsi', 'STOP', 1.1, '2026-08-26T10:34:00', 'long', 2)},
    {'label': 'S 信号平仓（多头）', 'reason': 'S',
     's': ('X', 15.12, 0.00, 15.12, '信号平仓', 60.0, 66.0, 2.2, '百花医药',
           '通用算法综合-0.45[S] vwap, rsi', 'S', 1.0, '2026-08-26T13:55:00', 'long', 2)},
    {'label': '多头 TRAIL 移动止盈', 'reason': 'TRAIL',
     's': ('X', 15.15, 0.50, 15.14, '移动止损线', 64.0, 72.0, 2.8, '百花医药',
           '通用算法综合-0.56[S] vwap, rsi', 'TRAIL', 1.2, '2026-08-26T14:57:00', 'long', 2)},
    {'label': 'TIME 时间止损', 'reason': 'TIME',
     's': ('X', 15.10, -0.20, 15.10, '时间止损', 58.0, 65.0, 2.0, '百花医药',
           '通用算法综合-0.50[S] vwap, rsi', 'TIME', 0.9, '2026-08-26T11:15:00', 'long', 2)},
    {'label': 'EOD 收盘强平', 'reason': 'EOD',
     's': ('X', 15.16, -0.10, 15.16, '收盘强平', 61.0, 67.0, 2.3, '百花医药',
           '通用算法综合-0.54[S] vwap, rsi', 'EOD', 1.3, '2026-08-26T15:00:00', 'long', 2)},
]


def main():
    print(f'推送 {len(CASES)} 种止损类型测试卡片 → 信号群\n')
    results = []
    for c in CASES:
        card = emit_card(c['s'], sym=c.get('sym', '600721.SH'), sim=True)
        # 标题加 [TEST] 前缀
        old_title = card['card']['header']['title']['content']
        card['card']['header']['title']['content'] = f"[TEST] {old_title}"
        # footer 补测试说明（避免误判为真实信号）
        for el in card['card'].get('elements', []):
            if el.get('tag') == 'note':
                note = el['elements'][0].get('content', '')
                el['elements'][0]['content'] = f"{note} ｜ [P6 标签测试 {c['reason']}]"
        ok, code, msg, attempts = _push_retry(WEBHOOK_URL, card)
        results.append({'label': c['label'], 'reason': c['reason'], 'ok': ok,
                        'code': code, 'msg': msg, 'attempts': attempts})
        print(f"  {'✅' if ok else '❌'} {c['label']:16s} → code={code} attempts={attempts}"
              f"{'' if ok else ' ' + str(msg)[:80]}")
    n_ok = sum(1 for r in results if r['ok'])
    print(f'\n结果: {n_ok}/{len(results)} 推送成功')


if __name__ == '__main__':
    main()
