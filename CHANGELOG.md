# miji 版本算法说明（CHANGELOG）

> 版本号规则：MAJOR.MINOR.PATCH（完整规则见 `docs/versioning.md`，每次改动必须对照判断）。
> 说明仅标注各版本**核心算法与信号语义**的差异，便于回溯。

## v9.3.0 — 生产优化版（08-02 部署线，P0-P3 落地）
> 父版本：v9.2.2（floor 漏顶漏底修复基线）。2026-08-02 上线，watchdog v3.1 周一 08-03 09:25 自动拉起生效。
> 状态：**生产发布版**（tag v9.3.0，分支 release/v9.3.0）。

### 本版本落地项（相对 v9.2.2 的信号语义变化）
- **P0 — MACD 背离强度门槛 mhd=0.15**：`core/miji_alpha.py` 新增 `MHD_THRESHOLD = env TP_MHD_THRESHOLD（默认 0.15）`；`check_miji_trigger/check_b_trigger/check_s_trigger` 透传 `min_hist_diff`（生产此前恒 0.0，本次真正接入）。弱背离（hist 差值 < 0.15）= 噪音不再触发。
- **P1 — ATR 波动率门控（per-symbol）**：`atr_min_pct`（默认 None=关，env TP_ATR_MIN_PCT），watchlist 5 只全部启用 0.25；滤低波动标的的烂信号。
- **P3-1 — 多周期 MACD 方向过滤 mpr_b60（per-symbol）**：B 侧 60m 同向过滤，仅空仓 B 入场透传；S 侧不动。`data/monitor_config.json` 热重载。
- **早盘规则**：`TP_MORNING_B_MHD` 默认关（实证 mhd 门槛放宽会放回噪音）。
- **出场/成本**：`core/exit_manager.py` 用户费率成本模型（佣金万一、印花税卖出万5.641 仅股票、滑点 2bps）；`core/datasource.py` 腾讯域名池 failover + 新浪 1m 第三级兜底。
- **S 信号专项（阶段D）**：`gate_sell` 加 vwap_dev_ceil/atr_min_pct_s 参数，**回测结论不落地**（S_vwap_dev 最优箱是入场过滤语义，与 s_signal_exit 出场架构错配，负优化已放弃）。

### 验证
- 40 只调参池固定全集口径 + watchlist 5 只独立验证；ATR×mpr 叠加中位胜率 47.8→56.2%（+8.4pp）；B 信号保留 47%。
- 自检 9/9 通过；生产配置触发链路（600570 07-16 → B=6 S=2）；watchlist==monitor_config。
- 部署验收报告：`output/deploy_acceptance_20260802.html`。

## research/v9.3.0-mtf — MTF 多时间框架共振研究 (V15) + 盲 holdout 结论（研究态，未发布）
> 研究线：在 v9.2.x floor 门控 + miji_alpha 因果摆点之上，验证「高周期(15m)同向摆点共振门控」能否把分钟级信号从 HFT 噪声中分离出可交易 edge。
> 状态：**研究完成，结论为「降噪有效、无泛化 edge」，不进入生产。**

### 背景与动机
- 方向一（多时间框架共振）是 miji 分钟级信号框架的延续优化；战场 = 5-∞ 分钟 T+0 区间 + T+1 隔夜 swing。
- 已知痛点：5-30 分 HFT 噪声桶（PF 0.37）拖垮池化 PF 至 0.88；|imb| regime 滤镜方向反了（moved 组 PF 反而更高）。

### 方法（floord 因果摆点 + MTF 门控）
- 信号：沿用 floord 因果 K-bar 摆点（W=K=5, GAP=3）+ miji_alpha，1m 信号确认 bar 下一 bar 收盘执行（严格因果、无前视）。
- MTF 共振门控：当日 1m 聚合成 5m/15m 连续 K 线（跨午休不串根），同阈值/同 basis 跑 pivot_signals；高周期摆点可见 1m 索引 = (idx+1)*tf，与 1m 自身 exec=idx+1 平行 → 无前视。
- 共振判据：1m 买信号执行前，要求存在同向高周期摆点，可见索引 j≤i 且 i−j≤lookback（V15: 15m/240；V5: 5m/120）。
- 变体：V1(无门控) / V15(15m) / V5(5m) / Vboth(OR)。
- 复用：真实成本（个股买0.05%/卖0.10%含印花税，ETF 0.09%来回）、最小持有5min、止损1.5%、最长3日、T+1/T+0 区分、IS/OOS 拆分（61 天窗口前半/后半）。

