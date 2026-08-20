# 做T方法体系（Methodology Framework）v1.1.0

> **文档版本**：v1.1.0  
> **制定日期**：2026-08-20  
> **对应算法版本**：tpoint v10.3.0  
> **文档位置**：`docs/methodology_framework.md`  
> **需求来源**：散户专属做T秘籍（截图：本质 / 两种手法 / 三大神技 / 仓位与纪律四大块）  
> **维护**：tpoint 自动化 agent；后续每次结构/量化/参数变更需 bump 版本号  
>
> **与其它文档的层级关系**：
>
> | 层级 | 文档 | 职责 |
> | --- | --- | --- |
> | 本方法论层（本文） | `docs/methodology_framework.md` | 方法体系、量化定义、覆盖率与评估口径的权威真源 |
> | KPI 定位层 | `docs/positioning.md` | tpoint 三大核心竞争力 + 与卡方等参照系统的差异 |
> | 简介层 | `docs/v10_algorithm_intro.md` | 一分钟读懂 + 当前监控标的 |
> | 操作层 | `docs/t0_playbook.md` | 买入/监控/卖出 三阶段人工执行手册 |
> | 参数层 | `docs/parameter_inventory.md` | 全量参数基线表（含开源对照） |

---

## 0. 文档元信息（Metadata）

| 字段 | 值 |
| --- | --- |
| 文档版本 | **v1.1.0** |
| 制定日期 | 2026-08-20 |
| 对应算法版本 | tpoint v10.3.0 |
| 对应分支 | `feat/intraday-capture-v10.2.0`（叠加 v10.3.0 综合评分模型） |
| 上游需求 | 散户专属做T秘籍·截图（本质/手法/神技/纪律）+ 用户要求「三大策略+RSSI 综合评分模型」 |
| 维护者 | tpoint 自动化 agent |
| 关联模块 | `core/indicators.py` `core/miji_alpha.py` `core/primitives.py` `core/factor_registry.py` `core/exit_manager.py` `core/shadow_v3.py` `core/composite_scorer.py`（v10.3.0 新增） |
| 变更范围 | 新增「综合评分模型」章节（§4.5），将三大神技 + RSI 整合为连续加权评分引擎 |

---

## 1. 版本号规则（独立于算法版本）

| 维度 | 触发变更 |
| --- | --- |
| **MAJOR** | 体系结构调整：新增第四神技 / 新增第五纪律 / 新增评价维度 |
| **MINOR** | 单条神技或单条纪律的量化定义新增/修订（体系结构不变） |
| **PATCH** | 阈值数值微调、措辞修订、链接修正 |

示例：
- `v1.1.0` = 新增「三大神技 + RSI 综合评分模型」（量化融合层，MINOR：结构不变、新增可量化整合方法）。
- `v2.0.0` = 新增第四神技（如「分时 BOLL 收口」）或第五纪律（MAJOR：体系结构调整）。
- `v1.0.1` = 修正 KDJ 超卖阈值（PATCH：数值微调）。
- 与算法 `v9→v10` 升级解耦：**方法论 bump 由本文驱动，算法 bump 由 `VERSION`/`CHANGELOG` 驱动**。

---

## 2. 本质（Essence）

**1 行定义**：利用已持有的「底仓」，在同一交易日内完成 **N 次「高抛低吸」**（N≥1），变相实现 T+0。

**3 条边界（不满足任一即出局）**：

| # | 边界 | 系统含义 |
| --- | --- | --- |
| 1 | **底仓前提** | 必须先持有 ≥ 1 份标的；不允许裸空做T |
| 2 | **日内闭环** | 当日买入的所有 T 份额必须当日卖出（不跨日持仓） |
| 3 | **等额对冲** | 单次 T 的买/卖数量相等，不改变底仓净头寸 |

**目标量化（与 KPI §8 对齐）**：单笔 T 期望净收益 ≥ 0.05%（盖住双边成本，且有正 alpha）。

---

## 3. 两种手法（Dual Mode）

