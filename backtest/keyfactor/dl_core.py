#!/usr/bin/env python3
"""tpoint 数据层公共模块 —— 异常框架 + 数据完整性校验 + 自迭代扫描。

供给:
  - download_tickflow.py : 底层抓取, 落盘前/后做完整性校验, 不达标隔离到 .bad
  - download_supervisor.py: 自迭代引擎, 反复扫描缺口并重下, 直到全达标

设计目标 (对应需求 "自带数据完整性检测 + 完整异常处理框架 + 自迭代"):
  1. 异常框架: 所有抓取/校验异常归入 DlError 异常族, 每个异常带 category(便于归类统计)
     与 retryable(决定能否重试). classify() 把底层原始异常映射到框架异常.
  2. 完整性校验: verify_csv() 用标准库 csv 模块(无 pandas 依赖, 快), 检查:
       文件存在/非空 -> 表头列 -> 时间戳严格递增且唯一 -> OHLC 合法性(容差) ->
       成交量非负 -> 零成交量空价分钟视为"无成交"(跳过) -> 交易时间合法 ->
       跨度完整性(覆盖交易日数, 兼容低流动性票).
     返回结构化 IntegrityReport, ok=False 时带 errors 清单.
  3. 自迭代扫描: IntegrityStore 用 (mtime,size) 缓存校验结果, 反复扫描时只重验
     变更过的文件, 未变文件直接命中缓存, 让多轮扫描代价可控.
"""
import os
import json
import time
import csv
import threading

# ---------- 常量 ----------
BARS_PER_MONTH = 21 * 240        # ~5040, 1 月约 21 交易日 * 240 分钟
DEFAULT_COVERAGE = 0.90          # 行数覆盖率(仅作信息展示)
TRADING_DAYS_PER_MONTH = 21
SPAN_MIN_RATIO = 0.80             # 跨度完整性: 至少覆盖 months*21*该比例 个交易日
REQUIRED_COLS = ["symbol", "name", "timestamp", "trade_date", "trade_time",
                  "open", "high", "low", "close", "volume", "amount"]


def _build_trading_minutes():
    """A 股连续竞价分钟内集合 (09:30-11:30, 13:00-15:00, 含 15:00 收盘集合)。"""
    s = set()
    for h in (9, 10, 13, 14):
        for m in range(60):
            s.add(f"{h:02d}:{m:02d}")
    for m in range(31):                 # 11:00-11:30
        s.add(f"11:{m:02d}")
    for m in range(1, 60):             # 13:01-13:59
        s.add(f"13:{m:02d}")
    s.add("13:00")
    s.add("15:00")                     # 15:00 收盘集合竞价/最后一分钟
    return s


TRADING_MINUTES = _build_trading_minutes()


def expected_days(months, ratio=SPAN_MIN_RATIO):
    return int(months * TRADING_DAYS_PER_MONTH * ratio)


# =====================================================================
# 异常框架 (Exception Framework)
# =====================================================================
class DlError(Exception):
    """所有下载/校验错误的基类。

    category : 错误类别, 用于统计/告警分组
    retryable: 是否可重试 (引擎据此决定重试 or 终止)
    """
    category = "unknown"
    retryable = True

    def __init__(self, msg, *, sym=None, cause=None):
        super().__init__(msg)
        self.sym = sym
        self.cause = cause

    def to_dict(self):
        return {"category": self.category, "retryable": self.retryable,
                "sym": self.sym, "msg": str(self)}


class NetworkError(DlError):
    category = "network"; retryable = True


class TimeoutError_(DlError):
    category = "timeout"; retryable = True


class RateLimitError(DlError):
    category = "ratelimit"; retryable = True


class AuthError(DlError):
    category = "auth"; retryable = False


class ApiError(DlError):
    category = "api"; retryable = False


class PartialDataError(DlError):
    category = "partial"; retryable = True


class IntegrityError(DlError):
    category = "integrity"; retryable = True


class DiskError(DlError):
    category = "disk"; retryable = False


