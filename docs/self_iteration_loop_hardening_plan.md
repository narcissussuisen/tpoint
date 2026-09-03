# tpoint 自迭代闭环硬化方案 v2.1（v11.0.0 前置基建）

- 日期：2026-09-02（v1 审计响应版）；09-02 夜 v2（评审整合）；**09-03 夜 v2.1：新增 T1.5 仓位模型统一**（闭环系统发现 P0-20260903-reverseT-not-modeled，经代码级核验坐实）
- 依据：2026-09-02 外部审计报告 + 6 个关键文件实际核验 + 外部评审报告（7 修正/T0 新增/3 拍板建议）+ **2026-09-03 闭环 P0 研判（三套仓位模型并存）逐行核验（6 项全部坐实，另发现寻优链"漏反T"失真模式）**
- 定位：**这不是平行新计划，是 R2P 计划 R0（基建）的补强版** —— R2P 原案未覆盖口径泄漏、失败语义、成交口径、仓位模型四个可信性缺陷；R5（regime 门控）因第一性算术论证提前为首个候选变更。R3（ML 灰度）/R4（T0T 借鉴）不变，排后。

## 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1 | 09-02 | 审计响应初版：T1-T8 任务卡 |
| v2 | 09-02 夜 | 评审整合：新增 T0；T1 run_id/产物校验/RUNNING 预写/五态；T2 两层确定性；T3 拆 A/B+exec 四元组；T4 hash 分层；T5 七态+deploy gate；T6 daemon_stage；T7 shadow 池纠错；T8 动作分离+量纲公式；硬门槛六条；四项拍板 |
| v2.1 | 09-03 夜 | **新增 T1.5 仓位模型统一**（P0-20260903-reverseT-not-modeled）；硬门槛第七条（仓位模型未统一→回测侧数字只作趋势参考）；排序 T0→T1→**T1.5**→T2→…；新增 §8 口径污染勘误表；T2 复核表明确依赖 T1.5 先行 |

## 0. 核心诊断

1. **backlog 只写不消费**。`data/feedback_backlog.jsonl` 08-11 就写入了同参修复（`signal_exit_same_param`）、+1 bar 延迟（`exec_delay_bars`）、涨跌停过滤、成本压力测试的完整方案，至今除 selfheal/no-oos 外全部 `status: open`。系统缺的不是发现问题能力，是把 backlog 当工作队列调度的闭环。
2. **loop_engine 空转**（`loop_engine.py:46-52`）：全阶段 done 后每日 15:05 空返回并推送"全部完成"。P11 改进 +1.04pp 标注"研究态不入生产"。
3. **bat 吞错**（`run_daily_review.bat` 全文）：所有步骤 `if errorlevel 1 echo [WARN]` 后继续，任务永远显示成功。
4. **无版本锚点**：信号、live_review、closed_loop_state 均不带 config_hash，实盘表现无法归因到配置版本（当前 `closed_loop_state.json` 的 config_snapshot 为空对象）。**深层版本="文件生成了，但不知道是哪套算法生成的"**——运行身份（run identity）缺失。
5. **回测可信性缺陷**：factor_optimizer 混合口径（同参两跑 Δwr −2.0/+3.2pp，backlog `P0-20260811-cfg-state-leak` 仍 open——已修 oos_validate.py，factor_optimizer.py 仍带泄漏）；信号 bar 即成交；涨跌停/停牌不过滤。
6. **成本倒挂**（第一性算术）：603318 往返成本 0.116%（佣金 0.02 + 印花 0.056 + 滑点 0.04），已观测交易日池级 ATR 0.108–0.181%，多数日子低于绝对成本下限。**参数级调优无法改变这个算术，只能做 regime 维度的开仓资格过滤**。现有 `regime_gate` 只管趋势维度（downtrend suppress），缺波动率维度。

## 1. 总原则

**先校准武器（L0）→ 再装自动扳机（L1）→ 打靶验证（L2）。顺序不可逆。**

理由：loop_engine 当前空转是"最安全的状态"。若不先修复口径泄漏就让它常驻并自动 promote/rollback，等于让一个产出不可信结论的引擎拿生产做实验。

