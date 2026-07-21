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
import os, sys, json, time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed  # 保留导入(备用), 但数据拉取已改为串行见下
import threading
from datasource import MootdxDataSource as TickFlow
from miji_alpha import compute_miji_indicators, check_b_trigger, check_s_trigger
from indicators import stars, K1
# 出场管理：接 exit_manager 的移动止损/硬止损/S信号出场（P0 待办）
from exit_manager import make_config

# ========== 路径配置化（跨平台，无需硬编码绝对路径） ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_version():
    """读 VERSION 文件，失败回退硬编码（与 VERSION 文件同步）。"""
    try:
        with open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return '9.1.4'
VERSION = _load_version()

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
# 监控标的列表（优先于持仓文件，实现"监控标的"与"持仓"解耦）
WATCHLIST_FILE = _cfg('watchlist_file', 'TP_WATCHLIST_FILE', os.path.join(BASE_DIR, 'data', 'watchlist.json'))
WEBHOOK_URL = _cfg('webhook_url', 'TP_WEBHOOK_URL', "https://open.feishu.cn/open-apis/bot/v2/hook/1d241455-447b-4017-b9a3-4ecb61912369")
# 锁文件放到项目 data/ 目录（跨会话共享，避免 SYSTEM 与用户会话 temp 不同导致锁失效）
LOCK_FILE = os.path.join(BASE_DIR, 'data', '.monitor.lock')
PID_FILE  = os.path.join(BASE_DIR, 'data', '.monitor.pid')
# 风控 Agent（模式②）写入的顶层闸门文件；缺失/过期/坏→NONE（放行，永不误伤生产做T）
RISK_OVERRIDE_FILE = _cfg('risk_override_file', 'TP_RISK_OVERRIDE',
                          os.path.join(BASE_DIR, 'data', 'risk_override.json'))

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

# 任务三/四：自由双向 + 动态仓位
COLDOWN_BARS = 3      # 同方向信号最小间隔(bar)，替代原墙钟秒级冷却（replay 单次 detect_for 下 now 冻结会导致仅首信号触发）
MAX_SIZE_PCT = 8       # 单标的累计仓位上限(成)
SCAN_INTERVAL = int(_cfg('scan_interval', 'TP_SCAN_INTERVAL', 15))   # 每轮扫描间隔(秒)，默认15；盘外心跳亦用此值

# ========== 静默零信号告警（2026-07-21 复盘新增：堵"数据中断静默吞信号"漏洞） ==========
# 某标的连续 N 轮（交易时段、过开盘宽限期后）无分钟K bar → 推信号群告警，避免像今日这样静默吞掉整日。
ALERT_MISS_ROUNDS = int(_cfg('alert_miss_rounds', 'TP_ALERT_MISS_ROUNDS', 6))   # 6×15s≈90s
ALERT_GRACE_MIN = int(_cfg('alert_grace_min', 'TP_ALERT_GRACE_MIN', 5))          # 开盘前后宽限分钟数
ALERT_WEBHOOK = _cfg('alert_webhook', 'TP_ALERT_WEBHOOK', WEBHOOK_URL)           # 默认信号群(已确认)
def strength_size(g_dev_pct, m_present):
    """按信号强度推导仓位(成)：强(偏离≥2% 或 含MACD背离)→4成，否则2成。"""
    strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
    return 4 if strong else 2

# ========== 监控标的 (沿用v8: 从持仓文件自动同步) ==========
def get_exchange(code):
    if code.startswith(('000','001','002','003','300','301')):
        return '.SZ'
    if code.startswith(('600','601','603','605','688')):
        return '.SH'
    return '.SZ'

def _limit_up_threshold(sym):
    """涨停阈值（日内最高涨幅≥此值即视为涨停 regime）。
    与 tickflow 前复权口径一致：PC 与 1m 同为前复权，日涨幅比值保真。
    主板 10% / 创业板(300/301)·科创板(688) 20% / 北交所(8/4/92) 30%。"""
    code = sym.split('.')[0]
    if code.startswith(('300','301','688')):
        return 0.20
    if code.startswith(('8','4','92')):
        return 0.30
    return 0.10

def load_targets():
    prompt_file = PROMPT_FILE
    try:
        with open(prompt_file, encoding='utf-8') as f:
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

# 监控标的加载：唯一数据源 = data/watchlist.json
# 2026-07-21 移除硬编码 5 持仓兜底：所有标的必须从此文件动态读取，
# 避免启动时读失败→整日扫描错误标的(实证: 10:28 161129 未被监控)。
def _load_watchlist():
    """从 data/watchlist.json 读取监控标的 {sym: name}。文件不存在或为空则报错退出。"""
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, encoding='utf-8') as f:
                wl = json.load(f)
            if wl and isinstance(wl, dict) and len(wl) > 0:
                return wl
    except Exception as e:
        print(f"❌ 读取 watchlist.json 失败: {e}")
    print("❌ watchlist.json 不存在、为空或格式错误。无法确定监控标的，monitor 无法启动。")
    print(f"   请在 {WATCHLIST_FILE} 中配置至少一个标的（如 {json.dumps({'161129.SZ': '原油LOF易方达'}, ensure_ascii=False)}）")
    return None