| 手法 | 中文 | 标准定义 | 生产信号路径 | 触发场景 |
| --- | --- | --- | --- | --- |
| **正T** | 先买后卖（long-first） | 同日内 buy → sell；要求已有底仓，可接等量低吸后回升时卖出 | B（detect_signals）→ exit (S/TRAIL) | 开盘急跌 / 急跌后回升预期 |
| **反T** | 先卖后买（short-first） | 同日内 sell → buyback；要求已有可卖底仓，冲高时先减仓再低位回补 | S → close-and-buyback | 开盘高开急冲 / 单边冲高后回落预期 |

| 维度 | 正T | 反T |
| --- | --- | --- |
| 底仓要求 | 必须已持有 ≥ T 份额 | 必须已持有可卖份额（≥ T） |
| 主风险 | 买在半山腰（假反弹） | 卖在起涨点（强势踏空） |
| 启用条件 | `trend ≥ 0`（生产默认） | `trend ≤ 0`（震荡/下降环境） |
| 反向豁免 | 反T 后 `REV_CLOSE_BARS=30`（30 bar）内 B 豁免趋势过滤 | 同左 |
| 信号上限 | `MAX_B_DAILY=12` | `MAX_S_DAILY=12` |
| 冷却 | `COOLDOWN=120s`（同标同向） | 同左 |
| T+1 结算闸门（v9.4.0+） | T+1 个股每方向每日 1 次完整往返；T+0 LOF/ETF 不限 | 同左 |

---

## 4. 三大神技（Three Techniques — 核心可量化）

### 神技 #1 · 分时均线「引力定律」（Mean Reversion）

**文字定义**：价格偏离均线过远会回归。**急涨**远离均线 → 卖；**急跌**远离均线 → 买。

> 注：截图原文在"卖"侧与"买"侧均带「急跌远离均线→买」，按上下文分别解读为 S 触发「急涨远离→卖」与 B 触发「急跌远离→买」。

| 维度 | 量化定义 |
| --- | --- |
| 指标 | VWAP（典型价加权） + ATR（14 周期 Wilder） |
| 公式 | 下轨 = VWAP − K1·ATR；上轨 = VWAP + K1·ATR；极端轨 = ±K2·ATR |
| 当前参数 | K1 = 1.0（标准轨） / K2 = 2.0（极端轨）；生产引子轨 `K1_V2=0.8` / `K2_V2=1.8` |
| B 触发 | 前根 close/low 戳破下轨 + 当根收回 → 原因 `回踩下轨`；或 low 戳破极端轨 + 长下影 ≥ 1·ATR → `极端超卖反弹` |
| S 触发 | 前根戳破上轨 + 反转 K（阴线/上影）+ 当根收回 → 原因 `反弹遇阻`；或对上极端轨 + 长上影 |
| 实现位置 | `core/indicators.py`（v9）+ `core/miji_alpha.py`（gravity 引力因子） |
| 数据需求 | 1m OHLCV（含成交量） |
| 风险点 | 单边趋势日内会连续突破，造成 left-trade（追涨杀跌）；由 trend 过滤（EMA20>EMA60 + ADX>20）缓解 |
| v10.2.0 改动 | 无（行为不变；与神技#2/#3 并联运行） |

### 神技 #2 · 量价背离「动能衰竭」（Vol-Price Divergence）

**文字定义**：**价格新高**但成交量一波比一波小 → 可能回调（顶背离）；**价格新低**但成交量萎缩 → 可能反弹（底背离）。

| 维度 | 量化定义 |
| --- | --- |
| 指标 | price_high/low（窗口极值） + vol_ratio（当前/均量） |
| 公式 | `LOCAL_W=15`（窗口）；`DIV_VOL_RATIO=0.7`（量缩阈值） |
| B 判定 | `C[i] == min(C[i-W+1..i])` + `vol_ratio[i] < vol_ma[i]·0.7` → 底背离成立 |
| S 判定 | `C[i] == max(...)` + `vol_ratio[i] < 0.7` → 顶背离成立 |
| 当前生产状态 | `VOL_DIV_ENABLED=False`（默认关；实证净负 −1.49pp 已禁用，详见 `parameter_inventory.md` §1） |
| v10.2.0 重启 | 新因子 `f_vol_price_div` 注册到 `FACTORS`；`LOCAL_W=15` / `DIV_VOL_RATIO=0.7`；v3 检测候选加分（仍在 shadow，未上生产） |
| 实现位置 | `core/factor_registry.f_vol_price_div`；`core/indicators.detect_signals_v3` |
| 数据需求 | 1m OHLCV（**必须有量**，缺量则跳过不报） |
| 风险点 | 强趋势日（连续一字/缩量突破）易漏；由 `trend_strong` 二次确认缓解 |

