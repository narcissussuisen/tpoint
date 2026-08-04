# -*- coding: utf-8 -*-
"""tpoint 系统自检：配置校验 / 数据源连通 / 日志模块 / 任务调度 / 核心服务（2026-08-02）"""
import os, sys, json, datetime, subprocess, socket

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
OUT = []
PASS, WARN, FAIL = '✅', '⚠️', '❌'

def rec(status, item, detail=''):
    OUT.append(f'{status} {item}' + (f' — {detail}' if detail else ''))

def check_config():
    print('\n===== 1. 配置校验 =====')
    # VERSION
    try:
        v = open(os.path.join(BASE, 'VERSION'), encoding='utf-8').read().strip()
        rec(PASS, f'VERSION 文件 = {v}')
    except Exception as e:
        rec(FAIL, 'VERSION 文件', str(e))
    # watchlist
    try:
        wl = json.load(open(os.path.join(BASE, 'data', 'watchlist.json'), encoding='utf-8'))
        rec(PASS, f'watchlist.json {len(wl)} 只标的: {", ".join(wl.values())}')
    except Exception as e:
        rec(FAIL, 'watchlist.json', str(e))
    # monitor_config per-symbol
    try:
        mc = json.load(open(os.path.join(BASE, 'data', 'monitor_config.json'), encoding='utf-8'))
        rec(PASS, f'monitor_config.json {len(mc)} 只标的 per-symbol 配置')
        # 一致性：watchlist 与 monitor_config 对齐
        missing_cfg = [k for k in wl if k not in mc]
        rec(PASS if not missing_cfg else WARN,
            f'watchlist↔monitor_config 对齐', f'缺配置: {missing_cfg}' if missing_cfg else '全部对齐')
        for k, v in mc.items():
            rec(PASS, f'  {k}: mpr={v.get("mpr_enable")} periods={v.get("mpr_periods")} atr={v.get("atr_min_pct")}')
    except Exception as e:
        rec(FAIL, 'monitor_config.json', str(e))
    # config/monitor_config.json（config 目录下是否重复存在）
    ccfg = os.path.join(BASE, 'config', 'monitor_config.json')
    if os.path.exists(ccfg):
        try:
            cc = json.load(open(ccfg, encoding='utf-8'))
            rec(WARN, 'config/monitor_config.json 重复存在', '确认与 data/ 下是否一致')
        except Exception:
            pass
    # env 变量核查（生产启动关键）
    envs = ['MACD_GATE_MODE', 'TP_MHD_THRESHOLD', 'TP_ATR_MIN_PCT', 'TP_MONITOR_CONFIG']
    rec(WARN if not os.environ.get('MACD_GATE_MODE') else PASS,
        f'当前会话 MACD_GATE_MODE={os.environ.get("MACD_GATE_MODE", "(未设置)")}',
        '生产需=floor' if not os.environ.get('MACD_GATE_MODE') else '')
    # 关键路径
    for p in ['core/monitor.py', 'core/miji_alpha.py', 'core/datasource.py', 'scripts/watchdog.py',
              'scripts/daily_signal_review.py', 'venv/Scripts/python.exe']:
        full = os.path.join(BASE, p)
        rec(PASS if os.path.exists(full) else FAIL, f'关键文件 {p}')

def check_datasource():
    print('\n===== 2. 数据源连通性 =====')
    try:
        sys.path.insert(0, os.path.join(BASE, 'core'))
        sys.path.insert(0, BASE)
        from datasource import MootdxDataSource
        ds = MootdxDataSource()
        # mootdx 日K
        try:
            df = ds.get('161129.SZ', period='1d', count=5)
            rec(PASS if df is not None and len(df) > 0 else WARN,
                f'mootdx 日K连通 (161129.SZ {len(df) if df is not None else 0} 根)')
        except Exception as e:
            rec(FAIL, 'mootdx 日K', str(e)[:100])
        # mootdx 实时 quote
        try:
            q = ds.quotes('161129.SZ')
            rec(PASS if q is not None and len(q) > 0 else WARN,
                f'mootdx 实时报价: {len(q) if q is not None else 0} 行')
        except Exception as e:
            rec(FAIL, 'mootdx 实时报价', str(e)[:100])
        # 腾讯分时兜底
        try:
            tdf = ds._tencent_intraday_fallback('161129.SZ')
            rec(PASS if tdf is not None and len(tdf) > 0 else WARN,
                f'腾讯分时兜底: {len(tdf) if tdf is not None else 0} 根')
        except Exception as e:
            rec(FAIL, '腾讯分时兜底', str(e)[:100])
    except Exception as e:
        rec(FAIL, '数据源模块加载', str(e)[:150])