TARGETS = _load_watchlist()
if not TARGETS:
    # 延迟到 run() 启动时再次尝试（可能用户还没来得及写好配置）
    print("⚠️ 启动时未加载到标的，将在 run() 循环中周期重试 watchlist.json")
else:
    print(f"📋 监控标的来自 watchlist.json: {', '.join(TARGETS.values())}")

_EXCLUDE = {'920222.BJ', '920222.SZ'}
TARGETS = {k: v for k, v in TARGETS.items() if k not in _EXCLUDE}

tf = None  # lazy init: instantiated on first trading day inside run()

# ========== mootdx socket 串行锁 ==========
# 2026-07-20 修复：mootdx TdxHq_API 共享单 TCP socket 且 lock=None（未传 multithread=True）。
# ThreadPoolExecutor 并发 compute() 会导致 send/recv 交错 → 标的A收到标的B的响应数据
# （实证：161129.SZ 卡片显示现价309=688347.SH价位, +16934%涨幅）。
# 故对数据拉取加全局互斥锁，确保同一时刻只有一个线程在 socket 上做请求/响应往返。
_data_lock = threading.Lock()

# ========== 全局状态 ==========
STATE = {sym: {'PC': 0, 'WARM': None} for sym in (TARGETS or {})}

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


def now_ts():
    return datetime.now(CST).timestamp()

_last_push_ts = 0
MIN_PUSH_INTERVAL = 5

def push_batch(items, sim=False):
    """发推送。CARD_MODE: items 是 card dict 列表，逐条发 interactive 卡片；否则 items 是 text 列表，join 发 text。"""
    global _last_push_ts
    if not items:
        return
    now = time.time()
    wait = MIN_PUSH_INTERVAL - (now - _last_push_ts)
    if wait > 0:
        time.sleep(wait)
    if CARD_MODE:
        ok_all = True
        for item in items:
            print(f"  📡 PUSH(card) 准备: {str(item)[:60]}")
            try:
                r = requests.post(WEBHOOK_URL, json=item, timeout=5)
                _last_push_ts = time.time()
                resp = r.json()
                print(f"  📡 PUSH 响应: status={r.status_code} code={resp.get('code')} msg={resp.get('msg')}")
                if not (r.status_code == 200 and resp.get('code') == 0):
                    ok_all = False
                    print(f"  ⚠️ PUSH 失败: {r.text[:200]}")
            except Exception as e:
                ok_all = False
                print(f"  ⚠️ PUSH 异常: {e}")
        return ok_all
    text = '\n\n'.join(items)
    print(f"  📡 PUSH 准备: {len(items)}条, 内容前60字: {text[:60]}")
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

def _send_alert(text):
    """静默零信号 / 数据源中断告警 → 信号群 webhook（交易时段用户即时可见）。
    失败静默（不阻塞主循环）。"""
    try:
        r = requests.post(ALERT_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=5)
        resp = r.json()
        if not (r.status_code == 200 and resp.get('code') == 0):
            print(f"  ⚠️ 告警推送失败: {r.text[:120]}")
    except Exception as e:
        print(f"  ⚠️ 告警推送异常: {e}")

def compute(sym):
    """v9: 调算法层计算全部指标（数据拉取受 _data_lock 保护，防止 mootdx socket 串标）"""
    with _data_lock:  # 2026-07-20 fix: 互斥访问共享 mootdx TCP socket
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

    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data

def emit(sig_type, price, chg_pct, level_val, level_type, rsi, temp, vol_r, name, tag='', exit_reason='', day_chg=None, bar_trade_time='', pos_pct=None):
    """构造推送文本并写 signal.txt。v9.1.2: 加 [K:HH:MM] 信号K时刻 + EXIT 双口径(当日涨跌/持仓盈亏)。"""
    k_tag = f' [K:{bar_trade_time[11:16]}]' if bar_trade_time and len(bar_trade_time) >= 16 else ''
    # 出场管理推送（接 exit_manager）：B开仓后跟踪，TRAIL/S触发平仓提醒
    if sig_type == 'X':
        reason = f" [{exit_reason}]" if exit_reason else ''
        chg_sign = '+' if chg_pct >= 0 else ''
        day_sign = '+' if (day_chg or 0) >= 0 else ''
        day_str = f'{day_sign}{day_chg:.1f}%' if day_chg is not None else 'N/A'
        lines = [
            f"🔵 {name} EXIT{reason} {pos_pct if pos_pct is not None else POS_PCT}成{(' ' + tag) if tag else ''}{k_tag}",
            f"现价 {price:.2f}（当日 {day_str} / 持仓 {chg_sign}{chg_pct:.1f}%）",
            f"{level_type}{level_val:.2f} RSI={rsi:.1f} 温度={temp:.0f}"
        ]
        msg = '\n'.join(lines)
        print(msg)
        with open(SIGNAL_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now(CST).strftime('%H:%M:%S')}]{k_tag}\n{msg}\n\n")
        return msg
    emoji = '🟢' if sig_type == 'B' else '🔴'
    op_type = 'BUY' if sig_type == 'B' else 'SELL'
    chg_sign = '+' if chg_pct >= 0 else ''
    star = stars(sig_type, temp, vol_r)
    lines = [
        f"{emoji} {name} {op_type} {pos_pct if pos_pct is not None else POS_PCT}成 {star}{(' ' + tag) if tag else ''}{k_tag}",
        f"现价 {price:.2f}（{chg_sign}{chg_pct:.1f}%）",
        f"{level_type}{level_val:.2f} RSI={rsi:.1f} 温度={temp:.0f}"
    ]
    msg = '\n'.join(lines)
    print(msg)
    with open(SIGNAL_FILE, 'a') as f:
        f.write(f"[{datetime.now(CST).strftime('%H:%M:%S')}]{k_tag}\n{msg}\n\n")
    return msg