### 神技 #3 · 分时 MACD「背离确认」（MACD Divergence）

**文字定义**：**买** = 股价新低 + MACD 红柱缩短 / 绿柱收敛；**卖** = 股价新高 + MACD 红柱缩短 / 绿柱放大。

| 维度 | 量化定义 |
| --- | --- |
| 指标 | MACD（fast=12 / slow=26 / signal=9）；`hist = (DIF−DEA)·2` |
| 公式 | `DIF=EMA(C,12)−EMA(C,26)`；`DEA=EMA(DIF,9)`；`HIST=(DIF−DEA)·2` |
| 窗口 | `LOCAL_W=15`（价格/动能对齐窗口） |
| B 判定 | `C[i] < min(C[i-W..i-1])` + `hist` 较 `hist[i-1]` 缩短（仍 >0 或仍 <0 收敛） → 「底背离」 |
| S 判定 | `C[i] > max(C[i-W..i-1])` + `hist` 三连降（绿柱放大） → 「顶背离」 |
| 背离强度 | `hist[i] − min(hist[i-W..i-1]) ≥ 0.15` → 强背离（候选阈值，未上生产） |
| 当前生产设置 | `min_hist_diff=0.0`（生产全放行） |
| 推荐改进 | 借鉴 T0T 双点法（价格极值+MACD 极值双窗比较）；实证最优 0.15（8 标的转正 +7.77%），未接入生产 |
| 实现位置 | `core/factor_registry.f_macd_div`；`core/miji_alpha`（macd_div 因子）；`core/primitives`（K 线背离基础） |
| 数据需求 | 1m OHLC（无量也可算） |
| 风险点 | 单点背离假信号多；推荐双点（T0T 风格）替代，但尚未实现 |

### 4.x 协同指标（Co-indicators — 不属于神技但常联动）

| 指标 | 参数 | 在 v9/v10 中的角色 |
| --- | --- | --- |
| **KDJ** | N=9, K=3, D=3 → K/D/J | v10.2.0 新增；因果前向实现 `core/primitives.compute_kdj`；J<0 或 K<20 → B 候选加分；J>100 或 K>80 → S 候选加分 |
| **RSI** | 14 | v9 已有；RSI<35 → B 加分；RSI≥55 → S 加分；`temperature` 子因子（权重 0.4） |
| **EMA 趋势** | 5 / 20 | `trend ∈ {+1,0,−1}`（fast vs slow + slope）；`trend_strong` 需 `confirm_bars=8` |
| **量比 vol_ratio** | 当前 / 均量 | `VOL_THRESHOLD=2.0` 入门用；`vol_in_gate=False`（仅计分不参与放行） |
| **温度 temp** | 0.4/0.2/0.2/0.2 | RSI + 涨跌幅 + 量比 + 偏离 加权；做信号星级、过滤参考 |

### 4.5 · 综合评分模型（Composite Scoring — v10.3.0 新增）

> **定位**：把 §4 的三大神技 + §4.x 的 RSI 从「离散布尔触发（v3）」升级为「连续加权评分引擎（v4）」。
> 实现：`core/composite_scorer.py`（`CompositeConfig` + `detect_signals_v4`）；纯增量，不改动 v9/v2/v3/monitor。

**为何要从布尔触发升级为连续评分**（本质区别）：

- v3（`detect_signals_v3`）用「多条件 AND」布尔触发 → 信号是 0/1 离散值，易信号爆发（密度失控）或漏触（条件卡太严）。
- v4（`detect_signals_v4`）每个组件先算**连续子评分 `C ∈ [-1, 1]`**（方向 × 强度），再加权求和 → 天然支持「强度分级 / 多因子融合 / 权重可配」。

