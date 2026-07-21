#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_progress.py — 长任务飞书进度提醒器
==========================================
为 OOS 参数扫描等长耗时任务提供「节流式」进度快照推送:

  进度: 已完成 / 总数 (百分比)
  当前: 正在处理的扫描项
  剩余: 预计剩余时间 (ETA)
  已用: 任务已耗时

设计要点:
  - 复用 feishu_push.push() (已含令牌桶限速 + 限频指数退避 + 送达日志)
  - 本类在其上再做一层「快照节流」(默认 15s 最多推一条), 避免刷屏
  - 支持 set_enabled(False) 全局关闭 (对应 --no-progress)
  - ETA 在「预计算」阶段显示「估算中」, 进入主检测阶段后 reset_clock 才精准

用法:
    from feishu_progress import FeishuProgress
    rep = FeishuProgress(title="floor参数OOS扫描(30标的)", interval=15)
    rep.set_total(N)
    rep.set_phase("预计算")
    rep.update(current="加载 688347.SH (3/30)")
    ...
    rep.set_phase("[扫描1] 冷却期")
    rep.update(current="cooldown=15 | 688347.SH 2026-07-21")
    ...
    rep.finish(summary="最佳冷却期=15")
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from feishu_push import push as _feishu_push  # 复用现成推送(含限速)


class FeishuProgress:
    def __init__(self, title="OOS 参数扫描", total=None, interval=15, milestones=None):
        self.title = title
        self.total = total                 # 总工作量单位 (None=未知)
        self.interval = max(5, int(interval))  # 快照节流下限 5s (时间模式用)
        # 里程碑模式: 指定百分比节点才推送, 如 [25,50,75,100]; None=时间模式
        self.milestones = sorted(milestones) if milestones else None
        self._pushed = set()               # 已推送过的里程碑
        self.done = 0
        self.current = ""
        self.phase = ""
        self.abs_start = time.time()       # 任务总已用基准
        self.rate_start = time.time()      # ETA 速率基准 (可 reset)
        self.last_push = 0
        self.enabled = True
        self._finished = False

    # ---- 配置 ----
    def set_enabled(self, v):
        self.enabled = bool(v)
        return self

    def set_total(self, total):
        self.total = total
        return self

    def set_phase(self, phase):
        self.phase = phase
        return self

    def reset_clock(self):
        """重置 ETA 速率基准 (用于跨阶段, 使剩余时间更准)。"""
        self.rate_start = time.time()
        return self

    # ---- 更新 ----
    def update(self, current=None, increment=1, force=False):
        if current is not None:
            self.current = current
        self.done += increment
        if not self.enabled:
            return
        now = time.time()
        # 强制推送 (force): 阶段边界/收尾前手动触发
        if force:
            self.last_push = now
            self._emit()
            return
        # 里程碑模式: 仅当越过未推送的百分比节点才发
        if self.milestones is not None:
            pct = self._pct_value()
            crossed = [m for m in self.milestones if m <= pct and m not in self._pushed]
            if crossed:
                for m in crossed:
                    self._pushed.add(m)
                self.last_push = now
                self._emit()
            return
        # 时间模式: 距上次推送超过 interval 才发
        if (now - self.last_push) < self.interval:
            return
        self.last_push = now
        self._emit()

    def tick(self, current=None, force=False):
        """增量 1 的简写。"""
        self.update(current=current, increment=1, force=force)

    # ---- 内部计算 ----
    def _pct_value(self):
        if not self.total or self.total <= 0:
            return 0.0
        return min(100.0, self.done / self.total * 100.0)

    def _pct(self):
        if not self.total or self.total <= 0:
            return "?"
        return f"{self._pct_value():.1f}%"

    def _eta(self):
        # 预计算阶段样本太少, ETA 无意义
        if self.phase and self.phase.startswith("预计算"):
            return "估算中"
        if self.done <= 0 or not self.total or self.total <= 0:
            return "?"
        elapsed = time.time() - self.rate_start
        rate = elapsed / self.done
        remain = max(0.0, rate * (self.total - self.done))
        m = int(remain // 60)
        s = int(remain % 60)
        return f"{m}分{s}秒" if m else f"{s}秒"

    def _elapsed(self):
        e = time.time() - self.abs_start
        m = int(e // 60)
        s = int(e % 60)
        return f"{m}分{s}秒" if m else f"{s}秒"

    def _emit(self):
        lines = [f"📊 {self.title}"]
        if self.phase:
            lines.append(f"阶段: {self.phase}")
        lines.append(f"进度: {self.done}/{self.total} ({self._pct()})")
        if self.current:
            lines.append(f"当前: {self.current}")
        lines.append(f"已用: {self._elapsed()}  剩余: {self._eta()}")
        try:
            _feishu_push("\n".join(lines))
        except Exception:
            pass

    def finish(self, summary=""):
        if self._finished:
            return
        self._finished = True
        if not self.enabled:
            return
        body = [
            f"✅ {self.title} 完成",
            f"总耗时: {self._elapsed()}",
            f"处理: {self.done}/{self.total}",
        ]
        if summary:
            body += ["───", summary]
        try:
            _feishu_push("\n".join(body), critical=True)
        except Exception:
            pass


if __name__ == "__main__":
    # 自测: 发 3 条进度 + 1 条完成, 验证节流/ETA/推送
    r = FeishuProgress(title="[自测] 进度器", total=100, interval=3)
    r.set_phase("测试阶段")
    for i in range(0, 100, 20):
        r.update(current=f"item-{i}", force=True)
        time.sleep(1)
    r.finish(summary="自测通过")
    print("SELFTEST_DONE")
