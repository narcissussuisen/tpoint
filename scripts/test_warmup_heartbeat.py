# -*- coding: utf-8 -*-
"""scripts/test_warmup_heartbeat.py — TF 预热期间心跳不中断回归（2026-09-10）

事故：2026-09-10 11:53:39 收到「v9 监控告警 · tpoint 服务中断」（严重 / 心跳停滞 424s）。
排查结论：**不是假报，是真 BUG**。
  - 告警时刻回推 424s = 11:46:35，正是手工重启杀旧进程的瞬间；新进程 11:46:52 起，
    但 `data/metrics.json` 直到 11:55:30 才被重新写入 → 7 分钟零心跳。
  - 根因：`run()` 的首轮 `write_metrics` 在 while 主循环内，而 `_warmup_tf()` 在主循环**之前**；
    mootdx「选择最快的服务器」+ 退避重试单次可阻塞 ~170s（实测 11:48:23 / 11:51:15 / 11:54:09
    三次重试，间隔 172s / 174s），远超 alert_engine 的 `service_stale_s=120`。
  - ⇒ 任何一次进程重启只要预热偏慢，就必然误报「服务中断」，把唯一的存活哨兵变成噪声。
    注意「加一次预热前心跳」是不够的（单次探测 172s 仍 > 120s），必须**阻塞期间持续打点**。

修复：探测放子线程跑，主线程每 `WARMUP_HB_INTERVAL_S`(30s) 刷一次心跳。

本测试锁定三条不变量：
  ① 阻塞期间心跳持续（最大间隔 ≤ 间隔 + 容差）—— 这是修复的核心；
  ② 心跳写者恒为**主线程**（write_metrics 非线程安全，禁止第二个写者）；
  ③ 预热成功路径行为不变（返回 True、tf 被接管、数据校验口径不变）。

运行：venv/Scripts/python.exe scripts/test_warmup_heartbeat.py     （约 20s）
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core'))

import pandas as pd          # noqa: E402
import monitor as M          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


BLOCK_S = 4.0          # 每次「探测」阻塞时长（模拟 mootdx 选服务器）
HB_INTERVAL = 1.0      # 测试用缩小的心跳间隔（生产 30s）


class _StubTF:
    """替代 monitor.TickFlow：.client 阻塞 _block_s 秒后返回（或抛异常）。"""
    _block_s = 0.0
    _payload = None
    _raise = None
    _made = 0

    def __init__(self):
        type(self)._made += 1
        self.klines = self

    @property
    def client(self):
        time.sleep(self._block_s)
        if self._raise is not None:
            raise self._raise
        return object()

    def get(self, *a, **k):
        return self._payload


def _run_warmup(payload, block_s, raise_exc=None):
    """在打桩环境下跑一次 _warmup_tf()，返回 (结果, 心跳时间戳列表, 心跳线程名集合)。"""
    stamps, threads = [], set()
    orig = (M.write_metrics, M._log_event, M.TARGETS, M.WARMUP_HB_INTERVAL_S, M.TickFlow, M.tf)
    M.write_metrics = lambda *a, **k: (stamps.append(time.time()),
                                       threads.add(threading.current_thread().name))
    M._log_event = lambda *a, **k: None
    M.TARGETS = {'300010.SZ': 'ST豆神'}
    M.WARMUP_HB_INTERVAL_S = HB_INTERVAL
    _StubTF._block_s, _StubTF._payload, _StubTF._raise, _StubTF._made = block_s, payload, raise_exc, 0
    M.TickFlow = _StubTF
    M.tf = None
    try:
        res = M._warmup_tf()
        tfobj = M.tf
    finally:
        (M.write_metrics, M._log_event, M.TARGETS, M.WARMUP_HB_INTERVAL_S, M.TickFlow, M.tf) = orig
    return res, stamps, threads, _StubTF._made, tfobj


def main():
    print('\n=== 1. 探测被阻塞时心跳必须持续（核心不变量）===')
    t0 = time.time()
    res, stamps, threads, made, _ = _run_warmup(payload=None, block_s=BLOCK_S)
    dur = time.time() - t0
    # 3 次尝试 × 阻塞 4s + 退避 (1s+2s) ≈ 15s
    print(f'  实测耗时 {dur:.1f}s，心跳 {len(stamps)} 次，TickFlow 构造 {made} 次')
    check('全失败返回 False（行为不变）', res is False, f'got={res}')
    check('三次重试都发生（构造 3 次）', made == 3, f'got={made}')
    blocked = made * BLOCK_S
    expect_min = int(blocked / HB_INTERVAL) - 1
    check('心跳次数 ≥ 阻塞时长/间隔（即阻塞期间在持续打点）',
          len(stamps) >= expect_min, f'got={len(stamps)} expect>={expect_min}')
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    max_gap = max(gaps) if gaps else 0.0
    check(f'心跳最大间隔 ≤ 间隔+容差({HB_INTERVAL + 0.6:.1f}s) —— 修复前此处会是整段 {blocked:.0f}s',
          max_gap <= HB_INTERVAL + 0.6, f'got={max_gap:.2f}s')
    check('退避 sleep 窗口(≤7s)未造成超阈空窗：总跨度覆盖全部阻塞时间',
          (stamps[-1] - stamps[0]) >= blocked - HB_INTERVAL,
          f'span={stamps[-1] - stamps[0]:.1f}s blocked={blocked:.0f}s')

    print('\n=== 2. 心跳写者必须只有主线程（write_metrics 非线程安全）===')
    check('全部心跳写均来自 MainThread', threads == {'MainThread'}, f'got={threads}')

    print('\n=== 3. 预热成功路径行为不变 ===')
    df = pd.DataFrame({'close': [1.0, 2.0]})
    res2, stamps2, threads2, made2, tfobj = _run_warmup(payload=df, block_s=1.0)
    check('返回 True', res2 is True, f'got={res2}')
    check('仅构造 1 次（成功即返回，不再退避重试）', made2 == 1, f'got={made2}')
    check('tf 被成功实例接管', isinstance(tfobj, _StubTF), f'got={type(tfobj).__name__}')
    check('成功路径同样至少打过一次心跳', len(stamps2) >= 1, f'got={len(stamps2)}')

    print('\n=== 4. 构造抛异常路径（连接失败）不破坏心跳 ===')
    res3, stamps3, threads3, made3, _ = _run_warmup(payload=None, block_s=0.2,
                                                    raise_exc=RuntimeError('connect failed'))
    check('抛异常仍走满 3 次重试并返回 False', res3 is False and made3 == 3,
          f'res={res3} made={made3}')
    check('异常路径也有心跳', len(stamps3) >= 1, f'got={len(stamps3)}')

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
