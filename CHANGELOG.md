# CHANGELOG

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/),
versioning follows [Semantic Versioning](https://semver.org/).

---

## [9.1.0] - 2026-07-13

### Added
- `detect_signals_v2()` — first-principles factor iteration (mean-reversion + momentum confirmation + asymmetric B/S design)
  - B: VWAP-K1*ATR oversold zone + reversal K-line + EMA20/RSI momentum + volume + trend==1; downtrend day adds yang-line + body>=0.3ATR + RSI<35 + EMA20 rising
  - S: VWAP+K2*ATR extreme overbought + local top(15min) + RSI>=55 falling + close<prev + volume; no trend restriction
  - Cross-signal cooldown (gap minutes between B and S)
  - Constants: K1_V2=0.8, K2_V2=1.8, M_V2=1.2, S_RSI_GATE=55, B_RSI_OVERSOLD=35
- `datasource.py` — `_server_ok()` data-validation fallback (servers that connect but return empty data now auto-fallback to bestip)
- `scripts/playback_gl.py` — real-code T+0 playback for GanLiYaoYe (603087.SH)
- `scripts/playback_gl_0709.py` — full v9 system run on 2026-07-09 real data with candidate diagnostics
- `scripts/factor_v2_iterate.py` — v2 factor grid-search self-iteration on 3-day real data
- `scripts/factor_v2_report.py` — v2 comparison report generator
- `scripts/e2e_simulation.py` — full-chain simulation (start -> fetch -> detect -> alert -> report)
- `scripts/e2e_report_send.py` — send e2e report to Feishu
- `docs/t0_playbook.md` — T+0 forward trading playbook
- `docs/factor_v2_compare.html` — old-v9 vs new-v2 three-day comparison report
- `docs/playback_gl_report.html` — GanLiYaoYe playback report
- `docs/playback_gl_0709_report.html` — 07-09 full system run report

### Changed
- `monitor.py` — concurrent fetch (ThreadPoolExecutor), self-healing lock takeover, pre-market heartbeat
- `config/monitor_config.json` — scan_duration threshold 10s -> 45s; 6 alert rules
- `scripts/install_tasks.bat` — pure ASCII + CRLF encoding fix
- `scripts/run_monitor.bat` / `run_engine.bat` — path fix (core/*.py, logs/)

### Fixed
- Zombie single-instance lock (PID holders in SYSTEM session 0)
- datasource empty-data bug (TCP connects but returns no bars -> auto-fallback to bestip)
- pc_map bug in factor iteration (used current-day close as PC instead of previous-day close)

### Results
- v2 factor: 4 signals / 3 real / 75% hit rate (old v9: 1 signal / 0 real / 0%)
- 07-09 (wide swing 4.7%): v2 caught 1 real B (+0.61%); old v9 caught 0
- 07-10 (surge +5.95%): v2 caught 2 real S; old v9 caught 0
- 07-13 (drop -2.3%): v2 correctly stayed flat; old v9 had 1 false signal

---

## [9.0.0] - 2026-07-07

### Added — initial v9 release ("miji")
- `indicators.py` — pure algorithm layer: VWAP/ATR/EMA/ADX/RSI/sentiment thermometer/volume ratio + B/S signal detection
  - B: trend==1 + lower-band touch + reversal K-line + volume ratio>=2.0
  - S: trend in {-1,0} + upper-band touch + reversal K-line + volume ratio>=2.0
  - Constants: K1=1.0, K2=2.0, VOL_THRESHOLD=2.0, MAX_B_DAILY=12, MAX_S_DAILY=12
- `exit_manager.py` — trailing stop-loss (0.4% activate, 0.6% trail), hard stop, time stop, S-signal exit, EOD force-close
- `entry_filter.py` — entry quality filter
- `monitor.py` — production monitor: real-time signal detection + Feishu push + single-instance lock + state persistence
- `alert_engine.py` — watchdog alert engine: polls metrics.json, evaluates 6 alert rules, pushes Feishu cards
- `datasource.py` — Mootdx/通达信 data source (TCP 7709, multi-server fallback)
- `feishu_alert.py` — Feishu interactive card builder
- `backtest/backtest.py` — v8 vs v9 hit-rate comparison
- `backtest/backtest_extended.py` — extended backtest with exit manager
- `backtest/backtest_exit.py` — exit manager backtest
- `backtest/diagnostic.py` — B-signal diagnostic tool
- `backtest/compare.py` — strategy comparison tool
- `backtest/download_data.py` — historical data downloader
- `backtest/backtest_minute.py` — minute-level backtest
- `tests/selftest.py` — local algorithm validation with synthetic data
- `docs/design.md` — v9 design document (VWAP paradigm shift from v8)
- `docs/deploy.md` — deployment guide
- `scripts/install_tasks.bat` — register SYSTEM scheduled tasks
- `scripts/run_monitor.bat` / `run_engine.bat` — service launchers
- `scripts/restart.bat` — one-click restart
- `config/monitor_config.json` — Feishu webhook + monitor settings + 6 alert rules

### Replaces
- v8 support/resistance crossing strategy (LONGCROSS-based, 0 S-signals in downtrend)