**计算逻辑（全部因果前向，无未来函数）**：

| 组件 | 公式 | 语义 |
| --- | --- | --- |
| `C_vwap`（神技#1） | `−tanh((close − vwap) / (k1·ATR))` | 价低于带 → 正(买)；高于带 → 负(卖)，饱和有界 |
| `C_vol_div`（神技#2） | `±shrink`，`shrink = clip((0.7 − vol_ratio)/0.7, 0, 1)` | 价极值 + 缩量 → 动能衰竭；买底背离取正、卖顶背离取负 |
| `C_macd_div`（神技#3） | `±strength`，`strength = clip((hist − hist_min)/(|hist_min|+ε), 0, 1)` | 价新低而 MACD 不新低（底背离）取正；反之取负 |
| `C_rsi`（RSI 协同） | `clip((rsi_neutral − rsi) / ((overbought − oversold)/2), −1, 1)` | RSI 超卖 → 正；超买 → 负，线性映射 |

**加权融合与信号规则（显式加权规则）**：

```
composite = (w_vwap·C_vwap + w_vol_div·C_vol_div + w_macd_div·C_macd_div + w_rsi·C_rsi) / Σw   # Σw = 权重和（自动归一化）
信号：  composite ≥ buy_threshold  → B
        composite ≤ −sell_threshold → S
        否则 → HOLD（不出信号）
强度：  strength = |composite|；strong(≥0.62) / medium(≥0.50) / weak
触发条件： triggers = 所有 |C| > trigger_eps(0.02) 的组件（带方向 ±1 与贡献值），供审计/解释
```

**可配置参数（`CompositeConfig` / `DEFAULT_CONFIG`）**：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `w_vwap / w_macd_div / w_rsi / w_vol_div` | 1.2 / 0.9 / 0.8 / 0.7 | 四组件权重（量价背离实证净负，刻意低配）；自动归一化 |
| `rsi_period / rsi_oversold / rsi_overbought / rsi_neutral` | 14 / 35 / 65 / 50 | RSI 周期与超买超卖线（**用户明确要求可配**） |
| `vwap_k1 / vwap_k2` | 0.8 / 1.8 | 神技#1 标准轨 / 极端轨倍数 |
| `div_local_w / div_vol_ratio` | 15 / 0.7 | 神技#2 窗口 / 量缩阈值 |
| `macd_fast / slow / signal` | 12 / 26 / 9 | 神技#3 MACD 周期 |
| `buy_threshold / sell_threshold` | 0.50 / 0.50 | 综合分出信号门槛（0.35=灵敏、0.55=严控；落 §8 健康密度带附近） |
| `strong_band / medium_band` | 0.62 / 0.50 | 强度分级阈值 |
| `trend_b_allowed / trend_s_allowed` | `(1,)` / `(−1,0,1)` | 趋势门控（B 仅上升市，沿用 v2 生产；S 全放行） |
| `signal_gap / max_b / max_s` | 8 / 12 / 12 | 节奏（防同段行情两面抓 + 控密度） |

**结构化输出（每条信号字段）**：

```
type, idx, price, score(带符号综合分), strength(|score|), strength_band,
rsi, trend, reason, vol_ratio,
components{vwap, vol_div, macd_div, rsi},   # 四组件逐项贡献（供分解）
weights{w_vwap, w_vol_div, w_macd_div, w_rsi},
triggers[{name, dir(±1), val}, ...]          # 实际触发的组件明细
```

**生产可用性 / 回测结论（离线 tickflow 1m，688111×20日 + 603039×2日）**：

- 四组件本身**均衡无偏**（vwap 均值 +0.04 / rsi +0.07，>0 占比均≈52%），无系统性方向偏差。
- 默认趋势门控下 v4 **S 信号主导**（本样本 B/S≈4/112）：因 `exit_manager` 仅支持**正T**配对，S 侧无法参与 round-trip，
  故 v4 的「胜率」指标在本样本仅反映极少量 B 的样本噪声 —— **不代表 v4 真实质量，需 反T 回测支持才能公平评估**。
