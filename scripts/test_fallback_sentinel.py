# -*- coding: utf-8 -*-
"""scripts/test_fallback_sentinel.py — 数据源「合成口径降级」哨兵回归（2026-09-10 P0 → B）

事故：2026-09-10 mootdx 因**服务器选择缺陷**（旧硬编码列表整批僵化 + `pytdx_hosts[:30]` 截断）
全天返空，monitor 380/380 轮走兜底，**降级 2.5 小时全程静默**。

⚠️ 关键语义修正（用户 2026-09-10 决策）：「兜底」**不等于**「降级」——
  - 落到 `sina`（新浪 1m）：**真实 OHLC**，与 mootdx 同质量 ⇒ **口径无损，不告警**
  - 落到 `tencent_synth`（腾讯分时）：**合成 OHLC**（丢弃真实分钟影线）⇒ ATR 中位低估 **41.8%**
    （448 标的-日实测）、1.5×ATR 反T止损窄 42% ⇒ **这才是真降级，要告警**
故哨兵从 `fallback_rate` 改为 `synth_rate`（阈值 0.5→0.3，更敏感）；`fallback_rate` 规则
保留但 `enabled:false`，指标继续落盘供可观测/复盘。

⚠️ 必须保住的三条不变量：
  1. `*_rounds is None`（保活/盘前/午休）的样本**排除在分母外**——否则午休积攒的 0
     会稀释比率、把恢复后的告警拖延一个窗口。
  2. 阈值比较是**严格 >**。
  3. `require_up=true` → 心跳不新鲜时不评估（根因统一由 service_up 表达）。

运行：venv/Scripts/python.exe scripts/test_fallback_sentinel.py
"""
import io
import json
import os
import re
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

RULE_NAME = '数据源合成口径过高'


def _sample(now, fb, symbols, age=0.0, synth=None):
    """synth 缺省 = fb（即兜底就是合成口径）；显式传 0 表示「兜底但口径无损（新浪）」"""
    return {'ts': now - age, 'scan_duration_s': 1.0, 'signals': 0, 'errors': 0,
            'symbols': symbols, 'last_bar_ts': None, 'fallback_rounds': fb,
            'synth_rounds': (fb if synth is None else synth), 'status': 'running'}


def fired(alerts):
    return [a for a in alerts if a.get('name') == RULE_NAME]


def rate_of(alerts):
    """跑一次 evaluate 并返回该规则的展示值（无告警 → None）"""
    a = fired(alerts)
    if not a:
        return None
    m = re.match(r'^\s*(-?[\d.]+)', str(a[0].get('value', '')))
    return float(m.group(1)) if m else None


def run(buf):
    return AE.evaluate(buf[0], buf, time.time(), CFG)