def classify(exc, sym=None):
    """把底层原始异常映射到框架异常, 便于统一重试/告警策略。"""
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or "read timed" in msg:
        return TimeoutError_(f"timeout: {exc}", sym=sym, cause=exc)
    if "rate" in msg or "429" in msg or "11232" in msg or "too many" in msg:
        return RateLimitError(f"rate limited: {exc}", sym=sym, cause=exc)
    if "auth" in msg or "401" in msg or "403" in msg or "unauthorized" in msg or "permission" in msg:
        return AuthError(f"auth: {exc}", sym=sym, cause=exc)
    if "connection" in msg or "reset" in msg or "broken" in msg or "network" in msg \
            or "socket" in msg or "reset by peer" in msg:
        return NetworkError(f"network: {exc}", sym=sym, cause=exc)
    if "partial" in msg or "incomplete" in msg:
        return PartialDataError(f"partial: {exc}", sym=sym, cause=exc)
    return DlError(f"{type(exc).__name__}: {exc}", sym=sym, cause=exc)


# =====================================================================
# 完整性校验 (Integrity Verification)
# =====================================================================
class IntegrityReport:
    __slots__ = ("sym", "path", "ok", "bars", "errors", "first_ts", "last_ts",
                 "coverage", "ndates")

    def __init__(self, sym, path):
        self.sym = sym
        self.path = path
        self.ok = False
        self.bars = 0
        self.errors = []
        self.first_ts = None
        self.last_ts = None
        self.coverage = None
        self.ndates = 0

    def add(self, msg):
        self.errors.append(msg)

    def to_dict(self):
        return {"sym": self.sym, "ok": self.ok, "bars": self.bars,
                "errors": self.errors, "first_ts": self.first_ts,
                "last_ts": self.last_ts, "coverage": self.coverage,
                "ndates": self.ndates}


def verify_csv(path, sym, months, expect_full=True,
               coverage=DEFAULT_COVERAGE, strict=True):
    """校验单个 1m CSV 的数据完整性。纯标准库, 无 pandas 依赖。

    expect_full=False 时只做结构校验(用于已标记短历史的票);
    expect_full=True 时额外要求 覆盖交易日数 >= expected_days(months) (跨度完整性,
    兼容低流动性票——只要时间跨度铺满, 稀疏也算完整)。
    返回 IntegrityReport。
    """
    rep = IntegrityReport(sym, path)
    if not os.path.exists(path):
        rep.add("file_missing")
        return rep
    try:
        size = os.path.getsize(path)
    except OSError as e:
        rep.add(f"stat_error:{e}")
        return rep
    if size == 0:
        rep.add("empty_file")
        return rep

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            header = next(r, None)
            if header is None:
                rep.add("no_header")
                return rep
            header = [h.strip() for h in header]
            if header != REQUIRED_COLS:
                rep.add(f"bad_columns:{header}")
                if strict:
                    return rep
            idx = {c: i for i, c in enumerate(header)}
            ts_i = idx["timestamp"]; o_i = idx["open"]; h_i = idx["high"]
            l_i = idx["low"]; c_i = idx["close"]; v_i = idx["volume"]
            td_i = idx["trade_date"]; tt_i = idx["trade_time"]
            tt_present = tt_i < len(header)
            td_present = td_i < len(header)

            prev_ts = None
            dup = 0
            nonpos = 0
            bad_ohlc = 0
            notrade_with_vol = 0
            neg_vol = 0
            bad_tt = 0
            nan_ct = 0
            n = 0
            dates = set()

            for row in r:
                if len(row) < len(header):
                    rep.add("short_row")
                    continue
                n += 1
                # --- timestamp (必须存在且唯一递增) ---
                try:
                    ts = int(float(row[ts_i]))
                except Exception:
                    ts = None
                    nan_ct += 1
                if ts is not None:
                    if prev_ts is not None:
                        if ts < prev_ts:
                            rep.add("timestamp_not_ascending")
                        elif ts == prev_ts:
                            dup += 1
                    prev_ts = ts
                # --- trade_date (用于跨度完整性) ---
                if td_present:
                    d = row[td_i].strip()
                    if d:
                        dates.add(d)
                # --- volume ---
                try:
                    v = float(row[v_i])
                except Exception:
                    v = None
                if v is not None and v < 0:
                    neg_vol += 1
                v_is_zero = (v is not None and v == 0)
                # --- OHLC: 空且 volume=0 视为"无成交分钟", 跳过校验 ---
                ohlc = [row[o_i].strip(), row[h_i].strip(),
                         row[l_i].strip(), row[c_i].strip()]
                if all(s == "" for s in ohlc):
                    if not v_is_zero:
                        notrade_with_vol += 1   # 有成交量却无价 -> 异常
                else:
                    try:
                        o, h, l, c = (float(x) for x in ohlc)
                    except Exception:
                        o = h = l = c = None
                        nan_ct += 1
                    if None not in (o, h, l, c):
                        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                            nonpos += 1
                        # 容差: A股价格最多3位小数, 先四舍五入到4位消除
                        # tickflow 浮点噪声(~1e-14); 仅当偏差 > 1e-3 才计违规。
                        ro, rh, rl, rc = round(o, 4), round(h, 4), round(l, 4), round(c, 4)
                        if rh < rl - 1e-3:
                            bad_ohlc += 1
                        elif rh < ro - 1e-3 or rh < rc - 1e-3 or rl > ro + 1e-3 or rl > rc + 1e-3:
                            bad_ohlc += 1
                # --- trade_time (格式 "YYYY-MM-DD HH:MM:SS") ---
                if tt_present:
                    tt = row[tt_i].strip()
                    if len(tt) >= 16 and tt[10] == " ":
                        hm = tt[11:16]
                        if hm not in TRADING_MINUTES:
                            bad_tt += 1
                    elif tt:
                        bad_tt += 1

            rep.bars = n
            rep.ndates = len(dates)
            if n == 0:
                rep.add("zero_rows")
                return rep
            if dup:
                rep.add(f"duplicate_timestamps:{dup}")
            if nan_ct:
                rep.add(f"nan_cells:{nan_ct}")
            if nonpos:
                rep.add(f"nonpositive_ohlc:{nonpos}")
            if bad_ohlc:
                rep.add(f"ohlc_inconsistent:{bad_ohlc}")
            if notrade_with_vol:
                rep.add(f"vol_no_price:{notrade_with_vol}")
            if neg_vol:
                rep.add(f"negative_volume:{neg_vol}")
            if bad_tt:
                rep.add(f"bad_trade_time:{bad_tt}")
            if prev_ts is not None:
                rep.last_ts = prev_ts
            rep.coverage = round(n / (months * BARS_PER_MONTH), 3) if months else None
            # 跨度完整性: 以"覆盖交易日数"衡量, 兼容低流动性票
            if expect_full and len(dates) < expected_days(months):
                rep.add(f"incomplete_days:{len(dates)}<{expected_days(months)}")
            rep.ok = (len(rep.errors) == 0)
    except Exception as e:
        rep.add(f"parse_error:{e}")
    return rep


