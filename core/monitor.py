#!/usr/bin/env python3
"""
T点监控 v9 — VWAP+ATR+趋势过滤+量价确认+情绪温度计
核心改进 vs v8:
  1. VWAP 替代静态支撑阻力(日内水平线,不随价格下移) → 解决S不触发
  2. 趋势过滤(EMA20/60+ADX) → 信号与趋势同向,下跌主动发S
  3. 量价确认(量比+K线形态) → 收敛B误发
  4. 情绪温度计(RSI+涨跌+量比+偏离) → 加权门控
飞书webhook直推 + 本地文件备份
算法层见 indicators.py (monitor/backtest/selftest共用)
"""
import os, sys, json, time, tempfile
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from datasource import MootdxDataSource as TickFlow
from indicators import (
    compute_indicators, check_b_trigger, check_s_trigger, stars,
    K1, K2, TEMP_HOT, TEMP_COLD,
)
# 出场管理：接 exit_manager 的移动止损/硬止损/S信号出场（P0 待办）
from exit_manager import make_config

# ========== 路径配置化（跨平台，无需硬编码绝对路径） ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _env_or(name, default):
    """环境变量优先，否则用默认（脚本相对路径 / 原值）。"""
    v = os.environ.get(name)
    return v if v else default

def _load_config_json():
    """可选：与脚本同目录的 config.json 作为配置兜底（键值覆盖常量）。"""
    p = os.path.join(BASE_DIR, 'config.json')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"  ⚠️ 读取 config.json 失败: {e}")
    return {}

_CFG = _load_config_json()
def _cfg(name, env_name, default):
    if env_name in os.environ and os.environ[env_name]:
        return os.environ[env_name]
    if name in _CFG and _CFG[name] is not None:
        return _CFG[name]
    return default

API_KEY = _cfg('api_key', 'V9_API_KEY', "tk_60a2170efd294c82b2245324a268b2a8")
CST = timezone(timedelta(hours=8))
SIGNAL_FILE = _cfg('signal_file', 'SIGNAL_FILE', os.path.join(BASE_DIR, 'data', 'signal.txt'))
STATE_FILE  = _cfg('state_file',  'STATE_FILE',  os.path.join(BASE_DIR, 'data', 'state.json'))
METRICS_FILE = _cfg('metrics_file', 'METRICS_FILE', os.path.join(BASE_DIR, 'data', 'metrics.json'))
PROMPT_FILE = _cfg('prompt_file', 'TP_PROMPT_FILE', os.path.join(BASE_DIR, '..', 'stock-pool', 'prompt-common.md'))
WEBHOOK_URL = _cfg('webhook_url', 'TP_WEBHOOK_URL', "https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d")
# 锁文件放到系统临时目录（跨平台），不再写死 /tmp
LOCK_FILE = os.path.join(tempfile.gettempdir(), 'tpoint_monitor.lock')
PID_FILE  = os.path.join(tempfile.gettempdir(), 'tpoint_monitor.pid')

# ========== 出场管理配置（生产方向，已锁定） ==========
# 仅移动止损：浮盈≥0.4% 激活，从浮动高点回撤 0.6% 触发平仓；关硬止损/时间止损；S信号作自然出场
EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
# 如需调参（如开硬止损/时间止损），改这里或经 config.json 的 exit_config 传入

# ========== 跨平台文件锁（Windows 用 msvcrt，Unix 用 fcntl） ==========
if os.name == 'nt':
    import msvcrt
    def _acquire_lock(lf):
        lf.seek(0)
        msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
    def _release_lock(lf):
        try:
            lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
else:
    import fcntl
    def _acquire_lock(lf):
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def _release_lock(lf):
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass

COOLDOWN = 120          # B/S共用冷却(秒)
MAX_B_DAILY = 12
MAX_S_DAILY = 12

