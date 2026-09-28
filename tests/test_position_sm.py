# -*- coding: utf-8 -*-
"""tests/test_position_sm.py — T1.5 单一仓位状态机语义测试（2026-09-03）

验收场景：
  1. 反T（S→B回补，有底仓）：1 trip，reason='B回补'，净收益 = 毛收益 - 双边成本
     （对照 09-03 实盘 603318：S@9.62→B@9.41，毛 2.183%，净应为 2.067%）
  2. 无底仓禁反T：S 不建仓（禁裸卖空），0 trips
  3. 正T（B→S）：1 trip，reason='S'
  4. 成本方向（关键回归）：反T 净收益 ≠ 毛收益 + 成本（simulate_bidirectional
     翻转实现的 bug：净 = -gross_long + cost，成本变补贴，本状态机必须修复）
  5. 信号单用：B+S 混合序列每笔信号只做一个动作
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.exit_manager import make_config, cost_for_symbol
from core.simulate_position_sm import simulate_position_sm

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail else ""))


def mk_prices(n=240, base=9.6):
    """平坦价格序列 + 一个价格路径覆盖（供信号用真实 bar 价）。"""
    c = [base] * n
    h = [base + 0.01] * n
    lo = [base - 0.01] * n
    atr = [0.02] * n
    return {'n': n, 'c': c, 'h': h, 'lo': lo, 'atr': atr, 'trend': None,
            'pc': 10.0, 'sym': '603318.SH', 'date': '2026-09-03'}


def mk_cfg():
    """纯信号出场配置（关 STOP/TIME/TRAIL），隔离状态机语义本身。"""
    return make_config(use_stop=False, use_time=False, use_trailing=False,
                       s_signal_exit=True, use_fixed_stop=False)


def main():
    # 成本：603318 沪市股票 佣金万一双边 + 印花税卖边万5.641 + 滑点 2bps/边
    buy_c, sell_c = cost_for_symbol('603318.SH')
    total_cost = buy_c + sell_c
    print(f"成本: buy={buy_c:.4f}% sell={sell_c:.4f}% total={total_cost:.4f}%")
    cfg = mk_cfg()

    # ---- 场景 1+4：反T（S→B回补），09-03 实盘镜像 ----
    p = mk_prices()
    # S@9.62 (idx 60)，B@9.41 (idx 90) —— 直接把信号价写进价格序列供出场用 bar close
    p['c'][90] = 9.41
    sigs = [{'type': 'S', 'idx': 60, 'price': 9.62, 'reason': 'test'},
            {'type': 'B', 'idx': 90, 'price': 9.41, 'reason': 'test'}]
    out = simulate_position_sm([('2026-09-03', sigs)], [('2026-09-03', p)],
                               config_long=cfg, config_short=cfg,
                               cost=(buy_c, sell_c), has_base=True)
    t = out['trips']
    gross_expect = round((9.62 - 9.41) / 9.62 * 100, 3)  # 2.183
    net_expect = round(gross_expect - total_cost, 3)      # ~2.067
    check("S1 反T 生成 1 trip", len(t) == 1 and t[0]['side'] == 'S', f"trips={len(t)}")
    check("S1 reason=B回补", t and t[0]['exit_reason'] == 'B回补', f"reason={t[0]['exit_reason'] if t else None}")
    check("S1 毛收益≈2.183%", t and abs(t[0]['gross_ret_pct'] - gross_expect) < 0.01,
          f"gross={t[0]['gross_ret_pct'] if t else None}")
    check("S4 净收益=毛-成本(≈2.067, 非+成本)", t and abs(t[0]['ret_pct'] - net_expect) < 0.01,
          f"net={t[0]['ret_pct'] if t else None} expect={net_expect}")
    check("S4 净<毛（成本是支出）", t and t[0]['ret_pct'] < t[0]['gross_ret_pct'])

    # ---- 场景 2：无底仓禁反T（B 仍可正T，但不得出现 side='S' trip） ----
    out2 = simulate_position_sm([('2026-09-03', sigs)], [('2026-09-03', p)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=False)
    n_short2 = sum(1 for x in out2['trips'] if x['side'] == 'S')
    check("S2 无底仓 0 反T trip（禁裸卖空）", n_short2 == 0,
          f"short_trips={n_short2} total={len(out2['trips'])}")

    # ---- 场景 3：正T（B→S） ----
    p3 = mk_prices()
    p3['c'][90] = 9.83
    sigs3 = [{'type': 'B', 'idx': 60, 'price': 9.62, 'reason': 'test'},
             {'type': 'S', 'idx': 90, 'price': 9.83, 'reason': 'test'}]
    out3 = simulate_position_sm([('2026-09-03', sigs3)], [('2026-09-03', p3)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True)
    t3 = out3['trips']
    g3 = round((9.83 - 9.62) / 9.62 * 100, 3)
    n3 = round(g3 - total_cost, 3)
    check("S3 正T 1 trip side=B", len(t3) == 1 and t3[0]['side'] == 'B')
    check("S3 reason=S", t3 and t3[0]['exit_reason'] == 'S')
    check("S3 净收益=毛-成本", t3 and abs(t3[0]['ret_pct'] - n3) < 0.01,
          f"net={t3[0]['ret_pct'] if t3 else None} expect={n3}")

    # ---- 场景 5：信号单用（B,S 同日交替，无双重计费） ----
    # 序列 B(60) S(90) B(120) S(150)：S 被正T 用作出场（pos=long 时 S=平仓），
    # 状态机应产出 2 个正T trip；旧相加口径 simulate_day 2 正T + bidirectional 2 反T = 4（双计费）
    p5 = mk_prices()
    for i, px in [(60, 9.62), (90, 9.83), (120, 9.60), (150, 9.80)]:
        p5['c'][i] = px
    sigs5 = [{'type': 'B', 'idx': 60, 'price': 9.62}, {'type': 'S', 'idx': 90, 'price': 9.83},
             {'type': 'B', 'idx': 120, 'price': 9.60}, {'type': 'S', 'idx': 150, 'price': 9.80}]
    out5 = simulate_position_sm([('2026-09-03', sigs5)], [('2026-09-03', p5)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True)
    check("S5 信号单用：4 信号 → 2 trips（非 4）", len(out5['trips']) == 2,
          f"trips={len(out5['trips'])} (旧相加口径会给 4)")

    # ---- 场景 6：反T EOD 强平（无 B 回补信号） ----
    p6 = mk_prices()
    p6['c'][200:] = [9.3] * 40
    sigs6 = [{'type': 'S', 'idx': 60, 'price': 9.62}]
    out6 = simulate_position_sm([('2026-09-03', sigs6)], [('2026-09-03', p6)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True)
    t6 = out6['trips']
    check("S6 反T 无回补 → EOD 强平 1 trip", len(t6) == 1 and t6[0]['exit_reason'] == 'EOD',
          f"trips={len(t6)}")

    # ================================================================= #
    # exec_delay_bars=1（人审卡 P-20260928-samebar 方案 A，2026-09-28 裁决）
    # 语义：信号 bar 收盘确认、次根 bar open 成交；末根 bar 信号 drop；
    #       成交 bar 一字锁板放弃；STOP/FIXSTOP/TRAIL/TIME 保持盘中触发价。
    # ================================================================= #
    print("\n-- exec_delay_bars=1 臂 --")

    # ---- D1：正T 延迟成交（B@60 → fill o[61]；S@90 → fill o[91]） ----
    pd1 = mk_prices()
    pd1['o'] = [9.6] * 240
    pd1['o'][61] = 9.65   # B 成交价（次根 open）
    pd1['o'][91] = 9.90   # S 出场成交价（次根 open）
    sigsd1 = [{'type': 'B', 'idx': 60, 'price': 9.62, 'reason': 't'},
              {'type': 'S', 'idx': 90, 'price': 9.83, 'reason': 't'}]
    od1 = simulate_position_sm([('2026-09-03', sigsd1)], [('2026-09-03', pd1)],
                               config_long=cfg, config_short=cfg,
                               cost=(buy_c, sell_c), has_base=True, exec_delay_bars=1)
    td1 = od1['trips']
    gd1 = round((9.90 - 9.65) / 9.65 * 100, 3)
    nd1 = round(gd1 - total_cost, 3)
    check("D1 正T 1 trip", len(td1) == 1 and td1[0]['side'] == 'B', f"trips={len(td1)}")
    check("D1 入场=次根 open 9.65（非信号价 9.62）",
          td1 and abs(td1[0]['entry_price'] - 9.65) < 1e-9,
          f"entry={td1[0]['entry_price'] if td1 else None}")
    check("D1 出场=次根 open 9.90", td1 and abs(td1[0]['exit_price'] - 9.90) < 1e-9,
          f"exit={td1[0]['exit_price'] if td1 else None}")
    check("D1 entry_idx=61（TIME 锚定成交 bar）",
          td1 and td1[0].get('entry_idx') == 61,
          f"entry_idx={td1[0].get('entry_idx') if td1 else None}")
    check("D1 净=毛-成本", td1 and abs(td1[0]['ret_pct'] - nd1) < 0.01,
          f"net={td1[0]['ret_pct'] if td1 else None} expect={nd1}")

    # ---- D2：末根 bar 信号 drop（idx=n-1 无次 bar） ----
    pd2 = mk_prices()
    pd2['o'] = [9.6] * 240
    sigsd2 = [{'type': 'B', 'idx': 239, 'price': 9.62}]
    od2 = simulate_position_sm([('2026-09-03', sigsd2)], [('2026-09-03', pd2)],
                               config_long=cfg, config_short=cfg,
                               cost=(buy_c, sell_c), has_base=True, exec_delay_bars=1)
    check("D2 末根 bar 信号 drop（0 trips）", len(od2['trips']) == 0,
          f"trips={len(od2['trips'])}")

    # ---- D3：缺 'o' 数组 + delay=1 ⇒ ValueError（fail-loud） ----
    try:
        simulate_position_sm([('2026-09-03', sigsd1)], [('2026-09-03', mk_prices())],
                              config_long=cfg, config_short=cfg,
                              cost=(buy_c, sell_c), has_base=True, exec_delay_bars=1)
        check("D3 缺 'o' 抛 ValueError", False, "未抛异常")
    except ValueError:
        check("D3 缺 'o' 抛 ValueError", True)
    except Exception as e:
        check("D3 缺 'o' 抛 ValueError", False, f"抛了 {type(e).__name__}")

    # ---- D4：exec_delay_bars=2 ⇒ ValueError（仅支持 0/1） ----
    try:
        simulate_position_sm([('2026-09-03', sigsd1)], [('2026-09-03', pd1)],
                              config_long=cfg, config_short=cfg,
                              cost=(buy_c, sell_c), has_base=True, exec_delay_bars=2)
        check("D4 delay=2 抛 ValueError", False, "未抛异常")
    except ValueError:
        check("D4 delay=2 抛 ValueError", True)
    except Exception as e:
        check("D4 delay=2 抛 ValueError", False, f"抛了 {type(e).__name__}")

    # ---- D5：成交 bar 一字涨停禁买入（信号放弃） ----
    pd5 = mk_prices()
    pd5['o'] = [9.6] * 240
    # 次根 bar 61 一字涨停：lo>=10.98（pc=10, 10% 限制）
    pd5['lo'][61] = 10.99; pd5['h'][61] = 11.0; pd5['o'][61] = 11.0
    sigsd5 = [{'type': 'B', 'idx': 60, 'price': 9.62}]
    od5 = simulate_position_sm([('2026-09-03', sigsd5)], [('2026-09-03', pd5)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True, exec_delay_bars=1)
    check("D5 成交 bar 一字涨停 → 入场放弃（0 trips）", len(od5['trips']) == 0,
          f"trips={len(od5['trips'])}")

    # ---- D6：反T 延迟成交（S@60 → fill o[61]；B@90 回补 fill o[91]） ----
    pd6 = mk_prices()
    pd6['o'] = [9.6] * 240
    pd6['o'][61] = 9.62   # S 建仓成交价（次根 open）
    pd6['o'][91] = 9.41   # B 回补成交价（次根 open）
    sigsd6 = [{'type': 'S', 'idx': 60, 'price': 9.65, 'reason': 't'},
              {'type': 'B', 'idx': 90, 'price': 9.45, 'reason': 't'}]
    od6 = simulate_position_sm([('2026-09-03', sigsd6)], [('2026-09-03', pd6)],
                               config_long=cfg, config_short=cfg,
                               cost=(buy_c, sell_c), has_base=True, exec_delay_bars=1)
    td6 = od6['trips']
    gd6 = round((9.62 - 9.41) / 9.62 * 100, 3)
    nd6 = round(gd6 - total_cost, 3)
    check("D6 反T 1 trip reason=B回补",
          len(td6) == 1 and td6[0]['side'] == 'S' and td6[0]['exit_reason'] == 'B回补',
          f"trips={len(td6)}")
    check("D6 建仓=次根 open 9.62（非信号价 9.65）",
          td6 and abs(td6[0]['entry_price'] - 9.62) < 1e-9,
          f"entry={td6[0]['entry_price'] if td6 else None}")
    check("D6 净=毛-成本", td6 and abs(td6[0]['ret_pct'] - nd6) < 0.01,
          f"net={td6[0]['ret_pct'] if td6 else None} expect={nd6}")

    # ---- D7：delay=0 显式传参与默认行为一致（回归锚） ----
    pd7 = mk_prices()
    pd7['c'][90] = 9.83
    sigsd7 = [{'type': 'B', 'idx': 60, 'price': 9.62, 'reason': 'test'},
              {'type': 'S', 'idx': 90, 'price': 9.83, 'reason': 'test'}]
    od7a = simulate_position_sm([('2026-09-03', sigsd7)], [('2026-09-03', pd7)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True)
    od7b = simulate_position_sm([('2026-09-03', sigsd7)], [('2026-09-03', pd7)],
                                config_long=cfg, config_short=cfg,
                                cost=(buy_c, sell_c), has_base=True, exec_delay_bars=0)
    same = (len(od7a['trips']) == len(od7b['trips']) == 1
            and od7a['trips'][0]['ret_pct'] == od7b['trips'][0]['ret_pct']
            and od7a['trips'][0]['entry_price'] == od7b['trips'][0]['entry_price'])
    check("D7 delay=0 显式=默认（samebar 回归锚）", same,
          f"a={od7a['trips'][0]['ret_pct'] if od7a['trips'] else None} "
          f"b={od7b['trips'][0]['ret_pct'] if od7b['trips'] else None}")

    print(f"\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
