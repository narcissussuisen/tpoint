# -*- coding: utf-8 -*-
"""tpoint 系统自检：依赖/版本审计 + 核心子系统自检（2026-08-02）"""
import json, os, re, sys, platform, subprocess, socket, time, importlib

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
VENV_PY = os.path.join(BASE, 'venv', 'Scripts', 'python.exe')
REQ = os.path.join(BASE, 'config', 'requirements.txt')

def load_venv_versions():
    """从 site-packages 的 dist-info 目录名解析 包名->版本（venv 真相源）。"""
    sp = os.path.join(BASE, 'venv', 'Lib', 'site-packages')
    out = {}
    for name in os.listdir(sp):
        m = re.match(r'^(.*?)-(\d[\w.]*[a-zA-Z0-9]*)\.dist-info$', name)
        if m:
            out[m.group(1).replace('_', '-').lower()] = m.group(2)
    return out

def load_req_versions():
    """解析 config/requirements.txt -> 包名->版本约束。"""
    out = {}
    with open(REQ, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([A-Za-z0-9_.\-]+)(.*)$', line)
            if m:
                out[m.group(1).lower()] = m.group(2).strip() or '（未固定）'
    return out

def import_check(mods):
    """逐个 import 并返回版本。"""
    out = {}
    for m in mods:
        try:
            mod = importlib.import_module(m)
            v = getattr(mod, '__version__', '?')
            out[m] = str(v)
        except Exception as e:
            out[m] = f'IMPORT_FAIL: {e}'
    return out

def main():
    print('=' * 68)
    print('tpoint 环境审计 — venv 真相源 vs requirements.txt 对照')
    print('=' * 68)
    print(f'运行环境: python {platform.python_version()} @ {sys.executable}')
    print(f'VENV python: {platform.python_version()} @ {VENV_PY}')
    print(f'OS: {platform.platform()}')

    venv_ver = load_venv_versions()
    req_ver = load_req_versions()
    print(f'\n[venv 实装包数] {len(venv_ver)}  |  [requirements 声明数] {len(req_ver)}')

    print('\n--- requirements 声明 vs venv 实装 对照 ---')
    missing = []
    mismatch = []
    for pkg, req in sorted(req_ver.items()):
        installed = venv_ver.get(pkg)
        status = 'OK' if installed else 'MISSING'
        if not installed:
            missing.append(pkg)
        elif installed != req.strip('=') and re.fullmatch(r'[\d.]+', req.strip('=')):
            mismatch.append((pkg, req.strip('='), installed))
        print(f'  {pkg:<24} req={req:<12} venv={str(installed):<12} [{status}]')

    print('\n--- venv 实装但 requirements 未声明（潜在遗漏依赖） ---')
    undeclared = [p for p in sorted(venv_ver) if p not in req_ver]
    for p in undeclared:
        print(f'  {p:<24} venv={venv_ver[p]}')

    print('\n--- 核心导入冒烟测试（生产 pythonw 同款路径） ---')
    smoke = import_check(['numpy', 'pandas', 'mootdx', 'pytdx', 'requests', 'matplotlib'])
    for m, v in smoke.items():
        print(f'  {m:<14} -> {v}')

    print(f'\n[结论] requirements 缺失包: {len(missing)} | 版本不一致: {len(mismatch)} | 未声明实装: {len(undeclared)}')
    if missing:
        print(f'  缺失: {missing}')
    if mismatch:
        for p, r, i in mismatch:
            print(f'  不一致: {p} req={r} venv={i}')

if __name__ == '__main__':
    main()