# ========= 飞书交互卡片（v9.1.2 重建，对齐测试群买卖卡片模板） =========
CARD_MODE = True   # True: 发 interactive 卡片；False: 回退纯文本 emit
POS_PCT = 3         # 单次做T仓位（成），替代原硬编码"3成"

def _map_sample(sig_type, tag):
    """样例标签映射：B+均线引力→回踩支撑 / S+均线引力→反弹遇阻 / MACD→背离。"""
    t = (tag or '').strip('[]')
    if sig_type == 'B':
        if '均线引力' in t: return '回踩支撑'
        if 'MACD' in t or '绿柱' in t: return '背离企稳'
    elif sig_type == 'S':
        if '均线引力' in t: return '反弹遇阻'
        if 'MACD' in t or '红柱' in t: return '背离见顶'
    return t if t else '—'

def emit_card(s, sym=None, sim=False):
    """构造飞书 interactive 卡片（v9.1.3 精简版）。
    正文仅留 4 项：①标的·操作·仓位 ②操作点位 ③操作依据 ④信号时间戳；
    其余调试参数（RSI/温度/量比/距触发%/原tag）折叠到卡片底部「备注」灰显。
    s = 13元组。配色：买入=绿 / 卖出=红 / 出场=蓝（与用户约定一致）。
    """
    sig_type, price, chg, level_val, level_type, rsi, temp, vol_r, name, tag, exit_reason, day_chg, bar_tt = s[:13]
    pos_pct = s[13] if len(s) >= 14 else POS_PCT
    is_b, is_s, is_x = sig_type == 'B', sig_type == 'S', sig_type == 'X'
    # 标题 + 配色（用户约定：买绿 / 卖红 / 出场蓝）
    code = (sym.split('.')[0] if sym else (name or ''))
    if is_b:
        op, color = '买入', 'green'
    elif is_s:
        op, color = '卖出', 'red'
    else:
        if exit_reason in ('STOP', 'TRAIL', 'TIME'):
            op, color = '止损', 'blue'
        elif exit_reason == 'B':   # 空仓回补 = 买回
            op, color = '买入', 'green'
        else:                     # exit_reason == 'S' 平多 = 卖平
            op, color = '卖出', 'red'
    title = f'{code} {op} {pos_pct}成'
    star = stars(sig_type, temp, vol_r)
    chg_sign = '+' if chg >= 0 else ''
    sample = _map_sample(sig_type, tag)
    bt = bar_tt[11:16] if bar_tt and len(bar_tt) >= 16 else ''
    # 行1：标的·操作｜做T仓位 ★（op 已在上方按 sig_type+exit_reason 智能判定，勿覆盖）
    line1 = f"{name}·{op}｜做T·{pos_pct}成 {star}"
    # 行2：操作点位（买卖用动态 level_type；出场双口径）
    if is_x:
        day_sign = '+' if (day_chg or 0) >= 0 else ''
        day_str = f'{day_sign}{day_chg:.1f}%' if day_chg is not None else 'N/A'
        reason = f" [{exit_reason}]" if exit_reason else ''
        line2 = f"现价 {price:.2f}（当日 {day_str} / 持仓 {chg_sign}{chg:.1f}%）{reason}"
    else:
        line2 = f"现价 {price:.2f}（{chg_sign}{chg:.1f}%）｜{level_type} {level_val:.2f}"
    # 行3：操作依据
    line3 = f"依据：{sample}"
    # 行4：信号K时间戳
    line4 = f"信号K：{bt}"
    # 备注（底部折叠，灰显）：调试参数
    trigger_pct = (level_val - price) / price * 100 if price > 0 else 0.0
    trig_sign = '+' if trigger_pct >= 0 else ''
    footer = (f"RSI={rsi:.0f} 温={temp:.0f} 量比={vol_r:.1f} "
              f"距触发{trig_sign}{trigger_pct:.1f}% 原tag=\"{tag}\" ｜ "
              f"v9 ({VERSION})·SIM·仅供参考非投资建议")
    if sim:
        footer += " [SIM]"
    md = lambda t: {"tag": "lark_md", "content": t}
    elements = [
        {"tag": "div", "text": md(f"**{line1}**")},
        {"tag": "div", "text": md(line2)},
        {"tag": "div", "text": md(line3)},
        {"tag": "div", "text": md(line4)},
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"template": color, "title": {"tag": "plain_text", "content": title[:100]}},
            "elements": elements,
        },
    }