# ========== 监控标的 (沿用v8: 从持仓文件自动同步) ==========
def get_exchange(code):
    if code.startswith(('000','001','002','003','300','301')):
        return '.SZ'
    if code.startswith(('600','601','603','605','688')):
        return '.SH'
    return '.SZ'

def load_targets():
    prompt_file = PROMPT_FILE
    try:
        with open(prompt_file) as f:
            lines = f.readlines()
    except:
        return {}
    targets = {}
    in_table = False
    for line in lines:
        stripped = line.strip()
        if '持仓硬锁定' in stripped:
            in_table = True
            continue
        if not in_table:
            continue
        if stripped.startswith('##') and '持仓硬锁定' not in stripped:
            break
        if not stripped.startswith('|'):
            continue
        if '---' in stripped or '标的' in stripped:
            continue
        cols = [c.strip() for c in stripped.split('|')]
        if len(cols) < 4:
            continue
        try:
            name = cols[2]
            code = cols[3]
            _ = int(code)
            if code.startswith(('5','1')):
                continue
            sym = code + get_exchange(code)
            targets[sym] = name
        except (ValueError, IndexError):
            pass
    return targets

TARGETS = load_targets()
if not TARGETS:
    TARGETS = {
        '300975.SZ': '商络电子',
        '601869.SH': '长飞光纤',
        '603938.SH': '三孚股份',
        '300395.SZ': '菲利华',
        '301526.SZ': '国际复材',
    }
    print("⚠️ 无法读取持仓文件，使用硬编码列表")

_EXCLUDE = {'920222.BJ', '920222.SZ'}
TARGETS = {k: v for k, v in TARGETS.items() if k not in _EXCLUDE}

tf = TickFlow()

# ========== 全局状态 ==========
STATE = {sym: {'PC': 0, 'WARM': None} for sym in TARGETS}

def refresh_daily(sym=None):
    syms = [sym] if sym else list(TARGETS.keys())
    now_cst = datetime.now(CST)
    today_str = now_cst.strftime('%Y-%m-%d')
    for s in syms:
        try:
            d = tf.klines.get(s, period='1d', count=60, as_dataframe=True)
            if d is None or len(d) == 0:
                name = TARGETS.get(s, s)
                print(f"   ⚠️ {name}({s}) 无日K数据, 跳过")
                continue
            d = d.sort_values('trade_date')
            last_date = str(d['trade_date'].iloc[-1])[:10]
            if last_date == today_str:
                STATE[s]['PC'] = float(d['close'].iloc[-2])
            else:
                STATE[s]['PC'] = float(d['close'].iloc[-1])
            STATE[s]['WARM'] = d['close'].values[-30:]
        except Exception as e:
            name = TARGETS.get(s, s)
            print(f"   ❌ {name}({s}) 日K刷新失败: {e}")
    if len(syms) == 1:
        return STATE[syms[0]]['PC'], STATE[syms[0]]['WARM']
    return None

refresh_daily()
for sym, name in TARGETS.items():
    print(f"  {name}({sym}) PC={STATE[sym]['PC']:.2f}")

def now_ts():
    return datetime.now(CST).timestamp()

_last_push_ts = 0
MIN_PUSH_INTERVAL = 5

def push_batch(signals_text):
    global _last_push_ts
    if not signals_text:
        return
    now = time.time()
    wait = MIN_PUSH_INTERVAL - (now - _last_push_ts)
    if wait > 0:
        time.sleep(wait)
    text = '\n\n'.join(signals_text)
    print(f"  📡 PUSH 准备: {len(signals_text)}条, 内容前60字: {text[:60]}")
    try:
        r = requests.post(WEBHOOK_URL, json={
            "msg_type": "text", "content": {"text": text}
        }, timeout=5)
        _last_push_ts = time.time()
        resp = r.json()
        print(f"  📡 PUSH 响应: status={r.status_code} code={resp.get('code')} msg={resp.get('msg')}")
        if r.status_code == 200 and resp.get('code') == 0:
            return True
        print(f"  ⚠️ PUSH 失败: {r.text[:200]}")
    except Exception as e:
        print(f"  ⚠️ PUSH 异常: {e}")
    return False

