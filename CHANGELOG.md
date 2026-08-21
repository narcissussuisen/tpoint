# miji 版本算法说明（CHANGELOG）

> 版本号规则：MAJOR.MINOR.PATCH（完整规则见 `docs/versioning.md`，每次改动必须对照判断）。
> 说明仅标注各版本**核心算法与信号语义**的差异，便于回溯。
>
> **方法论版本**（独立轴）：当前 `METHODOLOGY_VERSION=1.1.0`，权威真源 `docs/methodology_framework.md`。
> 方法论版本号与算法版本号**解耦**：方法论 bump 由方法论文档驱动，算法 bump 由 `VERSION` 驱动；
> 两者对齐索引见 `docs/methodology_framework.md` §11。

## v10.5.0（2026-08-21）R2P 全链路闭环交付版（P0→P5 固化）
> 本次为「Research-to-Prod gap-closing」6 阶段路线图的**固化交付版**：P0 地基(DET) → P1.1/P1.2/P1.3 噪声与门控 → P2 出场盈亏比治理 → P3 配对/反T → P4 regime门控+OOS → P5 固化+监控+说明。
> 每个阶段均经**量化专家 agent 独立评审 gate（PASS）** 方可进入下一阶段（loop engineering）。

### 核心变更（相对 v10.4.0）
- **P1.1 节奏降噪**：`general_algorithm.signal_gap` 6→8（冗余 B/S 47/57%→30/36%，DA/TEP 质量持平），已写入 `monitor_config.json`。
- **P1.2 评判口径**：信号有效性主判据 δ=0.5%（EHR 跳至 B 68.7%/S 63.5%），仅改评判标准（`tpoint_signal_validity_criteria_v1.md`），零风险。
- **P1.3 量比门控复核**：DET 三重证据（含 ATR/实盘无ATR 双口径）证伪旧 +14.5pp 收益（基于被证伪 WR 口径），`vol_ratio_b_max` 置 `null`（移除）。
- **P2 出场盈亏比治理（核心）**：
  - P2.2 实盘移植 FIXSTOP=1.5% 固定硬止损（EV 中性尾端断路器，封尾端 -8.5%→-1.5%）+ `run()` 15:00 EOD 强制平仓 `can_sell` 闭环（commit `d1ec1ae`）。
  - P2.1 `core/exit_v3.py` 三条件止损（硬/趋势/时间）适配正T，镜像 `simulate_bidirectional.py` 反T出场。
- **P3 配对/反T层**：`bidirectional_enable=true` 启用；出场按持仓方向解耦——空仓(反T)走 `EXIT_CFG_SHORT`(DEFAULT类: 硬止损atr1.5+时间90+trail0.4/0.6, FIXSTOP off，保留正期望)，多仓(正T)走 `EXIT_CFG`(PROD: trail0.4/0.6+FIXSTOP1.5)；修复 `bidir=False+空仓遇S` 的 `pos['side']` None 崩溃（commit `ab88137`）。
  - 验收（faithful 双向解耦回测，161129+513310 各79天）：反T 池级 n=594 WR=37.4% **净 +51.7%**（≥0 PASS）；双向合计净 -77.28% ≥ 正T -128.98%（增量正贡献 PASS）。
- **P4 regime 门控（OOS 验证）**：`GeneralConfig` 新增 `regime_gate`(默认关=fail-safe)；`_regime_suppress_b` 平滑趋势门控——持续下行 regime（窗口内 -1 占比≥阈）抑制 B（仅影响 B，S 不动，fail-open）；经 `_build_general_cfg` 热重载传播到实时。参数固定(lookback40/thresh0.5)、未在 train 寻优，OOS 验证启用（commit `81a9c8c`）。
  - 验收（时间切 train60%/test40%，正T PROD出场）：池级 TRAIN net -66.49%→-17.34%(+49.15pp) | TEST(OOS) -54.06%→-15.07%(+38.99pp)；方向一致 → 启用 `regime_gate=true`。
- **P5 固化**：本版本统一提交 P0–P4 全部改动；监控 `scripts/selfcheck_daily.py` 周期自检完好；本说明文件（`docs/tpoint_v10.5.0_release.md`）随版交付。

### 量化验收总览（DET Framework v1.0 + OOS）
- **信号质量成立（P0）**：全池 192 标的/6024 日/90323 信号，B-DA@60 69.5%/S-DA@60 72.7%，TEP pos_rate 100%（每笔理论净值全正）→ 找极端顶底能力系统性成立。
- **反T 净期望正（P3）**：+51.7%，按「期望值启用」决策成立。
- **regime 门控 OOS 正（P4）**：样本外净 +38.99pp，未过拟合。
- **长侧（正T）原始信号净仍为负（WR 37.6% / 净 -128.98% raw）**：v5 通用引擎原始信号（无冷却/ML/regime 过滤）既有问题，非 P3/P4 引入；regime 门控已显著止血（正T OOS 净 -15%），但长侧 alpha 仍待 R2P 后续治理（因子 OOS / 买点确认升级）。

