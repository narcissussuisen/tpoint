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
import time
import urllib.request
import re
import pandas as pd
import numpy as np
from mootdx.quotes import Quotes

# 实测可用的通达信服务器（2026-07-20 验证：旧列表全失效，180.153.18.170 可用）
# 2026-07-20: 原 10 个服务器 TCP 连通但 bars 返回空数据，已替换为实测可用的
_TDX_SERVERS = [
    ('180.153.18.170', 7709),   # 上海电信主站Z1（2026-07-20 实测可用，含基金/LOF）
    ('180.153.39.51', 7709),    # 上海电信主站Z2
    ('202.108.25.241', 7709),   # 北京联通主站
    ('218.75.126.9', 7709),     # 杭州电信
    ('115.238.56.198', 7709),   # 杭州电信2
    ('115.238.90.165', 7709),   # 杭州联通
    ('60.12.136.250', 7709),    # 杭州移动
    ('218.108.98.154', 7709),   # 杭州网通
    ('180.153.39.50', 7709),    # 上海电信
    ('61.135.142.73', 7709),    # 北京联通2
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


def _retry_with_backoff(fn, max_retries=3, base=1.0, cap=7.0, label='', on_retry=None):
    """对取数调用做指数退避重试（2026-07-21 复盘改进）。
    应对开盘/盘中瞬时 socket 抖动、LOF 分钟K 开盘常空等边界：
    失败即按 base/2base/4base...（封顶 cap，< SCAN_INTERVAL 15s）退避重试，
    on_retry 在每次重试前回调（用于重连）。全失败抛最后一次异常。
    注：仅用于"失败需重试"的边界；兜底都失败时必须由上层(静默告警)感知，禁止无限重试。"""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                if on_retry:
                    try:
                        on_retry()
                    except Exception:
                        pass
                wait = min(base * (2 ** attempt), cap)
                print(f"  ⚠️ {label} 取数失败(retry {attempt+1}/{max_retries}, {wait:.1f}s后重试): {e}")
                time.sleep(wait)
            else:
                print(f"  ⚠️ {label} 取数失败(已重试{max_retries}次): {e}")
    raise last_exc


def tdx_client(market='std'):
    """创建 mootdx 客户端，规避 0.11.x BESTIP 空串 bug。
    四级兜底：① 显式 _TDX_SERVERS 探测+数据校验 → ② pytdx hosts 列表兜底(104个) →
    ③ bestip 测速+校验 → ④ 裸 factory。
    仅当服务器能真返回数据时才采用，否则自动回退，保证跨网络环境可用。
    2026-07-20: 原 _TDX_SERVERS 全失效，新增 pytdx hosts 兜底确保至少一个可用。"""
    # ① 显式服务器列表
    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            try:
                cli = Quotes.factory(market=market, server=(ip, port))
            except Exception:
                continue
            if _server_ok(cli):
                return cli
    # ② pytdx hosts 兜底（104 个服务器，限制前 30 个避免太久）
    try:
        from pytdx.config.hosts import hq_hosts as _pytdx_hosts
        for host in _pytdx_hosts[:30]:
            ip = host[1] if len(host) >= 2 else None
            if not ip or (ip, 7709) in _TDX_SERVERS:
                continue
            if _probe(ip, 7709):
                try:
                    cli = Quotes.factory(market=market, server=(ip, 7709))
                except Exception:
                    continue
                if _server_ok(cli):
                    return cli
    except Exception:
        pass
    # ③ bestip 测速
    try:
        cli = Quotes.factory(market=market, bestip=True)
        if _server_ok(cli):
            return cli
    except Exception:
        pass
    # ④ 裸 factory（不校验，最后手段）
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
        def _fetch():
            return self.client.bars(symbol=code, frequency=freq, offset=count)
        try:
            df = _retry_with_backoff(_fetch, max_retries=3, base=1.0, cap=7.0,
                                     label=f'mootdx日K {sym}', on_retry=self.reconnect)
        except Exception as e:
            print(f"  ⚠️ mootdx日K获取失败 {sym}: {e}, 重连后最后一试")
            self.reconnect()
            try:
                df = self.client.bars(symbol=code, frequency=freq, offset=count)
            except Exception as e2:
                print(f"  ⚠️ mootdx日K重连后仍失败 {sym}: {e2}")
                df = None
        if df is None or len(df) == 0:
            print(f"  ⚠️ mootdx日K返回空 {sym}（服务器连通但无数据）。"
                  f"如需真实行情备份，请走 tdx-connector / westock-mcp 连接器。")
            return None
        # 字段对齐 tickflow
        df = df.copy()
        if 'datetime' in df.columns:
            # 2026-07-21 fix: 同 intraday，异常 datetime coerce 为 NaT 并丢弃
            dt = pd.to_datetime(df['datetime'], errors='coerce')
            bad = dt.isna()
            if bad.any():
                df = df[~bad].copy()
                dt = dt[~bad]
                print(f"  ⚠️ 丢弃 {int(bad.sum())} 行异常 datetime（如 '2004-00-00'）")
            df['trade_date'] = dt.dt.strftime('%Y-%m-%d')
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
        2026-07-21 增强：mootdx 对 LOF/T+0 基金分钟K稀疏或为空时，
        自动降级到腾讯分时接口兜底（确保基金 BS 信号能正常生成）。"""
        code, _ = _to_mootdx_sym(sym)
        # 2026-07-21 复盘改进：mootdx 主源加指数退避重试（应对开盘/盘中瞬时 socket 抖动），
        # 失败即重连再试；腾讯分时兜底开盘即生效，mootdx 行数<5 也触发兜底并优先真实 OHLC。
        def _fetch_mootdx():
            return self.client.bars(symbol=code, frequency=8, offset=320)
        try:
            df = _retry_with_backoff(_fetch_mootdx, max_retries=3, base=1.0, cap=7.0,
                                     label=f'mootdx分钟K {sym}', on_retry=self.reconnect)
        except Exception as e:
            print(f"  ⚠️ mootdx分钟K获取失败 {sym}: {e}")
            df = None
        # 选更优源：真实 OHLC(mootdx) 优先且需>=5 行（compute 要求）；
        # mootdx<5 行时降级腾讯分时；两源均<5 行则 mootdx 3-4 行凑合（compute 会拒收<5）。
        mootdx_ok = df is not None and len(df) >= 5
        fb = self._tencent_intraday_fallback(sym)
        tencent_ok = fb is not None and len(fb) >= 5
        if mootdx_ok:
            chosen = df  # 真实 OHLC 优先
        elif tencent_ok:
            chosen = fb
            print(f"  ✅ 腾讯分时兜底成功 {sym}: {len(fb)} 根分钟线")
        elif df is not None and len(df) >= 3:
            chosen = df  # 两源均不足5行，mootdx 3-4 行凑合（compute 将因<5行拒收）
            print(f"  ⚠️ mootdx 仅 {len(df)} 行且腾讯兜底失败，compute 将因<5行拒收")
        elif fb is not None and len(fb) >= 3:
            chosen = fb
            print(f"  ⚠️ 腾讯仅 {len(fb)} 行且 mootdx 失败，compute 将因<5行拒收")
        else:
            print(f"  ⚠️ 所有数据源均无分钟K数据 {sym}（mootdx+腾讯均失败/不足3行）")
            return None
        df = chosen
        df = df.copy()
        if 'datetime' in df.columns:
            dt = pd.to_datetime(df['datetime'], errors='coerce')
            bad = dt.isna()
            if bad.any():
                df = df[~bad].copy()
                dt = dt[~bad]
                print(f"  ⚠️ 丢弃 {int(bad.sum())} 行异常 datetime（如 '2004-00-00'）")
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

    def _tencent_intraday_fallback(self, sym):
        """腾讯分时接口兜底：当 mootdx 无分钟K时（LOF/T+0 基金常见），
        从腾讯分时 API 拉取当日分钟线，组装成与 intraday() 同格式的 DataFrame。
        数据格式：每行 "HHMM price volume amount"，从 09:30 到当前时间。
        返回 DataFrame[trade_time, trade_date, open, close, high, low, volume] 或 None。"""
        try:
            tcode = _tencent_code(sym)  # 'sz161129'
            # 腾讯全量分时接口（返回当天所有分钟线）
            url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tcode}'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.qq.com/',
            })
            raw = urllib.request.urlopen(req, timeout=10).read().decode('gbk', errors='ignore')
            # 解析 JSON: {"data": {"sz161129": {"data": {"data": ["0930 1.995 ...", ...], "date":"20260721"}}}}
            import json as _json
            j = _json.loads(raw)
            # 导航到 data 数组
            d = j.get('data', {})
            sym_data = d.get(tcode, {}) or d.get(list(d.keys())[0] if d else '', {})
            inner = sym_data.get('data', {})
            lines = inner.get('data', [])
            date_str = inner.get('date', '')
            if not lines or not date_str:
                return None
            rows = []
            prev_price = None
            for line in lines:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                hhmm = parts[0]
                price = float(parts[1])
                volume = float(parts[2]) if len(parts) >= 3 and parts[2] != '0' else 0.0
                if len(hhmm) == 4:
                    tt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {hhmm[:2]}:{hhmm[2:]}:00"
                else:
                    continue
                if prev_price is None:
                    o = h = l = price
                else:
                    o = prev_price  # 开盘价用前一根收盘近似（分时无真实 OHLC）
                    h = max(prev_price, price)
                    l = min(prev_price, price)
                rows.append({
                    'trade_time': pd.Timestamp(tt),
                    'trade_date': f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                    'open': o,
                    'close': price,
                    'high': h,
                    'low': l,
                    'volume': volume,
                })
                prev_price = price
            if len(rows) < 3:
                return None
            df = pd.DataFrame(rows)
            # 过滤 volume=0 且 price 不变的"死"行（集合竞价阶段），保留至少首尾
            # 但不过滤太多——有些基金确实低活跃，有少量交易就是有效数据
            return df.sort_values('trade_time').reset_index(drop=True)
        except Exception as e:
            print(f"  ⚠️ 腾讯分时兜底失败 {sym}: {e}")
            return None

    def historical_1m(self, sym, day, offset=2000):
        """历史某日 1m K线（mootdx 主源）。
        拉取 offset 根 1m 再按 trade_date==day 过滤，供历史日模拟（如 161129 的 07-17）使用。
        注意：2026-07-20 验证 LOF 基金(如161129)在可用服务器(180.153.18.170)下能正常返回分钟K；
        之前"常返回空"是因旧 _TDX_SERVERS 失效，非基金本身问题。服务器失效时仍应走
        tdx-connector / westock-mcp 连接器兜底。"""
        code, _ = _to_mootdx_sym(sym)
        def _fetch_hist():
            return self.client.bars(symbol=code, frequency=8, offset=offset)
        try:
            df = _retry_with_backoff(_fetch_hist, max_retries=3, base=1.0, cap=7.0,
                                     label=f'mootdx历史1m {sym}', on_retry=self.reconnect)
        except Exception as e:
            print(f"  ⚠️ mootdx历史1m获取失败 {sym}: {e}, 重连后最后一试")
            self.reconnect()
            try:
                df = self.client.bars(symbol=code, frequency=8, offset=offset)
            except Exception as e2:
                raise RuntimeError(
                    f"mootdx 未返回 {sym} 的1m数据（重连后仍失败）。"
                    f"若持续失败，请改用 tdx-connector 或 westock-mcp 连接器拉取历史1m再写入 CSV。"
                ) from e2
        if df is None or len(df) == 0:
            raise RuntimeError(
                f"mootdx 未返回 {sym} 的1m数据（服务器可能失效，已新增 pytdx hosts 兜底）。"
                f"若持续失败，请改用 tdx-connector 或 westock-mcp 连接器拉取历史1m再写入 CSV。"
            )
        df = df.copy()
        if 'datetime' in df.columns:
            dt = pd.to_datetime(df['datetime'], errors='coerce')
            bad = dt.isna()
            if bad.any():
                df = df[~bad].copy()
                dt = dt[~bad]
                print(f"  ⚠️ 丢弃 {int(bad.sum())} 行异常 datetime（如 '2004-00-00'）")
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
