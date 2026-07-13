# T点 v9 部署说明

> 部署目标：openclaw 服务器 `/home/gem/workspace/agent/workspace-main/tpoint/`
> 策略：与 v8 并行灰度运行，selftest + 回测验证后再切换生产

## 一、文件清单

| 文件 | 用途 | 依赖 |
|------|------|------|
| `v9_indicators.py` | 纯算法层（VWAP/ATR/趋势/量比/温度计/触发判定） | numpy |
| `monitor_v9.py` | 生产监控（实时推送飞书） | v9_indicators, tickflow, requests |
| `backtest_v9.py` | 回测对比 v8/v9 命中率 | v9_indicators, tickflow, pandas |
| `v9_selftest.py` | 本地算法验证（合成行情，无需 tickflow） | v9_indicators, numpy |
| `v9_design.md` | 设计文档 | — |

## 二、部署步骤

```bash
# 1. 上传文件到服务器
scp tpoint/v9_*.py tpoint/v9_design.md gem@server:/home/gem/workspace/agent/workspace-main/tpoint/

# 2. 服务器端确认依赖
cd /home/gem/workspace/agent/workspace-main/tpoint/
python3 -c "import numpy, requests, pandas; from tickflow import TickFlow; print('依赖OK')"

# 3. 本地验证（服务器上跑，验证算法逻辑）
python3 v9_selftest.py
# 预期: 下跌趋势 v9-S > 0 且 v8-S = 0

# 4. 回测验证（真实数据）
python3 backtest_v9.py 5   # 回测最近5个交易日
# 关注: v9 总命中率 ≥ v8, 且 v9 信号量收敛(量价确认效果)
```

## 三、灰度并行运行（不替换 v8）

v9 与 v8 独立运行，互不干扰（不同 PID 锁、不同信号文件）：

```bash
# v8 继续生产推送
nohup python3 monitor_v8.py > monitor_v9_gray.log 2>&1 &

# v9 灰度（先注释 WEBHOOK_URL 或推送到测试群，避免干扰）
# 修改 monitor_v9.py 的 WEBHOOK_URL 为测试机器人，或加 [灰度] 前缀
nohup python3 monitor_v9.py > monitor_v9_gray.log 2>&1 &
```

灰度期对比 `v8_signal.txt` vs `v9_signal.txt`，观察：
- v9 在下跌趋势是否主动发 S
- v9 的 B 信号是否更精准（量价确认后误发减少）
- v9 信号总量是否收敛

## 四、切换生产

灰度验证（建议 ≥ 5 个交易日）通过后：

```bash
# 1. 停 v8
kill $(cat /tmp/monitor_v8.pid)
# 2. 启 v9（恢复正式 WEBHOOK_URL）
nohup python3 monitor_v9.py > monitor_v9.log 2>&1 &
# 3. 更新 cron/selfcheck 拉起脚本指向 v9
#    修改 restart.sh / cron_poll.py 中的 monitor_v8 → monitor_v9
```

## 五、关键参数调优（backtest_v9 验证后）

若回测发现信号过少/过多，优先调：

| 现象 | 调整 | 位置 |
|------|------|------|
| 信号过少 | K1↑（1.0→1.5）或 VOL_THRESHOLD↓（1.2→1.0） | v9_indicators.py |
| 信号过多/误发 | K1↓（1.0→0.7）或 VOL_THRESHOLD↑ | v9_indicators.py |
| 趋势误判 | ADX_THRESHOLD↑（20→25） | v9_indicators.py |
| S 仍偏少 | TEMP_COLD↓（30→20）放宽杀跌抑制 | v9_indicators.py |

## 六、与 v8 的关键差异（运维注意）

1. **v9 多一个文件依赖**：`v9_indicators.py` 必须与 `monitor_v9.py` 同目录，否则 import 失败。
2. **状态文件独立**：`v9_state.json` / `v9_signal.txt`，与 v8 的 `v8_*` 不冲突，可并行。
3. **PID 锁独立**：`/tmp/monitor_v9.lock`，不与 v8 抢锁。
4. **VWAP 依赖 volume**：若 tickflow 的 intraday 无 volume 字段，v9 自动退化为等权均价（效果打折但不崩溃），日志会显示 `has_vol=False`。

## 七、回滚

```bash
kill $(cat /tmp/monitor_v9.pid)
nohup python3 monitor_v8.py > monitor_v8.log 2>&1 &
```

> 投资有风险，所有信号仅供参考，不构成投资建议。
