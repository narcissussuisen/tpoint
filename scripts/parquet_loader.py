# -*- coding: utf-8 -*-
"""
tpoint parquet 数据加载适配层

把 F:/WorkBuddyItem/a股分钟线/parquet_qfq_*/{sym}.parquet（前复权分钟线）
读成与 evaluate_signal_validity_v2.load_days 完全一致的返回结构：

    days = { 'YYYY-MM-DD': (o, h, l, c, v) }   # 各为 np.array，按时间升序

用法（在评估/回测脚本中）：
    import parquet_loader
    days = parquet_loader.load_days_parquet('600000.SH.parquet')
    # 或按代码直接定位文件
    days = parquet_loader.load_symbol('600000.SH', data_root=...)

列结构（与 1m_clean csv 对齐 + adj_factor）：
    datetime, trade_date, trade_time, open, high, low, close, volume, amount, adj_factor
"""
import os
import numpy as np

try:
    import pyarrow.parquet as pq
    _HAS_PYARROW = True
except ImportError:
    _HAS_PYARROW = False


def load_days_parquet(path):
    """从 parquet 文件读全历史，返回 days dict（同 load_days 结构）"""
    if not _HAS_PYARROW:
        raise RuntimeError('pyarrow 未安装，无法读取 parquet')
    t = pq.read_table(path)
    df = t.to_pandas()
    df = df.sort_values('datetime').reset_index(drop=True)
    days = {}
    for d, g in df.groupby('trade_date', sort=False):
        g = g.sort_values('trade_time')
        days[d] = (
            g['open'].to_numpy(dtype=np.float64),
            g['high'].to_numpy(dtype=np.float64),
            g['low'].to_numpy(dtype=np.float64),
            g['close'].to_numpy(dtype=np.float64),
            g['volume'].to_numpy(dtype=np.float64),
        )
    return days


DEFAULT_ROOT = r'F:/WorkBuddyItem/a股分钟线/parquet_qfq_2005_2006_2007_2008'


def load_symbol(sym, data_root=None):
    """按代码定位 parquet 并读取。data_root 默认指向 DEFAULT_ROOT（2005-2008 前复权）"""
    if data_root is None:
        data_root = DEFAULT_ROOT
    p = os.path.join(data_root, f'{sym}.parquet')
    if not os.path.exists(p):
        return None
    return load_days_parquet(p)


def scan_symbols(data_root=None):
    """列出 data_root 下所有可用标的（不含 .parquet 后缀）"""
    if data_root is None:
        data_root = DEFAULT_ROOT
    if not os.path.exists(data_root):
        return []
    return sorted(f.replace('.parquet', '') for f in os.listdir(data_root) if f.endswith('.parquet'))
