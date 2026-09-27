# AI 自主闭环·每日固定议程（docs/ai_loop_agenda.md）

> 版本：v1.0（2026-09-28）｜ 执行者：WB 每日自动化唤起的 AI 会话（交易日盘后）
> 机械层：`scripts/ai_loop/daily_agent.py`（guard/collect/review-merges/apply/finalize）
> 本文件是 AI 会话的**操作程序**——判断在这里做，机械动作一律走 daily_agent 子命令。

## 第0步 闸门与采集（机械）

```bash
cd C:\Users\YZP\WorkBuddy\Claw\tpoint
venv/Scripts/python.exe scripts/ai_loop/daily_agent.py guard    # exit 77 = 静默结束，不要继续
venv/Scripts/python.exe scripts/ai_loop/daily_agent.py collect  # 产出 digest
```

- digest 已汇总：live_review 最新日报、effect_ledger 尾20、backlog open 项、最新随机对照判决、
  生产 GT 配置、v4_gray 最新、push_audit 尾5、连续维持计数、近10日配置变更、vol_regime shadow 5日、复检队列。
- 对照真源：`docs/knowledge_alignment_map.md`（K1-K7 条款↔实现映射 + 待验证清单 + 判读纪律）。

## 第1步 降效复核（机械，先于一切判断）

```bash
venv/Scripts/python.exe scripts/ai_loop/daily_agent.py review-merges
```

≤5 个交易日内 ai_loop/daily_iterate 的合入，池级 net_wr 降 >3pp 会被**自动回滚**并告警。
复核结果影响当日诊断基调（回滚发生 = 上次判断错误，必须在 diagnoses 里归因）。

## 第2步 诊断（AI 判断）

对照 K1-K7 逐条判定当日状态，产出 **≤3 个假设**，每个必须：
- 标注来源：知识条款编号（K1-K7）/ 实证ID（报告文件路径）/ 工程拍定；
- 挂证据路径（digest 字段 / output 文件 / ledger 条目）；
- 统计结论必标样本量；n<30 只能写「观察」，不得写「结论」。

特别关注（复检项队列，data/ai_recheck.json）：
1. regime_gate 薄样本复检：n_trips 累积 ≥30 后重跑 ON/OFF A/B（当前证据 n_trips=8）。
2. 权重候选短名单（buy_threshold=0.55 / w_vwap=1.4 / w_macd_div=0.7 / w_vol_div=0.4 / w_rsi=1.0）：
   样本增厚后大样本复检（当前 n=11 日，只观察）。
3. vol_regime_gate promote：shadow 计数 ≥10 交易日（digest 的 vol_shadow_promote_hint 会提示）
   且 suppressed 子集净贡献为负 → 出人审提案（方法=random_control_validator --set vol_regime_gate=true
   与基线 ON/OFF 配对 A/B，同 regime_gate 判例）。
4. signal_gap 网格 {4,6,8,12}：先对齐验证器 gap=6 vs 生产 8 口径再扫。

**backlog 消费（每日必做，消除「只写不消费」）**：
- digest 的 backlog_open 逐条过一遍，每条给出去向：
  `venv/Scripts/python.exe scripts/ai_loop/daily_agent.py backlog --id <id> --status triaged --note "<判断>"`
  已转提案 → `--status proposal`；有证据关闭 → `--status closed --note "<关闭理由>"`（无理由拒绝）；
  仍需观察 → 维持 open 但在当日 diagnoses 里说明原因。
- 查看：`backlog --list [--status open]`。目标：open 条目数持续下降，零「超 5 日 open 未 triaged」。

## 第3步 验证指挥（AI 判断 → L0 机械执行）

**只允许调用 L0 白名单脚本**；禁止直接改 core/ 代码做实验：

| 用途 | 命令 |
|---|---|
| 随机对照（一票否决） | `venv/Scripts/python.exe scripts/random_control_validator.py --sym <SYM> --days 60 --m 500 --set k=v --tag <tag>` |
| 信号质量三轴+对照 | `venv/Scripts/python.exe scripts/evaluate_signal_validity_v2.py --random-control <SYM>` |
| OOS 验证 | `venv/Scripts/python.exe scripts/oos_validate.py`（按现有用法） |

判据（与 knowledge_alignment_map 判读纪律一致）：
- PASS = p<0.05 且 z≥1.0 且 n≥30；任何候选不 PASS 不得进合入。
- n<30 只观察不合入；权重/新规则/promote/全局块 = 人审项。

## 第4步 合入 gate（机械强制）

**AI 可自动合入**（三重闸门全过才放行，任一不过 apply 直接拒绝）：

```bash
venv/Scripts/python.exe scripts/ai_loop/daily_agent.py apply \
  --sym <SYM> --param general_algorithm.<param> --value <v> \
  --random-z <z> --delta-pp <pp> --proposal-id <id> --note "<证据摘要>"
```

- 白名单：buy_threshold / sell_threshold / signal_gap / min_hist_diff / vol_ratio_b_max / has_base（per-symbol）；
- 证据：random_z ≥1.0 且 delta_pp ≥+1.0（且 AI 已核实 n≥30）；
- 频控：24h 最多 1 次，一次只改一个参数（脚本强制）。

**人审**（写入 data/ai_proposals/<date>.json，并在 finalize 的 human_review 列出）：
_global 全局块、composite 权重、core/ 代码、watchlist 增删、ml_enable、vol_regime_gate promote、
signal_gap 口径变更、VERSION bump。提案卡必须含：
proposal_id / hypothesis / knowledge_clause / change / validation_plan / rollback / evidence_paths。

## 第5步 落盘 + 推送（机械）

AI 把当日判断写成 draft JSON（verdict / diagnoses / proposals / applied / human_review / maintain_reason），然后：

```bash
venv/Scripts/python.exe scripts/ai_loop/daily_agent.py finalize --decisions <draft.json>
```

- 产出 `data/ai_decisions/<date>.json`（status=done，幂等标记）+ 飞书摘要
  `[AI闭环 <date>] 诊断N 提案M 合入K 人审J 维持C(连续x日)`；
- **连续 5 个交易日维持 → 自动升级**：backlog P1 + 飞书告警，明日议程必须产出超参数空间的
  结构性提案（escalation），不允许继续维持。

## 边界（违反即流程错误）

- 不碰 monitor 进程（改 core/ 后需重启监控的场景一律人审）；
- 不手工编辑 data/monitor_config.json（唯一通道 = apply 子命令 → effect_ledger）；
- 不做 per-symbol 参数拟合类提案（用户已否决的过拟合路线；优化走大样本/全市场口径）；
- 不把「兜底口径数据日」的复盘当对账依据（降级日必须标注数据源）。
