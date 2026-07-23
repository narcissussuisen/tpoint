# miji 版本算法说明（CHANGELOG）

> 版本号规则：MAJOR.MINOR.PATCH；PATCH=同一算法框架内的修复/硬化（每个修复 +1）。
> 说明仅标注各版本**核心算法与信号语义**的差异，便于回溯。

## v9.2.1 — 收敛第一性 + 风险节点修复（固化 tag v9.2.1-converged）
- **回归第一性（07-22 收敛）**：
  - 盘后假告警副作用治理：删除残留 `data/risk_override_secondary.json` 的 HALT_BUY（避免次日禁买）；回退 Fix4b 兜底 + secondary 合并逻辑。
  - 复盘口径根治：`daily_signal_review.py` 以 `state.json` 为权威源（实盘推送次数），复算仅作参考并明确标注，消除「复算幻影信号」。
  - 送达可靠：monitor 加失败补发队列 `data/push_pending.jsonl`；复盘 webhook 从信号群 `1d241455` 分离到复盘群 `849577f5`，缓解飞书频限丢推。
  - 风控 fail-open：`risk_override.json` 过期即 NONE 放行（vr_risk agent 未运行不阻断信号）。
- **风险节点修复（07-23）**：
  - 节点1：`config/monitor_config.json` 的 `monitor.session.open_m` 15→25，对齐 alert_engine 评估窗口与 monitor 实际扫描起始(9:25)，消除 9:15–9:25 误报空窗。
  - 节点2：新增 `scripts/install_daily_review.ps1` + `run_daily_review.bat`，支持计划任务 `tpoint_daily_review`（周一至五 15:30）自动推送复盘。
  - 节点4/5：`monitor.py` 加午休 `last_bar_ts=null` 不变量注释（防 data_lag_s 误报）；`tf is None` 兜底已就绪（L1080–1091）。
- **单实例锁加固**：`alert_engine.py` 重写 `acquire_single_instance`（失败关句柄 + 活实例安静退出，防 crash-loop）；`run_engine.bat` 启动自清理。
- 本版本为 07-22/07-23 两波未提交改动固化点；相对 v9.2.0 算法信号语义无变化（仍为 floor 门控）。

## v9.2.0 — floor 门控正式上线（替代 strict）
- **架构解耦**：新建 `backtest/keyfactor/_gate_floor.py` 共享门控模块（`gate_buy`/`gate_sell` 纯函数），消除 `miji_engine.py` 与 `miji_alpha.py` 的重复门控逻辑。
- **门控切换**：生产默认 `MACD_GATE_MODE` 从 `strict` → `floor`（通过 `run_monitor.bat`/`run_engine.bat` 环境变量设置，无需改动算法代码）。
- **改进1 — 价格天花板 S 冷却期**：`floor_sell_cooldown_bars`（OOS 扫出最优值），上次天花板 S 后 N bar 内禁止再以"价格天花板"触发 S，抑制趋势日卖飞噪声。
- **改进2 — 涨停/近涨停日 S 抑制**：`floor_suppress_day_chg`（OOS 扫出最优值），日涨幅≥阈值时关闭价格天花板 S 通道，仅保留 MACD 背离 S。
- **改进3 — 趋势诊断**（预留）：`floor_trend_threshold` 趋势感知缩放框架已搭好，参数暂按默认（无缩放）。待多日 OOS 确认趋势-噪声关系后启用。
- **验证**：106 标的 OOS 参数扫描（冷却期 10 值 + 涨停抑制 8 值 + 趋势分组诊断），选拐点处最优参数；`py_compile` + 今日真实 1m 无回归重放验证通过。

## v9.1.5 — 数据源韧性 + 静默零信号告警 + tf 预热
- datasource: `_retry_with_backoff` 退避重连；修正 3-4 行死区；腾讯兜底开盘即生效。
- monitor: 静默零信号告警（连续 6 轮≈90s 无 bar→信号群 1d241455）+ tf 预热校验。

## v9.0.0
- miji 做T策略初始版本（基线）。

## v9.1.0
- 关键因子研究线基线：三因子共振引擎初版（gravity + vol_div + macd_div，分钟级）。
- 〔注：tag `v9.1.0` 与 `v9.0.0` 同指提交 `ed53f40`，属误打；`v9.0.0` 已删除，以 `v9.1.0` 为准。〕

## v9.1.1
- MACD swap 版：卖点形态→`+1`触发B / 买点形态→`-1`触发S。研究态 skill 由 `-2.44%` 升至 `+2.77%`。
- 〔注：该 swap 使 B/S 语义与 MD 文档"急拉不追→卖 / 急跌不杀→买"相反，v9.1.2 已撤销。〕

## v9.1.2
- 撤销 MACD swap，恢复 MD 文档语义：卖点（价格新高）→`-1`触发S / 买点（价格新低）→`+1`触发B。
- 早盘 `i < LOCAL_W(15)` 降级 gravity-only（macd 未成型时不误杀）。
- webhook 修正；推送双口径（当日% / 持仓%）；对称T卡片。