def check_logs():
    print('\n===== 3. 日志模块 =====')
    logs = os.path.join(BASE, 'logs')
    for f in ['watchdog.log', 'monitor_console.log', 'alert_engine_console.log', 'monitor_lifecycle.log']:
        p = os.path.join(logs, f)
        if os.path.exists(p):
            sz = os.path.getsize(p)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p))
            age = (datetime.datetime.now() - mtime).total_seconds() / 3600
            rec(PASS, f'{f} ({sz/1024:.0f}KB, {age:.0f}h前更新)')
        else:
            rec(WARN, f'{f} 不存在', '首次运行或已清理')
    # 根 .monitor.log
    if os.path.exists(os.path.join(BASE, '.monitor.log')):
        rec(PASS, '.monitor.log 存在')
    # err.txt 检查
    try:
        err = open(os.path.join(BASE, 'err.txt'), encoding='utf-8').read().strip()
        rec(FAIL if err else PASS, 'err.txt', err[:120] if err else '为空')
    except Exception:
        rec(PASS, 'err.txt', '不存在或不可读')

def check_scheduler():
    print('\n===== 4. 任务调度 =====')
    # 计划任务（通过 schtasks 只读查询——沙箱可能禁用，fallback 提示）
    try:
        r = subprocess.run(['schtasks', '/query', '/fo', 'LIST'], capture_output=True, text=True, timeout=15)
        tasks = [l for l in r.stdout.splitlines() if 'tpoint' in l.lower()]
        rec(PASS if tasks else WARN, f'计划任务发现 {len(tasks)} 个 tpoint 相关', ', '.join(tasks[:6]) if tasks else 'schtasks 输出无 tpoint')
    except Exception as e:
        rec(WARN, '计划任务查询', f'schtasks 被禁/失败: {str(e)[:80]}')
    # 注册表自启（只读查询）
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Run')
        try:
            val, _ = winreg.QueryValueEx(key, 'tpoint_watchdog')
            rec(PASS, f'注册表自启 tpoint_watchdog: {val}')
        except FileNotFoundError:
            rec(FAIL, '注册表自启 tpoint_watchdog', '未找到，登录后不会自动启动 watchdog!')
        winreg.CloseKey(key)
    except Exception as e:
        rec(WARN, '注册表自启查询', str(e)[:80])

def check_services():
    print('\n===== 5. 核心服务状态 =====')
    # watchdog 进程
    try:
        import ctypes, ctypes.wintypes as wt
        TH32CS_SNAPPROCESS = 0x00000002
        class PE(ctypes.Structure):
            _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)), ('th32ModuleID', wt.DWORD),
                        ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long),
                        ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_char * 260)]
        h = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        pe = PE(); pe.dwSize = ctypes.sizeof(PE)
        pids = {}
        if ctypes.windll.kernel32.Process32First(h, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.decode('utf-8', 'ignore').lower().startswith('python'):
                    pids[pe.th32ProcessID] = pe.szExeFile.decode('utf-8', 'ignore')
                if not ctypes.windll.kernel32.Process32Next(h, ctypes.byref(pe)):
                    break
        ctypes.windll.kernel32.CloseHandle(h)
        # pid 文件
        for label, pf in [('watchdog', 'data/.watchdog.pid'),
                          ('monitor', 'data/.monitor.svc.pid'),
                          ('alert_engine', 'data/.alert_engine.pid')]:
            pp = os.path.join(BASE, pf)
            if os.path.exists(pp):
                pid = open(pp).read().strip()
                alive = pid in [str(p) for p in pids]
                rec(PASS if alive else WARN, f'{label} pid={pid}', '存活' if alive else 'pid 文件存在但进程不在（非交易日预期）')
            else:
                rec(WARN, f'{label} pid 文件缺失', '非交易日 monitor/engine 不 spawn，属预期')
        rec(PASS, f'python 进程 {len(pids)} 个', ', '.join(f'pid={p}' for p in list(pids)[:8]))
    except Exception as e:
        rec(FAIL, '进程检查', str(e)[:100])

def main():
    print(f'tpoint 系统自检 — {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'BASE: {BASE}')
    check_config()
    check_datasource()
    check_logs()
    check_scheduler()
    check_services()
    print('\n' + '=' * 60)
    print('自检汇总:')
    for line in OUT:
        print(line)
    n_pass = sum(1 for l in OUT if l.startswith('✅'))
    n_warn = sum(1 for l in OUT if l.startswith('⚠️'))
    n_fail = sum(1 for l in OUT if l.startswith('❌'))
    print(f'\n通过 {n_pass} / 警告 {n_warn} / 失败 {n_fail}')
    # 写 JSON 结果
    res = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checks': [{'status': l[:1], 'item': l[1:].split(' — ')[0].strip(), 'detail': (l.split(' — ')[1] if ' — ' in l else '')} for l in OUT],
        'pass': n_pass, 'warn': n_warn, 'fail': n_fail,
    }
    with open(os.path.join(BASE, 'output', 'selfcheck_2026-08-02.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print('结果已写 output/selfcheck_2026-08-02.json')

if __name__ == '__main__':
    main()
