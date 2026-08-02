# tpoint — T+0 minute-level monitor (v9.1.0)

Minute-level A-share T+0 trading strategy monitor with trailing stop-loss.

> 📌 **定位（2026-08-01 校正）**：个人持仓做 T 的"哨兵工具"——对用户自选标的（T+0 的 ETF/LOF + 持有个股）
> 做分钟级信号监控与飞书推送。核心 KPI = **信号质量 + 推送可靠性 + 开仓纪律**，不选标的、不自动下单、不承诺策略收益。
> 详细见 [docs/positioning.md](docs/positioning.md)（替代"对标卡方 60% 胜率"的旧考核口径）。

## Version

Current: see [VERSION](VERSION) / [CHANGELOG](CHANGELOG.md)

- **v9.0.0** — initial "miji" release: VWAP-based signal detection + trailing stop-loss
- **v9.1.0** — first-principles factor v2 iteration: 75% signal hit rate (was 0%)
- 后续版本见 CHANGELOG / git tags（`v9.2.2` 为当前生产构建）

## Structure

| Dir | Purpose |
|-----|---------|
| `core/` | **生产核心**：`monitor.py`(监控主循环)、`alert_engine.py`(推送/自愈)、`miji_alpha.py`(信号引擎)、`indicators.py`、`exit_manager.py`、`entry_filter.py`、`datasource.py`、`feishu_alert.py` |
| `config/` | 配置与依赖：`monitor_config.json` + **唯一依赖清单 `requirements.txt`**（已删除根目录重复那份） |
| `scripts/` | 启停/保活/运维脚本：`run_*.bat`、`watchdog.py`、`launch_watchdog.py`、`restart_monitors.py`、`repair_scheduled_task.bat`(原 `fix-schtask.bat`) 等 |
| `backtest/` | 回测框架；**仅保留生产门控引擎** `backtest/keyfactor/{_gate_floor.py, miji_engine.py, _paths.py}`（被 `core` 与生产脚本直接 import，勿移动/删除）；其余研究脚本已归档至 `archive/` |
| `tests/` | 自测：`selftest.py`、`test_target.py`（原 `core/_test_tgt.py`） |
| `docs/` | 设计/部署/手册 |
| `data/` | 运行时数据（metrics/state/signal/watchlist；由监控再生，部分 gitignore） |
| `logs/` | 日志（gitignore） |
| `output/` | 回测/诊断产物（当前运行产物目录） |
| `archive/` | **历史研究脚本与产物归档**：仅留本地磁盘追溯，**不纳入 git**（git 历史保留原件） |
| `.local/` | 本地密钥：`.local/ssh/` 为 git 推送私钥，**gitignore** |

> ⚠️ **生产关键文件**：`backtest/keyfactor/_gate_floor.py`（被 `core/miji_alpha.py` 导入）、`miji_engine.py`、`_paths.py` 是运行时硬依赖，重构时不得移动或删除。

## Quick start (administrator)

```bat
scripts\install_tasks.bat   :: register SYSTEM scheduled tasks (tpoint_monitor / tpoint_alert_engine)
scripts\restart.bat         :: one-click restart services
```

## Naming conventions

- **Files**: no version prefix in filenames; version tracked in `VERSION`
- **运维脚本**: `fix-schtask.bat` 已重命名为 `scripts/repair_scheduled_task.bat`；依赖清单统一用 `config/requirements.txt`（唯一真源，删除根目录重复份）
- **Git tags**: `v9.0.0`, `v9.1.0` (semantic versioning)
- **Git branches**: `main` (production), `feature/<topic>`, `fix/<topic>`, `refactor/<topic>`
- **Scheduled tasks**: `tpoint_monitor`, `tpoint_alert_engine`