- 验证：松弛 B 门控后方向恢复平衡（B=136/S=108）但 WR 反降至 18.8%，证明趋势门控确在保护质量（与 v2 一致）。
- 信号密度随阈值可调：0.35→0.55 映射 3.39→1.59 信号/百 bar（落入 §8 健康密度带 0.5–2.0）。
- **判定**：当前维持 v2 生产，v4 仅作离线评估/候选；推进前需补 反T 配对回测（exit_manager 缺口）+ 大样本验证。

---

## 5. 信号漏斗（Pipeline — 端到端 6 步）

```
[1m OHLCV bar]
   │
   ▼ ① 指标层
   core/primitives.compute_indicators(o,h,l,c,v,pc, has_vol=True)
   core/primitives.compute_kdj(h, lo, c)            ← v10.2.0
   core/factor_registry.*                           ← 神技#2/#3 因子
   └→ {vwap, atr, trend, vol_ratio, macd_hist, kdj_k/d/j, f_vol_price_div, f_macd_div}
   │
   ▼ ② 信号源（双栈 + 影子旁路）
   core/miji_alpha.compute_miji_indicators(...)     ← 双栈之一（floor 门控为主，生产）
   core/indicators.detect_signals_v2(...)           ← 双栈之二（生产）
   core/indicators.detect_signals_v3(...)           ← v10.2.0 新增（shadow 旁路，不接推送）
   core/composite_scorer.detect_signals_v4(...)     ← v10.3.0 新增（综合评分模型，离线评估/候选）
   │
   ▼ ③ 风险门控
   - ATR 波动下限 ≥ 0.25
   - mpr60 大周期方向过滤
   - macd_gate_mode=floor（生产）
   - signal_gap ≥ 8bar
   - REV_CLOSE_BARS=30
   - MAX_B/S_DAILY=12
   - COOLDOWN=120s
   │
   ▼ ④ 量能确认（v10.0.0+）
   B 须 vol_ratio ≤ 1.2·MA20（缩量回调）
   S 须 vol_ratio ≥ 1.0·MA20（平量以上）
   │
   ▼ ⑤ 结算闸门（v9.4.0+）
   T+1 个股每方向每日 1 次完整往返（防底仓磨损）
   T+0 LOF/ETF 不限
   │
   ▼ ⑥ 飞书推送
   push_audit.jsonl（必记）+ state.json（持仓实盘记账）
```

每层均可独立灰度/回滚（七项全指标门：收益 / 回撤 / 夏普 / 胜率 / 盈亏比 / 信号量 / 全集不降）。

---

## 6. 出场管理（Exit — 收盘前的「保护伞」）

| 路径 | 优先级 | 触发条件 | 当前生产设置 |
| --- | --- | --- | --- |
| 硬止损 | P0 | `close ≤ entry − STOP_ATR_MULT·ATR`（`STOP_ATR_MULT=1.5`） | **关闭**（`use_stop=False`） |
| S 信号出场 | P1 | 自然触发 S + 已持仓 | **开**（`s_signal_exit=True`） |
| 移动止损 | P2 | `max_fav ≥ entry·(1+0.4%)` 后 `close ≤ max_fav·(1−0.6%)` | **开**（`trail_activate_pct=0.4` / `trail_pct=0.6`） |
| 时间止损 | P3 | 持仓 bar 数 ≥ `time_stop_bars`（默认 90） | **关闭**（`use_time=False`） |
| EOD 强平 | P4 | 14:55（生产未推盘中禁新仓；尾盘系统不强平，靠人工） | 部分缺失 |

**核心原则**：
- 移动止损 = 「利润落袋」的默认保护。
- 硬/时间止损 = 默认关；需另行开启，先回测。
- 新规则可加但必须先过 §9 的覆盖率流程。

---

## 7. 仓位与纪律（Risk Governance — 截图四大块量化）