### 已知限制 / 上线硬门槛（须上线前闭环）
- **~~B5 入账价对账（硬门槛）~~ → P0 已验证/已固化**：审计 `scripts/prod_vs_bt_reconcile.py` 与 15 份历史 roundtrip 记录（`data/roundtrip/2026-*.jsonl`），`live` 记录 `entry_price` 已严格使用信号 bar close（与 `entry_bar_close` 最大偏差 **0.0%**，>0.1% 不匹配数 **0**），推送价仅作为 `push_slip_pct` 参考（最大 2.504%，与历史 2.6% 差距一致）。新增 `scripts/p0_b5_entry_audit.py` 作为周期性 B5 合规审计。P0 PASS。
- **个股泛化**：P4 OOS 池仅聚合 161129/513310（各79天），4 只个股样本<40天未入 OOS；regime 门控个股泛化待扩样本复现。
- **口径提示**：反T +51.7% 来自 166天/3标的全样本，正T OOS -15% 仅 32天/2标的，二者不可直接相加得"系统净"；真实双向系统净须以统一口径重算。
- **因子 OOS 调参**：路线图原 P4「压 δ 误差因子调参」因过拟合风险延后，信号已 DET 验证成立故不强制；如需做须在 OOS 框架下进行。

## v10.4.0（2026-08-20）通用算法 v5/GT 转正 + 双向反T + 离线长回测
> 统一命名：**做T策略 v5 / 引擎 GT v1.0**（`core/general_signal.py` 常量 `STRATEGY_VERSION='v5'`、
> `ENGINE_NAME='GT'`、`ENGINE_VERSION='1.0'`；`data/monitor_config.json._global.general_algorithm`
> 补 `strategy_version/engine` 字段）。v5 承接 v3(v10.2.0)→v4(死锁中间产物) 迭代链，v4 保留为灰度候选。

### 核心变更（软切换，不破坏既有兼容）
- **通用算法转正为 watchlist 生产驱动**（`use_general_engine=true`）：`core/general_signal.py`
  symbol-agnostic 连续评分（比率口径参数，无逐标的特例），B 侧防接飞刀（trend==-1 需局部底+超卖反转确认），
  S 侧全 regime 放行；`core/watchlist_engine.py` 统一驱动 + v4 影子灰度（`v4_gray_compare_*.json` 兼容保留）。
- **双向做T**（Track D / G-F3）：新增 `core/simulate_bidirectional.py` S→B 反T 配对（镜像正T出场规则，
  收益方向翻转），`simulate_dual` 合成双向。回测量化 S 侧：**反T 池级 WR 43.5% / 净 +331.77%**（正T 24.1% / −335.75%），
  双向合计净 −3.98% —— S 侧信号显著强于 B 侧。
- **signal.txt 引擎标记**：新增 `🔷 v5/GT` 独立注释行（`ENGINE_TAG`），不污染复盘 RE_TS/RE_SIG/RE_PX 整行匹配。
- **离线长回测基建**：`scripts/backtest_general_v5.py` 读 F:/keyfactor_data/1m（tickflow 真实 1m），
  支持 `--dual` 双向模式，输出 JSON+HTML。
- `general_signals_*.json` 顶层补 `strategy_version/engine_full` 字段（兼容 `engine=general` 旧值）。

### 离线长回测结论（6 标的 / 589 交易日 / 2651 trips）
- G1（WR≥55%，n≥20）：**FAIL，池级 WR 24.1%**（与 v2 离线 22.5% 同口径可比，生产 56.2% 含完整状态机：
  冷却/regime 门控/仓位）。
- 相对基线：v5 WR 24.1% > v2 22.5%（+1.6pp），单笔 −0.127% 优于 v2 −0.166%；但信号量 14.6× 放大总亏损。
- **结论**：v5 裸信号质量未达标，S 侧强 B 侧弱；下一迭代 = regime 门控 + 参数收紧（如 buy_threshold 0.45→0.55）
  压缩噪声信号，再验 G1。

## v10.3.0（2026-08-20）综合评分模型 v4 —— 三神技 + RSI 连续加权融合
> 用户要求「基于三大核心策略设计可供上线评估的核心算法指标体系，整合三大策略信号与 RSI 超买超卖，
> 构建综合评分模型并输出交易信号」。本版本**纯增量**：新增 `core/composite_scorer.py` 与 `detect_signals_v4`，
> 不动 v9 / v2 / v3 / monitor / exit_manager / shadow_v3 既有逻辑；v4 可与 v2/v3 并行回测对比。

### 核心设计：连续评分引擎（与 v3 布尔触发本质不同）
- v3 (`detect_signals_v3`) 用「多条件 AND」布尔触发 → 离散 0/1 信号，易信号爆发/漏触。
- v4 (`detect_signals_v4`) 每个组件输出**连续子评分 C ∈ [-1,1]**（方向×强度），加权求和得
  `composite = Σ w·C / Σ w ∈ [-1,1]`，综合分越阈值才出信号 → 天然支持强度分级 / 多因子融合 / 权重可配。

### 四大组件（出处：方法论 v1.x §4 三大神技 + RSI 协同）
- `C_vwap` 分时均线引力：`−tanh((close−vwap)/(k1·atr))`（价低于带→正/买，高于带→负/卖）
- `C_vol_div` 量价背离：价极值 + 缩量(vol_ratio<0.7) 的动能衰竭，strength∈[0,1] 由量缩程度决定
- `C_macd_div` MACD 背离：价极值 + 柱状不确认（价格新低而 MACD 不新低 / 反之），strength∈[0,1]
- `C_rsi` RSI 超买超卖：线性映射 `clip((rsi_neutral−rsi)/half_range, −1, 1)`

### 可配参数（`CompositeConfig` / `DEFAULT_CONFIG`）
- 权重 `w_vwap=1.2 / w_macd_div=0.9 / w_rsi=0.8 / w_vol_div=0.7`（量价背离实证净负，刻意低配）
- RSI `rsi_period=14 / oversold=35 / overbought=65 / neutral=50`
- 信号阈值 `buy_threshold=sell_threshold=0.50`（默认落在方法论 §8 健康密度带 0.5~2.0/百bar 附近；
  0.35=灵敏、0.55=严控）；强度档 `strong=0.62 / medium=0.50`