### 硬门槛七条（各任务验收 gate，violation = 禁止）

```
T1.5 未通过：回测/对账/门控的净收益数字只作趋势参考，禁止作任何上线/回退/promote 判据
T2 未通过：禁止任何自动调参
T3 未通过：禁止使用新收益数字做基线
T4 未通过：禁止声称线上效果可归因
T5 未通过：backlog 仍然只是文档，不算闭环
T6 未通过：loop_engine 只能验证，不能 promote/rollback
T8 未通过：vol-regime 只能 shadow-only
```

## 2. 任务卡

### T0 运行身份与配置一致性（L0，0.5d，P0）【v2 新增】

- 新增 `scripts/runtime_identity.py`：
  - `--begin`：生成 `run_id = YYYYMMDD-HHMMSS-<pid>`，落盘 `data/runtime_identity/YYYY-MM-DD/<run_id>.json`，同时写 `data/step_status/current_run.json`（T1 的 run 锚点）。
  - 采集：run_id / started_at / git_commit / git_dirty / VERSION / strategy_version / python_executable / execution_model_version / config_hash / watchlist_hash / model_hash。
  - `effective_strategy_hash` 占位 null，**T4 完成后回填**；hash 函数（canonical JSON + sha256）以 runtime_identity.py 为唯一实现源，T4 的 effect_ledger.py 必须 import 复用（同源同模块，禁止两套实现）。
  - 一致性校验（warn 不阻断，全部写入产物）：monitor 与复盘的配置路径一致（同 ROOT/data/）；watchlist.json 标的集合 vs monitor_config per_symbol 键集合（per_symbol 非空时多余键=陈旧残留告警）；git dirty 状态显式记录。
- `run_daily_review.bat` 第一步前调用。
- 验收：两次调用 run_id 唯一；产物含全部身份字段；watchlist 与 per_symbol 不一致时能在产物中看到告警。

### T1 失败语义透传（L0，0.5d，P0）

- 新增 `scripts/pipeline_status.py`（T1 核心）：
  - 状态七值：`RUNNING / OK / DEGRADED / FAILED / SKIPPED / NOT_RUN / INTERRUPTED`。终态判定：rc!=0 → FAILED；rc==0 且 expected_outputs 缺失 → DEGRADED；rc==0 且产物齐全 → OK；rc==77（约定跳过码）或 `--status skipped` → SKIPPED；EXPECTED_STEPS 清单内当日无任何记录 → NOT_RUN；某步最后一条记录为 RUNNING（进程中途被杀，上轮成功态不再残留）→ INTERRUPTED。
  - `running <step>`：步骤开始前预写 RUNNING 记录（评审修正：否则 Python 被杀后留下上一轮成功状态）。
  - `record <step> <rc> [--expected p1;p2]`：捕获 rc 前必须先 `set RC=%ERRORLEVEL%`（record 调用自身会重置 errorlevel）；校验 expected_outputs 存在性；追加 `data/step_status/YYYY-MM-DD.jsonl`，**每条记录带 run_id**。
  - `summarize`：只读取**当前日期 + 当前 run_id**（从 current_run.json 继承）的记录，每步取最后一条——防手工补跑两次时旧失败/旧成功互相污染（评审修正 #2）。
- `run_daily_review.bat`：头部 T0 `--begin`；每步前 running、每步后 record（关键步配 expected_outputs）；尾部 summarize，关键步（live_review / reconcile / daily_report / daily_iterate / closed_loop / auto_tune）任一 FAILED/INTERRUPTED → 推 b4eba7a9 全局群 + `exit /b 2`（计划任务显示失败）。
- `daily_report_push.py`：读当日 step_status（当前 run_id），任一 FAIL → 日报标题前缀 `[DEGRADED]`。
- 验收：手工注入 daily_iterate 失败（非法参数）→ bat exit 2 + 飞书告警 + schtasks Last Result=0x2；手工杀进程 → 该步显 INTERRUPTED 而非上轮 OK。
- ⚠️ bat 三铁律：CRLF、usebackq 禁双引号、引用脚本先验证存在；编辑前备份。

### T1.5 仓位模型统一（L0，1.5-2d，P0）【v2.1 新增，09-03 闭环 P0 坐实】

