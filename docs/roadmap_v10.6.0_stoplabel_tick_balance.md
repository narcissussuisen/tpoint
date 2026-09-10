# tpoint v10.6.0+ 路线图：B/S 失衡治理 + tick 数据接入 + 顶底捕捉增强

> **制定日期**：2026-08-26 ｜ **起点**：v10.5.0（R2P 全链路闭环版，2026-08-21 交付）
> **分支**：`feat/intraday-capture-v10.2.0` ｜ **引擎**：做T策略 v5 / GT-1.0
> **配套方法论**：`docs/methodology_framework.md` (v1.1.0)
> **本路线图来源**：用户 2026-08-26 复盘疑问（588170 B1/S8/X9 失衡 + 百花信号[止损]无法区分）+ 上轮对话整合
> **核心承诺**：直接可执行的开发路线图，每轮含目标 / 输入数据 / 验证指标 / 交付产出

---

## 0. 一句话定位

本路线图系统性解决 **三大问题**：
1. **止损信号语义坍缩** —— 当前 `monitor.py:985-993` 把 6 种 exit_reason 折叠为"止损"单标签，无法按优先级操作
2. **B/S 信号失衡** —— S/B 比 2.8:1（全监控）~ 8:1（588170.SH 单标的），B 侧4道门控 vs S 侧1道门控的结构性不对称
3. **顶底捕捉弱** —— `tick_cache/` 9 标的 × ~67日 ≈ 384 CSV 文件（每文件 ~3835 trades/日 = ~64 trades/min）**完全休眠**，从未被生产代码消费，导致微结构级顶底（毫秒级突破、主力单吸筹）漏掉

采用 **loop engineering P6 → P10 五阶段**迭代，每阶段独立可回滚，每阶段含量化专家 agent 评审 gate。

---

## 1. 止损信号分类与信号优先级对应规则（Section A）

### 1.1 当前 Bug：标签坍缩

`core/monitor.py:980-993` 的标签映射：

```python
# BUG: 把 STOP / TRAIL / TIME 三种性质完全不同的出场源折叠成同一个"止损"
if exit_reason in ('STOP', 'TRAIL', 'TIME'):
    op, color = '止损', 'blue'  # ← 6 种 exit_reason 折成 1 种标签
elif exit_reason == 'B':
    op, color = '买入', 'green'
else:  # S / FIXSTOP / EOD 全部归到这里
    if '空平' in (level_type or ''):
        op, color = '买入', 'green'
    else:
        op, color = '卖出', 'red'
```

**问题**：
- 用户在飞书看到 `[止损]` 标签，**无法区分**这是「风险兜底的 FIXSTOP」还是「浮盈保护的 TRAIL」还是「释放资金的 TIME」
- 6种 exit_reason 的操作含义截然不同，但 UI 完全相同 → 用户无法用优先级操作
- 用户截图"百花医药·止损 [TRAIL]"就是典型的"明明是锁利，却显示止损"——认知错配

### 1.2 完整 6 种 exit_reason 的语义矩阵

按 `core/exit_manager.py:172-228` 的出场顺序（已对齐实际生产路径）：

| 优先级 | exit_reason | 中文标签 | 颜色 | 操作含义 | 用户动作指引 |
|---|---|---|---|---|---|
| **P0** | `FIXSTOP` | **固定止损** | 🔴 red | 兜底断路器（lo 击穿 -1.5%） | **立即认错**，记一笔亏本次数 |
| **P1** | `STOP` | **破位止损** | 🟠 red | ATR 跌破 或 趋势翻空（line 184-196） | **立即认错**，下次入场更严 |
| **P2** | `S` | **信号平仓** | 🔵 blue | S 信号自然出场（line 201-204） | **正常**，按 S 信号价成交 |
| **P3** | `TRAIL` | **移动止盈** | 🟢 green | 浮盈 ≥0.4% 激活 → 回撤 0.6% 锁利 | **好信号**，记录 fav 幅度 |
| **P4** | `TIME` | **时间止损** | ⚪ gray | 持仓 90 根 bar 无进展（line 215-218） | 反思：是不是进早了 |
| **P5** | `EOD` | **收盘强平** | ⚪ gray | 14:55+ 日终兜底（line 222-227） | **不需要操作**，正常结算 |

