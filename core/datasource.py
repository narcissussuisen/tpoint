"""
datasource.py — mootdx 数据源，替代 tickflow
接口对齐 tickflow 的 TickFlow，让 monitor/backtest 改动最小。
数据源：mootdx（通达信 TCP 7709，免费无 Key，秒级实时）
关键差异 vs tickflow：
  1. symbol 格式：mootdx 用 6 位纯数字（'300975'），tickflow 用 '300975.SZ'
  2. 字段名：mootdx datetime→trade_date/trade_time，vol→volume
  3. mootdx 返回不复权价（日内监控无影响，回测跨除权需处理）
  4. mootdx 有 volume 字段（tickflow intraday 不确定），利好 v9 的 VWAP
"""
import socket
import urllib.request
import re
import pandas as pd
import numpy as np
from mootdx.quotes import Quotes

# 实测可用的通达信服务器（2026-06 验证，按延迟排序）
_TDX_SERVERS = [
    ('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709),
    ('123.60.73.44', 7709), ('116.205.163.254', 7709), ('121.36.225.169', 7709),
    ('123.60.70.228', 7709), ('124.71.9.153', 7709), ('110.41.147.114', 7709),
    ('124.71.187.122', 7709),
]


def _probe(ip, port, timeout=2.0):
    """TCP 握手探测服务器可达性"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _server_ok(client, market=0):
    """数据校验：服务器不仅要 TCP 连通，还要真能返回 K 线。
    规避'连得通但返回空数据'的僵服务器（某些网络环境下硬编码列表会如此）。"""
    try:
        df = client.bars(symbol='600519', frequency=9, offset=1, market=market)
        return df is not None and len(df) > 0
    except Exception:
        return False


def tdx_client(market='std'):
    """创建 mootdx 客户端，规避 0.11.x BESTIP 空串 bug。
    五级兜底：显式服务器探测+数据校验 → bestip测速+校验 → 裸factory → 报错。
    仅当服务器能真返回数据时才采用，否则自动回退，保证跨网络环境可用。"""
    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            try:
                cli = Quotes.factory(market=market, server=(ip, port))
            except Exception:
                continue
            if _server_ok(cli):
                return cli
    try:
        cli = Quotes.factory(market=market, bestip=True)
        if _server_ok(cli):
            return cli
    except Exception:
        pass
    try:
        cli = Quotes.factory(market=market)
        return cli
    except Exception as e:
        raise RuntimeError(
            "所有 mootdx 服务器不可达。海外网络通常超时(TCP 7709)，"
            "请走国内代理或更新 _TDX_SERVERS。错误：%s" % e
        )


def _to_mootdx_sym(sym):
    """tickflow 符号 → mootdx 符号 + 市场代码
    '300975.SZ' → ('300975', 0)  # 0=深圳
    '601869.SH' → ('601869', 1)  # 1=上海"""
    code = sym.split('.')[0]
    market = 0 if sym.endswith('.SZ') else 1
    return code, market


def _tencent_code(sym):
    """tickflow 符号 → 腾讯行情代码前缀：深交所 sz / 上交所 sh"""
    code = sym.split('.')[0]
    return ('sz' if sym.endswith('.SZ') else 'sh') + code


class MootdxDataSource:
    """替代 tickflow.TickFlow，接口对齐。
    用法：tf = MootdxDataSource(); tf.klines.get(sym, period='1d', count=60)"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        """懒加载 + 断线重连"""
        if self._client is None:
            self._client = tdx_client()
        return self._client

    def reconnect(self):
        """强制重连（跨天/异常时调用）"""
        self._client = tdx_client()

    @property
    def klines(self):
        return self  # 链式：tf.klines.get / tf.klines.intraday

    def get(self, sym, period='1d', count=60, as_dataframe=True):
        """日K线，对齐 tickflow tf.klines.get
        返回 DataFrame：trade_date, open, close, high, low, volume"""
        code, _ = _to_mootdx_sym(sym)
        freq = 9 if period == '1d' else (5 if period == '1w' else 9)
        try:
            df = self.client.bars(symbol=code, frequency=freq, offset=count)
        except Exception as e:
            print(f"  ⚠️ mootdx日K获取失败 {sym}: {e}, 重连重试")
            self.reconnect()
            df = self.client.bars(symbol=code, frequency=freq, offset=count)
        if df is None or len(df) == 0:
            print(f"  ⚠️ mootdx日K返回空 {sym}（服务器连通但无数据）。"
                  f"如需真实行情备份，请走 tdx-connector / westock-mcp 连接器。")
            return None
        # 字段对齐 tickflow
        df = df.copy()
        if 'datetime' in df.columns:
            df['trade_date'] = pd.to_datetime(df['datetime']).dt.strftime('%Y-%m-%d')
        if 'vol' in df.columns:
            df['volume'] = df['vol']
            df = df.drop(columns=['vol'])
        if 'volume' not in df.columns:
            df['volume'] = 0.0
        df['volume'] = df['volume'].clip(lower=0)  # 过滤异常负值/浮点噪声
        df = df.sort_values('trade_date').reset_index(drop=True)
        return df

    def intraday(self, sym, as_dataframe=True):
        """日内 1min K线，对齐 tickflow tf.klines.intraday
        返回 DataFrame：trade_time, trade_date, open, close, high, low, volume
        注意：mootdx 返回当日 + 部分昨日分钟（offset=320 多取些保证覆盖当日）"""
        code, _ = _to_mootdx_sym(sym)
        try:
            df = self.client.bars(symbol=code, frequency=8, offset=320)
        except Exception as e:
            print(f"  ⚠️ mootdx分钟K获取失败 {sym}: {e}, 重连重试")
            self.reconnect()
            df = self.client.bars(symbol=code, frequency=8, offset=320)
        if df is None or len(df) == 0:
            print(f"  ⚠️ mootdx分钟K返回空 {sym}（服务器连通但无数据）。"
                  f"如需真实行情备份，请走 tdx-connector / westock-mcp 连接器。")
            return None
        df = df.copy()
        if 'datetime' in df.columns:
            dt = pd.to_datetime(df['datetime'])
            df['trade_time'] = dt
            df['trade_date'] = dt.dt.strftime('%Y-%m-%d')
        if 'vol' in df.columns:
            df['volume'] = df['vol']
            df = df.drop(columns=['vol'])
        if 'volume' not in df.columns:
            df['volume'] = 0.0
        df['volume'] = df['volume'].clip(lower=0)  # 过滤异常负值/浮点噪声
        # 只保留当日数据（mootdx 可能返回跨日）
        today = pd.Timestamp.now().strftime('%Y-%m-%d')
        if 'trade_date' in df.columns:
            df = df[df['trade_date'] == today].reset_index(drop=True)
        return df

    def quotes(self, sym):
        """实时报价（46字段含五档盘口），tickflow 无此能力
        sym 支持 str 或 list"""
        if isinstance(sym, str):
            code, _ = _to_mootdx_sym(sym)
            return self.client.quotes(symbol=[code])
        codes = [_to_mootdx_sym(s)[0] for s in sym]
        return self.client.quotes(symbol=codes)

    def finance(self, sym):
        """财务快照（37字段），market: 0深圳 1上海"""
        code, market = _to_mootdx_sym(sym)
        return self.client.finance(symbol=code, market=market)

    def tencent_realtime(self, sym):
        """腾讯财经 HTTP 实时快照（HTTPS，无需 TCP 7709，不封 IP）。
        作为 mootdx 挂掉时的实时价备份源（a-stock-data 行情层之一）。
        返回 dict(name/open/prev_close/price/volume) 或 None。"""
        try:
            url = 'https://qt.gtimg.cn/q=' + _tencent_code(sym)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            raw = urllib.request.urlopen(req, timeout=8).read().decode('gbk', errors='ignore')
            m = re.search(r'"([^"]+)"', raw)
            if not m:
                return None
            f = m.group(1).split('~')
            if len(f) < 6:
                return None
            return {
                'name': f[1],
                'code': f[2],
                'price': float(f[3]),
                'prev_close': float(f[4]),
                'open': float(f[5]),
                'volume': int(f[6]) if f[6].isdigit() else 0,
            }
        except Exception as e:
            print(f"  ⚠️ 腾讯实时快照失败 {sym}: {e}")
            return None

    def historical_1m(self, sym, day, offset=2000):
        """历史某日 1m K线（mootdx 主源）。
        拉取 offset 根 1m 再按 trade_date==day 过滤，供历史日模拟（如 161129 的 07-17）使用。
        注意：mootdx 对 LOF 基金(如161129)常返回空 → 此时应走 tdx-connector / westock-mcp
        连接器兜底拉取，再写成 1m CSV（datasource 无法在模块内调用 MCP 工具）。"""
        code, _ = _to_mootdx_sym(sym)
        try:
            df = self.client.bars(symbol=code, frequency=8, offset=offset)
        except Exception as e:
            print(f"  ⚠️ mootdx历史1m获取失败 {sym}: {e}, 重连重试")
            self.reconnect()
            df = self.client.bars(symbol=code, frequency=8, offset=offset)
        if df is None or len(df) == 0:
            raise RuntimeError(
                f"mootdx 未返回 {sym} 的1m数据（LOF/基金常如此）。"
                f"请改用 tdx-connector 或 westock-mcp 连接器拉取历史1m再写入 CSV。"
            )
        df = df.copy()
        if 'datetime' in df.columns:
            dt = pd.to_datetime(df['datetime'])
            df['trade_time'] = dt
            df['trade_date'] = dt.dt.strftime('%Y-%m-%d')
        if 'vol' in df.columns:
            df['volume'] = df['vol']
            df = df.drop(columns=['vol'])
        if 'volume' not in df.columns:
            df['volume'] = 0.0
        df['volume'] = df['volume'].clip(lower=0)
        day_df = df[df['trade_date'] == day].reset_index(drop=True)
        if len(day_df) == 0:
            raise RuntimeError(f"mootdx 返回的1m数据不含 {day}（可能该日无交易或数据缺失）。")
        return day_df


# 兼容别名：让 monitor 的 `from tickflow import TickFlow` 改为
# `from datasource import MootdxDataSource as TickFlow` 即可
TickFlow = MootdxDataSource