def main():
    now = time.time()

    print('\n=== 1. config 规则已按决策落地 ===')
    rules = [r for r in CFG['alerts'] if r.get('metric') == 'synth_rate']
    check('config 中存在 synth_rate 规则', len(rules) == 1, f'got={len(rules)}')
    r0 = rules[0] if rules else {}
    check('阈值 threshold=0.3（比兜底率 0.5 更敏感）', r0.get('threshold') == 0.3, f'got={r0.get("threshold")}')
    check('op 为 >（严格大于）', r0.get('op') == '>', f'got={r0.get("op")}')
    check('window_s=300（≈20 轮 @15s）', r0.get('window_s') == 300, f'got={r0.get("window_s")}')
    check('severity=warning + require_up=true + enabled=true',
          r0.get('severity') == 'warning' and r0.get('require_up') is True and r0.get('enabled') is True,
          f'got={r0.get("severity")}/{r0.get("require_up")}/{r0.get("enabled")}')
    old = [r for r in CFG['alerts'] if r.get('metric') == 'fallback_rate']
    check('旧 fallback_rate 规则保留但已停用（可观测不告警）',
          len(old) == 1 and old[0].get('enabled') is False,
          f'got={len(old)}/{old[0].get("enabled") if old else None}')
    check('webhook 指向告警群 1d241455', '1d241455' in (CFG.get('feishu', {}).get('webhook_url') or ''))

    print('\n=== 2. ★ 核心语义：兜底到新浪（真实OHLC）不告警，落到合成口径才告警 ===')
    buf_sina = [_sample(now - i * 15, 1, 1, synth=0) for i in range(20)]
    check('100% 走兜底但全是新浪（synth=0）→ **不告警**', rate_of(run(buf_sina)) is None)
    buf_syn = [_sample(now - i * 15, 1, 1, synth=1) for i in range(20)]
    v = rate_of(run(buf_syn))
    check('100% 落到合成口径（synth=1）→ 告警且值=1.0', v is not None and abs(v - 1.0) < 1e-9, f'got={v}')

    print('\n=== 3. 阈值边界与梯度 ===')
    check('合成率 0.00 不触发', rate_of(run([_sample(now - i * 15, 0, 1, synth=0) for i in range(20)])) is None)
    check('合成率 0.25 不触发',
          rate_of(run([_sample(now - i * 15, 1, 1, synth=(1 if i < 5 else 0)) for i in range(20)])) is None)
    check('合成率恰好 0.30 不触发（严格 >）',
          rate_of(run([_sample(now - i * 15, 1, 1, synth=(1 if i < 6 else 0)) for i in range(20)])) is None)
    check('合成率 0.35 触发',
          rate_of(run([_sample(now - i * 15, 1, 1, synth=(1 if i < 7 else 0)) for i in range(20)])) is not None)

    print('\n=== 4. 不变量①：非扫描样本(保活/午休)必须排除在分母外 ===')
    buf_mix = ([_sample(now - i * 15, 1, 1, synth=1) for i in range(10)]
               + [_sample(now - (10 + i) * 15, None, 1, synth=None) for i in range(10)])
    v_mix = rate_of(run(buf_mix))
    check('保活样本不稀释合成率（值仍为 1.0 并触发）', v_mix is not None and abs(v_mix - 1.0) < 1e-9,
          f'got={v_mix}')

    print('\n=== 5. 不变量②：require_up —— 心跳不新鲜时不评估 ===')
    stale = [_sample(now - i * 15, 1, 1, synth=1) for i in range(20)]
    check('心跳停滞(999s) → 不报合成口径',
          rate_of(AE.evaluate(_sample(now, 1, 1, age=999, synth=1), stale, now, CFG)) is None)

    print('\n=== 6. 按 symbols 加权（不是按轮次）===')
    buf4a = [_sample(now - i * 15, 1, 4, synth=1) for i in range(20)]
    check('4 标的中 1 个落合成 → 0.25 不触发', rate_of(run(buf4a)) is None)
    buf4b = [_sample(now - i * 15, 4, 4, synth=2) for i in range(20)]
    v4 = rate_of(run(buf4b))
    check('4 标的中 2 个落合成 → 0.50 触发', v4 is not None and abs(v4 - 0.5) < 1e-9, f'got={v4}')

    print('\n=== 7. monitor.write_metrics 落盘 fallback_rounds + synth_rounds ===')
    td = tempfile.mkdtemp()
    fp = os.path.join(td, 'metrics.json')
    orig = M.METRICS_FILE
    M.METRICS_FILE = fp
    try:
        M.write_metrics(1.0, 0, 0, 0, 2, fallback_rounds=3, synth_rounds=1)
        d1 = json.load(io.open(fp, encoding='utf-8'))
        check('扫描轮：fallback_rounds=3 已落盘', d1.get('fallback_rounds') == 3, str(d1.get('fallback_rounds')))
        check('扫描轮：synth_rounds=1 已落盘', d1.get('synth_rounds') == 1, str(d1.get('synth_rounds')))
        M.write_metrics(0.0, 0, 0, 0, 2)          # 保活轮：不传 → None
        d2 = json.load(io.open(fp, encoding='utf-8'))
        check('保活轮：两个字段均为 null（区别于 0）',
              d2.get('fallback_rounds', 'MISSING') is None and d2.get('synth_rounds', 'MISSING') is None,
              f'{d2.get("fallback_rounds", "MISSING")}/{d2.get("synth_rounds", "MISSING")}')
    finally:
        M.METRICS_FILE = orig

    print('\n=== 8. datasource 数据源标记存在（哨兵分子来源）===')
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