### 1.3 新版标签映射（待 P6 实现）

```python
# core/monitor.py:980-993  替换为
EXIT_LABEL_MAP = {
    'FIXSTOP': ('固定止损', 'red'),       # 最高风险信号
    'STOP':    ('破位止损', 'orange'),     # 主动认错
    'S':       ('信号平仓', 'blue'),       # 自然出场
    'TRAIL':   ('移动止盈', 'green'),      # 浮盈保护成功
    'TIME':    ('时间止损', 'grey'),       # 反思信号
    'EOD':     ('收盘强平', 'grey'),       # 兜底
}
# B/EXIT_CFG_SHORT 回补走 "买入"
# 其他空平走 "卖出"
```

**验收**：用户在飞书看到不同 exit_reason → 显示不同标签 + 不同颜色 → 一眼分级。

---

## 2. 此前所有优化方向整合（Section B）

按优先级排序（P0 → P4），每项给出改动点 / 预期收益 / 风险 / 验收：

### 🔴 P0：止损模式切换 `stop_mode='atr'` → `'trend'`（最关键）

| 项 | 内容 |
|---|---|
| **改动** | `core/exit_manager.py:185-190`：`stop_mode='atr'` → `'trend'` |
| **依据** | 588170 当日 5 个 -0.4% 的 STOP 全为 ATR 噪音（"均值回归抄下影线策略的正确止损方式，不被正常下探洗掉"——`exit_manager.py:32-34` 注释原文） |
| **预期收益** | STOP 单笔数 ↓60-80%；当日 -0.4% 亏损消失 |
| **风险** | 低（v10.5.0 已同时开 FIXSTOP=1.5% 兜底） |
| **验收** | OOS 60日 → 正T net +15pp / WR +3pp |
| **依赖** | 无 |

### 🟡 P1：开启 `s_uptrend_guard=True`，平衡 B/S 不对称

| 项 | 内容 |
|---|---|
| **改动** | `core/general_signal.py:64`：`s_uptrend_guard: bool = False` → `True` |
| **依据** | B 侧 `b_downtrend_reversal=True`（line 58）已有4道门控，S 侧应**对称**——trend==1 时需"局部顶+超买反转"才放 S |
| **预期收益** | S/B 比 2.8:1 → 1.2:1；S 信号数 ↓35%；不丢真实顶部 |
| **风险** | 中（需 OOS 验证不能漏顶）。建议**灰度 1 周**（symbol 级 flag）→ 通过后才切全局 |
| **验收** | OOS 60日 → 反T net +20pp；正T 净不降 |
| **依赖** | 必须同时开 `regime_gate=True`（否则只压 S 不压 B 让失衡恶化） |

### 🟢 P2：调整 `buy_threshold / sell_threshold`

| 项 | 内容 |
|---|---|
| **改动选项** | 方案 A：`buy_threshold=0.40` / `sell_threshold=0.50`（保守）<br>方案 B：`buy_threshold=0.45` / `sell_threshold=0.55`（激进） |
| **依据** | 当前两边门槛对称（都0.45）但 B 多门控 S 少门控导致实际触发率失衡 |
| **预期收益** | B 信号数 ↑20-30%；S 信号数 ↓15-20% |
| **风险** | 中（可能多发 B 但质量下降）。**必须在 P1 同时开的条件下做** |
| **验收** | B/S 比回到 1.0-1.2:1；正T WR ≥45% |

### 🔵 P3：开启 `regime_gate=True`（已在 v10.5.0 默认开）

| 项 | 内容 |
|---|---|
| **改动** | `core/general_signal.py:83`：`regime_gate: bool = False` → `True`（OOS 已验证 +38.99pp） |
| **依据** | v10.5.0 已 OOS 验证 PASS（CHANGELOG §P4）；用户复盘 v10.5.0 启用此参数 |
| **预期收益** | 持续下行 regime B 被抑制（防接飞刀） |
| **风险** | **只压 B 不压 S** → 单独开启会让失衡恶化。**必须 P1 同步开** |
| **验收** | 已在 v10.5.0 PASS，无新增验收 |

### ⚪ P4：per-symbol 独立配置（精细化）

