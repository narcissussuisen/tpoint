"""core/shadow_v3.py — v3 信号影子旁路（只读 + 落日志，绝不触碰下单链路）

设计原则（最小化侵入、零副作用，2026-08-20 接入）：
1. 完全独立于生产 detector（monitor.detect_for / miji_alpha）。
2. 自行从原始 df 计算 indicators.compute_indicators + detect_signals_v3，
   不依赖 miji 的 data 字典结构（回避"双栈"漂移），且输入与生产同源（同一 df + 同一 PC）。
3. 所有异常内部吞掉，绝不向上传播（保证生产路径不受影响）。
4. 受 SHADOW_V3_ENABLED 总开关 + 环境变量 TPOINT_SHADOW_V3 双重控制（置 off/0 立即停用）。
5. 日志写入 <dir>/shadow_v3_<YYYY-MM-DD>.jsonl（逐轮追加，按 (sym,bar_ts,type) 进程内去重），
   供收盘后 v3 vs 生产 信号对比 / 胜率 / 盈亏比 证据积累。
6. 与生产 detect_for(trim_frontier=True) 对齐：剔除最后一根"进行中"bar，只对已收盘 bar 出信号。

本模块不修改 STATE / signal.txt / audit / 任何推送；monitor 未调用本模块时（总开关关），
import 即无副作用。
"""
import os
import json
import traceback
from datetime import datetime

# 总开关：置 False 即停用；环境变量 TPOINT_SHADOW_V3=0/false/off 亦可即时停用。
SHADOW_V3_ENABLED = True

# 日志目录（默认 tpoint 根目录下的 data/；可用环境变量重定向，便于冒烟测试）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log_dir():
    d = os.environ.get('TPOINT_SHADOW_V3_DIR')
    return d if d else os.path.join(_ROOT, 'data')


# 进程内去重：monitor 长驻，跨轮去重（同一 bar 的 v3 信号每天只记一次）
_seen = set()


def shadow_v3_log(sym, name, df, pc, now=None):
    """并行计算 v3 信号并落日志。返回 None。任何异常静默吞掉。

    入参：
      sym : 标的代码（如 '688111.SH'）
      name: 中文名
      df  : pandas DataFrame（同 monitor.data['df']，含 trade_time/open/high/low/close/volume）
      pc  : 昨收（与生产 STATE[sym]['PC'] 同源）
    """
    if not SHADOW_V3_ENABLED:
        return
    env = os.environ.get('TPOINT_SHADOW_V3', '1').strip().lower()
    if env in ('0', 'false', 'off', 'no', ''):
        return
    try:
        import numpy as np
        from indicators import compute_indicators, detect_signals_v3

        if df is None or len(df) == 0:
            return
        # 与生产 trim_frontier=True 对齐：剔除最后一根进行中 bar
        df_eval = df.iloc[:-1] if len(df) > 2 else df
        c = df_eval['close'].values.astype(float)
        h = df_eval['high'].values.astype(float)
        lo = df_eval['low'].values.astype(float)
        o = df_eval['open'].values.astype(float) if 'open' in df_eval.columns else c.copy()
        has_vol = 'volume' in df_eval.columns
        v = df_eval['volume'].values.astype(float) if has_vol else None
        if pc is None or pc <= 0 or len(c) < 3:
            return

        data = compute_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
        v3 = detect_signals_v3(data, pc)

        tts = df_eval['trade_time'].values if 'trade_time' in df_eval.columns else None
        _now = now or datetime.now()
        day = _now.strftime('%Y-%m-%d')
        recs = []
        for s in v3:
            bar_ts = str(tts[s['idx']])[:19] if (tts is not None and s['idx'] < len(tts)) else ''
            key = (sym, bar_ts, s['type'], s.get('reason'))
            if key in _seen:
                continue
            _seen.add(key)
            recs.append({
                'type': s['type'], 'idx': int(s['idx']), 'bar_ts': bar_ts,
                'price': float(s['price']), 'reason': s.get('reason'),
                'rsi': s.get('rsi'), 'kdj_k': s.get('kdj_k'),
                'kdj_j': s.get('kdj_j'), 'vol_price_div': s.get('vol_price_div'),
                'macd_div': s.get('macd_div'),
            })
        if not recs:
            return

        log_path = os.path.join(_log_dir(), f'shadow_v3_{day}.jsonl')
        os.makedirs(_log_dir(), exist_ok=True)
        line = {
            't': _now.strftime('%Y-%m-%d %H:%M:%S'),
            'sym': sym, 'name': name,
            'n_bars': int(data['n']), 'n_v3_new': len(recs),
            'v3': recs,
        }
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
    except Exception as e:
        # 影子模式故障绝不影响生产；仅本地打印以便排障
        try:
            print(f"  [shadow_v3] 静默跳过 {sym}: {e}")
        except Exception:
            pass
