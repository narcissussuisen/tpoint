# tpoint 历史结论可信度分级清单（2026-09-28）

> 起因：用户命题「tpoint 之前的历史尝试建立在不可信的测试环境中，结论不一定可信」。
> 方法：全仓证据盘点（CHANGELOG / backlog / output 报告 / 代码注释 / memory），每条挂证据路径。
> 总结论：**命题属实**。2026-08-02~09-03 期间大量验收/回测数字带一至多重失信；
> 每次发现后系统均有修复+勘误留痕，**治理本身可信**——本文是把分散勘误收敛成一张总表。

## 一、三级判定总表（当前仍被引用的关键结论）

### ❌ 作废级（明确不可信，禁止再引用）

| # | 结论 | 失信根因 | 证据 |
|---|---|---|---|
| X1 | v10.5.0 P3「反T 池级净 +51.7%」 | 裸卖空口径：反T 暴利 99% 是假象（底仓模型下仅 +4.67%/+2.76%，缩水 98.8-99.1%） | docs/base_position_findings.md；2026-08-23 证伪 |
| X2 | v10.0.0 vol_confirm 消融「ret +43.7%/sharpe 0.71→1.18」 | 死配置：vol_confirm 块缩进错误，自「上线」起**从未执行过一次**，归因错误 | CHANGELOG v10.1.1 L689-691 |
| X3 | roll20 wr_recalc 53.8 / g1 -3.3pp / R2「差 1.2pp 基本闭合」叙事 / R4a 止损证伪 | 仓位模型口径污染（simulate_day 纯正T + simulate_bidirectional 裸卖空 + 双重计费） | hardening_plan.md §8 口径污染勘误表（L252-266）明令作废 |
| X4 | p10「ML 过滤双向净 -129.14→-39.80」/ p11「+1.04pp」 | 同上双重计费污染，勘误=绝对量级不可信 | 同上勘误表；backlog P1-20260928-mlfilter-recaliber（09-28 已按修复口径重审，见 docs/knowledge_alignment_map.md ML 节） |
| X5 | 旧 ML 模型 AUC 0.707/0.806 的「因子优势」叙事 | train-serve 错配：无过滤训练样本含生产永远不会接的信号（161129@07-24：ML 5 信号 vs 生产 1 信号） | output/ml_vs_prod_mismatch_report_2026-08-02.html；09-28 修复口径重训 B 0.5753/S 0.5566 |
| X6 | vol_ratio_b_low 门控「+14.5pp」上线依据（08-18） | 被证伪 WR 口径+per-symbol 简易回测；08-21 三重证据一致证伪（净 WR 反降 0.9pp，仅削覆盖），已置 null | data/monitor_config.json `_note_20260821_p13`；CHANGELOG v10.5.0 L218 |
| X7 | 08-04~08-21 期间复盘数字（推送价 entry 口径） | entry 价口径 flip-flop：推送价 vs 信号 bar close 差 2.6%；08-21 起统一 bar close | CHANGELOG v9.3.1/v10.5.0 P0；prod_vs_bt_reconcile.py L247-267 |

### ⚠️ 存疑级（失信环境产出，修复后未按新口径重签发）

| # | 结论 | 失信根因 | 证据/处置 |
|---|---|---|---|
| S1 | v9.3.0 验收「ATR×mpr 47.8→56.2%（+8.4pp）」 | ①mpr 多周期 MACD 周期内前视（08-17 修；修复后 161129 baseline ret 10.86→2.73，冲击巨大）；②samebar 信号即成交前视（至今 open） | CHANGELOG L424-431；output/research/mpr_{leaky,fixed}_factor_opt.json；backlog P1-20260811-lookahead-samebar（open）⇒ 需修复口径重签发 |
| S2 | v10.0.1 四只 trail 寻优落盘 | 寻优引擎配置状态泄漏（同参两跑结论相反）；复核须 T1.5（09-27 才就位）后重跑 | backlog P0-20260811-cfg-state-leak / P0-20260811-reverify-0805（**均 open**） |
| S3 | p7 证伪结论（s_uptrend_guard/反T trend 全负优化） | 同 X3 口径污染，「负优化」方向可能与污染口径有关 | hardening_plan 勘误表标注「待复核」 |
| S4 | 08-05~08-12 实盘信号覆盖率/频率统计 | bar_key 跨日残留全天静默（P0 事故，漏发 14+ 条）+ 幽灵计数 | CHANGELOG v10.1.1/v10.1.2；leakage_inventory_20260812.html |
| S5 | 降级日（如 09-10）的复盘/对账数字 | 腾讯兜底合成 OHLC：ATR 中位低估 41.8%、48.4% 信号组合漂移 | CHANGELOG v10.9.4；memory/2026-09-10 §8-9（已修+哨兵，历史日未清洗） |
| S6 | 跨日聚合统计（roll20 等） | 归档 review 无 watchlist/版本口径戳，实为 5 段不同标的集合的混合样本；唯一净正段恰为「回填+旧口径」段 | backlog P2-20260925-archive-no-universe-stamp（open） |