def compute(sym):
    """v9: 调算法层计算全部指标"""
    df = tf.klines.intraday(sym, as_dataframe=True)
    if df is None or len(df) < 5:
        return None
    df = df.sort_values('trade_time').reset_index(drop=True)

    bar_date = str(df['trade_date'].iloc[0])
    today_str = datetime.now(CST).strftime('%Y-%m-%d')
    if bar_date != today_str:
        print(f"  ❌ {sym} intraday日期错误: {bar_date} != {today_str}, 跳过")
        return None

    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o = df['open'].values.astype(float) if 'open' in df.columns else c.copy()
    has_vol = ('volume' in df.columns)
    v = df['volume'].values.astype(float) if has_vol else None
    pc = STATE[sym]['PC']

    data = compute_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data

def emit(sig_type, price, chg_pct, level_val, level_type, rsi, temp, vol_r, name, tag='', exit_reason=''):
    # 出场管理推送（接 exit_manager）：B开仓后跟踪，TRAIL/S触发平仓提醒
    if sig_type == 'X':
        reason = f" [{exit_reason}]" if exit_reason else ''
        chg_sign = '+' if chg_pct >= 0 else ''
        lines = [
            f"🔵 {name} EXIT{reason}{' ' + tag if tag else ''}",
            f"现价 {price:.2f}（{chg_sign}{chg_pct:.1f}%）",
            f"{level_type}{level_val:.2f} RSI={rsi:.1f} 温度={temp:.0f}"
        ]
        msg = '\n'.join(lines)
        print(msg)
        with open(SIGNAL_FILE, 'a') as f:
            f.write(f"[{datetime.now(CST).strftime('%H:%M:%S')}]\n{msg}\n\n")
        return msg
    emoji = '🟢' if sig_type == 'B' else '🔴'
    op_type = 'BUY' if sig_type == 'B' else 'SELL'
    chg_sign = '+' if chg_pct >= 0 else ''
    star = stars(sig_type, temp, vol_r)
    lines = [
        f"{emoji} {name} {op_type} {star}{' ' + tag if tag else ''}",
        f"现价 {price:.2f}（{chg_sign}{chg_pct:.1f}%）",
        f"{level_type}{level_val:.2f} RSI={rsi:.1f} 温度={temp:.0f}"
    ]
    msg = '\n'.join(lines)
    print(msg)
    with open(SIGNAL_FILE, 'a') as f:
        f.write(f"[{datetime.now(CST).strftime('%H:%M:%S')}]\n{msg}\n\n")
    return msg

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        saved_date = s.get('_daily_refreshed_date', '')
        today = datetime.now(CST).strftime('%Y-%m-%d')
        if saved_date and saved_date != today:
            keys_to_remove = [k for k in s.keys() if k.startswith('bar_') or k.startswith('_cooldown_') or k.startswith('pos_')]
            for k in keys_to_remove:
                del s[k]
            s['_daily_refreshed_date'] = None
        return s
    except:
        return {}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

def write_metrics(duration_s, signals, errors, last_bar_ts, symbols):
    """每轮扫描末写入 metrics.json，供告警引擎(watchdog)采集。
    包含：扫描耗时 / 本轮信号数 / 本轮错误数 / 最新行情棒时间 / 标的数。失败静默。"""
    try:
        with open(METRICS_FILE, 'w') as f:
            json.dump({
                'ts': time.time(),
                'scan_duration_s': round(duration_s, 3),
                'signals': signals,
                'errors': errors,
                'symbols': symbols,
                'last_bar_ts': last_bar_ts if last_bar_ts else None,
                'status': 'running',
            }, f)
    except Exception:
        pass

