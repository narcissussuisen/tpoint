#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
floor_signals_today.py — 用 floor(价格地板) MACD 门控计算单只标的今日的 BS 信号触发点位。

隔离 / 只读 / 零生产依赖:
  - 仅 import 本地隔离引擎 backtest/keyfactor/miji_engine (纯 numpy)
  - 仅读取 core/datasource 的 live 行情 (MootdxDataSource.intraday)
  - 不调用生产 monitor/推送, 不写任何生产配置/持仓文件

硬性因果约束 (禁用后视镜/未来函数):
  - 只喂"今日已收盘的分钟棒" (intraday 自动按 trade_date==today 过滤, 不含进行中那根)
  - 单交易日分段: 整段即单日 -> VWAP/EMA/每日上限(max_b=max_s=12) 按本日封顶, 无跨日污染
  - pc(前收) 取自前一交易日 1d 收盘, 非今日数据 -> 无后视
  - floor 模式本身因果: session 新低/新高仅用 bar i 之前窗口, VWAP=cumsum 前缀, MACD=递归EMA

输出: 对话表格(触发时间/价格/方向/共振分/因子) + output/<sym>_floor_<date>.csv
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

from core.datasource import MootdxDataSource  # noqa: E402
import miji_engine as ME  # noqa: E402

SYM = sys.argv[1] if len(sys.argv) > 1 else '161129.SZ'
MODE = 'floor'


def main():
    tf = MootdxDataSource()

    # 1) 今日已收盘 1m 棒 (部分日, 不含进行中那根) —— 因果、无未来棒
    df = tf.klines.intraday(SYM)
    if df is None or len(df) == 0:
        print(f'❌ 未能取到 {SYM} 今日行情 (非交易时段 / 网络失败)。未输出任何信号。')
        sys.exit(1)

    df = df.sort_values('trade_time').reset_index(drop=True)
    n_bars = len(df)
    t_first = str(df['trade_time'].iloc[0])
    t_last = str(df['trade_time'].iloc[-1])

    # 2) 前收 pc: 来自前一交易日 1d 收盘 (非今日数据, 避免后视)
    try:
        day_df = tf.klines.get(SYM, '1d', count=2)
        pc = float(day_df['close'].iloc[-2])
    except Exception as e:
        print(f'❌ 取前收失败: {e}')
        sys.exit(1)

    # 3) 构造引擎输入 (单交易日分段)
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc)

    # 4) floor 检测 (因果)
    sigs = ME.detect_miji_signals(data, pc, macd_gate_mode=MODE, enable=(True, True, True))

    # 5) 映射 + 输出
    date_str = str(df['trade_date'].iloc[0]) if 'trade_date' in df.columns else t_first[:10]
    out_dir = os.path.join(ROOT, 'output')
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f'{SYM}_floor_{date_str}.csv')

    rows = []
    for s in sigs:
        idx = s['idx']
        t = str(df['trade_time'].iloc[idx]) if idx < n_bars else '—'
        direction = '买入' if s['type'] == 'B' else '卖出'
        f = s.get('factors', {})
        rows.append({
            'trade_time': t,
            'price': s['price'],
            'direction': direction,
            'idx': idx,
            'resonance_score': s.get('resonance_score'),
            'gravity': f.get('gravity'),
            'vol_div': f.get('vol_div'),
            'macd_div': f.get('macd_div'),
            'day_chg': s.get('chg'),
            'detail': s.get('detail', ''),
        })

    # 写 CSV
    import csv as _csv
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                            ['trade_time', 'price', 'direction', 'idx', 'resonance_score',
                             'gravity', 'vol_div', 'macd_div', 'day_chg', 'detail'])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 控制台表格
    print('=' * 78)
    print(f'floor 信号触发点位 — {SYM}  {date_str}  (数据截止: {t_last}, 共 {n_bars} 根已收盘1m棒)')
    print(f'前收 pc = {pc:.3f} | 门控模式 = {MODE} (隔离研究引擎, 零生产依赖)')
    print('—' * 78)
    if not rows:
        print('今日截至当前无 floor 信号触发 (floor 较 strict 更严, 需价格新低/新高+超跌/超买)。')
    else:
        print(f'{"触发时间":<22}{"触发价":>9}  {"方向":<4} {"共振":>4}  g/vd/md')
        print('-' * 78)
        for r in rows:
            g = r['gravity']; vd = r['vol_div']; md = r['macd_div']
            print(f"{r['trade_time']:<22}{r['price']:>9.3f}  {r['direction']:<4} "
                  f"{str(r['resonance_score']):>4}  {g}/{vd}/{md}")
        print('-' * 78)
        print(f'共 {len(rows)} 个信号 (买入 {sum(1 for r in rows if r["direction"]=="买入")} / '
              f'卖出 {sum(1 for r in rows if r["direction"]=="卖出")})')
    print('=' * 78)
    print(f'CSV: {csv_path}')
    print('因果性自检: 触发价=信号棒收盘价(仅由0..i数据得出); pc 取前一交易日; 不含未来棒。')


if __name__ == '__main__':
    main()