### ✅ 可信级（修复后环境产出；引用时须带样本厚度标注）

| # | 结论 | 环境 | 残余注意 |
|---|---|---|---|
| V1 | **C_prod 56.2%**（live 基线） | live push_audit→roundtrip 口径，不经回测链；勘误表明令保留 | 旧 5 标的池口径；当前 watchlist 已切 300010 单标的，跨段可比性受限（S6 未闭环） |
| V2 | 随机对照「信号胜随机」net_wr 56.67% vs 随机 p50 33.3%（z=2.49，p=0.01，n=163 信号/11 日） | 09-27 修复口径（simulate_position_sm + 随机基线分层） | **胜随机≠盈利**：笔均净 -0.326pp；n=11 日薄 |
| V3 | regime_gate 保留判决（ON 75.0% vs OFF 56.7%，mean_net +0.007 vs -0.326pp） | 09-27 修复口径 ON/OFF 配对 | n_trips=8 薄样本，AI 闭环复检项（≥30 再审） |
| V4 | 18 臂权重/阈值扫描「全部 PASS vs 随机但 mean_net 全负，唯 regime_gate 达盈亏平衡」 | 09-27 修复口径 | **参数调优解不了成本倒挂**——当前最重要结论；候选只观察不合入 |
| V5 | ML filter 新裁决（B 不接 / S-only shadow 候选） | 09-28 修复口径数据集 ml_dataset_v2（生产候选信号集 27111 样本） | S 侧证据 round-trip OOS Δ+2.02pp；shadow 未接线 |
| V6 | 底仓模型三栏盈亏（基金合计 -3.22%/个股 +0.05%） | 08-23 合规口径 | 「单一下行市里无净 edge」的当前地基 |
| V7 | 推送延迟、数据源修复类运维结论（mootdx 选服 v10.10.0、兜底惰性化等） | 生产实测 | — |

## 二、失信环境七类根因（分类账）

a. **前视偏差**：多周期 MACD 周期内泄漏（08-17 修，leak_guard 栅栏守护）；trim_frontier live 同根（08-18 修）；**samebar 信号即成交至今未修（P1-20260811 open）——全部历史回测含系统性偏乐观**。
b. **train-serve 错配**：ML 无过滤训练（08-02，09-28 修）；验证器 baseline=GENERAL_DEFAULT（09-27 当场纠正）；寻优配置状态泄漏（P0-20260811 **open**）。
c. **数据源污染**：腾讯兜底合成 OHLC（09-10 修+哨兵）；mootdx 选服三缺陷（v10.10.0 修）；截断半天序列静默接受（P1-2026-09-24 **open**）。
d. **引擎漂移/口径并存**：miji_engine.py 漂移禁用；三套仓位模型并存全用错（09-03 实证 603318 live +2.067% vs recalc -2.348% 方向相反；T1.5 simulate_position_sm 09-27 收口的）。
e. **统计口径失真**：裸卖空伪利润；WR 毛/净口径；回填混合 watchlist；验收门假干净（x-leg-drop / verify-data-blind / emit-no-base-gate，均 open）。
f. **entry 价口径**：推送价 vs bar close（差 2.6%），08-21 统一 bar close。
g. **静默事故**：bar_key 跨日残留；vol_confirm 死配置；幽灵计数；bat 吞错「任务永远显示成功」。

## 三、残余风险与处置建议

| 残余 | 现状 | 建议 |
|---|---|---|
| ① samebar 前视 | open，全部历史回测偏乐观 | 人审提案卡：exec_delay_bars=1 修复 or 全部回测产物强制标注「samebar 口径偏乐观」（本文 S4 出卡） |
| ② 20+ 条 P0/P1 backlog open（验收门假干净类：cfg-state-leak / x-leg-drop / verify-data-blind / emit-no-base-gate / baseline-truncated-intraday / regime-zero-variance 等） | AI 闭环每日消费中 | 其中 **P0-20260811-cfg-state-leak 与 P1-2026-09-24-baseline-truncated-intraday 建议升为本周期最高优先**——它们在「验收门」位置上，门失信则后续一切结论失信 |
| ③ 历史日 live_review/reconcile 未按修复口径全面重算 | 未排期 | 列 backlog 由 AI 闭环排期（工程量大，不在本次范围） |
| ④ 1m_clean 数据质量无持续验证 + 评估池自选择/幸存者偏差 + 多测校正缺失 + 随机基线未匹配波动状态 + 验证器无正向功率测试 | 结构性 | 列入验证栈加固议程（docs/ai_loop_agenda.md 复检队列） |

## 四、给下游的引用规则（即日起）

1. 引用 X1-X7 任何数字 → 视为流程错误（同「漂移引擎禁用」级）。
2. 引用 S1-S6 → 必须带「存疑，修复口径未重签发」水印。
3. 引用 V1-V7 → 必须带样本量与口径注记（n、日期段、round-trip/静态、数据源）。
4. 新结论只允许产自修复后验证栈（见 docs/player_benchmark.md T0 前置）。