| 项 | 内容 |
|---|---|
| **改动** | `data/monitor_config.json` 增加 per-symbol 配置：<br>- 588170.SH（ETF）：`stop_mode='trend'`, `trail_activate_pct=0.3`, `trail_pct=0.5`<br>- 300759.SZ（创业板）：`trail_activate_pct=0.6`<br>- 600721.SH（沪主板活跃）：保持默认 |
| **依据** | 当前三只标的**纯池级默认**（memory 2026-08-26 改持仓时明确），但 ETF/创业板/主板的最优参数**结构性不同** |
| **预期收益** | ETF 噪音止损 -70%；创业板防过激 -20% |
| **风险** | 中（per-symbol 调参需两段式防过拟合） |
| **验收** | 每标的独立 OOS 40日 → 单标的 WR ≥45%；OOS 不降 |

---

## 3. Loop Engineering 迭代路径（Section C）

每阶段独立可回滚；每阶段含**目标 /输入数据 /验证指标 /交付产出 /评审 gate**。

### Phase P6 — 出场语义解耦（1 周，2026-08-26 → 2026-08-30）

| 维度 | 内容 |
|---|---|
| **目标** | 修复 `monitor.py:985-993` 的标签坍缩 bug；6 种 exit_reason 与 6 种标签 1:1 映射 |
| **输入数据** | `core/exit_manager.py:172-218`（6 个 exit_reason 源）；`core/feishu_alert.py`（卡片构造）；当前 `signal.txt` |
| **改动文件** | `core/monitor.py`（emit_card 中 980-993 行）<br>`core/feishu_alert.py`（配色字段扩展）<br>新增 `core/exit_label.py`（EXIT_LABEL_MAP 单一真源） |
| **验证指标** | ① 单元测试：`tests/test_exit_label.py` 覆盖全部 6 个 reason<br>② 实盘对照：当日 588170 9 个 X 应正确显示 [FIXSTOP]/[STOP]/[TRAIL]/[EOD]<br>③ 飞书卡片无错乱颜色 |
| **交付产出** | v10.6.0 release；commit `core/exit_label.py` 新建；`docs/tpoint_v10.6.0_release.md` |
| **评审 gate** | 量化专家 agent PASS：标签映射与 exit_reason 1:1，零行为变更（只改展示层） |
| **风险** | 极低（展示层改动不影响信号决策） |

### Phase P7 — B/S 平衡与顶底捕捉（2 周，2026-08-31 → 2026-09-13）

| 维度 | 内容 |
|---|---|
| **目标** | ① B/S 比 2.8:1 → 1.2:1<br>② 反 T 净 +20pp<br>③ 顶底捕捉率 ↑（短窗口反转命中率 ↑15pp） |
| **输入数据** | `core/general_signal.py:121-191`（B/S 触发函数）；`data/tick_cache/`（预热但 P8 才接入）；`monitor_config.json._global.general_algorithm` |
| **改动文件** | `core/general_signal.py:64`：`s_uptrend_guard=False` → `True`（灰度）<br>`core/general_signal.py:83`：`regime_gate` 验证已开<br>`core/general_signal.py:68-69`：阈值微调（方案 A 或 B）<br>`data/monitor_config.json`：per-symbol `stop_mode='trend'` |
| **验证指标** | ① 5 只 watchlist OOS 60日：S/B 比 1.0-1.4:1；正T WR +5pp；反T 净 +20pp<br>② 灰度 1 周（300759.SH 单标的先开）→ 全量<br>③ A/B 测试：开 vs 不开 OOS 对比 |
| **交付产出** | v10.7.0 release；`output/p7_oos_60d.html` 报告 |
| **评审 gate** | 量化专家 agent PASS + 用户确认（关键参数需用户拍板） |
| **风险** | 中（s_uptrend_guard 可能漏顶）。**必须先灰度单标的 5 日验证再全量** |

### Phase P8 — tick_cache / Level2 数据接入（2 周，2026-09-14 → 2026-09-27）