**问题（代码级核验 6/6 坐实）**：三套仓位模型并存，生产/门控/寻优路径全用错——

| 模型 | 语义 | 缺陷 | 被谁误用 |
|---|---|---|---|
| `exit_manager.simulate_day` | 纯多头（空仓只认 B，L158-170；S 只作出场，L201） | 反T 日的 S 被忽略、B 回补被误判正T 建仓持到 EOD → **系统性虚亏** | prod_vs_bt_reconcile L308、factor_optimizer L133、oos_validate、auto_tune（**寻优链 = 纯正T 口径，漏反T**） |
| `simulate_bidirectional` | 空仓遇 S 直接建反向仓（L50-60，无底仓检查） | **裸卖空**，虚高反T | p10_oos_verify L88-89、p11_gt_tb_v11 L177-178、p7_bs_balance L96、backtest_general_v5(simulate_dual)——与 simulate_day **无条件相加 = 同一组信号双重计费** |
| `simulate_base_position` | 唯一合规底仓模型（base>0 才许反T，正T/反T/持仓盈亏分栏） | 全仓仅 scripts/evaluate_base_position.py 一处引用 | 无人误用——**正确模型没人用** |

**实证（09-03 603318，同一组实盘推送 S@10:05 9.62 / B@10:35 9.41）**：live_roundtrip_review 反T 净 **+2.067%** vs reconcile 伪正T **-2.348%**，差 4.42pp 方向相反。反T 占实盘 trip 61%（22/36）；双非零日符号一致率仅 27.3%；pearson(live, recalc) = -0.184。

**任务内容**：
1. **接线（结构性改动，非纯接线）**：p10_oos_verify / p11_gt_tb_v11 / p7_bs_balance / backtest_general_v5 / prod_vs_bt_reconcile / factor_optimizer / oos_validate / auto_tune / backtest_screener 的仓位模型统一切到 `simulate_base_position`（跨日连续模拟，带底仓状态）。⚠️ 注意：simulate_base_position 是**跨日**模型（sigs_by_day），p10/p11/reconcile 现为**逐日独立**调用——接线需把调用结构改为按 symbol 收集多日后连续模拟，属中等重构（非零成本）。
2. **reconcile 底仓状态来源（待拍板）**：单日对账时底仓状态从哪来——
   - 方案 A：从前一交易日 reconcile 底仓状态续算（ledger 式跨日续传）；
   - 方案 B：从实盘 monitor 持仓状态读取（最忠实，依赖 pos 状态记录）；
   - 方案 C：当日无底仓证据时保守禁反T 统计（最保守，牺牲覆盖率）。
3. **静态断言（防复发）**：tests 增 grep 式检查——全仓禁止 `simulate_day(...) + simulate_bidirectional(...)` 无条件相加模式；新代码引用仓位模拟必须经 simulate_base_position 或显式 base>0 门控。
4. **重跑复核**：08-04 至今 reconcile + roll20 WR 复核（wr_prod 57.1 保留=实盘口径；wr_recalc 53.8 / g1 -3.3pp 作废重建）+ p10/p11 门重跑。
5. **勘误标注**：CHANGELOG 对 p10 验收数字（ML 过滤 -129.14→-39.80）加"口径污染勘误"标注——A/B 相对方向或有参考价值，绝对量级不可信。
6. **过渡期纪律（立即生效）**：reconcile 逐日 net/wr 与 p10/p11 门结果只作趋势参考，禁止作单日上线/回退判据。

**不受影响（缓冲带）**：生产 monitor 信号行为（monitor 侧已强制底仓模型）；live_roundtrip_review（实盘配对 = 真相源）；C_prod 56.2% 基线与 roll20 wr_prod 57.1（均为实盘 live 口径，不经回测口径）。

**验收**：接线后全链路无 simulate_day+bidirectional 相加（静态断言 PASS）；09-03 603318 reconcile 复算结果与 live_review 符号一致（反T 净 +2%±0.5pp）；08-04 至今 reconcile 重跑落盘 + roll20 g1 重建。

**backlog 映射**：P0-20260903-reverseT-not-modeled → 本卡（这是 backlog 驱动施工的第一单，本身即 T5 闭环价值的实战验证）。

