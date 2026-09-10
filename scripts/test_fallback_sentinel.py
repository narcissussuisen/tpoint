# -*- coding: utf-8 -*-
"""scripts/test_fallback_sentinel.py — 数据源兜底率哨兵回归（2026-09-10 P0）

事故：2026-09-10 mootdx 接口级失效（K线+报价返回空），monitor 全天 380/380 轮走腾讯分时兜底，
**降级跑了 2.5 小时全程静默**——因为 alert_engine 里根本没有「兜底率」这个指标。
而兜底数据（腾讯分时合成 OHLC）实测 ATR 中位低估 41.8%（448 标的-日），会放大止损被打概率。

修复：monitor 每轮把 `fallback_rounds` 写进 metrics.json → alert_engine 在滚动窗口里
聚合出 `fallback_rate` → config 规则 >0.5 告警（告警群 1d241455）。

⚠️ 关键设计不变量（本测试锁定）：
  1. `fallback_rounds is None` 的样本（保活/盘前/午休）**必须排除在分母外**——
     否则午休积攒的 0 会稀释兜底率，把恢复后的告警拖延一个窗口。
  2. 阈值比较是**严格 >**，恰好 0.5 不告警。
  3. `require_up=true` → 心跳不新鲜时不评估（根因统一由 service_up 表达，避免冗余告警）。

运行：venv/Scripts/python.exe scripts/test_fallback_sentinel.py
"""
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import alert_engine as AE          # noqa: E402
import monitor as M                # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


CFG = json.load(io.open(os.path.join(ROOT, 'config', 'monitor_config.json'), encoding='utf-8'))
AE.is_trading_today = lambda: True          # 消除交易日依赖，保证测试确定性

RULE_NAME = '数据源兜底率过高'


def _sample(now, fb, symbols, age=0.0):
    return {'ts': now - age, 'scan_duration_s': 1.0, 'signals': 0, 'errors': 0,
            'symbols': symbols, 'last_bar_ts': None, 'fallback_rounds': fb,
            'status': 'running'}


def fired(alerts):
    return [a for a in alerts if a.get('name') == RULE_NAME]


def val_of(alert):
    """alert['value'] 是**格式化后的展示串**（可能带单位），取前导数值部分。"""
    import re as _re
    m = _re.match(r'^\s*(-?[\d.]+)', str(alert.get('value', '')))
    return float(m.group(1)) if m else None