| 纪律 | 文字版 | 量化版 | 系统执行 |
| --- | --- | --- | --- |
| **单标做T ≤ 1/3 仓位** | 不可满仓T | T 份额 ≤ 底仓 × 1/3；T 资金 ≤ 可用资金 × 33% | 系统不强制（用户人工执行）；UI 标注建议 |
| **做T收益覆盖手续费** | 先算成本再做T | 单笔 T 期望净收益 ≥ 双边总成本（个股 ≈ 0.116% / ETF ≈ 0.06% / 北交所 ≈ 0.175%） | 通过 `cost_model` 在 `simulate_day` 中扣减 |
| **严格止损** | 方向错就止损 | 单笔 T 最大单腿浮亏 ≤ `2·day_atr_pct` 或 ≤ T 期望收益的 −2× | 靠 `exit_manager.s_signal_exit + TRAIL` 共同保护；硬止损默认关 |
| **大盘风险谨慎** | 大盘不好 → 不做T | mpr60 大周期方向过滤（下跌趋势提高放行门槛） + 信号密度上限 | 系统支持；具体阈值由人工对账 |

**附加纪律（系统侧已有，截图未列）**：

- 持股期冷却：`COOLDOWN=120s`
- 每日信号上限：`MAX_B_DAILY=12` / `MAX_S_DAILY=12`
- 信号最小间隔：`SIGNAL_GAP=8` bar
- 单仓位模型：持仓中不重发 B（避免加仓摊薄）
- 影子模式独立开关：`SHADOW_V3_ENABLED=1; TPOINT_SHADOW_V3=1`

---

## 8. 评估指标体系（KPI — 七维度）

> 与 `positioning.md` 的 3 个核心竞争力（KPI-1/2/3）正交；此处为「方法体系层」的纯量化指标，是覆盖率门（§9）判定的依据。

| 维度 | 指标 | 公式 | 阈值（参考线） |
| --- | --- | --- | --- |
| 收益 | 总收益率 | `sum(ret)` | — |
| 收益 | 年化收益 | `sum(ret)·(240/bars)` | ≥ 8%（年化）合格 |
| 风险 | 最大回撤 | `max(drawdown)` | ≤ 20% |
| 风险调整 | Sharpe | `mean(ret)/std(ret)·√N` | ≥ 0.7 |
| 风险调整 | t1 Sharpe | 同上，按交易日聚合 | ≥ 1.0 |
| 胜率 | 净胜率 net WR | `wins/total`（after cost） | ≥ 50% 合格；≥ 55% 良好 |
| 胜率 | 毛胜率 gross WR | `wins/total`（before cost） | `gross − net ≤ 8pp`（成本侵蚀监控） |
| 盈亏 | 盈亏比 | `mean(win)/mean(loss)` | ≥ 1.2 |
| 密度 | 日均信号数 | `total/(days·n_sym)` | 0.5–2.0 / 日 / 标 |
| 成本 | 覆盖比 | `avg_win_pct / total_cost_pct` | ≥ 1.5 |
| 推送 | 丢推率 | `push_failed/total_signals` | `= 0`；延迟 < 5s |
| 纪律 | 无效信号率 | `signals_with_net<0 / total` | 单调下降 |
| 资源 | 单 call 耗时 / 峰值堆 | `tracemalloc + time.perf_counter` | 单 call < 0.5s / 峰值 < 200MB |

**七指标门（任一不达标即不灰度上线）**：

1. 收益 ↑
2. 回撤 ↓
3. 夏普 ↑
4. 胜率 ↑
5. 盈亏比 ↑
6. 信号量在合理密度区间
7. 全集（多标合并）不降

---

## 9. 覆盖率标准（Release Tiers）

| Tier | 名称 | 触发条件 | 覆盖率 / 时长 | 通过判定 |
| --- | --- | --- | --- | --- |
| **L0** | 单标的离线 | 算法变更后 | 单标最近 ≥ 20 完整交易日 | 七指标门 + 单标的胜率 ≥ 50% |
| **L1** | 全集离线 | L0 通过 | 全部标的 × 全历史 | 七指标门 + 全集 Sharpe ≥ 0.7 |
| **L2** | 单标的影子 | L1 通过 | 单标 ≥ 5 交易日并行落盘（不接推送） | `shadow_v3_*.jsonl` 行数 > 0，无异常抛错 |
| **L3** | 单标灰度 | L2 通过 + 用户拍板 | 单标 ≥ 20 交易日热重载（monitor 主路径用新版本，旧版本作 ref） | 七指标门 live 版满足 |
| **L4** | 全集灰度 | L3 通过 | 全部标的 ≥ 20 交易日 | 七指标门 + 推送可靠性 = 100% |
| **L5** | 全量上线 | L4 通过 + 周五评审 | 长期监控；per-symbol 异常可秒级回滚 | 由 commit + tag 标记 |

