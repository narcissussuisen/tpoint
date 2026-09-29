# tpoint RSI 主循环操作程序 v2.0（docs/rsi_loop_agenda.md）

> 版本：v2.0（2026-09-28）｜ 取代 `docs/ai_loop_agenda.md` v1.0（该文件保留为参数层通道的参考）
> RSI = 递归自我改进（Recursive Self-Improvement，Raven V0.2.0 / EvoAlpha Curator 模式）：
> AI 利用执行结果与反馈，持续改进产生这些结果的机制本身。
> 目标函数 = `docs/player_benchmark.md` 的 T0/T1/T2（选手基准）；判据真源 = `docs/knowledge_alignment_map.md` K1-K7。
> 机械执行器：`scripts/ai_loop/rsi_agent.py`（七步）+ `gates.py`（四道闸）+ `spec_freeze.py`（冻结面守卫）+ `rsi_watchdog.py`（自健康）。

## 一、七步循环（AI 会话操作程序）

**日循环（轻，周一~五 15:45，automation 6a856b94；含 feedback_loop 回灌——原 15:45 独立会话
「tpoint 反馈闭环驱动」已于 2026-09-29 合并入本会话）**：
0. **guard/selfcheck**：`rsi_agent.py selfcheck`（交易日+幂等+环境探针+当日测试快照）。rc=77 静默结束。
1. **measure (light)**：`rsi_agent.py measure` → digest；读 `data/rsi/bench_baseline.json`（curator 维护）与当日 live_review/reconcile，产出「今日 vs T0/T1/T2 差距快照」。
2. **diagnose**：对照 K1-K7 与 backlog（≤3 个假设），**只准引用 K 条款 / backlog ID / 实证报告路径，禁止发明判据**；n<30 只写「观察」。
3. **propose（可选）**：需要参数改动 → `daily_agent.py apply`（参数层通道，三重闸门）；需要代码改动 → 写 `data/ai_proposals/<date>.patch.json`（字段见 spec_freeze.PATCH_REQUIRED_KEYS），留给 curator 会话走四道闸；需要 spec 变更 → 人审提案卡。
4. **finalize**：`daily_agent.py finalize --decisions <draft>`（落盘+飞书摘要）。
5. **observe**：`rsi_agent.py observe`（validity_queue 到期复核；无到期项则秒回）。

**curator 会话（重，周一/三/五 16:30，automation tpoint-rsi-curator）**：
1. selfcheck → measure（含 `gates.py bench --tag <round>` 池级基准，分钟级长跑）。
2. 处理 pending patch：应用到工作树 → `gates.py validate`（闸二/闸三）→ `gates.py bench --tag cand` + `gates.py compare <cand> <baseline>`（闸一/闸四）→ 写 gates_result 进 patch → `rsi_agent.py apply-code --patch <patch> --random-z <z>`。
3. 四闸任一 FAIL ⇒ 退回 diagnose，FAIL 原因留痕（负面结果也是 RSI 资产）。
4. 留痕：iteration_log.jsonl（apply-code 自动）+ 本会话 handoff（写 `data/rsi/handoff.json`）。

**全池重锚重活（17:00 后 / 周末）**：`exit_anchor_grid.py`/`random_control_validator.py` 级长跑，longtask 三件套（信号免疫+beat/feishu+DONE_FLAG）。

## 二、四道闸（gates.py，任一 FAIL = 退回）

| 闸 | 内容 | 判据 |
|---|---|---|
| 一 重放基准窗 | 固定 seed/池/窗（data/rsi/benchmark_windows.json）重放 | net_wr ≥ 基线−1.0pp 且 mean_net 不恶化 |
| 二 验证器自检+保留率 | `random_control_validator --self-test` rc=0 | 且 n_trips 保留率 ≥90%（防「剩 3 笔全胜」虚胖） |
| 三 测试基线 | tests/ 全量 vs 当日 step-0 快照 | 新增失败 = 0 |
| 四 随机对照不退步 | 基准窗池级 z | z≥1.0 且 p<0.05 且 n≥30 且 z 不低于当前生产配置 |

## 三、AI 驱动边界（三层，2026-09-28 用户「完全由 AI 驱动」裁决的落地）

| 层 | 通道 | 自动化程度 |
|---|---|---|
| 参数层 | `daily_agent.py apply`（白名单+random_z≥1.0+delta_pp+24h 频控） | ✅ AI 自审（既有，不变） |
| 代码层 | `rsi_agent.py apply-code`（四道闸+spec_hash 不变+黑名单外+工作树干净+每日≤1 次） | ✅ AI 自审 + 60 天 validity_queue 到期复核（退步>3pp 自动 git revert） |
| spec 层 | 人审提案卡（data/ai_proposals/） | ⛔ 永不自动 |

