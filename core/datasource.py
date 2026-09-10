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
import os
import time
import urllib.request
import re
import json
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from mootdx.quotes import Quotes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ========== 通达信服务器选择（2026-09-10 重构） ==========
# 历史事故（本次根因）：旧硬编码 10 台（180.153.x 等，注释写「2026-07-20 实测可用」）在 2026-09-10
# 全部退化为「TCP 通 + get_security_count 正常 + get_security_bars 返空」，而 tdx_client() 第②级
# 只扫 `pytdx hosts[:30]`——**实测可用的 9 台服务器在 pytdx 103 条列表里的索引是 [49, 70~77]**，
# 全部被 [:30] 截断跳过 ⇒ 四级兜底跑完落到「不校验的裸 factory」⇒ 拿到一个 bars 不通的客户端
# ⇒ monitor 全天返空、降级到 HTTP 兜底（日志「mootdx日K返回空（服务器连通但无数据）」）。
# ⚠️ 禁令：不要再恢复 [:30] 截断；不要让未校验的裸 factory 兜底（宁可不给客户端，让上层走 HTTP 兜底）。
# ⚠️ 服务器池会腐化（07-20 / 08-20 / 09-10 已三次），故种子之外还必须有「动态发现 + 缓存」。

#: 种子服务器：2026-09-10 全网实测（103 台候选 → TCP 可达 31 台 → **bars 真正可用 9 台**）。
#: 仅作**快速路径**（逐个 _probe + _server_ok，命中即返回，通常 <3s）；全失败才走动态发现。
_TDX_SEED_SERVERS = [
    ('59.36.5.11', 7709),
    ('117.34.114.13', 7709), ('117.34.114.14', 7709), ('117.34.114.15', 7709),
    ('117.34.114.16', 7709), ('117.34.114.17', 7709), ('117.34.114.18', 7709),
    ('117.34.114.20', 7709), ('117.34.114.27', 7709),
]
#: 兼容旧名（历史脚本/注释可能引用；语义已变为「种子」）
_TDX_SERVERS = _TDX_SEED_SERVERS

#: 动态发现缓存（避免每次启动都全表扫描）
TDX_CACHE_FILE = os.path.join(BASE_DIR, 'data', 'tdx_servers.json')
TDX_CACHE_TTL_S = 6 * 3600          # 6 小时；过期即重扫
TDX_PROBE_TIMEOUT_S = 2.0
TDX_DISCOVER_WORKERS = 8            # 并发探测上限（过高可能触发服务端风控）

#: 数据源策略（固化产物）：由 scripts/datasource_benchmark.py 连续 3~5 交易日达标后写入。
#: 缺失/损坏/字段非法 → 回退硬编码安全默认（见 _intraday_order）。
DATASOURCE_POLICY_FILE = os.path.join(BASE_DIR, 'config', 'datasource_policy.json')
_DEFAULT_FALLBACK_ORDER = ['sina', 'tencent']       # 真实 OHLC 优先，合成口径末位
_VALID_FALLBACK_SOURCES = ('sina', 'tencent')


def _read_datasource_policy():
    """读 data source 策略文件 → dict（缺失/损坏/字段非法 → {}）"""
    try:
        with open(DATASOURCE_POLICY_FILE, encoding='utf-8') as f:
            p = json.load(f)
        if not isinstance(p, dict):
            return {}
        order = p.get('fallback_order')
        if not isinstance(order, list) or not order:
            return {}
        clean = [s for s in order if s in _VALID_FALLBACK_SOURCES]
        if not clean:
            return {}
        p['fallback_order'] = clean
        return p
    except Exception:
        return {}


def _intraday_order():
    """HTTP 兜底源的尝试顺序。优先级：env `TP_INTRADAY_PREFER` > 策略文件 > 硬编码默认。

    历史：2026-09-10 之前顺序是「腾讯分时(合成 OHLC) → 新浪 1m(真实 OHLC)」，
    与本文件注释自述的口径相反（新浪质量优）。现默认**真实 OHLC 优先**。
    `TP_INTRADAY_PREFER=tencent` 可一键回滚到旧顺序（纯 env，无需改码）。
    """
    env = (os.environ.get('TP_INTRADAY_PREFER') or '').strip().lower()
    if env == 'tencent':
        return ['tencent', 'sina']
    if env == 'sina':
        return ['sina', 'tencent']
    pol = _read_datasource_policy()
    if pol.get('fallback_order'):
        return list(pol['fallback_order'])
    return list(_DEFAULT_FALLBACK_ORDER)