### 关键结果
- **in-sample（8 只手工标的 = 茅台/平安/宁德/招行/华虹/璞泰来/中韩ETF/原油LOF，P0-A+B 配置）**：
  - 池化 PF：V1 0.88 → **V15 1.08**；净额 −84% → +17.4%。
  - 增益集中在华虹(688347, V15 PF 1.95) + 中韩ETF(513310, V15 PF 1.15) 两只；其余 6 只 V15 仍 <1。
  - 5m 共振(V5)反而更差(0.79)，Vboth(OR)被稀释回 0.87 → 共振须站足够高周期，5m 仍是噪声。
- **盲 holdout（同窗口、V15 原参数不重调）**：
  - 159985 豆粕ETF（缓存未调参，同窗口）：P0-A+B/V15 PF=0.89（净额 −0.9%）→ 未复现 edge。
  - **40 只新鲜 T+0 ETF/LOF**（37 只 mootdx 现拉 + 缓存 513040/518880/159985，longonly）：**池化 PF=0.605（整池亏损）**；PF>1 仅 6/40（15%）；其中 162411/164824 笔数仅 13-17（小样本假阳性）。
  - 真正边际 PF>1 的（513100纳指1.61 / 513500标普1.34 / 513520日经1.12 / 159980有色1.14）在成本+噪声下不显著。

### 方法论收获（最重要）
- 「in-sample 内部 OOS 切分」只能防参数过拟合，**防不住宇宙过拟合**（从 8 只里挑 2 幸存者 = 极端事后选择）。
- 升级为「**新宇宙盲 holdout**」：参数与标的在调参时均不可见，同一 61 天窗口、V15 原参数不重调 → 才戳穿选择偏差。
- 任何「某策略在某标的上 PF>1」的声称，今后必须过盲 holdout 这一关。

### 结论
- V15 作为**降噪器有效**（砍掉 HFT 噪声桶、保住 swing 段），但**未制造可泛化 alpha**；盲池池化 PF 0.605 钉死这点。
- 撤回此前「上线 688347+513310」建议（事后选择偏差）。miji V15 门控目前不足以构成可上线模块。
- 下一步：① 对 4075 只个股的 T+1 隔夜 regime 跑同样盲 holdout（验证 floord 原本战场）；② 见「V15 优化方向」讨论。

## research/v9.3.0-vwap-mr — VWAP 均值回归移植 + 盲 holdout（研究态，未发布）
> 研究线：把聚宽 ATR/VWAP 分时做T 文档的「VWAP 均值回归做T」移植进 miji 盲 holdout 框架，验证「日内均值回归」是否比 floord 摆点有更优的泛化 edge。
> 状态：**盲 holdout 结论与 miji floord 一致 —— 无泛化 edge。**
> 分支：`exp/v9.3.0-vwap-mr`（基于 release/v9.2.2 @ 66b4b4d）。脚本：`backtest/keyfactor/miji_vwap_mr.py`。

### 方法（复用 miji harness）
- 信号：当日累计量价 VWAP = cumsum(close*vol)/cumsum(vol)（因果）；偏离 VWAP ≥0.8% 反向做T，止损0.5%/止盈0.6%（入场价计），回归 VWAP 平仓，冷却10根（docx 原参数，**不重调**）。
- 战场/成本复用：T+0 ETF/LOF（longonly）多空日内往返、尾盘强平，成本买0.05%/卖0.05%；T+1 个股（bidirectional）仅做多（买背离），当日不可卖 → 次日起 revert/stop/最长3日平仓，成本买0.05%/卖0.10%含印花税。
- 盲 holdout 框架复用：窗口严格 = miji in-sample 同一 61 天（2026-04-17..2026-07-16，读 metrics.json['common']）；T+0 盲池=40 只（缓存+现拉 ETF/LOF，排除 in-sample 513310/161129）；T+1 盲池=从4075 缓存个股先验等距抽样 124 只（排除 in-sample 个股，覆盖≥55/61天）。容错加载：缓存存在交易所后缀错配（如 513310.SZ 存为 513310.SH_1m.csv）按数字前缀 glob 解析。

