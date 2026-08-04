#!/usr/bin/env python3
"""tpoint 做T监控一键启动：自动探测有依赖的 Python 解释器，然后起 monitor.py。

用法：
    python run.py            # 自动找 venv python → 起 core/monitor.py
    TP_SCAN_INTERVAL=15 python run.py    # 可环境变量调参
"""
import os
import sys
import subprocess
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(ROOT, "core")
VENV_PYTHON = os.path.join(ROOT, "venv", "Scripts", "python.exe")

# 候选 Python 解释器：tpoint venv → 当前 → WB 托管 → 系统 PATH
_CANDIDATE_PYTHONS: list[str] = []


def _find_python() -> str:
    """找到一个已安装 mootdx/requests 的 Python 解释器。"""
    global _CANDIDATE_PYTHONS

    # 1) tpoint 项目 venv（最优先）
    if os.path.isfile(VENV_PYTHON):
        _CANDIDATE_PYTHONS.append(VENV_PYTHON)

    # 2) 当前解释器
    if sys.executable not in _CANDIDATE_PYTHONS:
        _CANDIDATE_PYTHONS.append(sys.executable)

    # 3) WorkBuddy 托管 venv
    for wb_venv in (
        os.path.expanduser(r"~\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
        os.path.expanduser(r"~\.workbuddy\binaries\python\envs-default\Scripts\python.exe"),
    ):
        if os.path.isfile(wb_venv) and wb_venv not in _CANDIDATE_PYTHONS:
            _CANDIDATE_PYTHONS.append(wb_venv)

    # 4) 系统 PATH
    system_py = shutil.which("python") or shutil.which("python3")
    if system_py and system_py not in _CANDIDATE_PYTHONS:
        _CANDIDATE_PYTHONS.append(system_py)

    seen: set[str] = set()
    for py in _CANDIDATE_PYTHONS:
        py = os.path.abspath(py)
        if py in seen or not os.path.isfile(py):
            continue
        seen.add(py)
        try:
            r = subprocess.run(
                [py, "-c", "import mootdx, requests; print('mootdx', mootdx.__version__)"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                ver = r.stdout.decode().strip()
                print(f"[run] 使用解释器: {py} ({ver})")
                return py
        except Exception:
            continue

    print("[run] 错误: 未找到安装了 mootdx/requests 的 Python 解释器！")
    print(f"[run] 已检查: {list(seen)}")
    print("[run] 请先安装依赖: pip install -r config/requirements.txt")
    sys.exit(1)


def main() -> None:
    py = _find_python()

    print("[run] 启动 tpoint 做T监控 ...")
    print(f"[run] 工作目录: {CORE}")
    print("[run] Ctrl+C 停止")

    cmd = [py, "monitor.py"]
    try:
        subprocess.check_call(cmd, cwd=CORE)
    except KeyboardInterrupt:
        print("[run] 已停止")


if __name__ == "__main__":
    main()