def emit_signal(s, sym=None, sim=False):
    """dispatch：CARD_MODE→卡片，否则纯文本 fallback。返回 msg_or_card。sym 用于卡片标题(标的代码)。"""
    if CARD_MODE:
        return emit_card(s, sim=sim)
    return emit(*s)

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            s = json.load(f)
        saved_date = s.get('_daily_refreshed_date', '')
        today = datetime.now(CST).strftime('%Y-%m-%d')
        if saved_date and saved_date != today:
            keys_to_remove = [k for k in s.keys() if k.startswith('bar_') or k.startswith('_cooldown_') or k.startswith('pos_') or k.startswith('_miss_') or k.startswith('alerted_miss_')]
            for k in keys_to_remove:
                del s[k]
            s['_daily_refreshed_date'] = None
        return s
    except:
        return {}

def save_state(s):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f)

def write_metrics(duration_s, signals, errors, last_bar_ts, symbols):
    """每轮扫描末写入 metrics.json，供告警引擎(watchdog)采集。
    包含：扫描耗时 / 本轮信号数 / 本轮错误数 / 最新行情棒时间 / 标的数。
    2026-07-20 fix: 改用原子写入（先写 .tmp 再 os.replace），消除 Windows 文件锁竞争导致
    alert_engine 读不到 metrics.json 而误报「未检测到心跳文件」的问题。失败静默。"""
    try:
        import tempfile as _tf
        _dir = os.path.dirname(METRICS_FILE) or '.'
        _fd, _tmp = _tf.mkstemp(suffix='.tmp', dir=_dir, prefix='metrics_')
        try:
            with os.fdopen(_fd, 'w', encoding='utf-8') as f:
                json.dump({
                    'ts': time.time(),
                    'scan_duration_s': round(duration_s, 3),
                    'signals': signals,
                    'errors': errors,
                    'symbols': symbols,
                    'last_bar_ts': last_bar_ts if last_bar_ts else None,
                    'status': 'running',
                }, f)
            os.replace(_tmp, METRICS_FILE)  # 原子替换（同目录）
        except Exception:
            # 写 tmp 失败时清理临时文件
            try: os.unlink(_tmp)
            except OSError: pass
    except Exception:
        pass  # 外层兜底：磁盘/权限等不可恢复异常静默吞掉，不阻塞主循环

def _compute_stop_price(entry_price, atr, entry_idx, cfg):
    """硬止损价（仅 use_stop 时有意义）；关闭时返回 -inf，不影响移动止损比较。"""
    if not cfg['use_stop']:
        return -1e9
    return entry_price - cfg['stop_atr_mult'] * atr[entry_idx]


def _mk_exit(reason, name, price, pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times):
    """构造一条 EXIT 信号元组（13元组）。v9.1.2: 加 day_chg(当日涨跌) + bar_trade_time。
    v9.1.2-trend: 支持 side 对称（多仓/空仓）+ 'B'回补 reason。"""
    side = pos.get('side', 'long')
    entry = pos['entry_price']
    if side == 'long':
        chg = (price - entry) / entry * 100 if entry > 0 else 0.0       # 多仓盈亏
    else:
        chg = (entry - price) / entry * 100 if entry > 0 else 0.0       # 空仓盈亏(卖高-买低)
    day_chg = (price - pc) / pc * 100 if pc > 0 else 0.0         # 当日涨跌幅
    if reason == 'TRAIL':
        if side == 'long':
            level_val = pos['max_fav'] * (1 - EXIT_CFG['trail_pct'] / 100.0)
        else:
            level_val = pos['max_fav'] * (1 + EXIT_CFG['trail_pct'] / 100.0)
        level_type = '移动止损线'
    elif reason in ('S', 'B'):   # 多仓平多 / 空仓回补：按 price 相对 VWAP±K1·ATR 实际位置动态标注
        upper = vwap[i] + K1 * atr[i]
        lower = vwap[i] - K1 * atr[i]
        if price >= upper:
            level_val, level_type = upper, '触及上轨'
        elif price <= lower:
            level_val, level_type = lower, '触及下轨'
        else:
            level_val, level_type = price, '区间内'
    elif reason == 'STOP':
        level_val = pos['stop_price'] if pos['stop_price'] > -1e8 else price
        level_type = '硬止损线'
    else:  # TIME
        level_val = price
        level_type = '超时强平'
    tag = f"[{pos.get('entry_reason','')}]" if pos.get('entry_reason') else ''
    bt = str(trade_times[i]) if trade_times is not None and i < len(trade_times) else ''
    return ('X', price, chg, level_val, level_type,
            rsi14[i], temp[i], vol_ratio[i], name, tag, reason, day_chg, bt)