- 趋势门控 `trend_b_allowed=(1,)`（B 仅上升市，沿用 v2 生产）/ `trend_s_allowed=(−1,0,1)`（S 全放行）
- 节奏 `signal_gap=8 / max_b=max_s=12`

### 结构化输出（每条信号）
`type, idx, price, score(带符号综合分), strength(|score|), strength_band, rsi, trend, reason,
components{vwap,vol_div,macd_div,rsi}, weights{...}, triggers[...]` —— 供评分审计 / 解释 / 回测。

### 回测验证结论（离线 tickflow 1m，688111×20日 + 603039×2日；午间对比脚本 v2_v3_noon_compare.py）
- 组件本身均衡无偏（vwap 均值 +0.04 / rsi +0.07，>0 占比均≈52%），无系统性偏差。
- 默认趋势门控下 v4 信号 S 主导（本样本 B/S≈4/112）；因 exit_manager 仅支持**正T**配对，S 侧无法回测，
  故 v4 的「胜率」指标在本样本主要反映极少量 B 的样本噪声，**不代表 v4 真实质量**。
- 验证：松弛 B 门控后方向恢复平衡(B=136/S=108)但 WR 反降至 18.8%，证明趋势门控确在保护质量。
- 信号密度随阈值可调：0.35→0.55 映射 3.39→1.59 信号/百bar（落入健康带）。
- **判定：v4 需 反T(先卖后买)回测支持才能公平评估其 S 侧；当前维持 v2 生产，v4 仅作离线评估/候选。**
- 待办：① 给 exit_manager 加 反T 配对（或 v4 专用 反T eval）；② 门控松弛后做大样本回测；③ 再决定 shadow 接入。

## v10.2.0（2026-08-20）三大神技 + KDJ —— 更灵敏精准的日内波动捕获
> 用户依据 v14《散户专属做T秘籍》提出三大神技（分时均线引力 / 量价背离 / 分时MACD背离），
> 结合 KDJ / RSI 指标重构信号判定。本版本**纯增量**：不动 v9 / v2 / monitor / exit_manager 既有逻辑，
> 新增 v3 检测函数 + 5 个新因子，行为可与 v2 并行回测对比。

### 补遗：v3 影子旁路（2026-08-20 盘前接入，不改动生产）

- 目的：在不开盘前替换生产算法的前提下，并行积累 v3 信号证据（遵循"v3 胜率待 1+ 月 live 沉淀"纪律）。
- 新增 `core/shadow_v3.py`：从 `monitor.data['df']` 原始 bar 自行算 `indicators.compute_indicators` + `detect_signals_v3`，
  落日志到 `data/shadow_v3_<date>.jsonl`。完全独立于 `detect_for` / `miji_alpha`，不读 miji 的 data 字典（回避双栈漂移）。
- 注入点：`monitor.run()` 主循环 `sigs=_risk_gate(...)` 之后、`emit_signal` 之前，一行 fire-and-forget + 双重 try/except。
- 护栏：① 进程内按 (sym,bar_ts,type) 去重；② 剔除最后一根进行中 bar（与生产 trim_frontier=True 对齐）；
  ③ 总开关 `SHADOW_V3_ENABLED` + 环境变量 `TPOINT_SHADOW_V3` 可即时停用；④ 所有异常内部吞掉，绝不阻断生产。
- 复盘工具：`scripts/shadow_v3_review.py` 聚合 jsonl 并按 (标的,分钟,方向) 与生产 signal.txt 对比重叠/独有。
- 本补遗提交的 monitor.py 同时含此前未提交的 v10.2.0 monitor 改动（_bar_tradability、detect_for 的 trim_frontier/vol_ratio_b_max），
  均属 v10.2.0 分支预定状态。

### 新增算法（来自《散户专属做T秘籍》三大神技）

#### 神技#1：分时均线"引力定律"（继承自 v9/v2，无改动）
- 价位偏离 VWAP 过远会回归。急涨远离均线 → 卖；急跌远离均线 → 买。
- 实现：`core/indicators.py` 中 `vwap[i] ± K*ATR[i]` 标准/极端轨（沿用 K1_V2=0.8 / K2_V2=1.8）。

#### 神技#2：量价背离"动能衰竭"（v10.2.0 新增）
- 价格新高但成交量一波比一波小 → 可能回调；价格新低但成交量萎缩 → 可能反弹。
- v9/v2 仅有量比（vol_ratio）单维度，未做价×量联合"背离"判定。
- 新因子 `factor_registry.f_vol_price_div`：当前 bar 创近 LOCAL_W=15 根新高 + vol_ratio < 窗口均量×0.7 → 返回 +1；
  价新低 + 量缩 → 返回 -1（用于 B 候选的反弹信号）。

#### 神技#3：分时 MACD"背离确认"（v10.2.0 新增）
- 买 = 股价新低 + MACD 红柱缩短 / 绿柱收敛；
- 卖 = 股价新高 + MACD 红柱缩短 / 绿柱放大。
- v9/v2 仅有 `macd_hist` 趋势因子，无背离事件检测。
- 新因子 `factor_registry.f_macd_div`：价新高 + 绿柱放大（hist 三连降）→ +1（S 候选）；
  价新低 + 红柱缩短（hist 三连降但仍 >0）→ -1（B 候选）。

### 协同指标