def _compute_stop_price(entry_price, atr, entry_idx, cfg):
    """硬止损价（仅 use_stop 时有意义）；关闭时返回 -inf，不影响移动止损比较。"""
    if not cfg['use_stop']:
        return -1e9
    return entry_price - cfg['stop_atr_mult'] * atr[entry_idx]


def _mk_exit(reason, name, price, pos, vwap, atr, rsi14, temp, vol_ratio, i):
    """构造一条 EXIT 信号元组（11元组，末位为 exit_reason）。"""
    entry = pos['entry_price']
    chg = (price - entry) / entry * 100 if entry > 0 else 0.0
    if reason == 'TRAIL':
        level_val = pos['max_fav'] * (1 - EXIT_CFG['trail_pct'] / 100.0)
        level_type = '移动止损线'
    elif reason == 'S':
        level_val = vwap[i] + K1 * atr[i]
        level_type = '触及上轨'
    elif reason == 'STOP':
        level_val = pos['stop_price'] if pos['stop_price'] > -1e8 else price
        level_type = '硬止损线'
    else:  # TIME
        level_val = price
        level_type = '超时强平'
    tag = f"[{pos.get('entry_reason','')}]" if pos.get('entry_reason') else ''
    return ('X', price, chg, level_val, level_type,
            rsi14[i], temp[i], vol_ratio[i], name, tag, reason)