## v9.1.3（commit `1ed2c7a`）
- A1 动态出场标签：触及上/下轨按价格相对 VWAP±K1·ATR 实际位置动态填，修正 000938 那种"价在+5%上轨之上却标触及下轨"误导。
- A2 涨停开空抑制：当日最高涨幅≥阈值（主板10%/创业板·科创板20%/北交所30%）时 gate 反T开空（锚定 + 开仓双 gate）。
- 数据源硬化：mootdx 主源（= a-stock-data 真实行情）+ 腾讯 `qt.gtimg.cn` 实时快照兜底；空返回显式报错。
- 卡片精简：4 字段（点位/仓位/时间戳/依据）+ 调试参数折叠至「备注」。
- 推送标题动态模板 `{代码} {操作} {仓位}成`（买绿/卖红/出场蓝）。
- 自由双向配对 + 动态 2/4 成仓位（按信号强度，非固定模板）。
- 持续监控：方向冷却改为 bar-index（`COLDOWN_BARS`），不再用冻结墙钟 `now`，replay 单次扫描可捕获全天所有有效波动点。

## v9.1.4（本提交）
- **核心 MACD 严格极值判定**：`local_high = h[i] > h[start:i].max()`、`local_low = lo[i] < lo[start:i].min()`（原 `<=`/`>=` 且切片含自身 `h[start:i+1]` → 改为**严格 `<`/`>` 且对比【前序】窗口极值(不含自身 `h[start:i]`)**）——"价格创新高/新低"须真正严格超越此前所有 K 线，平局(与前高相等)不再触发；避免含自身切片下严格 `>` 恒为 False 导致 MACD 信号整体失效的回归。
- **走平封板保护**：`if h[i] == lo[i]: return 0`（一字/停牌/涨停封板 OHLC 全等）直接跳过，不在平盘 bar 误判局部极值。
- **修复 000938 涨停顶虚假买点**：2026-07-16 `X[B]@10:32`（-9.19%）根因 = 涨停封板平盘 bar 同时满足 `lo[i] <= lo[win].min()` 误判新低 → 虚假 `+1` → 反T回补在涨停顶触发。严格化 + 走平跳过后该回补消失，仅剩合法 `S@09:32`。
- **全量同步**：`macd_divergence_signal` 与 `volume_divergence_signal` 同款修复，且 `core/miji_alpha.py`（实盘）与 `backtest/keyfactor/miji_engine.py`（研究态）语义保持一致，避免 live/回测漂移。
- 清理：`core/monitor.py` 删除死常量 `COLDOWN = 120`（方向冷却已由 `COLDOWN_BARS` 接管）；`data/signal.txt` 解除索引追踪（真正被 .gitignore 忽略）。

## v9.1.5（复盘韧性改进，2026-07-21）
> 背景：当日 161129 全天 0 信号根因 = 开盘数据源（mootdx LOF 分钟K 返回空 + 腾讯兜底同窗口为空 → compute 返 None），且旧进程 tf 初始化窗口抛 NoneType 错；系统因 `errors=0` 静默吞掉整日。
- **① 数据源韧性（core/datasource.py）**：
  - 新增 `_retry_with_backoff`（指数退避 1/2/4s 封顶 <15s），套用于 `intraday()`/`get()`/`historical_1m()` 的 mootdx 取数，失败即重连再试。
  - **修正 3–4 行死区**：旧逻辑 mootdx 给 3–4 行时不走腾讯兜底、compute 又拒收 `<5` → 静默 None；现 mootdx 行数 `<5` 即触发腾讯分时兜底，并**优先真实 OHLC（mootdx≥5 行）**，否则选腾讯；两源皆 `<5` 才返回少量数据（compute 仍拒收，但已显式日志）。
  - 腾讯分时兜底开盘即生效（LOF/T+0 基金救命稻草）。
- **② 静默零信号告警（core/monitor.py，P0）**：
  - `run()` 维护 per-symbol `_miss_{sym}` 计数（持久化于 state.json，跨重启）；本轮无 bar 则 +1，有数据则清零并解除去抖锁。
  - 阈值 `ALERT_MISS_ROUNDS=6`（≈90s 持续无 bar）→ 推**信号群 webhook `1d241455`**（已与用户确认）告警「⚠️ 数据源中断告警：XX 已连续 N 轮无分钟K…」。
  - 误报治理：仅交易时段计数；开盘宽限期 `ALERT_GRACE_MIN=5`（09:30–09:35 与 13:00–13:05 不计数）；去抖（告警一次后置 `alerted_miss_` 锁，数据恢复才清除，避免刷屏）。
  - `load_state` 清理新增 `_miss_`/`alerted_miss_` 键（跨日重置）。
- **③ tf 预热（core/monitor.py）**：
  - 新增 `_warmup_tf()`：进程锁后、主循环前强制 `TickFlow()` + 建立连接 + 校验取数（对标 `_server_ok`），失败指数退避重试 3 次；全失败返回 False 并标记 `st['_tf_unhealthy']`，**不退出进程**（避免与自启冲突），交由 ② 感知。
  - `if tf is None` 软兜底保留；初始化失败一次性推 `🚨 数据源初始化失败` 告警。
- 验证：`py_compile` 通过；单测 `_retry_with_backoff`（重试/超限抛）、`intraday` 源选择 5 场景、开盘宽限期边界 + 去抖状态机全 OK；真实 1m 重放无回归（161129 今日仍 `strict(B=2,S=1)`）。