#### KDJ（v10.2.0 新增，因果前向）
- `core/primitives.compute_kdj(h, lo, c)`：SSE 经典定义 N=9, K=3, D=3，J=3K-2D。
- 注册因子 `kdj_k` / `kdj_d` / `kdj_j`。
- v3 触发门：J<0 或 K<20 → B 候选加分；J>100 或 K>80 → S 候选加分。

#### RSI（已有，沿用）
- v2 的 RSI<35 / RSI≥55 门控在 v3 中保留为兜底（与 v2 路径兼容）。

### 新增函数

- `core/primitives.compute_kdj(h, lo, c, n=9, k_period=3, d_period=3)` → (k, d, j) ndarray
- `core/factor_registry.f_kdj_k/d/j`、`f_vol_price_div`、`f_macd_div`（注册到 FACTORS）
- `core/indicators.detect_signals_v3(data, pc)`：融合 3 神技 + KDJ + RSI 的多路径触发
  - B 触发路径（任一满足）：
    a. v2 兼容路径：标准/极端轨触及 + 反转 K + trend==1
    b. 神技#3 强势路径：MACD 底背离 + 反转 K + (量价底背离 或 KDJ 超卖)
    c. 神技#2 强势路径：量价底背离 + KDJ 超卖 + 反转 K
    d. KDJ 超卖路径：J<0/K<20 + 标准轨触及 + 反转 K
  - S 触发路径对称（MACD 顶背离 / 量价顶背离 / KDJ 超买 / 标准轨）
  - 新增 reason 类型：`MACD底背离` / `MACD顶背离` / `量价底背离` / `量价顶背离` / `KDJ超卖反弹` / `KDJ超买回落`
  - 跨型信号冷却沿用 v2 的 SIGNAL_GAP=8 分钟
  - 输出 dict 兼容 v2 格式（type/idx/price/chg/rsi/trend/reason/vol_ratio），扩展 kdj_k/d/j/macd_div/vol_price_div 字段
- `core/indicators.compute_indicators` 返回 dict 增加 `kdj_k` / `kdj_d` / `kdj_j` 三个键（向后兼容）

### 验证

- 新增 `tests/test_v10_2_0_intraday_capture.py` **5/5 PASS**：
  1. KDJ 数值正确性（J=3K-2D 恒等、K 越界检查）
  2. 新因子因果守护（perturbation_test，n_checks=12，worst_diff=0）
  3. v3 信号兼容性（必填字段全）
  4. v3 灵敏性（合成日内波动数据：v3=7 信号 vs v2=0）
  5. v3 新 reason 触发（强背离场景：3 次量价底背离命中）
- 旧回归无破坏：`tests/test_leak_guard.py` 4/4、`tests/test_evolution.py` 4/4 仍 PASS。

### 不在本次范围（避免动 monitor 生产代码）
- 暂不替换 monitor.detect_for 默认检测器；v3 通过 `indicators.detect_signals_v3` 暴露供回测对比与后续灰度。
- monitor_config.json 未变更（仍由 daily_iterate.py / auto_tune.py 护栏控制参数）。
- 数据质量哨兵（live vs recalc）口径不变。

### 已知缺口（下一轮迭代方向）
- v3 新增 reason 在生产回测上的胜率/盈亏比尚未验证 → 需要至少 1 个月的 live 信号积累
  （daily_signal_review 自动沉淀），后续在 research/ 跑 v2 vs v3 对比报告。
- `vol_price_div` / `macd_div` 阈值（LOCAL_W=15 / DIV_VOL_RATIO=0.7）当前为合理起点，
  待 evolution.py 池级 OOS 评估后微调（参考 candidate `vol_ratio_b_low` 的晋升路径）。

## 2026-08-18 — 大更新：零未来函数 + 池级因子演化引擎（4 Phase 全量落地）
> 战略转向：优化对象从「per-symbol 参数(trail/atr)」升级为「因子/门控规则」，目标函数为
> **池级** total_ret + 净夏普 + 逐年稳健（不对单一标的调参）。4 阶段：零未来函数→单一因子源→因子演化→清理。

### Phase 1 — 零未来函数 + 池级评估基建
- `monitor.detect_for` 增 `trim_frontier`：live 只对已收盘 bar 出信号（修「live 同根前视」）；`run()` 传 True。
- 新增 `monitor._bar_tradability`：锁涨停不可买/锁跌停不可卖/停牌不可成交（修「涨跌停/停牌无过滤」）。
- 新增 `core/pool_eval.py`：全池合并 round-trip → 池级 total_ret/净夏普/逐年稳健。
- 修复 `exit_manager.simulate_day` 的 entry_date 未透传 bug（6 处 `_mk_trip` 补 `entry_date=day_date`）→ 逐年口径真正生效。

### Phase 2 — 单一因子源 + 因子注册表
- 新增 `core/primitives.py`：ema/atr/vwap/rsi/vol_ratio 单一实现；`indicators.py` 与 `miji_alpha.py` 改 import，删除复制版。
- 新增 `core/factor_registry.py`：因子注册表（8 个因果因子），纳入 leak_guard 守护。

### Phase 3 — 因子演化引擎 MVP
- 新增 `core/evolution.py`：候选门控 → 池级 IS/OOS → 晋升/淘汰（OOS total_ret 改善 + wr 不降才 PROMOTE）。
- 新增 `scripts/factor_evolve.py`：演化 CLI，落盘 `output/research/factor_evolution_<date>.json`。
- 演化实测：`vol_ratio_b_low` PROMOTE（OOS Δret +9.61pp）；`rsi_b_oversold` DEMOTE（IS +12.41%→OOS -7.11pp 反转）；单标的深跌拟合反例 DEMOTE。