### T2 寻优同参 + 确定性（L0，1.5d，P0）

- `factor_optimizer.eval_config`：**deepcopy 原配置 → apply_candidate → evaluate → finally restore 包住整个网格单元**（评审修正：不能只围单个赋值）。边界处理五项：原配置不存在 / 原配置半缺（只有 trail_pct 无 trail_activate_pct）/ 候选评估抛异常 / 进程中断退出 / 多标的切换。
- 报告 JSON 加 `signal_exit_same_param: true` / `param_hash` / `input_data_hash` / `config_hash` / `random_seed: 0`。
- 新增 `tests/test_optimizer_determinism.py`，**两层验收**（评审修正：不要求裸逐字节一致）：
  1. 规范化结果 hash 一致（剔除 generated_at / 机器路径 / 环境信息 / 枚举顺序 / 浮点格式后）；
  2. 去除运行元数据后的报告逐字节一致。
  断言 513310 (0.3/0.5) 案例不再出现 n=33/31 漂移。
- **闭环收尾（评审修正 #3）**：T2 完成后把 backlog `P0-20260811-cfg-state-leak` 标记 `fixed`（附再验证证据），并**用修复后的优化器重新生成候选**——不能只修 oos_validate.py 就完事。
- 复核（backlog `P0-20260811-reverify-0805`）：修复口径重跑 161129/300308/688111/513310 现行 trail（backlog 原文四标的；v1 误写 300759，**v2 纠正：300759 无任何回测痕迹，系笔误**），输出复核表进周报。
- 验收：两层确定性回归 PASS + 复核表落盘 + backlog 状态闭环。

### T3 成交口径修正（L0，1d，P0）——分两阶段【v2 拆分】

- **T3-A（只增不改）**：`core/exit_manager.simulate_day` 新增字段与开关，旧口径默认保留：
  - `exec_delay_bars` 开关（默认 1）。语义精确定义：B/S 信号在 bar i 收盘确认 → 成交在 bar i+1 开盘；exit 信号在 bar j 收盘确认 → 成交在 bar j+1 开盘。**禁止简单把 entry/exit 价格索引 +1**——必须处理：exit_exec_bar ≤ entry_exec_bar（非法，放弃）；期间已出现新反向信号（吃新信号）；次 bar 不存在（尾盘）；EOD 强平与正常 exit 重复计算。
  - 每条 trip 增加审计四元组：`entry_signal_bar / entry_exec_bar / exit_signal_bar / exit_exec_bar` + `entry_exec_reason / exit_exec_reason`。
  - 尾盘信号无次 bar → 放弃 trip，记 `end_of_day_skip` **独立计数，不混入 n_loss**。
  - 可交易性：一字板判定 = high==low **且按板块计算触及涨跌停价**（主板 10% / 创业板科创板 20% / ST 5%，基于前收；LOF 按基金规则）+ 当日有效成交量 >0；volume==0/缺 bar 拒绝；`skipped_untradable` 独立计数进报告。
  - 输出口径对照报告：旧/新 total_ret、旧/新 WR、受影响 trip 数、放弃 trip 数、无法成交 trip 数、尾盘无次 bar trip 数、滑点压力（3bps/边）下差值、盈亏平衡滑点。
- **T3-B（正式切换，审计后）**：对照报告经审计确认后，新口径设为唯一基线；旧基线标注 `legacy_samebar` **永久保留、标记废弃、不参与任何自动 promote/rollback 比较**；新基线自 T3-B 完成日起生效（防止新旧口径混比）。
- `oos_validate` / `auto_tune`：滑点 3bps/边 压力口径复核（backlog `P2-cost-sensitivity`）。
- **已拍板（09-02 夜）**：接受历史回测数字变差——挤水分不是退化。
- 验收：T3-A 口径对照报告落盘 → T3-B 切换 + legacy 标记；改 core/ 后杀 monitor 进程让 watchdog 重拉。

### T4 effect ledger（L0，1d，P0）