def detect_for(sym, name, data, st):
    """v9 信号检测 + 出场管理跟踪（接 exit_manager）。

    算法判定统一走 indicators.check_*_trigger，与回测/selftest 一致。
    出场管理：B开仓→移动止损跟踪→S信号/回撤触发平仓并推送 EXIT（单仓位日内T）。
    """
    signals = []
    now = now_ts()
    today = datetime.now(CST).strftime('%Y%m%d')
    b_count = st.get(f'_b_count_{sym}_{today}', 0)
    s_count = st.get(f'_s_count_{sym}_{today}', 0)
    pc = STATE[sym]['PC']
    if pc <= 0:
        return signals

    c = data['c']; lo = data['lo']; vwap = data['vwap']; atr = data['atr']
    trend = data['trend']; rsi14 = data['rsi']; temp = data['temp']; vol_ratio = data['vol_ratio']
    n = data['n']; df = data['df']

    # 当前持仓（跨扫描/重启持久化在 st 中）
    pos = st.get(f'pos_{sym}')  # None 或 {'entry_price','entry_idx','max_fav','entry_reason','stop_price'}

    unchecked = 0; b_match = 0; s_match = 0
    for i in range(2, n):
        bar_key = f"bar_{sym}_{i}"
        if st.get(bar_key):
            continue
        unchecked += 1
        if atr[i] <= 0:
            st[bar_key] = now
            continue

        exited = False
        had_pos = pos is not None
        # ===== 持仓中：出场管理检查（优先级 硬止损 > S信号 > 移动止损 > 时间止损）=====
        if had_pos:
            if c[i] > pos['max_fav']:
                pos['max_fav'] = float(c[i])
            # 1) 硬止损（生产默认关）
            if EXIT_CFG['use_stop']:
                if EXIT_CFG['stop_mode'] == 'trend':
                    if trend is not None and trend[i] == -1:
                        signals.append(_mk_exit('STOP', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i))
                        pos = None; exited = True
                else:
                    if lo[i] <= pos['stop_price']:
                        signals.append(_mk_exit('STOP', name, pos['stop_price'], pos, vwap, atr, rsi14, temp, vol_ratio, i))
                        pos = None; exited = True
            # 2) S信号出场（自然目标，保留）
            if not exited and EXIT_CFG['s_signal_exit']:
                ts, rs = check_s_trigger(data, i)
                if ts:
                    signals.append(_mk_exit('S', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i))
                    pos = None; exited = True
            # 3) 移动止损（浮盈保护，生产核心杠杆）
            if not exited and EXIT_CFG['use_trailing']:
                fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                if fav_ret >= EXIT_CFG['trail_activate_pct']:
                    trail_stop = pos['max_fav'] * (1 - EXIT_CFG['trail_pct'] / 100.0)
                    if c[i] <= trail_stop and trail_stop > pos['stop_price']:
                        signals.append(_mk_exit('TRAIL', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i))
                        pos = None; exited = True
            # 4) 时间止损（超时强平）
            if not exited and EXIT_CFG['use_time'] and (i - pos['entry_idx']) >= EXIT_CFG['time_stop_bars']:
                signals.append(_mk_exit('TIME', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i))
                pos = None; exited = True

        # ===== 空仓：B建仓 / S提醒（仅本bar开头无持仓才走，避免出场bar重复推送）=====
        if not had_pos:
            tb, rb = check_b_trigger(data, i)
            if tb:
                b_match += 1
                if b_count < MAX_B_DAILY:
                    last = st.get(f'_cooldown_{sym}_B', 0)
                    if now - last >= COOLDOWN:
                        chg = (c[i] - pc) / pc * 100
                        st[f'_cooldown_{sym}_B'] = now
                        b_count += 1
                        lower_std = vwap[i] - K1 * atr[i]
                        tag = f'[{rb}]' if rb and rb != '回踩下轨' else ''
                        signals.append(('B', c[i], chg, lower_std, '触及下轨',
                                        rsi14[i], temp[i], vol_ratio[i], name, tag, ''))
                        # 开仓：进入出场管理跟踪（单仓位日内T）
                        pos = {'entry_price': float(c[i]), 'entry_idx': i,
                               'max_fav': float(c[i]), 'entry_reason': rb or '',
                               'stop_price': _compute_stop_price(float(c[i]), atr, i, EXIT_CFG)}
            ts, rs = check_s_trigger(data, i)
            if ts:
                s_match += 1
                if s_count < MAX_S_DAILY:
                    last = st.get(f'_cooldown_{sym}_S', 0)
                    if now - last >= COOLDOWN:
                        chg = (c[i] - pc) / pc * 100
                        st[f'_cooldown_{sym}_S'] = now
                        s_count += 1
                        upper_std = vwap[i] + K1 * atr[i]
                        tag = f'[{rs}]' if rs and rs != '反弹遇阻' else ''
                        signals.append(('S', c[i], chg, upper_std, '触及上轨',
                                        rsi14[i], temp[i], vol_ratio[i], name, tag, ''))
        else:
            # 持仓中：统计S匹配（日志用），不再发独立S推送（已由 EXIT 覆盖）
            ts, rs = check_s_trigger(data, i)
            if ts:
                s_match += 1

        st[bar_key] = now

    if unchecked > 0:
        print(f"  [{datetime.now(CST).strftime('%H:%M:%S')}] {name} n={n} unchecked={unchecked} "
              f"b_match={b_match} s_match={s_match} B={b_count}/{MAX_B_DAILY} S={s_count}/{MAX_S_DAILY}"
              f" 持仓={'Y' if pos else 'N'}")

    st[f'_b_count_{sym}_{today}'] = b_count
    st[f'_s_count_{sym}_{today}'] = s_count
    st[f'pos_{sym}'] = pos  # 持久化持仓（跨扫描/重启）
    return signals

def is_trading_today():
    now = datetime.now(CST)
    if now.weekday() >= 5:
        return False
    today_str = now.strftime('%Y-%m-%d')
    holidays_2026 = {
        '2026-01-01', '2026-01-02',
        '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
        '2026-02-02', '2026-02-03',
        '2026-04-06',
        '2026-05-01', '2026-05-04', '2026-05-05',
        '2026-06-19',
        '2026-09-25', '2026-09-28', '2026-09-29', '2026-09-30',
        '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
    }
    if today_str in holidays_2026:
        return False
    return True