### Phase 4 — ML 下线 + 清理
- ML 影子下线：`data/monitor_config.json` 161129 `ml_enable=false`（ml_features 丢失本就 fail-open）。
- watchlist/config 对齐：移除孤儿 688111/300308，补 300757；标的集收敛为 {161129, 300757, 513310}。
- 删除 `miji_alpha.py` 死代码块（5m/指数门控、mtf、aggregate_60m_direction、_np_asarray，全仓零引用）；修 MPR 过时注释。

## 2026-08-17 — 前视偏差防护栅栏（AQuA 论文研判回灌，跨版本 Integrity 层）
> 来源：普林斯顿×蚂蚁 AQuA「自进化量化研究智能体」+ QuantML A股复现。
> 性质：**不改动信号语义的防御性 Integrity 层**，独立于版本号，全版本生效。

### 新增 — `core/leak_guard.py`（未来扰动测试）
- `perturbation_test(feat_fn, ohlcv)`：在多个切分点 k 仅对 k 之后"未来"bar 灌高斯噪声，
  重算全量特征并比对 k 及之前的历史值；历史值不变 → 无泄漏。可复算、可回归的硬检验。
- `assert_no_lookahead`：失败抛 AssertionError（接入 selftest / pytest）。
- 覆盖 **v9 特征栈**（`compute_indicators`）与 **miji 特征栈**（`compute_miji_indicators`）
  两套信号引擎统一受防；新增含未来数据的特征会立刻变红（防回归）。
- `tests/test_leak_guard.py` 含红测：故意构造"全样本均值做分母"的泄漏特征，断言栅栏抓出 →
  证明栅栏非摆设（否则比漏报更危险）。

### 修复 — miji 多周期 MACD 周期内前视泄漏（真实线上 bug）
- `core/miji_alpha.py:compute_multi_period_macd` 原用 `np.maximum.at(seg_max, boundary, c)`
  取"段内 max close"，但段 [boundary(b), boundary(b)+p) 含 b 之后的未来 bar —— 即 bar b 的
  60m 重采样 rc[b] 偷看了同周期内未来分钟（如 9:31 含 10:30 收盘）。
- **影响范围**：生产 `monitor_config.json` 中 161129/513310/688111/300308 四个标的
  `mpr_enable:"B"`（B 侧 60m 方向过滤**默认开启**），即该泄漏在生产 B 侧真实生效。
- 修复：改为段内截至当前 bar 的**因果运行最大值**（rc[b] 仅依赖 c[0..b]），受 leak_guard 守护。
- 修复后栅栏 v9/miji 双栈均 ok=True。建议对 mpr 启用标的重跑回测/OOS 以量化真实收益变化。

### 增强 — `core/exit_manager.aggregate_metrics` 增逐年稳健口径（AQuA 第三点：IC≠夏普）
- 新增 `yearly`（按 entry_date 年份聚合：净收益/胜率/净夏普/是否正年）、`yearly_consistent`
  （是否逐年全正）、`worst_year`。净夏普的逐年稳健性比聚合夏普更能暴露样本外分布漂移。
- `simulate_day` 透传 `prices['date']` 到 trip 的 `entry_date`；无日期路径（实时/旧回放）自动跳过。
- `factor_optimizer.metrics_of` 透出 `yearly`；`oos_validate` 裁决理由追加"逐年全正/存在负年"提示；
  `backtest_screener` 汇总打印追加逐年标记。全链路统一消费同一口径。

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

## v9.4.1（2026-08-04）小版本 —— T0/T1开关启用 + trail两段式PASS灰度688111
- 用户批准启用 settle_split_enable=true（生产实测；T1三只每方向日限1次往返）
- two_stage_trail_review.py 首跑：stage1 tune_pool_40 全池 48.3%→53.4%（0.5/0.5，+5.1pp，n=3838）；stage2 watchlist 池级 56.2%→63.2%（+7.0pp，无单只劣化>2pp）→ PASS
- core/monitor.py：exit_param() per-symbol 出场参数覆盖（热重载，缺省回退全局）
- 灰度：688111 trail 0.4/0.6→0.5/0.5（3日观察期，回滚=删该字段）；其余4只维持 0.4/0.6

## v9.4.2（2026-08-05 凌晨）小版本 —— 0805 迭代：R-A 零信号归因 + R-B 振荡器验证
- ra_zero_signal_grid.py：atr×mhd×mpr 联合网格（3标的×27组合全历史）→ MHD 零影响、主闸门=mpr60+atr；无既多又准松绑点，参数不动
- rb_oscillator_eval.py：RSI(20/80、30/70)/KDJ_J 裸反转离线验证 → 全部不及格（wr 21-42%，pl<1），振荡器不可作独立触发源
- 收敛假设：振荡器降级为确认/过滤器（引力信号+RSI极值位置过滤），513310/600570 盈亏比<1 指向出场侧

## v10.0.0（2026-08-05）大版本 —— 量能确认因子 + watchlist调整（删300058/600570 增300308）
- core/v10_confirm.py 量能确认（B缩量≤1.2x/S平量≥1.0x，尾窗无前视）；monitor 接 settle 闸门后（vol_confirm 热重载）
- 消融（5只全历史）：池级 ret 13.08→18.80(+43.7%)、wr 53.9→55.5%、dd 17.88→15.73%、sharpe 0.71→1.18
- 300308 中际旭创验证：ret 5.24→26.41、wr 52.6→56.3%、dd 18.67→10.77%、sharpe 0.24→1.78（F盘回填98d，fdisk_backfill.py）
- 688111 trail 灰度撤销回退 0.4/0.6（胜率虚胖实证：pl 1.0→0.49、ret 8.18→-4.29）
- 否决留档：RSI/KDJ 位置过滤/裸触发（rb_oscillator_eval、v10_factor_grid 消融证据）
- 注：本地 .git 对象库 0805 凌晨损坏（index.lock/EDR 干扰链），经 GitHub tags + 工作区重建恢复；v9.3.1~v9.4.2 历史经远端 tags 完整保留

