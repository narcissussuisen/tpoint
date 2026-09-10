# -*- coding: utf-8 -*-
"""
watchdog_v5_tune.py — v5调优v2 心跳看门狗（防卡死/中断无人知）

与主调优进程 v5_systematic_tune_v2.py 配合：
  - 主进程每个里程碑写 HEARTBEAT 文件 + 推飞书；
  - 本看门狗每 60s 检查 HEARTBEAT 更新时间，若超过 STALL_MIN 分钟未更新且未见完成标记，
    向飞书全局群推送 ⚠️ 卡死/中断告警（持续等待，不退出）。
  - 主进程写完 DONE_FLAG 后本看门狗退出。

用法：python scripts/watchdog_v5_tune.py   （建议以 run_in_background 启动）
"""
import os, sys, time, subprocess, datetime, traceback

# 进程级信号免疫
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        import signal as _sig
        _sig.signal(getattr(_sig, _s), _sig.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
OUT = os.path.join(ROOT, 'output')
HEART = os.path.join(OUT, 'v5_tune_v2_heartbeat.txt')
DONE = os.path.join(OUT, 'v5_tune_v2_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'
STALL_MIN = 20          # 心跳超过 20 分钟未更新 → 判定疑似卡死
ALERT_GAP_MIN = 10      # 同一卡死状态最短告警间隔（避免刷屏）

sys.path.insert(0, OUT)


def push(text):
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f'[watchdog push failed] {e}')


def main():
    push(f"🐕 v5调优v2 看门狗已启动 | 心跳超 {STALL_MIN} 分钟无更新将告警 | 监控文件: {os.path.basename(HEART)}")
    last_alert = 0.0
    while True:
        if os.path.exists(DONE):
            push("🐕 v5调优v2 看门狗退出（检测到完成标记）")
            break
        now = time.time()
        if os.path.exists(HEART):
            age_min = (now - os.path.getmtime(HEART)) / 60.0
            if age_min > STALL_MIN:
                if (now - last_alert) / 60.0 > ALERT_GAP_MIN:
                    try:
                        last = open(HEART, encoding='utf-8').read().strip().splitlines()[-1][-140:]
                    except Exception:
                        last = '(无法读取心跳)'
                    push(f"⚠️ v5调优v2 疑似卡死/中断：心跳已 {int(age_min)} 分钟未更新\n最后心跳: {last}\n（看门狗继续等待，主进程若恢复会自动续推）")
                    last_alert = now
        else:
            # 心跳文件尚未生成（主进程可能还在加载数据 ~1-2min），稍等
            if (now - last_alert) / 60.0 > ALERT_GAP_MIN:
                push(f"⏳ v5调优v2 看门狗：尚未检测到心跳文件（主进程可能正在加载192标数据）")
                last_alert = now
        time.sleep(60)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        try:
            subprocess.run([sys.executable, NOTIFY, f"💥 v5调优v2 看门狗异常: {e}"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        print(traceback.format_exc())
        raise
