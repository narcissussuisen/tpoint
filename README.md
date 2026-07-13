# tpoint — T+0 minute-level monitor (v9.1.0)

Minute-level A-share T+0 trading strategy monitor with trailing stop-loss.

## Version

Current: **v9.1.0** (see [VERSION](VERSION) / [CHANGELOG](CHANGELOG.md))

- **v9.0.0** — initial "miji" release: VWAP-based signal detection + trailing stop-loss
- **v9.1.0** — first-principles factor v2 iteration: 75% signal hit rate (was 0%)

## Structure

| Dir | Purpose |
|-----|---------|
| `core/` | Core modules: `monitor.py`, `indicators.py`, `exit_manager.py`, `alert_engine.py`, `datasource.py`, `feishu_alert.py` |
| `config/` | Configuration files (`monitor_config.json`, `requirements.txt`) |
| `scripts/` | Startup, maintenance, and analysis scripts (`.bat` + `.py`) |
| `backtest/` | Backtesting tools and historical data |
| `tests/` | Self-tests (`selftest.py`) |
| `docs/` | Design, deployment, and playbook documentation |
| `data/` | Runtime data (metrics, state, signals) |
| `logs/` | Log files |

## Quick start (administrator)

```bat
scripts\install_tasks.bat   :: register SYSTEM scheduled tasks (tpoint_monitor / tpoint_alert_engine)
scripts\restart.bat         :: one-click restart services
```

## Naming conventions

- **Files**: no version prefix in filenames; version tracked in `VERSION`
- **Git tags**: `v9.0.0`, `v9.1.0` (semantic versioning)
- **Git branches**: `main` (production), `feature/<topic>`, `fix/<topic>`, `refactor/<topic>`
- **Scheduled tasks**: `tpoint_monitor`, `tpoint_alert_engine`