## v10.0.1（2026-08-05）最优因子上线（用户指令：解除优化阻碍，total_ret优先口径）
- 161129.SZ trail: 0.4/0.6 → 0.5/0.6（ret 4.02%→4.14%，wr 49.4%→51.9%）
- 513310.SH trail: 0.4/0.6 → 0.3/0.5（ret -8.74%→-3.15%，wr 43.3%→44.8%）
- 688111.SH trail: 0.4/0.6 → 0.5/0.8（ret -32.62%→-29.23%，wr 47.0%→48.0%）
- 300308.SZ trail: 0.4/0.6 → 0.5/0.6（ret 5.24%→14.1%，wr 52.6%→58.3%）
- 回测报告：output/optimal_deploy_2026-08-05.html

## v10.1.0（2026-08-05）自迭代闭环 + 独立因子研究
- 新增 daily_closed_loop.py：五环自迭代闭环（检验→排查→寻优→组合回测→次日算法落盘），bat 第10步
- 新增 gate_ablation.py：零推送标的闸门消融探针（区分算法卡死 vs 生产侧抑制）
- 新增 deploy_optimal.py：最优因子上线器（total_ret 优先 + wr 不降硬约束），4只 trail 已上线（v10.0.1）
- 新增 factor_research_independent.py：独立因子研究（Alpha101/191+经典23因子 IC 评估）
- 修复：_today.py / fdisk_daily_update.py 从 git 历史还原（v10.0.0 灾难恢复丢失，bat %D% 空根因）
- 加固：ml_features 缺失 fail-open 守卫；selfcheck/watchdog 修复
- 结论：分钟级 alpha 集中于均值回复族（HLPOS IC -0.437 最优）；Alpha101 移植无效被数据否决

## v10.1.1（2026-08-12）P0 生产事故修复 —— bar 已处理标记跨日残留致「非周一全天静默零信号」
> 信号语义**不变**（intended behavior 未改，修的是让信号真正能发出来）→ 按 docs/versioning.md 判为 PATCH。
> 影响面：v10 上线（08-05）以来，除周一外的交易日若 monitor 未重启，全天零信号零推送。

### 根因（一句话）
`detect_for` 的已处理标记 `bar_{sym}_{i}`（i = 当日 1m bar 行号 0~239）跨日必然同名碰撞，而
「跨日清理」只写在 `load_state()`（= **进程重启**且日期变了才执行）。monitor 在交易日收盘后走
keepalive 分支持续存活（只有周末/非交易日才 `sys.exit(0)`）→ 周二~周五连续运行时昨日 240 个
标记原封不动带进新交易日，`detect_for` 对当日每根同名 bar 命中 `if st.get(bar_key): continue`
→ **一根都不评估**，全天静默零信号、零推送、零异常日志。

### 证据链
- `data/state.json`：718 个 `bar_*` 标记时间戳 **100% 为 08-10**，无一为 08-11/08-12。
- 同时 `_b_count_/_s_count_{sym}_20260811 = 0` 键**存在** → detect_for 被调用过，但一根 bar 未评估。
- `data/signal.txt`：`[2026-08-07]` 有信号、`[2026-08-10]` 有信号（周一，周末退出重启清过标记）、
  **`[2026-08-11]` 整天空白**（周二，连续运行）。
- 离线用生产同源 `detect_for` + 空 st 重放 08-11 F盘 tickflow：300757 出 **4 个信号**
  （B@09:35 481.54 / X@09:51 / S@10:20 488.60 / X@14:02）→ 算法侧无缺陷，纯属被标记吞掉。
- 日志侧同步排除：08-11 sina_ok=67、no_intraday 仅盘前 09:25-09:33 预热、951 轮扫描、
  process_exc=0、首扫白名单放行=0、推送批次=0 → 数据健康、零异常，唯独零信号。
- 排除项：selfcheck 连通性误报（已修）、`risk_override.json` 已过期（`_load_risk_override` 返回
  NONE，fail-open）、数据层断流、算法参数。

### 修复（双层，core/monitor.py）
1. **结构免疫**：`bar_key` 加日期维度 → `f"bar_{sym}_{YYYYMMDD}_{i}"`（detect_for 与 run() 首扫
   标记两处同步），跨日 idx 碰撞在结构上不可能再发生。
2. **运行态锚点清理**：把「跨日清理」从重启路径搬到 run() 的 `_daily_refreshed_date` 变化分支，
   日期一变即清 `bar_/pos_/_cooldown_/_miss_/alerted_miss_`（与重启路径同键集），并重置
   `first_scan_done`。⚠️ 明确保留 `_b_count_/_s_count_{sym}_{YYYYMMDD}`（每日复盘实盘权威源）。
   顺带修掉次生缺陷：`pos_*` 幽灵持仓跨日残留（会用昨日 entry_price/entry_idx 管今日出场）。

### 验证
- 回归测试 `scripts/test_bar_key_crossday.py` **5/5 PASS**（数据=300757 08-11 真实 1m）：
  A 空 st=4 ／ B 旧格式残留全量=4（对历史脏键免疫）／ C 新格式昨日全量=4（结构免疫）／
  D 新格式当日全量=0（**当日去重仍生效**，防重复推送刷屏）／ E 清理键集保住复盘权威计数键。