### 关键结果（与 miji MTF 盲 holdout 对比）
- **T+0 盲池（40 只）**：PF>1 = **7/40（17.5%）**；**池化 PF = 0.590**（整池亏损）。
  - 对比 miji MTF：6/40（15%）、池化 0.605 → VWAP 命中率略高但池化 PF 几乎相同，**二者统计不可区分，皆 <1**。
  - 7 个「赢家」中 160140/164824 笔数仅 2（小样本假阳性），其余 n=9-37、PF 1.0-2.7，在成本+噪声下不显著。
- **T+1 盲池（124 抽样，有效 111）**：PF>1 = **1/111（0.9%）**；**池化 PF = 0.513**（整池亏损）。唯一 601005.SH（n=39, PF=1.32）近乎噪声。
- **in-sample 参考（7 只有数据，601318 无缓存）**：PF>1 = **0/7**（161129 0.58 / 300750 0.79 / 513310 0.54 / 600036 0.23 / 600519 0.11 / 603659 0.31 / 688347 0.95）→ VWAP 连 miji「幸存者」都未讨好。

### 结论
- **VWAP 均值回归（docx 头牌策略）在盲 holdout 上同样证伪**：与 floord 摆点高度同构（T+0 命中率 15%↔17.5%、池化 PF 0.605↔0.590），说明问题在「T+0 ETF/LOF 日内均值回归」这类信号本身在该 61 天窗口/regime 下无 edge，而非某个具体信号实现。
- 文档自报 65% 胜率 = 510300 单标的 in-sample 产物，正是我们已逃离的幸存者陷阱；本盲 holdout 框架即为其反向验证。
- **miji 作为「可交易策略」彻底证伪**（floord 摆点 + VWAP 均值回归两类基础信号均过不了盲 holdout）。应回归其生产定位：**盘中监控信号**（floor 门控 + miji_alpha 的异动提示），而非回测交易策略。
- T+1 隔夜「买背离持有」 adaptation 也失败，但属对 docx 日内策略的隔夜改造，非 docx 本意，结论权重低于 T+0 对比。

### 产物
- 脚本：`backtest/keyfactor/miji_floord_mtf.py`（MTF共振）、`miji_floord_holdout.py`（单标的holdout）、`miji_floord_holdout_batch.py`（批量盲holdout）、`fetch_holdout_1m.py`（拉37只新鲜T0）、`probe_holdout_pool.py`（候选池探测）。
- 报告：`output/miji_floord_mtf/report.html`、`output/miji_floord_holdout/holdout_metrics.json`、`output/miji_floord_holdout_batch/batch_report.html` + `batch_metrics.json`。

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

## v9.3.1（2026-08-04）每日自迭代小版本 —— 复盘报告实盘化重构 + 因子寻优引擎上线
- 报告改为实盘推送视角五节（〇投递诊断/一round-trip有效性/二负收益根因/三实盘基线/四行情图仅实推标的/五优化空间清单），复算内容全部移出报告转后台
- 新增 live_roundtrip_review.py（实推配对/净盈亏口径/波动段归因/优化空间生成）、factor_optimizer.py（trail+atr网格寻优）、daily_iterate.py（每日自迭代闭环+护栏热更+版本记录）、push_tpoint_review.py（动态标题单群推送）
- review_charts.py 改 1m 分时+实推标注+仅推送标的；build_review_html.py 全部重写
- core/monitor.py 写后缓冲（_buffered_append/ctypes降级/自愈回写，防落盘断流丢审计）；prod_vs_bt_reconcile.py live_counts 改明细优先+state_mismatch哨兵
- 价格口径：当日同源行情用推送价（实证 bar close 会错判盈亏）；配对同分钟去重
- 寻优首跑：4/5只 trail→0.5/0.5 候选（+3.7~12.5pp）待两段式复核；atr 维持 0.25
- 注：core/monitor.py 同时包含 08-02/08-03 工作区遗留的 ML shadow 接入改动（一并入库）

## v9.4.0（2026-08-04）大版本 —— T+0/T+1 结算制度拆分（开关默认关，待用户确认启用）
- 新增 core/settle_rules.py：T0/T1 规则模块（T1 每方向每日1次完整往返状态机；T0 原样）
- core/monitor.py：_risk_gate 后接入 settle_rules.filter_signals，开关=_global.settle_split_enable（默认 false，生产行为与 v9.3.1 一致）
- docs/t0_t1_split.md：差异口径（信号生成/调仓频率/成交规则）与回滚说明