def _load_risk_override():
    """读取风控闸门文件；缺失/过期/坏 → 'NONE'（放行，永不误伤生产做T）。"""
    try:
        if not os.path.exists(RISK_OVERRIDE_FILE):
            return 'NONE'
        with open(RISK_OVERRIDE_FILE, encoding='utf-8') as f:
            o = json.load(f)
        exp = o.get('expires_at')
        if exp:
            try:
                exp_dt = datetime.strptime(exp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
                if datetime.now(CST) > exp_dt:
                    return 'NONE'
            except Exception:
                pass
        act = o.get('action', 'NONE')
        if act in ('HALT_BUY', 'FORCE_SELL', 'ALLOW_BUY', 'NONE'):
            return act
        return 'NONE'
    except Exception:
        return 'NONE'


def _risk_gate(sym, name, data, st, sigs, action):
    """模式② 顶层闸门：把风控 action 套在 miji_alpha 之上（不影响入场点算法）。
    - HALT_BUY / FORCE_SELL：跳过所有 B（新开/加仓）信号；
    - FORCE_SELL：对已持仓强制生成清仓 EXIT（信号形状与算法自然出场完全一致）；
    - NONE / ALLOW_BUY：完全不变。
    返回（可能过滤/追加后的）sigs。"""
    if action in ('HALT_BUY', 'FORCE_SELL'):
        sigs = [s for s in sigs if s and s[0] != 'B']
    if action == 'FORCE_SELL':
        # 已有自然出场则不再重复强制
        if not any(s and s[0] in ('X', 'S') for s in sigs):
            pos = st.get(f'pos_{sym}')
            if pos is not None:
                try:
                    c = data['c']; vwap = data['vwap']; atr = data['atr']
                    rsi14 = data['rsi']; temp = data['temp']; vol_ratio = data['vol_ratio']
                    n = data['n']
                    pc = STATE[sym]['PC']
                    df = data.get('df')
                    trade_times = df['trade_time'].values if df is not None else None
                    i = max(2, n - 1)
                    price = c[i]
                    sz = pos['size_pct']
                    sigs.append(_mk_exit('S', name, price, pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                    print(f"  🛑 [risk-gate] FORCE_SELL 已对 {name} 强制生成清仓信号")
                except Exception as e:
                    print(f"  [risk-gate] FORCE_SELL 构造失败 {name}: {e}")
    return sigs


def detect_for(sym, name, data, st):
    """v9 信号检测 + 出场管理（v9.1.3+：自由双向 / 动态仓位 / 持续监控）。

    - 取消严格 B/S 交替配对（任务三）：每根 bar 独立评估买卖；同侧累加仓位，
      反向强信号按持仓规模平仓（如两笔2成买入→多4成，遇强卖出→提示卖出4成）。
    - 仓位不固定模板，由信号强度动态推导（strength_size）。
    - 监控全时段持续（任务四）：不限制每日仅一次完整周期；以 bar 索引冷却放行
      所有有效波动点，replay 单次 detect_for 下也能连续触发。
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
    trade_times = df['trade_time'].values if df is not None else None

    # 当前持仓（跨扫描/重启持久化在 st 中）；新增 size_pct 累计仓位(成)
    pos = st.get(f'pos_{sym}')  # None 或 {'side','entry_price','entry_idx','max_fav','entry_reason','stop_price','size_pct'}

    run_hi_max = -1e9   # 当日截至当前 bar 的最高价（涨停 regime 判定，A2）
    for i in range(2, n):
        bar_key = f"bar_{sym}_{i}"
        if st.get(bar_key):
            continue
        if atr[i] <= 0:
            st[bar_key] = now
            continue
        run_hi_max = max(run_hi_max, data['h'][i])
        near_limit_up = ((run_hi_max - pc) / pc >= _limit_up_threshold(sym)) if pc > 0 else False

        # ===== 持仓中：出场管理（硬止损 > 反向信号 > 移动止损 > 时间止损） =====
        if pos is not None:
            side = pos['side']
            if side == 'long':
                if c[i] > pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            else:
                if c[i] < pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            sz = pos['size_pct']
            exited = False
            # 1) 硬止损（生产默认关）
            if not exited and EXIT_CFG['use_stop']:
                if EXIT_CFG['stop_mode'] == 'trend':
                    if trend is not None and ((side == 'long' and trend[i] == -1) or (side == 'short' and trend[i] == 1)):
                        signals.append(_mk_exit('STOP', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos = None; exited = True
                else:
                    if side == 'long' and lo[i] <= pos['stop_price']:
                        signals.append(_mk_exit('STOP', name, pos['stop_price'], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos = None; exited = True
                    elif side == 'short' and c[i] >= pos['stop_price']:
                        signals.append(_mk_exit('STOP', name, pos['stop_price'], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos = None; exited = True
            # 2) 反向信号自然平仓
            if not exited and EXIT_CFG['s_signal_exit']:
                if side == 'long':
                    ts, rs = check_s_trigger(data, i)
                    if ts:
                        signals.append(_mk_exit('S', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos = None; exited = True
                else:
                    tb, rb = check_b_trigger(data, i)
                    if tb:
                        signals.append(_mk_exit('B', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos = None; exited = True
            # 3) 移动止损（浮盈保护；多仓/空仓对称）
            if not exited and EXIT_CFG['use_trailing']:
                if side == 'long':
                    fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 - EXIT_CFG['trail_pct'] / 100.0)
                        if c[i] <= trail_stop and trail_stop > pos['stop_price']:
                            signals.append(_mk_exit('TRAIL', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                            pos = None; exited = True
                else:
                    fav_ret = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 + EXIT_CFG['trail_pct'] / 100.0)
                        if c[i] >= trail_stop and trail_stop < pos['stop_price']:
                            signals.append(_mk_exit('TRAIL', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                            pos = None; exited = True
            # 4) 时间止损
            if not exited and EXIT_CFG['use_time'] and (i - pos['entry_idx']) >= EXIT_CFG['time_stop_bars']:
                signals.append(_mk_exit('TIME', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                pos = None; exited = True
            st[bar_key] = now
            continue

        # ===== 空仓：自由双向 + 动态仓位（任务三/四） =====
        tb, rb = check_b_trigger(data, i)
        ts, rs = check_s_trigger(data, i)
        if not (tb or ts):
            st[bar_key] = now
            continue
        # 买入 / 开多（或加多 / 平空回补）
        if tb:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rb or ''))
            last_b = st.get(f'_cooldown_{sym}_B', -9999)
            if s_pct > 0 and (i - last_b) >= COLDOWN_BARS and b_count < MAX_B_DAILY:
                st[f'_cooldown_{sym}_B'] = i
                b_count += 1
                chg = (c[i] - pc) / pc * 100
                tag = f'[{rb}]' if rb and rb != '回踩下轨' else ''
                lower_std = vwap[i] - K1 * atr[i]
                if pos is None:
                    signals.append(('B', c[i], chg, lower_std, '触及下轨',
                                    rsi14[i], temp[i], vol_ratio[i], name, tag, '', chg, str(trade_times[i]) if trade_times is not None else '', s_pct))
                    pos = {'side': 'long', 'entry_price': float(c[i]), 'entry_idx': i,
                            'max_fav': float(c[i]), 'entry_reason': rb or '',
                            'stop_price': _compute_stop_price(float(c[i]), atr, i, EXIT_CFG), 'size_pct': s_pct}
                elif pos['side'] == 'long':   # 加仓（累加同侧）
                    add = min(s_pct, MAX_SIZE_PCT - pos['size_pct'])
                    if add > 0:
                        ns = pos['size_pct'] + add
                        pos['entry_price'] = (pos['entry_price'] * pos['size_pct'] + c[i] * add) / ns
                        pos['max_fav'] = max(pos['max_fav'], float(c[i]))
                        pos['size_pct'] = ns
                        signals.append(('B', c[i], chg, lower_std, '触及下轨',
                                        rsi14[i], temp[i], vol_ratio[i], name, tag, '', chg, str(trade_times[i]) if trade_times is not None else '', add))
                else:   # 空仓中遇B → 平空回补（买入），按 min(信号强度, 空仓规模)
                    sz = min(s_pct, pos['size_pct'])
                    if sz > 0:
                        chg2 = (pos['entry_price'] - c[i]) / pos['entry_price'] * 100
                        signals.append(_mk_exit('B', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos['size_pct'] -= sz
                        if pos['size_pct'] <= 0:
                            pos = None
        # 卖出 / 开空（或加空 / 平多），对称；涨停 regime 抑制开空（A2）
        if ts:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rs or ''))
            last_s = st.get(f'_cooldown_{sym}_S', -9999)
            if s_pct > 0 and (i - last_s) >= COLDOWN_BARS and s_count < MAX_S_DAILY and not near_limit_up:
                st[f'_cooldown_{sym}_S'] = i
                s_count += 1
                chg = (c[i] - pc) / pc * 100
                tag = f'[{rs}]' if rs and rs != '反弹遇阻' else ''
                upper_std = vwap[i] + K1 * atr[i]
                if pos is None:
                    signals.append(('S', c[i], chg, upper_std, '触及上轨',
                                    rsi14[i], temp[i], vol_ratio[i], name, tag, '', chg, str(trade_times[i]) if trade_times is not None else '', s_pct))
                    pos = {'side': 'short', 'entry_price': float(c[i]), 'entry_idx': i,
                            'max_fav': float(c[i]), 'entry_reason': rs or '',
                            'stop_price': _compute_stop_price(float(c[i]), atr, i, EXIT_CFG), 'size_pct': s_pct}
                elif pos['side'] == 'short':   # 加仓（累加同侧）
                    add = min(s_pct, MAX_SIZE_PCT - pos['size_pct'])
                    if add > 0:
                        ns = pos['size_pct'] + add
                        pos['entry_price'] = (pos['entry_price'] * pos['size_pct'] + c[i] * add) / ns
                        pos['max_fav'] = min(pos['max_fav'], float(c[i]))
                        pos['size_pct'] = ns
                        signals.append(('S', c[i], chg, upper_std, '触及上轨',
                                        rsi14[i], temp[i], vol_ratio[i], name, tag, '', chg, str(trade_times[i]) if trade_times is not None else '', add))
                else:   # 多仓中遇S → 平多（卖出），按 min(信号强度, 多仓规模)
                    sz = min(s_pct, pos['size_pct'])
                    if sz > 0:
                        chg2 = (c[i] - pos['entry_price']) / pos['entry_price'] * 100
                        signals.append(_mk_exit('S', name, c[i], pos, vwap, atr, rsi14, temp, vol_ratio, i, pc, trade_times) + (sz,))
                        pos['size_pct'] -= sz
                        if pos['size_pct'] <= 0:
                            pos = None
        st[bar_key] = now

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

def _is_process_alive(pid):
    """检查 pid 是否仍在运行（同用户/跨用户均只查存在性，不杀）。"""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # Windows: 检查进程是否存在；Unix: 同语义
        return True
    except (ProcessLookupError, OSError, PermissionError):
        return False


def _remove_if_exists(path):
    """安全删除文件，忽略不存在的错误。"""
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except (FileNotFoundError, OSError, PermissionError):
        pass
    return False


def _clear_stale_lock(lock_file, pid_file):
    """若锁文件/PID 文件指向的进程已死，或文件已残留，则清理。"""
    # 1. 检查 PID 文件中的进程是否存活
    holder = None
    try:
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as _pf:
                _c = _pf.read().strip()
            if _c.isdigit():
                holder = int(_c)
    except Exception:
        pass

    # 2. 若 holder 已死，或未指定 PID 但锁文件存在，则尝试清理
    if holder is not None and not _is_process_alive(holder):
        _log_event('STALE_LOCK holder=%d gone, cleaning' % holder)
        _remove_if_exists(pid_file)
        _remove_if_exists(lock_file)
        return True
    if holder is None and os.path.exists(lock_file):
        # 没有 PID 文件但锁文件残留，也清理
        _log_event('STALE_LOCK no pid file, cleaning lock_file')
        _remove_if_exists(lock_file)
        return True
    return False


def _warmup_tf():
    """启动预热并校验 tf 连通性（2026-07-21 复盘改进：封初始化窗口）。
    强制触发连接 + 校验（对标 datasource._server_ok）；失败指数退避重试；
    全失败返回 False，交由静默告警感知，不退出进程（避免与自启机制冲突）。"""
    global tf
    max_tries = 3
    for attempt in range(max_tries):
        try:
            tf = TickFlow()
            _ = tf.client  # 强制建立 mootdx 连接
            ok = tf.klines.get('600519.SH', period='1d', count=1, as_dataframe=True)
            if ok is not None and len(ok) > 0:
                print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] ✅ tf 预热成功（数据源连通）")
                return True
            print(f"  ⚠️ tf 预热: 连接建立但无数据(retry {attempt+1}/{max_tries})")
        except Exception as e:
            print(f"  ⚠️ tf 预热失败(retry {attempt+1}/{max_tries}): {e}")
        if attempt < max_tries - 1:
            time.sleep(min(1.0 * (2 ** attempt), 7.0))
    _log_event('TF_WARMUP_FAILED all retries exhausted')
    return False


def run():
    global tf, TARGETS, STATE
    lock_file = LOCK_FILE
    pid_file = PID_FILE
    # 获取锁；若被占用，先检查 stale 再尝试接管，避免无限循环
    max_attempts = 10
    attempt = 0
    lf = None
    while attempt < max_attempts:
        attempt += 1
        # 先清理 stale lock（holder 进程已死或文件残留）
        _clear_stale_lock(lock_file, pid_file)

        try:
            lf = open(lock_file, 'w')
        except Exception as _e:
            _log_event('LOCK_OPEN_FAIL %r, retry' % _e)
            time.sleep(1)
            continue

        try:
            _acquire_lock(lf)
            break
        except (IOError, OSError):
            _log_event('LOCK_CONFLICT attempt=%d, yielding' % attempt)
            try:
                lf.close()
            except Exception:
                pass
            time.sleep(1)
    else:
        _log_event('LOCK_FAILED after %d attempts, exit' % max_attempts)
        sys.exit(1)

    lf.write(str(os.getpid()))
    lf.flush()
    with open(pid_file, 'w') as pf:
        pf.write(str(os.getpid()))
    _log_event('LOCK_ACQUIRED pid=%d' % os.getpid())

    # 注册退出清理：任何退出路径（return / sys.exit / 异常）都释放锁 + 清理 pid 文件
    # 关键：Windows 下必须先关闭文件句柄，再删除文件，否则 os.remove 会因"文件被占用"失败。
    import atexit
    def _cleanup_lock():
        try:
            _release_lock(lf)
        except Exception:
            pass
        try:
            lf.close()
        except Exception:
            pass
        _remove_if_exists(lock_file)
        _remove_if_exists(pid_file)
        _log_event('LOCK_RELEASED pid=%d' % os.getpid())
    atexit.register(_cleanup_lock)

    st = load_state()
    # 2026-07-21 复盘改进：启动即预热并校验 tf 连通性（封初始化窗口）
    tf_ok = _warmup_tf()
    st['_tf_unhealthy'] = (not tf_ok)
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
            # 盘前/午休/收盘后：持续写心跳，表明进程存活。
            # 消除飞书"未检测到心跳/服务中断"在非交易时段误报；
            # 真实崩溃仍由心跳停滞(>service_stale_s)触发，语义不变。
            if t > t.replace(hour=15, minute=1):
                _log_event('post-close keepalive (heartbeat only, no scan)')
            try:
                save_state(st)
                write_metrics(0.0, 0, 0, 0, len(TARGETS))
            except Exception:
                pass
            time.sleep(SCAN_INTERVAL)
            continue

        today_str = now.strftime('%Y-%m-%d')
        # 2026-07-21 fix: 确保 tf 必被初始化。
        # 旧逻辑仅在「当日首次刷新」时初始化 tf；若 monitor 在当日刷新之后才重启
        # （如崩溃修复后重启），_daily_refreshed_date 已是今日，tf 会一直保持 None
        # → 所有 compute 抛 'NoneType' object has no attribute 'klines'，全天零信号。
        if tf is None:
            try:
                tf = TickFlow()
                print(f"[{now.strftime('%H:%M:%S')}] 🔄 TickFlow连接初始化 (恢复 tf)")
            except Exception as e:
                print(f"  ⚠️ TickFlow初始化失败: {e}; 本轮跳过")
                if not st.get('_alerted_tf_down'):
                    st['_alerted_tf_down'] = True
                    _send_alert(f"🚨 数据源初始化失败：TickFlow 无法创建（{e}）。"
                                f"monitor 将持续跳过扫描直到恢复，请检查 mootdx 连接。")
                time.sleep(SCAN_INTERVAL)
                continue
        else:
            st.pop('_alerted_tf_down', None)

        # 周期重载 watchlist.json（唯一标的来源）
        # 启动时若文件不存在→空 TARGETS，每轮重试；正常后每轮检测变化
        _wl = _load_watchlist()
        if _wl and _wl != TARGETS:
            old_syms = set(TARGETS or {})
            new_syms = set(_wl)
            TARGETS = _wl
            for s in new_syms - old_syms:
                STATE[s] = {'PC': 0, 'WARM': None}
            added = new_syms - old_syms
            removed = old_syms - new_syms
            if added:
                print(f"📋 watchlist 新增: {', '.join(f'{TARGETS[s]}({s})' for s in added)}")
            if removed:
                print(f"📋 watchlist 移除: {', '.join(s for s in removed)}")
        elif not TARGETS and not _wl:
            pass  # 文件仍不可用，下一轮继续尝试
        if st.get('_daily_refreshed_date') != today_str:
            refresh_daily()
            st['_daily_refreshed_date'] = today_str
            if not os.path.exists(SIGNAL_FILE):
                with open(SIGNAL_FILE, 'w', encoding='utf-8') as f:
                    f.write(f"[{today_str}]\n")
            else:
                with open(SIGNAL_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    existing = f.read()
                if f'[{today_str}]' not in existing:
                    with open(SIGNAL_FILE, 'a', encoding='utf-8') as f:
                        f.write(f"\n[{today_str}]\n")
            print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 📅 日K已刷新 " +
                  ', '.join(f"{TARGETS[s]}={STATE[s]['PC']:.2f}" for s in syms))

        batch = []
        loop_start = time.time(); err_count = 0; max_bar_ts = 0.0; outer_err = False
        try:
            # 1) 并发拉取数据：I/O 瓶颈（Mootdx intraday 请求）
            #     ⚠️ 2026-07-20 fix: compute() 内部已加 _data_lock 互斥 mootdx socket，
            #     所以 ThreadPoolExecutor 的并发度只影响"等待 I/O"的线程调度开销，
            #     实际 socket 操作是串行的。若后续标的增多导致锁竞争明显，
            #     可直接改 max_workers=1（纯串行）消除线程创建开销。
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

            # 静默零信号检测：本轮未取到 bar 的标的计数（堵"数据中断静默吞信号"）
            if sym_data:
                st['_tf_unhealthy'] = False
            for sym in TARGETS:
                if sym in sym_data:
                    st[f'_miss_{sym}'] = 0
                    st.pop(f'alerted_miss_{sym}', None)  # 数据恢复 → 清除去抖锁
                else:
                    st[f'_miss_{sym}'] = st.get(f'_miss_{sym}', 0) + 1
            # 告警（仅交易时段 + 过开盘宽限期，去抖避免刷屏）
            def _in_grace(tt):
                g = ALERT_GRACE_MIN
                return (t.replace(hour=9, minute=30) <= tt < t.replace(hour=9, minute=30 + g)) or \
                       (t.replace(hour=13, minute=0) <= tt < t.replace(hour=13, minute=5))
            if not _in_grace(now.time()):
                for sym in TARGETS:
                    m = st.get(f'_miss_{sym}', 0)
                    key_a = f'alerted_miss_{sym}'
                    if m >= ALERT_MISS_ROUNDS and not st.get(key_a):
                        st[key_a] = True
                        nm = TARGETS[sym]
                        _send_alert(
                            f"⚠️ 数据源中断告警：{nm}({sym}) 已连续 {m} 轮"
                            f"(约{m*SCAN_INTERVAL}秒) 无分钟K数据，疑似盘中数据源中断，"
                            f"已静默跳过该标的信号。请检查 mootdx/腾讯行情连接。"
                        )
                        print(f"  🚨 已推送静默零信号告警 {nm}({sym}) miss={m}")

            # 2) 顺序处理信号：避免共享状态 st 竞争
            override_action = _load_risk_override()  # 模式② 顶层风控闸门（每轮读一次）
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
                        sigs = _risk_gate(sym, name, data, st, detect_for(sym, name, data, st), override_action)
                        for s in sigs:
                            msg = emit_signal(s, sym=sym)
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
            outer_err = True
            print(f"💥 [{now.strftime('%H:%M:%S')}] v9扫描崩溃: {e}")
            import traceback; traceback.print_exc()
            try:
                save_state(st)
            except:
                pass
            # 2026-07-21 P1: 崩溃也写入 metrics，闭合 errors 计数（避免指标盲区）。
            # 否则 write_metrics 在 try 内被跳过，errors 恒为 False，
            # 自检/看门狗只能靠心跳停滞间接推断崩溃，无法感知"活着但每轮崩"。
            try:
                write_metrics(time.time() - loop_start, 0,
                              err_count + 1, max_bar_ts, len(TARGETS))
            except Exception:
                pass
            print(f"  🔄 {SCAN_INTERVAL}秒后恢复扫描...")
        time.sleep(SCAN_INTERVAL)

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