- 新增 `scripts/effect_ledger.py`，**hash 输入范围分四层**（评审修正：不能只 hash monitor_config.json）：
  - `config_hash`：规范化 monitor_config.json（排序键、剔除 `_note*`）；
  - `watchlist_hash`：规范化 watchlist.json；
  - `model_hash`：data/ml/topbottom_xgb.json（存在时）；
  - `execution_model_version`：`samebar-legacy`（T3 前）/ `nextbar-v1`（T3 后）+ `strategy_version` + VERSION；
  - 组合生成 `effective_strategy_hash` = sha256(以上全部)，**T0 的占位字段在此回填**。hash 函数 import 自 runtime_identity.py（同源）。
- `core/monitor.py` 推信号时在 push_audit.jsonl 每条记录附 `effective_strategy_hash` + `config_hash` + `watchlist_hash`。**计算时机=实际发送前每轮扫描重算**（非进程启动缓存；v1 已如此设计，v2 增加测试锁死）。
- 新增热更测试（评审修正）：运行中修改 monitor_config → 下一条信号 hash 必须变化。
- `live_roundtrip_review.py` / `daily_closed_loop.py`：每日汇总 append 到 `data/effect_ledger.jsonl`：`date / run_id / effective_strategy_hash / params / regime / pushed / paired / net_ret / valid_rate`。
- `closed_loop_state.json` 快照补全（替换空 `{}`）。
- 验收：连续 2 个交易日后任意信号可反查当日 config_hash；ledger 无断天；热更测试 PASS。

### T5 feedback 调度与认领（L1，0.5d，P0）——施工与部署分离【v2 重设计】

- schtasks 注册 `tpoint_feedback_loop`：交易日 15:45（daily_review 15:30 之后），venv pythonw 驱动 `scripts/feedback_loop.py`。
- feedback_loop 状态机改为七态（评审修正：不做 open→applied 一步跳，与 feedback_loop.py:9 现有职责边界"不改 core/生产代码、不改 monitor_config.json"自洽）：
  ```
  open → triaged → claimed → implementing → staged → verified → closed
  ```
  - `staged`：代码/配置已在非生产或灰度环境准备好（未上生产）；
  - `verified`：有测试/回测/影子数据证据；
  - `closed`：PM 或用户批准完成。
- 每条 backlog 增补字段：`owner / mapped_task / change_scope(research|infra|production) / requires_approval / validation_commands / evidence_paths / rollback_plan`。
- **deploy gate 铁律**：生产配置变更**唯一通道 = T6 PM 裁决器**；feedback_loop.py 推进上限 = staged。`change_scope=research/infra` 可自动推进施工；`production` 必须 requires_approval + T6 gate（防止把"已 staged"误读为"已上线"）。
- 验收：schtasks /query 显示 Ready；次日 15:45 实跑有日志；backlog 至少一条走完 triaged→staged。

### T6 常驻 PM 裁决器（L1，1.5d，P1）——daemon_stage 设计【v2 重设计】

- 不把 PM 作为普通一次性 stage（v1 缺陷：done 会重新空转，pending 会每轮重复初始化）。改为 loop_state.json 显式常驻阶段：
  ```json
  "p_monitor": {"status": "monitoring", "kind": "daemon_stage", "last_run": "...", "last_decision": "...", "retry": 0}
  ```
  loop_engine.py `run_once` 增加分支：`if stage.kind == "daemon_stage": run_monitor(stage); return 0`（不参与 current_stage 的 done/pending 推进语义）。
- **幂等四原则**：同一日期只裁决一次；同一 shadow 结果不重复 promote；同一 rollback 不重复执行；每次决策写唯一 decision_id + 每次生产变更写前后 hash（防计划任务重试连续改两次配置）。
- 每日 15:05：读 effect_ledger 昨日条目 vs shadow 池对照 → GRAY 项 promote/rollback 裁决 → backlog open 项生成待施工摘要推 a35d7f52。
- promote 门槛（用户 08-26 已授权全自动合入，首次 promote 强制飞书通知）：影子期 ≥10 交易日；滚动 20d 净 WR ≥ 基线 +2pp；n≥30；滑点压力口径不降级。
- rollback 门槛：滚动 5d 净收益 < 基线 −1pp 自动回滚 + ledger 归档。
- 验收：15:05 产出 PM 日报，不再出现"全部完成"空转消息；同日重跑决策幂等（decision_id 去重）。