def main():
    now = time.time()

    print('\n=== 1. config 规则已按用户口径落地（告警群 1d241455 / 滚动20轮 / >50%）===')
    rules = [r for r in CFG['alerts'] if r.get('metric') == 'fallback_rate']
    check('config 中存在 fallback_rate 规则', len(rules) == 1, f'got={len(rules)}')
    r0 = rules[0] if rules else {}
    check('阈值 threshold=0.5', r0.get('threshold') == 0.5, f'got={r0.get("threshold")}')
    check('op 为 >（严格大于）', r0.get('op') == '>', f'got={r0.get("op")}')
    check('window_s=300（≈20 轮 @15s）', r0.get('window_s') == 300, f'got={r0.get("window_s")}')
    check('severity=warning + require_up=true', r0.get('severity') == 'warning'
          and r0.get('require_up') is True, f'got={r0.get("severity")}/{r0.get("require_up")}')
    check('webhook 指向告警群 1d241455', '1d241455' in (CFG.get('feishu', {}).get('webhook_url') or ''),
          str(CFG.get('feishu', {}).get('webhook_url'))[:60])

    print('\n=== 2. 全兜底 → 触发 ===')
    buf = [_sample(now - i * 15, 1, 1) for i in range(20)]
    a = fired(AE.evaluate(buf[0], buf, now, CFG))
    check('兜底率 1.0 触发告警', len(a) == 1, f'got={len(a)}')
    if a:
        check('告警值 = 1.0', abs((val_of(a[0]) or -1) - 1.0) < 1e-9, f'got={a[0]["value"]}')
        check('severity 透传为 warning', a[0].get('severity') == 'warning', str(a[0].get('severity')))

    print('\n=== 3. 无兜底 → 不触发 ===')
    buf0 = [_sample(now - i * 15, 0, 1) for i in range(20)]
    check('兜底率 0.0 不触发', len(fired(AE.evaluate(buf0[0], buf0, now, CFG))) == 0)

    print('\n=== 4. 混合 25% → 不触发；边界恰好 50% → 不触发（严格 >）===')
    buf25 = [_sample(now - i * 15, (1 if i < 5 else 0), 1) for i in range(20)]
    a25 = fired(AE.evaluate(buf25[0], buf25, now, CFG))
    check('兜底率 0.25 不触发', len(a25) == 0, f'got={len(a25)}')
    buf50 = [_sample(now - i * 15, (1 if i < 10 else 0), 1) for i in range(20)]
    a50 = fired(AE.evaluate(buf50[0], buf50, now, CFG))
    check('兜底率恰好 0.50 不触发', len(a50) == 0, f'got={len(a50)}')
    buf51 = [_sample(now - i * 15, (1 if i < 11 else 0), 1) for i in range(20)]
    check('兜底率 0.55 触发', len(fired(AE.evaluate(buf51[0], buf51, now, CFG))) == 1)

    print('\n=== 5. 关键不变量：非扫描样本(保活/午休)必须排除在分母外 ===')
    # 10 轮真扫描(全兜底) + 10 轮保活(fallback_rounds=None)
    buf_mix = ([_sample(now - i * 15, 1, 1) for i in range(10)]
               + [_sample(now - (10 + i) * 15, None, 1) for i in range(10)])
    a_mix = fired(AE.evaluate(buf_mix[0], buf_mix, now, CFG))
    check('保活样本不稀释兜底率（仍应为 1.0 并触发）', len(a_mix) == 1, f'got={len(a_mix)}')
    if a_mix:
        check('告警值仍为 1.0（未被子样本拉低）',
              abs((val_of(a_mix[0]) or -1) - 1.0) < 1e-9, f'got={a_mix[0]["value"]}')
    # 反证：若误把 None 当 0 计，会得到 10/20=0.5 → 不触发
    check('（反证）若误计 None 为 0 则应为 0.5 不触发 → 说明该分支确实生效',
          len(fired(AE.evaluate(buf_mix[0], buf_mix, now, CFG))) == 1)

    print('\n=== 6. require_up：心跳不新鲜时不评估 ===')
    stale = [_sample(now - i * 15, 1, 1) for i in range(20)]
    a_stale = fired(AE.evaluate(_sample(now, 1, 1, age=999), stale, now, CFG))
    check('心跳停滞(999s) → 不报兜底率（根因由 service_up 表达）', len(a_stale) == 0, f'got={len(a_stale)}')

    print('\n=== 7. 多标的：按 symbols 加权，不是按轮次 ===')
    # 4 标的、每轮仅 1 个走兜底 → 25%
    buf4 = [_sample(now - i * 15, 1, 4) for i in range(20)]
    check('1/4 标的兜底 → 0.25 不触发', len(fired(AE.evaluate(buf4[0], buf4, now, CFG))) == 0)
    buf4b = [_sample(now - i * 15, 3, 4) for i in range(20)]
    check('3/4 标的兜底 → 0.75 触发', len(fired(AE.evaluate(buf4b[0], buf4b, now, CFG))) == 1)

    print('\n=== 8. monitor.write_metrics 正确落盘 fallback_rounds ===')
    td = tempfile.mkdtemp()
    fp = os.path.join(td, 'metrics.json')
    orig = M.METRICS_FILE
    M.METRICS_FILE = fp
    try:
        M.write_metrics(1.0, 0, 0, 0, 2, fallback_rounds=3)
        d1 = json.load(io.open(fp, encoding='utf-8'))
        check('扫描轮：fallback_rounds=3 已落盘', d1.get('fallback_rounds') == 3, str(d1.get('fallback_rounds')))
        check('扫描轮：symbols=2 同时落盘', d1.get('symbols') == 2, str(d1.get('symbols')))
        M.write_metrics(0.0, 0, 0, 0, 2)          # 保活轮：不传 → None
        d2 = json.load(io.open(fp, encoding='utf-8'))
        check('保活轮：fallback_rounds=null（区别于 0）',
              d2.get('fallback_rounds', 'MISSING') is None, str(d2.get('fallback_rounds', 'MISSING')))
    finally:
        M.METRICS_FILE = orig

    print('\n=== 9. datasource 数据源标记存在（哨兵分子来源）===')
    src = io.open(os.path.join(ROOT, 'core', 'datasource.py'), encoding='utf-8').read()
    check("intraday() 写入 df.attrs['data_source']", "df.attrs['data_source'] = src" in src)
    check('四个取值齐备（mootdx/sina/tencent_synth/mootdx_partial）',
          all(v in src for v in ("'mootdx'", "'sina'", "'tencent_synth'", "'mootdx_partial'")))
    check('_fetch_pool 回滚开关 TP_INTRADAY_PREFER 存在', 'TP_INTRADAY_PREFER' in src)

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