> Tier 升降级由本文 §10 评估参数统一驱动；任何异常立刻降级至 L0 重新走起。

---

## 10. 评估参数（Eval Config — 环境变量与默认值）

| 参数 | 默认 | 覆盖方式 |
| --- | --- | --- |
| 成本模型 | 万一 + 印花万5.641 + 滑点 2bps | `TP_COST_OVERRIDE` 环境变量 |
| 回测窗口 | 最近 20 个完整交易日 | `TP_COMPARE_LASTN` |
| 标的范围 | `config/monitor_config.json` 全集 | `TP_COMPARE_SYMBOLS=688111,603039,...` |
| 评分复利 | 0（不折现） | `TP_DISCOUNT_FACTOR` |
| 硬止损阈值 | 关 | `USE_STOP=1; STOP_ATR_MULT=1.5` |
| 时间止损 | 关 | `USE_TIME=1; TIME_STOP_BARS=90` |
| 移动止损 | 开 | `USE_TRAILING=0`（关） |
| 影子开关 | 关 | `SHADOW_V3_ENABLED=1; TPOINT_SHADOW_V3=1` |
| 推送目标群 | 全局群 `b4eba7a9-0504-4bd6-8aa3-a60fc8154103` | `FEISHU_TARGET_HOOK` |
| 心跳 | `data/heartbeat.jsonl` | — |
| 信号间隔 | `SIGNAL_GAP=8` bar | `SIGNAL_GAP` |
| 上限 | `MAX_B_DAILY=12` / `MAX_S_DAILY=12` | `MAX_B_DAILY` / `MAX_S_DAILY` |
| 冷却 | `COOLDOWN=120s` | `COOLDOWN` |
| 综合评分阈值 | `buy/sell=0.50`（灵敏 0.35 / 严控 0.55） | 改 `core/composite_scorer.py` `CompositeConfig` / `DEFAULT_CONFIG` |
| 综合评分权重 | `vwap1.2 / macd_div0.9 / rsi0.8 / vol_div0.7` | 同上（自动归一化） |

---

## 11. 与算法版本的对应表（对齐锚）

| 算法版本 | 本方法论版本 | 关键变化 |
| --- | --- | --- |
| v9.3.0 | **v1.0.0**（首次量化） | 神技#1 + 神技#3 单点法 + 量价背离默认关 |
| v9.4.0 | v1.0.0 | 结算闸门（T+1 每日 1 次往返；T+0 LOF 不限） |
| v10.0.0 | v1.0.0 | 新增量能确认（B 缩量回调 / S 平量）+ 强制 `MACD_GATE_MODE=floor` |
| v10.1.x | v1.0.0 | 标注层小迭代 |
| v10.2.0 | v1.0.0 | 神技#2 重启（`f_vol_price_div`）+ 神技#3 双点候选 + KDJ 协同指标 + shadow 旁路 |
| v10.3.0 | **v1.1.0** | 新增「综合评分模型」：三大神技 + RSI 连续加权融合（`detect_signals_v4` / `core/composite_scorer.py`），参数全可配 + 结构化输出 |

> **解耦原则**：方法论版本号 ≠ 算法版本号。  
> 任何一次方法论变更（`v1.x.y`）独立 bump；任何一次算法变更（`vA.B.C`）单独 bump；二者通过本表对齐。

---

## 12. 变更记录（Changelog）

### v1.1.0（2026-08-20）新增综合评分模型章节