# =====================================================================
# 自迭代扫描 (Self-iteration scan with cache)
# =====================================================================
class IntegrityStore:
    """校验结果缓存: key=(mtime,size)。未变更文件直接命中, 多轮扫描代价可控。"""

    def __init__(self, path, batch_size=250):
        self.path = path
        self.data = {}
        self._lock = threading.Lock()
        self._batch = batch_size
        self._since = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except Exception:
            pass

    def check(self, sym, fpath, months, short_set, force=False):
        """返回 IntegrityReport; 命中缓存则直接返回缓存结论。"""
        try:
            st = os.stat(fpath)
            key = [int(st.st_mtime), st.st_size]
        except OSError:
            return verify_csv(fpath, sym, months, expect_full=(sym not in short_set))
        cached = self.data.get(sym)
        if not force and cached and cached.get("_key") == key:
            r = IntegrityReport(sym, fpath)
            r.ok = cached.get("ok", False)
            r.bars = cached.get("bars", 0)
            r.errors = cached.get("errors", [])
            r.first_ts = cached.get("first_ts")
            r.last_ts = cached.get("last_ts")
            r.coverage = cached.get("coverage")
            r.ndates = cached.get("ndates", 0)
            return r
        rep = verify_csv(fpath, sym, months, expect_full=(sym not in short_set))
        with self._lock:
            self.data[sym] = {"_key": key, "ok": rep.ok, "bars": rep.bars,
                               "errors": rep.errors, "first_ts": rep.first_ts,
                               "last_ts": rep.last_ts, "coverage": rep.coverage,
                               "ndates": rep.ndates, "ts": int(time.time())}
            self._since += 1
            if self._since >= self._batch:
                self._save()
                self._since = 0
        return rep

    def flush(self):
        """强制落盘剩余缓存（批量审计/引擎收尾时调用）。"""
        with self._lock:
            self._save()
            self._since = 0

    def forget(self, sym):
        with self._lock:
            self.data.pop(sym, None)
            self._save()


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else None
    if not p:
        print("usage: dl_core.py <csv_path> [months]")
    else:
        rep = verify_csv(p, os.path.basename(p).split("_")[0],
                         int(sys.argv[2]) if len(sys.argv) > 2 else 6)
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