| 维度 | 内容 |
|---|---|
| **目标** | 把休眠的 `data/tick_cache/` 接入生产链路；上线 3 秒聚合 + 大单识别 + 买卖盘失衡特征 |
| **输入数据** | `data/tick_cache/<sym>_<YYYYMMDD>.csv`（9 标的 × ~67 日 ≈ 384 文件）<br>每文件：`time(only HH:MM), price, vol, buyorsell(0=买/1=卖), volume, date`（~3835 trades/日） |
| **改动文件** | 新建 `core/tick_loader.py`（CSV 读取 + 时间戳推断 + 校验）<br>新建 `core/tick_features.py`（聚合 3秒 bar + 大单检测 + 买卖失衡度 + iceberg 探测）<br>新建 `core/tick_aggregator.py`（3秒 → 1分钟合成一致性校验）<br>`core/datasource.py`（接入 tick_loader 作为次级源） |
| **验证指标** | ① 3秒聚合重放一致性 ≥95% vs 1m bar（重采样 1m 后与原 1m 偏差 ≤1bp）<br>② 单元测试：`tests/test_tick_aggregator.py`<br>③ 数据覆盖率：161129/513310/688347 三个核心标的 ≥60 日 |
| **交付产出** | v10.8.0 release；`core/tick_features.py` 新建；`data/tick_features/` 输出特征 parquet |
| **评审 gate** | 量化专家 agent PASS（数据一致性）+ 性能测试（聚合 67日 < 60s） |
| **风险** | 中（时间戳只有 HH:MM 精度——需先看是否可推断秒级） |

**tick 数据集成详细设计**（见 §4）。

### Phase P9 — 顶底捕捉增强 + ML 增强（2 周，2026-09-28 → 2026-10-11）

| 维度 | 内容 |
|---|---|
| **目标** | ① 高/低捕捉命中率 ≥35%（当前未知，估测 <15%）<br>② 反T 净收益 +60pp（v10.5.0 PASS 51.7% → 目标 60%+）<br>③ 加入 ML filter（xgboost 二分类：信号 → 高捕捉概率？低捕捉概率？） |
| **输入数据** | P8 的 tick 特征（3s trade density / large-tape / 买卖失衡）；DET 顶底标签库（high@N / low@N，N=5/15/60 分钟） |
| **改动文件** | 新建 `core/top_bottom_features.py`（DET 标签生成 + 顶底特征）<br>新建 `core/ml_top_bottom.py`（xgboost 二分类器，灰度开关 `ml_filter_enable`）<br>`core/general_signal.py`：信号后置 ML filter（可选） |
| **验证指标** | ① OOS 60日：top/bottom 命中率 ≥35%（DET 框架严格口径）<br>② 反T 净 ≥+60pp<br>③ ML 推理耗时 <10ms/bar（实时性能） |
| **交付产出** | v10.8.1 release；`core/ml_top_bottom.py` 新建；`output/p9_topbottom_oos.html` |
| **评审 gate** | 量化专家 agent PASS（DET 框架下严格验证）+ 实盘灰度 5 日 |
| **风险** | 高（ML 过拟合风险）。**必须在 OOS train/test split 严格 60/40 的条件下做，且不得在 train 集寻优后直接套 test** |

### Phase P10 — 全栈 OOS 验证 + v10.9.0 交付（1 周，2026-10-12 → 2026-10-18）

| 维度 | 内容 |
|---|---|
| **目标** | 全部 P6-P9 改动端到端 OOS 验证；v10.9.0 全栈交付；交付文档+监控体系完整 |
| **输入数据** | 全 watchlist（当前 3 只 + P9 灰度扩到 5 只）；F盘历史 tick_cache 全量；v10.5.0 baseline 对照 |
| **改动文件** | 仅交付文档 + 监控阈值：`docs/tpoint_v10.9.0_release.md`；`scripts/selfcheck_daily.py` 增项 |
| **验证指标** | ① 全栈 OOS 60日：正T 净 ≥-5%（v10.5.0 -15%），反T 净 ≥+60%<br>② 双向合计净 ≥+30%（首次全栈正收益）<br>③ 5 只 watchlist pass 率 ≥4/5<br>④ 监控指标全 PASS：signal out age ≤120s；心跳 ≤120s；推送成功率 ≥99% |
| **交付产出** | v10.9.0 release；全栈 OOS 报告；交付说明文档；CHANGELOG §P6-P10 |
| **评审 gate** | 量化专家 agent 全栈 PASS + 用户最终验收 |
| **风险** | 低（只验证不新改） |

---

## 4. tick_cache / Level2 数据集成方案（Section D）

### 4.1 数据现状

