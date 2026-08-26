# -*- coding: utf-8 -*-
"""scripts/loop_engine/core.py — loop engineering 自迭代系统公共层

职责：git 封装 / 飞书推送 / 防重入锁 / 状态持久化 / 日志。
全部为纯标准库（urllib/subprocess/threading），可被 loop_engine.py 与各 stage 复用。

设计对齐现有基建：
  - push() 复用 daily_iterate.py 的 webhook 模式（a35d7f52 自迭代报告群）
  - git() 封装 subprocess（ROOT 内执行），push 用 .local/ssh/id_ed25519 代推
  - 锁文件 data/.loop_engine.lock（与 monitor 单实例锁同模式，防并发重入）
  - 状态文件 scripts/loop_engine/loop_state.json（断点续跑）
"""
import json
import os
import subprocess
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(LE_DIR, 'loop_state.json')
LOCK_FILE = os.path.join(ROOT, 'data', '.loop_engine.lock')
LOG_FILE = os.path.join(LE_DIR, 'loop_engine.log')

# 自迭代报告群 webhook（与 daily_iterate.py 一致）
HOOK_A35D7F52 = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
# 全局状态群（任务状态推送；规则二）
HOOK_GLOBAL = 'https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103'

SSH_KEY = os.path.join(ROOT, '.local', 'ssh', 'id_ed25519')

_lock = threading.Lock()
_lock_fh = None


# ========== 日志 ==========

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


# ========== 飞书推送（纯标准库） ==========

def push(text, hook=HOOK_A35D7F52):
    """推文本到飞书 webhook。返回响应文本；失败抛异常由调用方兜底。"""
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(hook, data=payload,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', 'replace')


def push_safe(text, hook=HOOK_A35D7F52):
    """推送不抛异常（失败仅记录日志），主链路不因推送失败中断。"""
    try:
        return push(text, hook=hook)
    except Exception as e:
        log(f'push failed: {e}')
        return None


# ========== git 封装 ==========

def git(*args, env_extra=None):
    """ROOT 内执行 git。push 场景默认注入 GIT_SSH_COMMAND 指向本地 key。"""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(['git', '-C', ROOT] + list(args),
                       capture_output=True, text=True, encoding='utf-8', env=env,
                       timeout=120)
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def git_push(*args):
    """push 专用：注入 SSH key。"""
    env = {'GIT_SSH_COMMAND': f'ssh -i {SSH_KEY} -o StrictHostKeyChecking=no '
                              f'-o BatchMode=yes -o ConnectTimeout=15'}
    return git(*args, env_extra=env)


# ========== 防重入锁 ==========

def acquire_lock(timeout=60):
    """拿 data/.loop_engine.lock 独占锁（互斥文件锁语义），防并发重入。
    返回 True=拿锁成功；False=超时（有另一实例在跑）。"""
    global _lock_fh
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _lock_fh = open(LOCK_FILE, 'x', encoding='utf-8')
            _lock_fh.write(str(os.getpid()))
            _lock_fh.flush()
            log(f'lock acquired pid={os.getpid()}')
            return True
        except FileExistsError:
            # 检查持有者是否存活（复用 monitor 单实例哨兵思路）
            try:
                holder = open(LOCK_FILE, encoding='utf-8').read().strip()
                if holder:
                    import ctypes
                    # Windows 下用 OpenProcess 探测；失败=进程已死 → 清陈旧锁
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                                           False, int(holder))
                    if not h:
                        os.remove(LOCK_FILE)
                        log(f'stale lock removed (holder pid={holder} dead)')
                        continue
                    ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass
            time.sleep(1)
    log('lock timeout: another loop_engine instance running')
    return False


def release_lock():
    global _lock_fh
    try:
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            log('lock released')
    except Exception as e:
        log(f'lock release failed: {e}')


# ========== 状态持久化 ==========

DEFAULT_STATE = {
    'version': 1,
    'current': 'p6_exit_label',
    'stages': {
        'p6_exit_label':      {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None},
        'p7_bs_balance':      {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None},
        'p8_tick_integration': {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None},
        'p9_topbottom_ml':    {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None},
        'p10_oos_verify':     {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None},
    },
    'history': [],
}


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            st = json.load(f)
        # 合并默认（新增 stage 时自动补齐）
        for k, v in DEFAULT_STATE['stages'].items():
            st.setdefault('stages', {}).setdefault(k, dict(v))
        return st
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(st):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def current_stage(st):
    """返回当前应执行的 stage key（pending/in_progress 优先，自动推进 done）。"""
    cur = st.get('current')
    s = st['stages'].get(cur, {})
    if s.get('status') in ('done', 'failed'):
        # 自动推进到下一个 pending
        order = list(DEFAULT_STATE['stages'].keys())
        try:
            i = order.index(cur)
        except ValueError:
            i = -1
        for k in order[i + 1:]:
            if st['stages'][k].get('status') == 'pending':
                st['current'] = k
                return k
        return None  # 全部完成
    return cur