# [轮次2-1 迭代] 腾讯行情服务域名池（服务器级 failover）。
# 07-31 盘中 getaddrinfo 间歇失败致腾讯分时兜底单域名单点故障（4 标的长时间失联）。
# 轮询策略：按序尝试，某域名网络异常 → 立即切下一个；全部失败才算兜底失败。
# 域名池含主备镜像（ifzq.gtimg.cn 主域 + 无前缀备用 + 镜像域），
# 任一可用即恢复分时数据，避免"一个域名 DNS 抖→全链路哑火"。
_TENCENT_HOSTS = [
    'web.ifzq.gtimg.cn',     # 主域（腾讯财经分时接口，实测可用）
    # 2026-08-07 清理两个死域名：
    #   - 'ifzq.gtimg.cn'        DNS 解析失败（getaddrinfo failed），每轮白重试
    #   - 'web.sqt.gtimg.cn'     返回 HTTP 400 "bad request"（非 JSON），解析报 Expecting value
    # 仅保留实测可用的 web.ifzq.gtimg.cn；跨厂商冗余由下方新浪 1m 兜底承担。
]

# 腾讯分时接口路径（域名池共用）
_TENCENT_PATH = '/appstock/app/minute/query?code='

# [轮次2-1 迭代] 独立厂商第三级兜底：新浪财经 1m K线（真实 OHLC，质量优于腾讯分时合成）。
# 与腾讯分时互为"跨厂商冗余"——腾讯全家族 DNS 故障时仍有新浪可兜底。
# 返回 250 根真实 OHLC 1m K 线（约 2 个交易日），调用方 intraday() 会过滤当日行。
_SINA_MINUTE_URL = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData?symbol={tcode}&scale=1&ma=no&datalen=250'