### T7 shadow 基准池（L2，1d，P1）——标的清单已纠错【v2 修正】

- 固定池 `data/shadow_pool.json`：**161129 / 300058 / 600570 / 513310 / 688111**（真实 C_prod 基线池，与 v9.3.0 基线 net WR 56.2% 可比）。
  - ⚠️ v1 误写 603039/300759：backtest 产物对二者零命中（603039 系个股关注清单串台、300759 疑与 backlog 300757 混淆）。评审报告沿用此错并据其论证"历史可比性"——恰恰不成立，v2 纠正。证据：`docs/reconcile_prod_vs_bt.md` §2 五标的双向对账表（池级 55.0% vs 生产 56.2%）。
  - shadow_pool.json 快照结构（评审建议采纳）：`pool_version / symbols / baseline_config_hash(T4 后回填) / baseline_start_date / baseline_end_date`。
  - 与生产并存说明：09-01 起 watchlist 已只剩 603318；shadow 池只记账不推送，无冲突。300058/600570 历史 1m 覆盖短无碍——shadow 为日增量口径，历史对照直接引用 v9.3.0 基线报告数字。
- `live_roundtrip_review.py --shadow`：对 shadow 池按当前生产参数跑同口径复盘，只落账本不推信号。
- 日报增 prod vs shadow 对照段：标的效应 ≈ prod − shadow_mean；解决"9 次换标的无基线"与"单标的样本不足"两个问题。
- 验收：连续 5 个交易日影子账本完整。

### T8 vol-regime 门控（L2，2d + 10 交易日影子，P1）——动作分离验证【v2 细化】

- **量纲公式（评审修正，防两个"ATR"混淆）**：
  ```
  required_intraday_move_pct = round_trip_cost_pct / expected_capture_ratio
  分档比较：pool_atr_pct / required_intraday_move_pct
  ```
  capture_ratio 用 F 盘历史（毛收益/当日 ATR）分位数估计；round_trip_cost_pct 按标的类型（股票含印花税 / ETF 无）分别计算。
- **第一阶段只 shadow 记录，零生产行为**（评审修正：门控与阈值调节分离验证）：
  - 当前生产算法照常推送；
  - vol-regime 只记录 `would_push / would_block / would_tighten / hypothetical_pnl`（含模拟配对 + hypothetical_net）；
  - **不同时改变** buy_threshold / sell_threshold / atr_min_pct / 是否推送——否则效果变化无法归因到具体动作。
- 三档（写入 `general_algorithm.vol_regime` 热重载旋钮）：
  - pool_atr ≥ 2×required_move：VOL_NORMAL；
  - 1–2×：VOL_TIGHTEN（buy_threshold 0.45→0.6，atr_min_pct 上调）；
  - < 1×：VOL_BLOCK → shadow-only——信号照算照记账（保完整统计流），不实盘推送（止血）。
- 生命周期：T2/T3 修复口径后 IS/OOS → GRAY ≥10 交易日（shadow 记录）→ T6 PM 裁决 → promote 时升 **v11.0.0** 正式切生产行为。**此变更同时是 L1 状态机的端到端验收用例**。
- **已拍板（09-02 夜）**：接受 VOL_BLOCK 档 shadow-only——成本倒挂（0.116% vs ATR 0.108-0.181%）下继续全推实盘是收集低质量交易而非增加有效样本。
- 验收：影子期完整账本（would_* 字段齐全）；promote/rollback 无论结果，决策均入 ledger。

## 3. 依赖与排期

```
并行组（Day 1 起，互不依赖）：
  T0 运行身份 ──→ T1 失败语义（T0 的 run_id 是 T1 的锚点，先后紧邻）✅ 已交付（09-03 首跑验收 PASS）
  T4 effect ledger（hash 地基，不改变交易行为）
  T5 feedback 调度骨架（schtasks + 七态状态机，先做调度骨架）

串行链（v2.1）：
  T1.5 仓位模型统一 → T2 寻优同参+确定性 → T3-A/B 成交口径 → T7 shadow 池 → T6 daemon PM → T8 vol-regime
  （T1.5 必须先于 T2/T3/T8：复核表、口径对照、IS/OOS 全部依赖正确仓位模型——
    否则 T2 复核表用污染口径重跑一遍 = 白跑；T2 的"同参修复"部分与 T1.5 正交可并行）
```