| 项 | 值 |
|---|---|
| **位置** | `data/tick_cache/` |
| **文件数** | 384 个（9 标的 × 30~67 日） |
| **覆盖标的** | 000001.SZ / 159985.SZ / 161129.SZ / 300750.SZ / 513310.SH / 600036.SH / 600519.SH / 603659.SH / 688347.SH |
| **每文件格式** | `time,price,vol,buyorsell,volume,date`（CSV，header） |
| **时间精度** | ⚠️ **只有 HH:MM（无秒）**——这是 P8 第一个要解决的问题 |
| **日均行数** | ~3835（000001.SZ 样本）≈ ~64 trades/min |
| **当前消费方** | **无**（完全休眠） |

### 4.2 P8 的三大工程任务

#### 任务 1：时间戳精度恢复
- **问题**：原始 CSV time 只有 HH:MM，无法做 3 秒聚合
- **方案 A**（推荐）：从盘中分钟 bar 推断秒级——每分钟内的 trade 按成交量 / 价格变化序列反推时序（精度 ±1s）
- **方案 B**（备选）：保持分钟级粒度，做 1min tick aggregation（每分钟 64 trades → 1 分钟特征），失去 3s 精度但工程简单
- **验收**：方案 A 重采样 1m 与原 1m bar 偏差 ≤1bp（与现 1m 数据对齐）

#### 任务 2：3 秒聚合（核心特征）
```python
# core/tick_aggregator.py 接口设计
def aggregate_3s(trades_df) -> pd.DataFrame:
    """3 秒粒度聚合：
    - open/high/low/close
    - volume / trade_count
    - buy_vol / sell_vol（按 buyorsell 区分）
    - vwap = sum(p*v)/sum(v)
    - large_tape_count（大单阈值：vol > 95% 分位）
    - buy_sell_imbalance = (buy_vol - sell_vol) / total_vol
    """
```

#### 任务 3：顶底特征工程
```python
# core/top_bottom_features.py
def detect_local_extremum(tick_features_3s, window_bars=20) -> pd.Series:
    """微结构级顶底标记：
    - iceberg_bid：连续 N 笔同价位大买单（吸筹）
    - iceberg_ask：连续 N 笔同价位大卖单（出货）
    - exhaustion_bar：放量反转（大阳/大阴 + 量缩跟随）
    - sweep_high/low：扫单后快速回撤
    """
```

### 4.3 与 B/S 信号的集成点

| 信号类型 | 当前数据 | P8 增强数据 |
|---|---|---|
| **B 信号**（正T/反T开仓） | 1m bar + composite_scorer | 1m bar + composite_scorer + **tick：大买单密度 + buy_sell_imbalance ↑** |
| **S 信号**（出场） | 1m bar | 1m bar + **tick：大卖单密度 + exhaustion_bar 检测** |
| **STOP 决策**（trail_mode） | 1m bar | 1m bar + **tick：连续大卖单触发更早止损** |
| **TRAIL 决策**（已激活） | 1m bar + max_fav | 1m bar + max_fav + **tick：获利了结大单 → 提前锁利** |

### 4.4 P8 风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| 时间戳精度恢复失败（方案 A 偏差大） | 中 | 降级方案 B（1min 聚合） |
| 3s 聚合计算量大、实时性能不达标 | 中 | 离线预计算特征 parquet + 实时查表 |
| 数据覆盖率不足（9 标的 67 日样本薄） | 高 | 仅对 161129/513310/688347（覆盖 ≥30 日）先开，其它延后 |
| 大单阈值在不同标的不一致 | 中 | per-symbol 自适应阈值（rolling 95% 分位） |

---

## 5. 顶底捕捉增强方案（Section E）

### 5.1 当前问题量化

v10.5.0 release 已知："长侧（正T）原始信号净仍为负（WR 37.6% / 净 -128.98% raw）"——**顶部未真正捕捉**的具体表现：

| 现象 | 数据点 |
|---|---|
| B 信号被快速打 STOP | 588170 当日 5/5 STOP 单笔 -0.4% |
| TRAIL 锁利过早 | 百花医药 13:55 EXIT [S] 持仓 +0.0%——本可拿到更高 |
| S 信号误判顶部 | 588170 当日 8 个 S 几乎全被快速打掉（顶部未到） |
| 顶/底点识别滞后 1m bar 级别 | 1m bar 粒度不够，毫秒级突破漏掉 |