- 实盘热切换（08-12 09:38 盘中）：备份 state → 清 725 残留键 → 游标回拨 09:30 → 杀 PID 44800
  → watchdog 拉起 PID 45316（新码）。state.json 键格式变为 `bar_161129.SZ_20260812_2`，
  `_s_count_300757.SZ_20260812` 由 0 → **1**（detect_for 恢复评估并产出信号）。
- 修复脚本侧同源缺陷：`scripts/sim_detect_today.py` 的首扫模拟键同步改带日期，否则标记永不命中
  → 抑制模拟静默失效。

### 已知损失（不补推，仅留档）
- 08-11 300757 漏发 4 条（B@09:35 481.54 / S@10:20 488.60 / X 两条）。
- 08-12 09:34 300757 **S @496.00**（触及上轨 495.60，dev +1.47%）—— 先被中毒进程吞，热切换后
  又落在首扫 now-3min 历史窗外未重发；已计入 `_s_count`，当日复盘可见。
- 陈旧信号不回灌补推（避免以过期价格发实时卡片误导决策）。

### 归并归档（08-11 完成但未单独发版，随本版入档）
- vol_confirm 死配置**结构修复**：量能确认过滤块原缩进在 `settle_rules` 的 `except` 分支内，而
  `split_enabled()` 正常返回 False 不抛异常 → 该过滤器自 v10.0.0「上线」起从未执行过一次；
  已 dedent 回主路径，同时按纪律把 4 只 `vol_confirm` 置 false（行为中性，待 OOS≥20 trip 复评）。
- `oos_validate` 配置状态泄漏修复（信号侧 trail 与网格出场侧不同参 → 同参数两跑结论相反）；
  513310 trail 终态 0.3/0.5。遗留：`factor_optimizer.py` 仍带同一泄漏，其报告仅作候选粗筛。

## v10.1.2（2026-08-12）P1 修复 —— 首扫白名单与抑制侧口径不一致致「幽灵计数」漏推
> 信号语义**不变**（detect_for 产出不变，修的是 emit 侧误杀），→ PATCH。
> 同日 v10.1.1 热切换后立刻实证到的**第二个独立缺陷**，是 08-07 那次修复只做了一半的残留。

### 根因（一句话）
08-07 把首扫抑制改成「持久化游标」时，只改了**抑制标记侧**（走游标路径时仅把 `<= 游标` 的 bar
标记为已处理，游标之后的 bar 交给 detect_for 真正评估），却漏改**emit 侧白名单**——那里仍硬编码
`recent_cutoff = now - 3min`。于是落在 **(游标, now-3min)** 区间的信号被当作"历史重扫"丢弃：
`detect_for` 已经把它计入 `_b_count_/_s_count_`，但一次都没推送 → **幽灵计数**
（复盘计数有、飞书没收到、`push_audit.jsonl` 无记录）。两侧口径互相矛盾，注释写的是 A 行为、
代码执行的是 B 行为。

### 证据链
- 08-12 罗博特科(300757) **S @496.00 @09:34**：游标 09:30、进程 09:40 起来 → 09:34 < 09:37 被丢；
  `_s_count_300757.SZ_20260812 = 1` 却 `grep -c 2026-08-12 data/push_audit.jsonl = 0`。
- `push_audit.jsonl` 最后一条为 08-10 14:30 → 08-11（P0 事故）与 08-12（本缺陷）均零推送记录。
- 代码自证：`core/monitor.py` 抑制侧注释明写「游标后的实时 bar 正常处理/推送」，emit 侧却用
  固定 3min 窗口 → 内部不一致，非外部因素。

### 修复（core/monitor.py）
- 新增可测试纯函数 `_first_scan_cutoff(now, cursor, use_cursor, recent_min=3, replay_max_age_s)`：
  - 走游标路径 → `cutoff = clamp(min(now-3min, 游标), 下界 = now - REPLAY_MAX_AGE_S)`
    → 游标之后的信号**全部放行**，同时以 10 分钟 age 兜底，避免游标异常陈旧时重发过期价格；
  - 非游标路径（长时间死亡/跨日/无游标/脏游标）→ 保持 `now-3min` 保守语义**不变**。
- run() 首扫分支改调该函数，并打印 `🧭 首扫白名单下界=HH:MM:SS (游标 … 对齐, 标的)` 便于事后审计。

### 验证
- 新增 `scripts/test_first_scan_cutoff.py` **6/6 PASS**：
  A 事故复现（游标09:30/now09:40 → cutoff 09:30，且断言"新放行 + 旧抑制"证明行为差异）／
  B 非游标路径不放宽／C 新游标不收紧窗口／D 陈旧游标被 age 兜底／E 脏游标安全回退／
  F 跨日边界 23:58→00:01 用 datetime 比较不错乱。
- P0 回归 `test_bar_key_crossday.py` 复跑仍 **5/5 PASS**（两次修复互不破坏）。
- 实盘热切换 08-12 09:58：杀 PID 51296 → watchdog 拉起 **PID 46436**，日志出现
  `🧭 首扫白名单下界=09:55:01 (游标 09:58:00 对齐)` → 新逻辑在线（该场景游标较新，按设计保留
  更宽松的 3min 窗口 = 测试用例 C 的行为）。

### 运维含义
- 此前"重启会吃掉重启前 ≤3 分钟内的真实信号"这一长期已知损耗（08-07 记录过一次）**至此消除**：
  只要是健康快速重启（间隔 ≤ RESTART_GRACE_MIN=15min），游标之后的信号都会补发。