- 建议开工序（v2.1）：**T0 → T1 → T1.5 → T2 → T3 → T4 → T5 → T7 → T6 → T8**；工程并行：T0/T1 已完成，T4/T5 可与 T1.5 并行推进。
- 串行总工期 ~10.5 个工作日（T1.5 +2d）；并行可压至 ~7.5 天；之后 T8 影子期 10 个交易日。
- **进度：T0/T1 已交付（commit 2c9c595，09-03 首跑验收 PASS：22 条记录 11 步全 OK，run_id 隔离生效，日报无 [DEGRADED]）。**

## 4. 版本规划（docs/versioning.md 口径）

| 任务 | 版本 | 说明 |
|---|---|---|
| T0/T1/T2/T4/T5 | PATCH（v10.9.1+） | 修复与基建，不改信号 |
| T3 | PATCH + CHANGELOG 醒目标注 | 回测口径重算，旧基线 legacy_samebar 作废 |
| T6/T7 | MINOR（v10.10.0） | 新功能旋钮 |
| T8 promote 时 | MAJOR（v11.0.0） | 改变信号推送行为 |

每个任务完成即 commit-tree + push（loose-ref 教训：commit 后立即 git log 验证 refs 落盘）。

## 5. 既有 backlog 映射（不重复建设）

| backlog id | 落到 | v2 补充 |
|---|---|---|
| P0-20260903-reverseT-not-modeled | **T1.5** | v2.1 核心新增（backlog 驱动施工第一单）；**09-03 夜施工进展：核心已交付**——simulate_position_sm 状态机（11/11 语义测试）+ reconcile 双侧接线 + 底仓 ledger（方案 A）；09-03 实证与 push_audit 逐条对齐 |
| **P0-20260903-live-review-pairing（新）** | **T1.5 追加项** | **Bug 4（09-03 夜实证）**：live_roundtrip_review 配对忽略 X(TRAIL/EOD) 出场推送——把"反T TRAIL 平仓(10:28) + 正T建仓(10:35) + EOD(15:00)"误配成"反T B回补"，09-03 日报"净 +2.067% 有效"实为 **-1.528%**。影响日报当日数字与有效判定；roll20 wr_prod 经 reconcile 模拟口径（另一失真）。修复：配对状态机化（X 出场为配对边界），修复后重算近端日报 |
| P1-20260903-capture-rate-59 | T8 前置研究 | 触发灵敏度（atr_min_pct/MHD 阈值进网格）与 vol-regime 同属"低波动日治理"主题，随 T8 一并研究 |
| P0-20260811-cfg-state-leak | T2 | **修完标 fixed + 用修复后优化器重新生成候选**（不能只修 oos_validate.py）；⚠️ v2.1：T2 复核表必须在 T1.5 之后跑（否则用污染口径复核 = 白跑） |
| P0-20260811-reverify-0805 | T2 复核步 | 标的= backlog 原文四标的（161129/300308/688111/513310）；⚠️ v2.1：同上，T1.5 后执行 |
| P1-20260811-lookahead-samebar | T3 | T3-A/B 分阶段 |
| P1-20260811-limit-halt | T3 | 一字板按板块涨跌停价 + 成交量 |
| P2-20260811-cost-sensitivity | T3 | 盈亏平衡滑点进对照报告 |
| P1-20260811-bat-early-alert | T1 | |
| P1-20260811-verify-delay | T1 | step_status 落盘即验证锚点 |
| P1-20260811-no-oos | 已修（oos_validate），T8 沿用 | |
| P0-20260811-selfheal | 已 verified | preflight 挂 T0 之后执行 |
| T0/T4/T5/T6/T7/T8 | 审计+评审新增 | 施工时 seed 进 backlog 补审计链（含 change_scope/owner 字段） |

## 6. 已拍板记录（2026-09-02 夜，用户四项）

