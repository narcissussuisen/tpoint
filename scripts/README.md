# scripts/ 目录结构说明（v10.5.0 规整后）

> 规整日期：2026-08-21　|　原则：**生产端零改动**，研究脚本按模块分层。

本目录混合了「生产运维」「生产支撑库」「运营复盘」「研究实验」「诊断」五类脚本。
脚本以 `python scripts/foo.py` 运行时，`scripts/` 自身在 `sys.path[0]`，故**兄弟 import**
（如 `from daily_signal_review import …`）依赖"同目录"。任何被生产脚本 import 的研究脚本
都不能移出 `scripts/` 根，否则会破坏生产。

---

## 一、`scripts/` 根（生产 + 运营，勿移动）

### 1.1 生产运维（被 `.bat`/计划任务直接调用，移动即断生产）
| 脚本 | 作用 |
|---|---|
| `run_*.bat` `install_tasks.bat` `repair_scheduled_task.bat` `install_daily_review.ps1` `install_selfcheck.ps1` | 启停/保活/安装计划任务 |
| `watchdog.py` `launch_watchdog.py` `restart_monitors.py` | 看门狗守护与紧急重启 |
| `selfcheck_daily.py` `net_health_watchdog.py` | 每日自检 / 网络健康看护 |
| `daily_signal_review.py` `live_roundtrip_review.py` `review_charts.py` `build_review_html.py` | 收盘复盘流水线 |
| `push_tpoint_review.py` `fdisk_daily_update.py` `prod_vs_bt_reconcile.py` `daily_report_push.py` `daily_iterate.py` `daily_closed_loop.py` `auto_tune.py` `pipeline_preflight.py` | 复盘推送 / 对账 / 自迭代 / 自动调参 |
| `_today.py` | 复盘流水线取交易日（被 `run_daily_review.bat` 调用） |

### 1.2 生产支撑共享库（被上列生产脚本以兄弟路径 import，**不能移走**）
| 脚本 | 被谁依赖 |
|---|---|
| `backtest_screener.py` | `prod_vs_bt_reconcile.py` `live_roundtrip_review.py` `factor_optimizer.py` `gate_ablation.py` |
| `factor_optimizer.py` | `daily_closed_loop.py` `oos_validate.py` |
| `oos_validate.py` | `auto_tune.py` `weekly_review.py` |
| `gate_ablation.py` | （import `backtest_screener`） |

### 1.3 运营 / 复盘 / 哨兵（保守保留在根）
`morning_review.py` `weekly_review.py` `floor_signals_today.py` `performance_stats.py`
`target_state_sentinel.py` `build_target_state_report.py` `p1_gray_monitor.py` `p1_review_metrics.py` `p0_b5_entry_audit.py`

### 1.4 回归测试（验证生产首扫抑制等行为，导入 `core.*`）
`test_bar_key_crossday.py` `test_first_scan_cutoff.py` `test_last_pushed_cutoff.py`

---

## 二、`scripts/research/`（研究/调参/回放实验，已从根迁来）

约 34 个一次性研究脚本（回测、参数搜索、消融、回放、验证、演化等）。
**运行方式**：直接 `python scripts/research/xxx.py` 即可——每个文件头部已注入：

```python
import sys, os
_H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_H)))            # scripts/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_H)), '..', 'core'))  # core/
```

该注入使其仍能解析根的 `daily_signal_review`/`backtest_screener` 与 `core/` 模块，无需额外设置 `PYTHONPATH`。
**请勿删除此头部**，否则研究脚本将因找不到兄弟/生产模块而导入失败。

同组依赖已一并迁入：`clean_1m_data+clean_all_pool`、`p2_diagnose+p2_rework+p2_watchlist_eval`、回放四件套。

---

## 三、`scripts/diag/`（诊断脚本，从仓库根迁来）

`diag_fallback.py` `diag_intraday.py` `diag_quotes.py` —— 行情/回测数据诊断，依赖 `core.datasource`。

---

## 四、命名与结构约定

- 全 `snake_case`，文件名不带版本号前缀（版本由 `VERSION` 统一管理）。
- 时间标记实验脚本（如 `replay_0805_0807`）保留原名、仅迁目录，避免引用错乱。
- 新增研究脚本请放入 `scripts/research/`；新增诊断放入 `scripts/diag/`；生产/运营脚本留在根。
- 生产边界权威判定见仓库根 `docs/` 与 `README.md`；废弃归档见根 `archive/`（如创建）。
