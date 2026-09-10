# -*- coding: utf-8 -*-
# watchdog_signal_validity_v2.py — DET v2.0 三轴评估看门狗
# 基于 longtask-feishu-monitor 模板实例化。

import os, sys, time, subprocess, traceback

for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        import signal as _sig
        _sig.signal(getattr(_sig, _s), _sig.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

TASK_TAG = "signal_validity_v2"
OUT = r"C:/Users/YZP/WorkBuddy/Claw/tpoint/output"
HEART = os.path.join(OUT, "signal_validity_v2_heartbeat.txt")
DONE = os.path.join(OUT, "signal_validity_v2_done.flag")
NOTIFY = r"C:/Users/YZP/.workbuddy/notify.py"
STALL_MIN = 20
ALERT_GAP_MIN = 10
POLL_SEC = 60


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
                    push(f"⚠️ [{TASK_TAG}] 疑似卡死/中断：心跳已 {int(age_min)} 分钟未更新\n最后心跳: {last}")
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