**spec 冻结面**（spec_hash 覆盖，变化=锁人审）：`core/general_signal.py`（判据语义）、`core/simulate_position_sm.py`（成交口径）、`docs/player_benchmark.md`、`docs/knowledge_alignment_map.md`、本文件。
**FROZEN_SURFACE 黑名单**（apply-code 硬拒）：`core/monitor.py`、`data/watchlist.json`、`data/monitor_config.json`（唯一通道=effect_ledger）、`VERSION`、`config.json`、`config/monitor_config.json`、spec 五文件、spec_freeze.py/gates.py 自身。
**旋钮立法 vs 取值**（K2-K4 数字锚点补齐模式）：给 GeneralConfig **加字段** = spec 变更 = 人审立法；字段**取值**扫描 = RSI 随机对照实验 = AI 自审。

## 四、纪律（铁律）

1. **随机对照一票否决**：p<0.05 且 z≥1.0 且 n≥30；n<30 只观察不合入。
2. **不发明判据**：diagnose 只引用 K1-K7 / backlog ID / 实证路径（用户对 RSI 能力的核心关切）。
3. **spec_hash 变化 = 锁人审**，无自动通道（对齐 EvoAlpha Curator spec_hash 规则）。
4. **盘中（09:15-15:00）零重活**；每交易日 ≤1 次代码层合入；参数层 24h 频控。
5. **backlog 每日 triage 消费**，open 单调下降为目标；closed 需理由不可逆。
6. **git loose-ref 防线**：apply-code 在 commit+tag 后二次 `git log` 校验；push 用显式 refspec，push 后 ls-remote 核对。
7. **同一套伪样本服务全部臂**（paired），真/伪臂 exec_delay 必须一致（闸一硬校验）。
8. **工作树脏 = 不 apply**（fail-closed）；飞书失败不阻断主流程但必须留痕。
9. 断点续跑：每步 rc 落 `data/rsi/<date>.state.json`；会话 crash 后从断点继续。
10. 水下数据纪律：兜底口径数据日的复盘不作对账依据；降级日产物标注数据源。

## 五、内容主线（Roll 排序：T0 口径债 > 薄样本证据债 > 功能性候选 > 遗留修复）

- **Round 1（已完成 2026-09-28）**：samebar A 落地 + 全量重锚（P1-20260811-lookahead-samebar closed）。
- **Round 2**：G2 regime_gate 样本增厚复检（日循环 measure 自动累积 n_trips 8→≥30；09-27 ON 臂正期望证据未撑过口径修复，复检必要性升级）。
- **Round 3**：ML S-only shadow 到期复核（P2-20260928）+ 验收门两项（P0-20260811-cfg-state-leak / P1-2026-09-24-baseline-truncated-intraday）+ K2/K3 旋钮立法提案（人审卡）。
- **里程碑**：T1 笔均净转正（池级 delay=1 口径）→ T2 各项逐个攻。

## 六、时序（交易日）

```
15:30 run_daily_review.bat 12步流水线 + step13 pipeline_postcheck 语义后检
      （后检：五 JSON date / review HTML 六节 / B5 entry_price 对齐 / 日志 marker / 数据质量哨兵；
        失败 rc=1/2 并推 a35d7f52 + b4eba7a9）
      —— 取代原 15:35「复盘+对账兜底」AI 会话（automation-1785721171231 已于 2026-09-29 停用）
15:45 automation 6a856b94 → RSI 日循环（轻）＝ 反馈闭环回灌 + 主循环（单会话串行）：
      feedback_loop（preflight + 再验证 + backlog 回灌）→ selfcheck → measure → diagnose
      → propose → finalize → observe → backlog triage
      —— 原 15:45 独立会话「tpoint 反馈闭环驱动」已合并入本会话（2026-09-29）
16:30 automation tpoint-rsi-curator（周一/三/五，重：四道闸+apply-code+observe）
17:00+ 全池重锚重活（longtask 三件套）
21:30 schtasks rsi_watchdog（机械自健康，零 AI 会话成本）
```

**2026-09-29 时序精简（人审批准）**：原每日 4 场 AI 会话（15:35 兜底 9.4 万 token + 15:45 反馈闭环
21.2 万 + 15:45 日循环 8.1 万，另加周一三五 16:30 curator 7.1 万）压缩为 2 场。依据：15:35 会话的
语义校验已可脚本化（`scripts/pipeline_postcheck.py`）、其校验项 `regime_gate=true` 已因 2026-09-29
人审关闭而失效、且该会话自 2026-09-01 起连续 19 个交易日未写执行记忆；15:45 两条会话职责重叠
（均维护 `data/feedback_backlog.jsonl`，且 feedback_loop 的再验证正是 RSI measure 的上游取数）。
合并后顺序铁律：**feedback_loop 必须早于 measure**（前者重跑 `live_roundtrip_review`，后者读其产物）。