def _log_event(msg):
    """诊断日志：写到文件（不依赖 stdout，避免 SYSTEM 会话 stdout 失效导致崩溃）。"""
    try:
        with open(os.path.join(BASE_DIR, 'logs', 'monitor_lifecycle.log'), 'a', encoding='utf-8') as _lf:
            _lf.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass

def run():
    lock_file = LOCK_FILE
    pid_file = PID_FILE
    # 获取锁；若被占用，尝试接管（同用户进程可用 os.kill 终止），否则让出
    while True:
        lf = open(lock_file, 'w')
        try:
            _acquire_lock(lf)
            break
        except (IOError, OSError):
            _log_event('LOCK_CONFLICT, attempting takeover')
            holder = None
            try:
                if os.path.exists(pid_file):
                    with open(pid_file) as _pf:
                        _c = _pf.read().strip()
                    if _c.isdigit():
                        holder = int(_c)
                        os.kill(holder, 15)  # SIGTERM/TerminateProcess（同用户可杀）
                        _log_event('takeover killed holder pid=%d' % holder)
            except ProcessLookupError:
                _log_event('takeover holder pid=%d already gone' % holder)
            except (PermissionError, OSError) as _e:
                _log_event('takeover cannot kill holder pid=%d (%r), yield' % (holder, _e))
                sys.exit(0)
            except Exception as _e:
                _log_event('takeover unexpected %r, yield' % _e)
                sys.exit(0)
            try: os.remove(lock_file)
            except Exception: pass
            try: os.remove(pid_file)
            except Exception: pass
            time.sleep(2)
            continue
    lf.write(str(os.getpid()))
    lf.flush()
    with open(pid_file, 'w') as pf:
        pf.write(str(os.getpid()))
    _log_event('LOCK_ACQUIRED pid=%d' % os.getpid())
    st = load_state()
    syms = list(TARGETS.keys())
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] v9 VWAP+ATR+趋势+量价+温度 启动 | {', '.join(TARGETS.values())}")

    if not is_trading_today():
        _log_event('EXIT not trading today')
        print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 非交易日, 退出")
        return

    first_scan_done = {sym: False for sym in syms}
    last_trading_check = datetime.now(CST).date()

    while True:
        now = datetime.now(CST)
        if now.date() != last_trading_check:
            last_trading_check = now.date()
        if now.weekday() >= 5 or not is_trading_today():
            print(f"[{now.strftime('%H:%M:%S')}] 📴 非交易日/周末, 退出")
            save_state(st)
            sys.exit(0)
        t = now.time()
        morning = t >= t.replace(hour=9, minute=25) and t <= t.replace(hour=11, minute=31)
        afternoon = t >= t.replace(hour=13, minute=0) and t <= t.replace(hour=15, minute=1)
        in_session = morning or afternoon
        if not in_session:
            if t > t.replace(hour=15, minute=1):
                _log_event('EXIT market close')
                save_state(st)
                print(f"[{now.strftime('%H:%M:%S')}] 📴 收盘退出")
                sys.exit(0)
            # 盘前/午休：仍持续写心跳，表明进程存活（消除飞书"未检测到心跳"盘前误报）
            try:
                write_metrics(0.0, 0, 0, 0, len(TARGETS))
            except Exception:
                pass
            time.sleep(30)
            continue

        today_str = now.strftime('%Y-%m-%d')
        if st.get('_daily_refreshed_date') != today_str:
            global tf
            from datasource import MootdxDataSource as TickFlow
            tf = TickFlow()
            print(f"[{now.strftime('%H:%M:%S')}] 🔄 TickFlow连接已重建 (新交易日)")
            refresh_daily()
            st['_daily_refreshed_date'] = today_str
            if not os.path.exists(SIGNAL_FILE):
                with open(SIGNAL_FILE, 'w') as f:
                    f.write(f"[{today_str}]\n")
            else:
                with open(SIGNAL_FILE, 'r') as f:
                    existing = f.read()
                if f'[{today_str}]' not in existing:
                    with open(SIGNAL_FILE, 'a') as f:
                        f.write(f"\n[{today_str}]\n")
            print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 📅 日K已刷新 " +
                  ', '.join(f"{TARGETS[s]}={STATE[s]['PC']:.2f}" for s in syms))

        batch = []
        loop_start = time.time(); err_count = 0; max_bar_ts = 0.0; outer_err = False
        try:
            # 1) 并发拉取数据：I/O 瓶颈（Mootdx intraday 请求）
            sym_data = {}
            max_workers = min(5, len(TARGETS))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_sym = {executor.submit(compute, sym): sym for sym in TARGETS}
                for future in as_completed(future_to_sym):
                    sym = future_to_sym[future]
                    name = TARGETS[sym]
                    try:
                        data = future.result()
                        if data:
                            sym_data[sym] = data
                        else:
                            print(f"  [warning] {name} no intraday data")
                    except Exception as e:
                        err_count += 1
                        print(f"  [warning] {name} compute exception: {e}")

            # 2) 顺序处理信号：避免共享状态 st 竞争
            for sym, name in TARGETS.items():
                data = sym_data.get(sym)
                if not data:
                    continue
                try:
                    try:
                        _bt = pd.to_datetime(data['df']['trade_time']).max().timestamp()
                        if _bt > max_bar_ts:
                            max_bar_ts = _bt
                    except Exception:
                        pass
                    if not first_scan_done[sym]:
                        first_scan_done[sym] = True
                        target_t = '13:00' if now.hour >= 13 else '09:30'
                        df = data['df']; n = data['n']
                        for idx in range(n):
                            if str(df['trade_time'].iloc[idx])[11:16] >= target_t:
                                for j in range(1, idx):
                                    st[f"bar_{sym}_{j}"] = now_ts()
                                break
                    sigs = detect_for(sym, name, data, st)
                    for s in sigs:
                        (sig_type, price, chg, level_val, level_type,
                         rsi, temp, vol_r, sig_name, tag, exit_reason) = s
                        msg = emit(sig_type, price, chg, level_val, level_type,
                                   rsi, temp, vol_r, sig_name, tag, exit_reason=exit_reason)
                        batch.append(msg)
                except Exception as e:
                    err_count += 1
                    print(f"  [warning] {name} process exception: {e}")
            if batch:
                print(f"  🔔 [{now.strftime('%H:%M:%S')}] 本轮信号 {len(batch)}条 → 推送")
                push_batch(batch)
            save_state(st)
            write_metrics(time.time() - loop_start, len(batch),
                          err_count + (1 if outer_err else 0), max_bar_ts, len(TARGETS))
            if not batch:
                print(f"  🔄 [{now.strftime('%H:%M:%S')}] 本轮无信号 ({len(TARGETS)}标的扫描完成)")
        except Exception as e:
            print(f"💥 [{now.strftime('%H:%M:%S')}] v9扫描崩溃: {e}")
            import traceback; traceback.print_exc()
            try:
                save_state(st)
            except:
                pass
            print(f"  🔄 10秒后恢复扫描...")
        time.sleep(30)

if __name__ == '__main__':
    _log_event('PROCESS_START argv=%s' % ' '.join(sys.argv[1:]))
    try:
        run()
    except Exception as _e:
        import traceback as _tb
        try:
            with open(os.path.join(BASE_DIR, 'logs', 'monitor_fatal.log'), 'a', encoding='utf-8') as _ff:
                _ff.write('[%s] FATAL %r\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), _e))
                _tb.print_exc(file=_ff)
                _ff.write('--- end ---\n')
        except Exception:
            pass
        _log_event('PROCESS_EXIT_FATAL %r' % _e)
        raise
    _log_event('PROCESS_EXIT_NORMAL')