- 后续若再出现「复盘计数 > 飞书收到条数」，优先按幽灵计数排查 emit 侧闸门，而非算法侧。

### 完整漏发清单（续作回灌 · 2026-08-12）
> 用生产同源 `monitor.detect_for` 重放 F 盘 tickflow 1m（空仓起步 + 无首扫抑制 = 理想上界），
> 与实盘 `data/push_audit.jsonl` 交叉核对，得出 P0+P1 事故全程的**用户可见漏发**。
> 脚本 `scripts/replay_incident_range.py` → `output/replay_incident_inventory.json`；
> 报告 `output/leakage_inventory_20260812.html`。

- **区间 08-05~08-12，当前 3 标的总体共漏发 14 条信号**：
  - **161129 原油LOF：5 条**（08-05 漏 4、08-06 漏 1；08-07~08-11 实发生成 0；08-12 生成 0 非漏发）。
    其最后一次实际推送为 08-03，事故期实质「全覆盖静默」，值得后续单独关注触发频率。
  - **513310 中韩半导体ETF：0 条**（引擎在事故期罕见触发，1 条历史推送早于窗口）。
  - **300757 罗博特科：9 条**（08-07 入池；08-07 漏 4〔2B+2X，P1〕、08-10 **全量送达 0 漏发 ✓**、
    08-11 漏 4〔P0/P1〕、08-12 漏 1〔S@496.00，P1 修复前窗口 09:30–09:58〕）。
- **08-10（周一）全量送达、0 漏发** = 关键反证：P0 仅在「连续交易日且进程未重启」时发作，
  周一因周末退出后全新启动无跨日残留 → 引擎本身健康，损失 100% 来自 P0+P1 两缺陷。
- **已知缺口**：08-05~08-06 历史 watchlist（300308/688111/300058，08-07 才改当前 3 标的）未重放，
  其 P0 静默日漏发未计入 14 条；300757 08-05/06「尚未入池」本就不计入。
- **处置**：不补推（过期价格发实时卡片误导决策），14 条均已计入 `_b/_s_count` 复盘可见。

## v10.1.3（2026-08-12）P2 修复 —— 首扫抑制边界从「最后扫描 bar」改为「最后推送信号」
> 信号语义**不变**（detect_for 产出不变，修的是首扫抑制的"已处理"判定口径），→ PATCH。
> 针对 08-12 用户反馈"今天唯一真实信号损耗点 = 首扫抑制太粗"的根治。

### 根因（一句话）
首扫抑制的"已处理"边界用的是 `BAR_CURSOR`（**最后扫描到的 bar 时间戳**），但**扫描 ≠ 推送**：
emit 侧抑制 / 进程崩溃都可能让某根 bar 的信号"被扫描却未推送"。按游标抑制会把这类
"已扫描未推送"的实时信号当成历史重扫丢弃——08-12 罗博特科 09:34 的 S 即此形态
（被首扫窗口吞掉）。游标只能证明"扫过"，不能证明"推过"。

### 修复（core/monitor.py）
- 新增持久化 `LAST_PUSHED_TS`（每标的今日最后推送信号时间戳，`data/last_pushed_ts.json`，
  跨进程持久化、跨日只保留今日条目）。emit 成功时由 `_record_pushed()` 写入（字符串
  `'YYYY-MM-DD HH:MM:SS'` 可直接字典序比较，取最大值且不回退）。
- 首扫抑制边界改用 `LAST_PUSHED_TS`：**仅抑制 `<= 它的信号（确已推送），严格晚于它的全部放行
  （含"扫描未推送"的缺口实时信号）**。下界判定用**严格大于**（`s_dt > cutoff`），保证恰好等于
  last_pushed 的信号不会被跨重启重复推送（防重复推送优先）。
- 叠加 `REPLAY_MAX_AGE_S`（10min）兜底：last_pushed 过旧（长断线）时回落 `now-10min`，
  避免恢复时把死亡期间的历史信号刷屏重发。
- **首扫不再按游标预标记已处理 bar**：改为重置当日 `_b/_s_count` 为 0、让 `detect_for` 重算全部
  bar（其自身逐根打 `bar_{sym}_{今日}_{i}` 标记）。该重算**幂等**——多次重启重置+重算结果一致，
  复盘计数 `_b/_s_count` 口径不变；同时既不会吞实时信号、也不会重复推送。
- `_first_scan_cutoff()` 扩展 `pushed_ts` 参数（旧游标路径作为无推送时间戳时的回退，兼容旧测试）。

### 验证
- 新增 `scripts/test_last_pushed_cutoff.py` **15/15 PASS**：边界语义／跨重启不重复推送／缺口恢复／
  长断线回落 floor／跨日裁剪／`_upd_last_pushed` 取最大不回退／旧游标路径等价。
- 旧回归无破坏：`test_first_scan_cutoff.py` 6/6、`test_bar_key_crossday.py` 5/5 仍 PASS。
- 实盘热切换 08-12 22:21：杀 PID 46436 → watchdog 拉起 **PID 9556**，无 import/syntax 错误，
  metrics 正常（真实首扫验证留待次日开盘）。

### 运维含义
- "重启吞实时信号"这一损耗点**至此根治**：无论重启几次，只要某信号被扫描但此前未推送，
  下次首扫都会以 last_pushed 为界补发；同时严格 `>` 判定保证不会重发已推送信号。
- `data/last_pushed_ts.json` 成为新的首扫审计依据（与 `bar_cursor.json` 并存，后者保留作兼容/观察）。