| # | 事项 | 拍板结果 |
|---|---|---|
| 1 | T3 基线挤水分 | **接受**，T3-A/T3-B 分阶段切换；legacy_samebar 永久保留+标记废弃+不参与 promote/rollback |
| 2 | shadow 池构成 | **真实 C_prod 五标的**：161129/300058/600570/513310/688111（reconcile_prod_vs_bt.md 铁证；否决 v1 的 603039/300759 笔误清单） |
| 3 | T8 低波动档 | **接受 shadow-only**（照算照记账不实盘推送），保完整统计流 |
| 4 | 开工 | **按 v2 落盘 + 立即开工 T0+T1**（2026-09-02 夜启动） |

## 7. 与审计/评审的差异说明

- **vs 审计**：审计建议第 1 优先修复 loop_engine 空转；本方案排至 T6。理由：带口径泄漏的自动闭环比空转更危险——空转只是不进步，自动 promote 不可信结论会主动劣化生产。bat 吞错（T1）与 feedback 调度（T5）提前：零风险纯止血项。
- **vs 评审**：7 项修正 + T0 全部采纳。评审引用事实复核：3 准确（feedback_loop.py:9 职责边界 / loop_engine.py:46-52 空转 / P0-20260811-cfg-state-leak 仍 open），1 错误——评审建议的 shadow 池清单（603039/688111/161129/513310/300759）系沿用 v1 T7 笔误，按其执行恰好丧失其主张的"历史可比性"；v2 以 reconcile_prod_vs_bt.md §2 铁证纠正为真实 C_prod 池。其余修正（run_id/产物校验/两层确定性/deploy gate/daemon_stage/动作分离/量纲公式）均提升方案严谨性，全文已整合。

## 8. 口径污染勘误表（v2.1，T1.5 完成前的结论信任级别）

| 结论/数字 | 信任级别 | 说明 |
|---|---|---|
| roll20 wr_recalc 53.8 / g1 -3.3pp | ❌ 作废 | reconcile 伪正T 口径（反T S 被忽略/B 回补误判建仓） |
| R2 单元对齐 gap 叙事（"差 1.2pp 基本闭合"） | ❌ 作废重建 | reconcile_prod_vs_bt.md 的对账方法被污染——但该文档的 C_prod 56.2% 实盘基线本身不受污染（live 口径） |
| p10 验收数字（ML 过滤双向净 -129.14→-39.80） | ⚠️ 降级参考 | 相加口径污染（信号双重计费）；同污染下 A/B 相对方向或可参考，绝对量级不可信；T1.5 后重跑 |
| p11 GT-TB v1.1 +1.04pp | ⚠️ 降级参考 | 研究态不入生产；同污染；T1.5 后重跑 |
| p7 证伪结论（s_uptrend_guard 等全负优化） | ⚠️ 待复核 | 相加口径下的证伪——"负优化"方向可能与污染口径有关；T1.5 后抽验 |
| factor_opt / oos_validate / auto_tune 寻优结果 | ⚠️ 失真 | 纯正T 目标函数漏 61% 反T 业务（603318）；网格最优解可能系统性偏向正T 友好参数 |
| 09-03 检验"当日算法判定：有效"（净 +2.067%） | ❌ 作废 | **Bug 4**：live_review 配对忽略 X(TRAIL) 出场推送，+2.067% 为误配幻觉；真实当日净 **-1.528%**（反T TRAIL +0.82% + 正T EOD -2.348%，与 push_audit 4 条推送逐条对齐） |
| C_prod 56.2% 基线 / roll20 wr_prod 57.1 | ✅ 保留 | 实盘 live 口径（push_audit→roundtrip） |
| 生产 monitor 信号行为 / 实盘资金 | ✅ 不受影响 | monitor 侧已强制底仓模型 |
| T7 shadow 池设计 / C_prod 可比性 | ✅ 保留 | 前提：shadow 复盘用 T1.5 修复后的仓位模型 |
| T6 promote 门槛（滚动 20d 净 WR ≥ 基线+2pp） | ⚠️ 数据源需明确 | v2.1 规定：只锚 live 实盘口径 + T1.5 修复后 shadow 账本；禁止用旧 reconcile 口径 |