### 5.2 顶底捕捉的两层定义

| 层 | 定义 | 验证方法 |
|---|---|---|
| **L1：粗粒度顶底** | N=15 分钟内 max/min（现有能力） | DET 框架已测：DA@60 B=69.5%/S=72.7%（不差，但收益仍负） |
| **L2：微结构顶底** | tick 级 5~20 秒窗口内 max/min + 大单反转 | 新增（依赖 tick_cache） |

### 5.3 P9 顶底特征工程

#### 标签生成（DET 严格口径）
```python
# core/top_bottom_features.py
def generate_extremum_labels(min1_bars, future_window_bars=[5,15,60]):
    """生成顶底标签：
    - is_local_top_N：未来 N 根 bar 内 c 为最大值
    - is_local_bot_N：未来 N 根 bar 内 c 为最小值
    - amplitude_N：未来 N 根 bar 内 max_ret / min_ret
    """
```

#### 特征工程（基于 P8 tick_features）
```python
TOP_BOTTOM_FEATURES = [
    'tick_density_3s_20s',     # 20s 内 trade 数密度
    'large_buy_count_60s',     # 60s 内大买单数量
    'large_sell_count_60s',    # 60s 内大卖单数量
    'buy_sell_imbalance_60s',  # 买卖失衡度
    'iceberg_bid_score',       # 吸筹分（连续大买）
    'iceberg_ask_score',       # 出货分（连续大卖）
    'exhaustion_bar_flag',     # 放量反转标记
    'sweep_high_flag',         # 扫单高点
    'sweep_low_flag',          # 扫单低点
]
```

#### ML 分类器
```python
# core/ml_top_bottom.py
class TopBottomFilter:
    """xgboost 二分类：信号 → 高/低捕捉概率
    - 仅用于反 T 出场时机优化（不用于正T开仓，避免过拟合）
    - 灰度开关：ml_topbottom_enable (per-symbol)
    """
```

### 5.4 验收标准

| 指标 | 当前 | P9 目标 |
|---|---|---|
| 高点捕捉命中率（N=15 min max） | ~15% | **≥35%** |
| 低点捕捉命中率 | ~25% | **≥45%** |
| 反 T 净收益（OOS 60 日） | +51.7% | **≥+60%** |
| ML 推理耗时 | N/A | **<10ms/bar** |

---

## 6. 版本与交付计划（Section F）

| 版本 | 交付日期 | 内容 | 评审 gate |
|---|---|---|---|
| **v10.6.0** | 2026-08-30 | P6：出场语义解耦（仅展示层） | 量化专家 agent 展示层 PASS |
| **v10.7.0** | 2026-09-13 | P7：B/S 平衡（s_uptrend_guard + regime_gate + per-symbol） | 用户拍板 + 量化专家 PASS + 5日实盘灰度 |
| **v10.8.0** | 2026-09-27 | P8：tick_cache 接入（仅数据层 + 离线特征） | 量化专家数据 PASS + 性能测试 |
| **v10.8.1** | 2026-10-11 | P9：顶底捕捉 + ML 增强（灰度） | 量化专家 OOS PASS + 5日实盘灰度 |
| **v10.9.0** | 2026-10-18 | P10：全栈 OOS 验证 + 端到端交付 | 量化专家全栈 PASS + 用户最终验收 |

### 6.1 关键依赖

```
P6 ──(展示层独立)─────────────┐
                              │
P7 ──(灰度单标的)──→ 全量──┐  │
                           │  │
P8 ──(数据层独立可并行)──┐  │  │
                        │  │  │
P9 ──(依赖 P8 特征)──────┤  │  │
                        │  │  │
P10 ─(全栈验证，依赖 P6-P9 全过)─┘──┘
```

**关键顺序**：P6 必须先于 P7；P8 可与 P7 并行；P9 必须在 P8 后；P10 必须最后。

### 6.2 风险缓解硬规则