def _probe(ip, port, timeout=TDX_PROBE_TIMEOUT_S):
    """TCP 握手探测服务器可达性（**只证明端口通，不证明 bars 可用**）"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _server_ok(client, market=0):
    """数据校验：服务器不仅要 TCP 连通，还要**真能返回 K 线**。
    规避'连得通但返回空数据'的僵服务器（2026-09-10 事故：硬编码列表整批僵化）。

    2026-09-10 增强：**同时校验 1 分钟线**（frequency=8）——信号链路实际消费的是 1 分钟 bar，
    与日线是不同 opcode，只验日线不足以证明可用。
    """
    try:
        d = client.bars(symbol='600519', frequency=9, offset=1, market=market)
        if d is None or len(d) == 0:
            return False
    except Exception:
        return False
    try:
        m = client.bars(symbol='600519', frequency=8, offset=5, market=market)
        return m is not None and len(m) > 0
    except Exception:
        return False


def _read_tdx_cache():
    """读动态发现缓存 → [(ip,port), ...]；过期/损坏/为空 → []"""
    try:
        with open(TDX_CACHE_FILE, encoding='utf-8') as f:
            c = json.load(f)
        if not isinstance(c, dict):
            return []
        if time.time() - float(c.get('validated_at', 0)) > TDX_CACHE_TTL_S:
            return []
        out = []
        for s in (c.get('servers') or []):
            ip = s.get('ip')
            if ip:
                out.append((ip, int(s.get('port', 7709))))
        return out
    except Exception:
        return []


def _write_tdx_cache(servers):
    """落盘动态发现结果（原子写；失败静默——缓存只影响启动速度，不是正确性依赖）"""
    try:
        os.makedirs(os.path.dirname(TDX_CACHE_FILE), exist_ok=True)
        tmp = TDX_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'validated_at': time.time(), 'ttl_s': TDX_CACHE_TTL_S,
                       'servers': [{'ip': ip, 'port': port} for ip, port in servers]}, f)
        os.replace(tmp, TDX_CACHE_FILE)
    except Exception:
        pass


def _pytdx_host_candidates():
    """pytdx 内置 hosts 全表（⚠️ 严禁截断——2026-09-10 事故正是 `[:30]` 把 9 台可用服务器全跳过）"""
    out = []
    try:
        from pytdx.config.hosts import hq_hosts as _hosts
        for h in _hosts:
            ip = h[1] if len(h) >= 2 else None
            if ip and (ip, 7709) not in out:
                out.append((ip, 7709))
    except Exception:
        pass
    return out


def _discover_tdx_servers(market='std', verbose=True):
    """动态发现可用服务器：并发 TCP 探测 → 可达者逐个真实验活 → 返回 bars 可用列表。
    成本：候选 ~103 台 × `_probe(2s)` 并发 8 路 ≈ 20~30s（顺序扫要 ~9 分钟）。"""
    seen, ordered = set(), []
    for ip, port in list(_TDX_SEED_SERVERS) + _pytdx_host_candidates():
        if (ip, port) not in seen:
            seen.add((ip, port))
            ordered.append((ip, port))
    reachable = []
    try:
        with ThreadPoolExecutor(max_workers=TDX_DISCOVER_WORKERS) as ex:
            futs = {ex.submit(_probe, ip, port): (ip, port) for ip, port in ordered}
            for fu in as_completed(futs):
                ip, port = futs[fu]
                try:
                    if fu.result():
                        reachable.append((ip, port))
                except Exception:
                    pass
    except Exception:
        reachable = [t for t in ordered if _probe(*t)]
    if verbose:
        print(f"  🔎 TDX 发现：候选 {len(ordered)} 台 → TCP 可达 {len(reachable)} 台，逐个校验 bars…")
    good = []
    for ip, port in reachable:
        try:
            cli = Quotes.factory(market=market, server=(ip, port))
        except Exception:
            continue
        if _server_ok(cli):
            good.append((ip, port))
    if verbose:
        print(f"  🔎 TDX 发现：bars 可用 {len(good)} 台 {[ip for ip, _ in good][:6]}")
    return good



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


#: 负缓存：动态发现失败后的一段时间内不再重扫（避免上层重试/重连触发「发现风暴」）
_TDX_DISCOVER_STATE = {'last_fail_ts': 0.0}
TDX_NEGATIVE_TTL_S = 120.0


def tdx_client(market='std'):
    """创建 mootdx 客户端（2026-09-10 三级重构，替换原四级兜底）。

    ① **种子服务器**（`_TDX_SEED_SERVERS`，实测可用）—— 快速路径，逐个 `_probe` + `_server_ok`
    ② **动态发现缓存**（`data/tdx_servers.json`，TTL 6h）—— 二次启动 <5s
    ③ **动态全表发现**（pytdx hosts 全表并发探测 + 真实验活，结果写缓存）

    全失败 → **raise**（不再回退到不校验的裸 factory）：让上层走 HTTP 真实 OHLC 兜底，
    而不是静默返回一个 bars 不通的客户端 —— 那正是 2026-09-10 全天返空的根因。
    应急逃生门：env `TP_ALLOW_UNVERIFIED_TDX=1` 才允许裸 factory 兜底（不推荐）。

    历史教训（勿重犯）：旧实现第②级用 `pytdx_hosts[:30]` 截断，而实测可用服务器在 103 条列表的
    索引是 [49, 70~77] ⇒ 9 台可用服务器 0 台进候选；再叠加「bestip 只测延迟」「第④级不校验」，
    最终稳定地落到 bars 不通的服务器上。
    """
    def _try(pairs, label):
        for ip, port in pairs:
            if not _probe(ip, port):
                continue
            try:
                cli = Quotes.factory(market=market, server=(ip, port))
            except Exception:
                continue
            if _server_ok(cli):
                print(f"  ✅ TDX 服务器可用({label}) {ip}:{port}")
                return cli
        return None

    cli = _try(_TDX_SEED_SERVERS, '种子')
    if cli is not None:
        return cli

    cli = _try(_read_tdx_cache(), '缓存')
    if cli is not None:
        return cli

    if time.time() - _TDX_DISCOVER_STATE['last_fail_ts'] < TDX_NEGATIVE_TTL_S:
        raise RuntimeError(
            "TDX 服务器暂不可用（负缓存 %ds 内不再重扫，避免发现风暴）"
            % int(TDX_NEGATIVE_TTL_S))

    good = _discover_tdx_servers(market=market)
    if good:
        _write_tdx_cache(good)
        cli = _try(good, '动态发现')
        if cli is not None:
            return cli
    _TDX_DISCOVER_STATE['last_fail_ts'] = time.time()

    if os.environ.get('TP_ALLOW_UNVERIFIED_TDX') == '1':
        try:
            cli = Quotes.factory(market=market)
            print("  ⚠️ TP_ALLOW_UNVERIFIED_TDX=1 → 回退到未校验的裸 factory（不推荐，可能全天返空）")
            return cli
        except Exception as e:
            raise RuntimeError("TDX 全部不可用（含裸 factory）：%s" % e)

    raise RuntimeError(
        "所有通达信服务器均不可用（TCP 可达但 bars 返空）。已禁用未校验的裸 factory 兜底 —— "
        "请让上层走 HTTP 真实 OHLC 兜底（新浪/腾讯），或检查网络/DNS。"
        "如需强制回退请设 TP_ALLOW_UNVERIFIED_TDX=1。")


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
        # [2026-09-10] 非规格化(denormal)垃圾值归零：收盘竞价占位 bar 会出现 5.877e-39 这类值——
        # 既不是真实成交量，也不是 0（0 = 该分钟无成交，是合法值）。⚠️ 只归零、**不删行**：
        # 删行会移动 bar 下标 i，进而错位 bar_key(f"bar_{sym}_{today}_{i}") 与指标数组对齐。
        try:
            _garbage = (df['volume'] > 0) & (df['volume'] < 1e-6)
            if bool(_garbage.any()):
                print(f"  ⚠️ 归零 {int(_garbage.sum())} 行异常成交量（denormal 垃圾值）")
                df.loc[_garbage, 'volume'] = 0.0
        except Exception:
            pass
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
        # [2026-09-10] 兜底改为**按需取数**：mootdx 正常时不再每轮白打一次 HTTP。
        # 旧实现无条件调用 `_tencent_intraday_fallback()`，实测 15s 轮询下 = **960 次 HTTP/日**；
        # 若把扫描间隔提到 3s 会放大到 **4800 次/日** —— 对无 SLA 的免费公共源是不必要的压力
        # 与限流风险（2026-09-10 已见 mootdx free 源全天失效）。惰性化后 mootdx 正常时 HTTP 恒为 0，
        # 只在真降级时才打；代价为零（本就在 mootdx 失败后才使用 fb）。
        fb = None if mootdx_ok else self._tencent_intraday_fallback(sym)
        # [2026-09-10 P0] 兜底实际命中哪个源（_fetch_pool 写在 attrs 里）；缺省按腾讯合成口径
        fb_src = (getattr(fb, 'attrs', None) or {}).get('fallback_source', 'tencent_synth') \
            if fb is not None else None
        tencent_ok = fb is not None and len(fb) >= 5
        if mootdx_ok:
            chosen = df  # 真实 OHLC 优先
            src = 'mootdx'
        elif tencent_ok:
            chosen = fb
            src = fb_src
            print(f"  ✅ 兜底成功({src}) {sym}: {len(fb)} 根分钟线")
        elif df is not None and len(df) >= 3:
            chosen = df  # 两源均不足5行，mootdx 3-4 行凑合（compute 将因<5行拒收）
            src = 'mootdx_partial'
            print(f"  ⚠️ mootdx 仅 {len(df)} 行且兜底失败，compute 将因<5行拒收")
        elif fb is not None and len(fb) >= 3:
            chosen = fb
            src = fb_src
            print(f"  ⚠️ 兜底仅 {len(fb)} 行({src}) 且 mootdx 失败，compute 将因<5行拒收")
        else:
            print(f"  ⚠️ 所有数据源均无分钟K数据 {sym}（mootdx+兜底均失败/不足3行）")
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
        # [2026-09-10] 非规格化(denormal)垃圾值归零：收盘竞价占位 bar 会出现 5.877e-39 这类值——
        # 既不是真实成交量，也不是 0（0 = 该分钟无成交，是合法值）。⚠️ 只归零、**不删行**：
        # 删行会移动 bar 下标 i，进而错位 bar_key(f"bar_{sym}_{today}_{i}") 与指标数组对齐。
        try:
            _garbage = (df['volume'] > 0) & (df['volume'] < 1e-6)
            if bool(_garbage.any()):
                print(f"  ⚠️ 归零 {int(_garbage.sum())} 行异常成交量（denormal 垃圾值）")
                df.loc[_garbage, 'volume'] = 0.0
        except Exception:
            pass
        # 只保留当日数据（mootdx 可能返回跨日）
        today = pd.Timestamp.now().strftime('%Y-%m-%d')
        if 'trade_date' in df.columns:
            df = df[df['trade_date'] == today].reset_index(drop=True)
        # [2026-09-10 P0 兜底率哨兵] 标记本轮实际命中的数据源，供 monitor 统计兜底率
        # 与复盘标注口径。取值：mootdx（真实OHLC，主源）/ sina（真实OHLC，兜底）/
        # tencent_synth（合成OHLC，兜底）/ mootdx_partial（<5行凑合）。
        df.attrs['data_source'] = src
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
        返回 DataFrame[trade_time, trade_date, open, close, high, low, volume] 或 None。

        2026-07-31 迭代改进（P0-1 数据源韧性）：
        - 复用 _retry_with_backoff（3 次 / 1s-4s 退避）应对盘中间歇性 DNS 抖动
          （getaddrinfo failed 曾致 07-31 全天漏推 ~50% 信号）；
        - 重试仅对"网络级异常"生效（urllib 抛错）；返回 None（无数据）不重试；
        - 总耗时 ≤ 首试+7s < SCAN_INTERVAL(15s)，不阻塞主循环。

        [轮次2-1 迭代] 服务器级 failover：
        - 腾讯域名单点 → 域名池 _TENCENT_HOSTS（3 个镜像）按序尝试；
        - 再加独立厂商新浪 1m（真实 OHLC）作第三级，抗"腾讯全家族 DNS 故障"；
        - 某源网络异常立即切换下一个（不重试当前源）；全池失败才对"整池"做 1 次退避重试；
        - 消除 07-31 "单个域名 DNS 抖→全天失联"单点故障。
        """
        def _fetch_sina():
            """新浪 1m K线（真实 OHLC，JSONP 格式）。返回 DataFrame 或 None。"""
            tcode = _tencent_code(sym)  # 'sz161129' 新浪同格式
            url = _SINA_MINUTE_URL.format(tcode=tcode)
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn/',
            })
            raw = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', errors='ignore')
            m = re.search(r'\((\[.*\])\)', raw, re.S)
            if not m:
                return None
            rows = []
            for d in json.loads(m.group(1)):
                try:
                    day, o, h, l, c, vol = (d['day'], float(d['open']), float(d['high']),
                                            float(d['low']), float(d['close']), float(d.get('volume', 0)))
                    rows.append({
                        'trade_time': pd.Timestamp(day),
                        'trade_date': str(day)[:10],
                        'open': o, 'close': c, 'high': h, 'low': l,
                        'volume': vol,
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if len(rows) < 3:
                return None
            df = pd.DataFrame(rows)
            return df.sort_values('trade_time').reset_index(drop=True)

        def _fetch_host(host, timeout=8.0):
            """单域名单次拉取，返回 DataFrame 或 None（无数据不算异常）。"""
            tcode = _tencent_code(sym)  # 'sz161129'
            url = f'https://{host}{_TENCENT_PATH}{tcode}'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.qq.com/',
            })
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode('gbk', errors='ignore')
            # 解析 JSON: {"data": {"sz161129": {"data": {"data": ["0930 1.995 ...", ...], "date":"20260721"}}}}
            import json as _json
            j = _json.loads(raw)
            # 导航到 data 数组
            d = j.get('data', {})
            sym_data = d.get(tcode, {}) or (d.get(list(d.keys())[0]) if d else {})
            inner = sym_data.get('data', {})
            lines = inner.get('data', [])
            date_str = inner.get('date', '')
            if not lines or not date_str:
                return None  # 无数据不算网络异常，不重试
            rows = []
            prev_price = None
            for line in lines:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                hhmm = parts[0]
                price = float(parts[1])
                volume = float(parts[2]) if len(parts) >= 3 and parts[2] != '0' else 0.0
                if len(hhmm) != 4:
                    continue
                tt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {hhmm[:2]}:{hhmm[2:]}:00"
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
            return df.sort_values('trade_time').reset_index(drop=True)

        def _fetch_pool():
            """兜底链（**真实 OHLC 优先**）：新浪 1m(真实 OHLC) → 腾讯域名池(分时,合成 OHLC) →
            全部失败抛异常给退避。单源失败打印切换日志；返回 None（无数据）也切下一源。

            [2026-09-10 P1b 顺序修正] 原顺序为「腾讯分时 → 新浪」，但本文件 51-53 行的注释
            早已写明新浪 1m 是**真实 OHLC、质量优于腾讯分时合成**，即执行顺序与既定口径相反。
            全历史 448 标的-日 A/B（把腾讯合成变换施加到 F 盘真实 1m）实测：合成/真实 ATR 比值
            中位 **0.582**（低估 41.8%，p10-p90 = 0.451~0.697），信号总量比 1.077（+7.7%，
            低于数量级门槛），但**逐日 48.4% 的信号组合发生变化**（总量守恒、位置漂移）。
            ⇒ 真实 OHLC 优先可消除 ATR 系统性低估（反T 1.5×ATR 止损在合成口径下窄 42%）。
            DNS 多样性不受损：mootdx 的 TCP 10 台服务器冗余不变，HTTP 层仍是跨厂商双源。

            回滚开关：env `TP_INTRADAY_PREFER=tencent` → 立刻恢复原顺序（无需改码）。
            固化开关：`config/datasource_policy.json` 的 `fallback_order`（基准达标后写入）。
            """
            def _try_sina():
                df = _fetch_sina()
                if df is not None:
                    df.attrs['fallback_source'] = 'sina'
                    print(f"  ✅ 新浪1m兜底成功 {sym}: {len(df)} 根 (真实OHLC)")
                return df

            def _try_tencent():
                last = None
                for host in _TENCENT_HOSTS:
                    try:
                        df = _fetch_host(host)
                        if df is not None:
                            df.attrs['fallback_source'] = 'tencent_synth'
                            return df
                    except Exception as e:
                        last = e
                        print(f"  ⚠️ 腾讯分时 {sym} 域名 {host} 失败，切换备用: {e}")
                if last is not None:
                    raise last
                return None

            # 顺序由 _intraday_order() 决定：env > config/datasource_policy.json > 默认(真实OHLC优先)
            order = [_try_sina if n == 'sina' else _try_tencent for n in _intraday_order()]
            last_exc = None
            for fetch in order:
                try:
                    df = fetch()
                    if df is not None:
                        return df
                except Exception as e:
                    last_exc = e
                    _nm = '新浪1m' if fetch is _try_sina else '腾讯分时'
                    print(f"  ⚠️ {_nm} {sym} 失败，切换备用: {e}")
            # 全池失败：抛最后一个异常触发退避重试（仅网络级失败会走到这）
            if last_exc is not None:
                raise last_exc
            return None  # 所有源都返回无数据（非网络异常），直接 None

        try:
            # [P0-1] 整池失败走退避重试（3次/1s-4s）；返回 None 不重试
            return _retry_with_backoff(_fetch_pool, max_retries=3, base=1.0, cap=7.0,
                                       label=f'腾讯分时池 {sym}')
        except Exception as e:
            print(f"  ⚠️ 腾讯分时兜底失败(全池重试3次) {sym}: {e}")
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
