# -*- coding: utf-8 -*-
# scripts/watchdog_exit_anchor_grid.py — G1 出场网格看门狗（longtask-feishu-monitor 模板实例化）
# 与主进程（scripts/research/exit_anchor_grid.py）各自以 run_in_background 启动。

import os, sys, time, subprocess, datetime, traceback

# 进程级信号免疫
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        import signal as _sig
        _sig.signal(getattr(_sig, _s), _sig.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

# ===== 需要参数化的部分 =====
TASK_TAG = "exit_anchor_grid"        # 必须与主进程的 TASK_TAG 一致
OUT = r"C:/Users/YZP/WorkBuddy/Claw/tpoint/output"  # 必须与主进程的 OUT 一致
HEART = os.path.join(OUT, f"{TASK_TAG}_heartbeat.txt")
DONE = os.path.join(OUT, f"{TASK_TAG}_done.flag")
NOTIFY = r"C:/Users/YZP/.workbuddy/notify.py"
STALL_MIN = 20          # 心跳超过 20 分钟未更新 → 疑似卡死
ALERT_GAP_MIN = 10      # 同一卡死态最短告警间隔（分钟，防刷屏）
POLL_SEC = 60           # 巡检周期（秒）
# =============================


def push(text):
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[watchdog push failed] {e}")


def main():
    push(f"🐕 [{TASK_TAG}] 看门狗已启动 | 心跳超 {STALL_MIN} 分钟无更新将告警")
    last_alert = 0.0
    while True:
        if os.path.exists(DONE):
            push(f"🐕 [{TASK_TAG}] 看门狗退出（检测到完成标记）")
            break
        now = time.time()
        if os.path.exists(HEART):
            age_min = (now - os.path.getmtime(HEART)) / 60.0
            if age_min > STALL_MIN:
                if (now - last_alert) / 60.0 > ALERT_GAP_MIN:
                    try:
                        last = open(HEART, encoding="utf-8").read().strip().splitlines()[-1][-140:]
                    except Exception:
                        last = "(无法读取心跳)"
                    push(f"⚠️ [{TASK_TAG}] 疑似卡死/中断：心跳已 {int(age_min)} 分钟未更新\n最后心跳: {last}\n（看门狗继续等待，主进程若恢复会自动续推）")
                    last_alert = now
        else:
            if (now - last_alert) / 60.0 > ALERT_GAP_MIN:
                push(f"⏳ [{TASK_TAG}] 看门狗：尚未检测到心跳文件（主进程可能正在加载数据）")
                last_alert = now
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            subprocess.run([sys.executable, NOTIFY, f"💥 [{TASK_TAG}] 看门狗异常: {e}"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        print(traceback.format_exc())
        raise