| 规则 | 来源 |
|---|---|
| 配对口径 + 成对测 | memory 2026-08-21 "半个修复比不修更危险" |
| 热修复必须当场 commit + push | memory 08-05 教训（v10 重建清光未提交修复） |
| PID 写盘 / 数据写盘必须 try/except + 哨兵 | memory 2026-08-21 教训 |
| OOS 严格 train/test split (60/40) | v10.5.0 §P4 OOS 验证规则 |
| 灰度先单标的 → 全量 | P7 s_uptrend_guard 风险 |
| ML 推理耗时 ≤10ms/bar | 实时性能硬门槛 |

---

## 7. 立即可执行的第一步（Section G）

> **不需要等待**，今日（2026-08-26）收盘后即可启动 P6 的代码工作：

### Step 1（今晚）：P6 代码实现
```bash
# 1. 在 feature 分支开 P6
git checkout -b feat/p6-exit-label-decouple

# 2. 新建 EXIT_LABEL_MAP 单一真源
# 文件：core/exit_label.py
EXIT_LABEL_MAP = {
    'FIXSTOP': ('固定止损', 'red'),
    'STOP':    ('破位止损', 'orange'),
    'S':       ('信号平仓', 'blue'),
    'TRAIL':   ('移动止盈', 'green'),
    'TIME':    ('时间止损', 'grey'),
    'EOD':     ('收盘强平', 'grey'),
}

# 3. 修改 core/monitor.py:980-993 引用 EXIT_LABEL_MAP
from exit_label import EXIT_LABEL_MAP

# 4. 新增 tests/test_exit_label.py
def test_all_reasons_have_label():
    for reason in ['FIXSTOP','STOP','S','TRAIL','TIME','EOD']:
        assert reason in EXIT_LABEL_MAP
```

### Step 2（明天）：P6 验证 + push
```bash
# 1. 跑单测
C:/Users/YZP/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest tests/test_exit_label.py -v

# 2. 模拟推送一张测试卡
# （用今日 signal.txt 中的 588170 9 个 EXIT 数据）

# 3. commit + push
git add core/exit_label.py core/monitor.py tests/test_exit_label.py
git commit -m "P6: 解耦止损标签，6 种 exit_reason 1:1 映射到中文标签与颜色"
git push -u origin feat/p6-exit-label-decouple

# 4. VERSION bump + CHANGELOG
echo "10.6.0" > VERSION
```

### Step 3（本周内）：P6 量化专家评审 gate
- 加载 quant expert agent
- 提交 OOS 验证报告（仅展示层改动，无需 OOS）
- PASS 后进入 P7

---

## 8. 参考引用（All Code Anchors）

| 引用 | 文件 / 行号 | 用途 |
|---|---|---|
| 6 种 exit_reason 源 | `core/exit_manager.py:172-228` | P6 标签映射的真源 |
| FIXSTOP 默认值 | `core/exit_manager.py:38-44` | P6 / P0 改动点 |
| B/S 触发函数 | `core/general_signal.py:121-191` | P7 改动点 |
| s_uptrend_guard 默认值 | `core/general_signal.py:64` | P7 改动点 |
| regime_gate 默认值 | `core/general_signal.py:83` | P3 改动点 |
| 标签坍缩 bug | `core/monitor.py:980-993` | P6 修复点 |
| tick 数据源 | `data/tick_cache/<sym>_<YYYYMMDD>.csv` | P8 数据源 |
| v10.5.0 release 评审 gate | `docs/tpoint_v10.5.0_release.md` | P10 模板 |
| 方法论框架 | `docs/methodology_framework.md` (v1.1.0) | 评价口径 |
| iteration plan v2 | `docs/self_iteration_plan_v2.md` | 历史教训 |
| daily_iterate.py | `scripts/daily_iterate.py` | 自动迭代基础设施 |
| exit_v3 三条件止损 | `core/exit_v3.py` | 出场侧备选方案 |

---

> ⚠️ **本路线图来源声明**：基于用户 2026-08-26 复盘疑问（588170.SH B1/S8/X9 失衡 + 百花医药[止损]标签无法区分止损类型）+ 当日信号分析 + 代码级审计 + v10.5.0 release 评审 gate 模板。所有改动点均给出文件/行号；所有优化项均给出验收指标；所有风险均给出缓解方案。
>
> ⚠️ **Disclaimer**：本路线图为开发计划，最终参数（如 s_uptrend_guard 阈值、ML 特征窗口）须经量化专家 agent 评审 gate PASS 与实盘灰度验证后方可全量上线。