| 类别 | 详情 |
| --- | --- |
| **背景** | 用户要求「基于三大核心策略设计可供上线评估的核心算法指标体系，整合三大策略信号与 RSI 超买超卖，构建综合评分模型并输出交易信号」 |
| **新增章节** | §4.5 综合评分模型（连续子评分 → 加权融合 → 阈值出信号）；含计算逻辑表、加权规则、可配参数表、结构化输出字段、回测结论 |
| **覆盖范围** | 与 tpoint v10.3.0（`core/composite_scorer.py` / `detect_signals_v4`）对齐；v2/v3/shadow_v3 不变 |
| **版本对应** | §11 增加 `v10.3.0 ↔ v1.1.0` 对齐行 |
| **回测结论（关键）** | 组件均衡无偏；默认趋势门控下 v4 S 主导（B/S≈4/112），因 exit_manager 仅正T 配对致 v4「胜率」不可代表真实质量；松弛 B 门控后方向平衡但 WR 反降，证门控在保护质量；密度随阈值 0.35→0.55 映射 3.39→1.59/百bar；**当前维持 v2 生产，v4 仅离线候选，需补 反T 回测** |
| **已标注缺口** | ① exit_manager 缺 反T(先卖后买) 配对，v4 的 S 侧无法公平回测；② 大样本/多标的回测未做；③ 综合分阈值/权重待 反T 回测后定稿 |
| **下一步** | 给 exit_manager 加 反T 配对（或 v4 专用 反T eval）→ 大样本回测 v4 B/S 双向 → 再决定 shadow 接入或回退 |

### v1.0.0（2026-08-20）初始化

| 类别 | 详情 |
| --- | --- |
| **背景** | 用户截图（散户专属做T秘籍·本质/手法/神技/纪律四大块）首次量化为系统化方法论 |
| **新增章节** | §0 元信息 / §1 版本号规则 / §2 本质 / §3 两种手法 / §4 三大神技（量化） / §5 信号漏斗 / §6 出场 / §7 仓位纪律 / §8 KPI / §9 覆盖率 / §10 评估参数 / §11 算法版本对应 / §12 变更记录 |
| **覆盖范围** | 与 tpoint v10.2.0 + v9/v2 + shadow_v3 完全对应 |
| **已标注缺口** | ① 神技#3 双点背离法未实现；② `min_hist_diff=0.15` 强背离阈值未上生产；③ 神技#2 在生产 `VOL_DIV_ENABLED=False`；④ EOD 盘中禁新仓未实现 |
| **下游引用** | `docs/t0_playbook.md` / `docs/parameter_inventory.md` / `docs/positioning.md` / `docs/v10_algorithm_intro.md` / `core/shadow_v3.py` / `core/exit_manager.py` / `core/indicators.py` / `core/miji_alpha.py` |
| **下一步** | 等待收盘 shadow_v3 证据 → 决定是否 bump 至 v1.1.0（新增第四条神技或第五纪律） |

---

## 13. 速查卡（One-Page Cheat Sheet）

```
┌────────────────────────────────────────────────────────────────┐
│                     做T方法体系 v1.1.0 速查                     │
├────────────────────────────────────────────────────────────────┤
│ 本质：底仓 + 日内多次高抛低吸（≥1 次），变相 T+0               │
│ 双模：正T（先买后卖）｜ 反T（先卖后买）                        │
│ 三神技：                                                      │
│   1. 分时均线引力    VWAP ± K·ATR（K=1.0/2.0）                │
│   2. 量价背离        价格新高(低)+量缩（0.7×）                │
│   3. MACD 背离       价极值+hist 收敛/放大（窗口 15）         │
│ 综合评分(v10.3.0)：  C_vwap+C_vol_div+C_macd_div+C_rsi 加权   │
│   → composite∈[-1,1]；≥+0.50出B / ≤-0.50出S；强度分三档        │
│ 四纪律：                                                      │
│   ① 单标 ≤1/3 仓位  ② 收益覆盖成本（≥0.116% 个股）           │
│   ③ 严格止损/TRAIL  ④ 大盘风险谨慎                            │
│ 七指标门：收益↑ 回撤↓ 夏普↑ 胜率↑ 盈亏比↑ 密度OK 全集不降     │
│ 覆盖率：L0 单离线 → L1 全离线 → L2 单影子 → L3 单灰度         │
│         → L4 全灰度 → L5 全量上线                             │
└────────────────────────────────────────────────────────────────┘
```
