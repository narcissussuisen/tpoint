# tpoint - v9-miji T+0 monitor

Minute-level A-share T+0 trading strategy monitor with trailing stop-loss.

- `core/`      : core Python modules
- `config/`    : configuration files
- `scripts/`   : startup and maintenance batch scripts
- `backtest/`  : backtesting tools
- `tests/`     : self-tests
- `docs/`      : documentation
- `data/`      : runtime data files
- `logs/`      : log files
- `venv/`      : Python virtual environment

Quick start (administrator):
  scripts\install_tasks.bat   # register SYSTEM scheduled tasks
  scripts\restart_v9.bat       # restart services
