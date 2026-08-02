# tpoint 做T策略 — 现状参数清单（2026-08-01）

> 数据源：`core/miji_alpha.py`（生产引擎，floor 门控）、`core/exit_manager.py`（出场+成本）、`core/indicators.py`（v9 旧版，已被 miji 取代）。
> 生产环境强制 `MACD_GATE_MODE=floor`（`scripts/run_monitor.bat:9` 等）。
> 用途：与 GitHub 开源 T+0 策略对照 + ML 特征工程的参数基线。

---

## 1. 核心指标

| 指标 | 参数 | 当前值 | 计算公式 | 判定逻辑 |
|---|---|---|---|---|
| MACD | fast/slow/signal | 12/26/9 | DIF=EMA(C,12)−EMA(C,26)；DEA=EMA(DIF,9)；HIST=(DIF−DEA)×2 | 红柱缩短/绿柱收缩 + 金叉死叉 + DIF 拐头 |
| MACD 背离 | 窗口 w | LOCAL_W=15 | 价格创窗口新高/低 + MACD 动能衰减 | 卖: 价格新高 + (红柱缩短∨死叉∨DIF拐头)；买: 价格新低 + (绿柱收缩∨金叉∨DIF拐头) |
| 背离强度 | min_hist_diff | 0.0（生产全放行） | 买: hist[i]−前窗hist最低；卖: 前窗hist最高−hist[i] | ≥阈值才放行；0.15 两轮实证最优（8标的转正+7.77%），未接入生产 |
| VWAP 引力 | VWAP_DEV_BUY/SELL | 0.6/0.6 | dev_pct=(C−VWAP)/VWAP×100；带=VWAP±0.6×ATR | C≤下带→买因子+1；C≥上带→卖因子−1 |
| VWAP | — | 日内累计 | TP=(H+L+C)/3；VWAP=Σ(TP×V)/Σ(V) | 无成交量退化等权 cumsum |
| ATR | period | 14 (Wilder) | TR=max(H−L,｜H−C前｜,｜L−C前｜)；ATR 递推 | 引力带缩放×0.6、地板阈值、出场参考 |
| 趋势 | EMA fast/slow | 5/20 | 价格 vs EMA20 方向 + EMA20 斜率 | +1/-1/0；trend_strong 需连续 confirm_bars=8 根 |
| RSI | period | 14 | 温度子因子 | W_RSI=0.4 权重参与 temp |
| 温度 temp | 权重 | 0.4/0.2/0.2/0.2 | RSI+涨跌幅+量比+偏离加权 | 状态温度计 |
| 量价背离 | 开关 | VOL_DIV_ENABLED=False | 近10根均量/前10根均量 | 底背离放量≥1.2/顶背离缩量≤0.8；实证净负−1.49pp 已禁用 |
| 地板/天花板 | FLOOR_DEV_PCT | 1.5% | 创15窗口新低/高 + 偏离VWAP≥1.5% | 强趋势(trend_strong)日 ×1.5 → 2.25% |

## 2. 门控与共振

| 参数 | 当前值 | 说明 |
|---|---|---|
| MACD_GATE_MODE | floor（生产） | floor=strict+地板/天花板；strict=B需m_factor==1；off=纯引力 |
| RESONANCE_THRESHOLD | 2 | ≥2 因子同向（g/m 参与放行，v 仅计分——**死参数，放行只看 g/m**） |
| enable | (True,True,True) | 消融开关 (gravity, vol_div, macd_div) |
| vol_in_gate | False | vol 不参与门控放行 |
| SIGNAL_GAP | 8 | 信号最小间隔(bar) |
| MAX_B_DAILY / MAX_S_DAILY | 12/12 | 每日信号上限 |
| REV_CLOSE_BARS | 30 | 反T：S 开反T后 30bar 内 B 豁免趋势过滤 |
| allow_reverse | True | 反T开关 |

## 3. 出场管理（core/exit_manager.py，生产配置）

| 参数 | 当前值 | 说明 |
|---|---|---|
| use_stop / stop_atr_mult | False / 1.5 | 硬止损已关 |
| use_time / time_stop_bars | False / 90 | 时间止损已关 |
| use_trailing | True | 移动止损开 |
| trail_activate_pct | 0.4 | 浮盈≥0.4% 激活 |
| trail_pct | 0.6 | 从浮动高点回撤 0.6% 触发 |
| s_signal_exit | True | S 信号自然出场 |
| 出场优先级 | — | 硬止损 > S信号 > 移动止损 > 时间止损 > EOD 强平 |

## 4. 成本模型（2026-08-01 用户费率）

| 项目 | 个股 | ETF/LOF | 北交所 |
|---|---|---|---|
| 佣金 | 万一(0.01%) | 万一 | 千分之0.575 |
| 印花税 | 万5.641(卖) | 0 | 0 |
| 滑点 | 2bps/边 | 2bps/边 | 2bps/边 |
| 双边总成本 | ≈0.116% | ≈0.06% | ≈0.175% |

## 5. 与开源策略对照（横向摘要，详见 output/research/open_source_survey.md）

| 因子 | 开源主流取值 | tpoint 当前 | 差异点 |
|---|---|---|---|
| MACD 参数 | 12/26/9（T0T/CSDN/掘金一致） | 12/26/9 | ✅ 一致 |
| MACD 背离 | 双点比较（价格极值+MACD极值，T0T window=5；逻辑58 回溯上次金叉双点） | 单点动能衰竭（价格创新低+相邻hist收缩） | ⚠️ tpoint 是简化单点法，开源多为双点背离 |
| 背离强度过滤 | T0T 要求 MACD+KDJ 双背离同现 | min_hist_diff=0.0（全放行） | ⚠️ 建议 0.15~0.5 |
| RSI | 14, 超卖30/超买70（CSDN） | 14（仅温度子因子，无独立阈值） | ⚠️ tpoint 无 RSI 独立门控 |
| KDJ | 9/3/3 金叉死叉+背离（T0T） | 无 | ❌ tpoint 无 KDJ |
| ATR | 14, 网格间距3~5倍（T0GridTrader） | 14, 引力带0.6倍 | ⚠️ 用法不同 |
| 布林带 | 20, 上下轨触发（T0GridTrader） | 无 | ❌ tpoint 无布林 |
| 量能 | 量比1.5放量/0.8缩量（CSDN） | 1.2/0.8（已禁用） | ⚠️ 已禁用 |
| 动态阈值 | 偏离度×(1+离散系数)（掘金） | 固定 0.6/1.5 | ⚠️ 无波动率自适应 |
| 网格 | ATR×3~5 间距 10档（T0GridTrader） | 无 | ❌ tpoint 非网格 |
| 多周期共振 | 月/周/日/分钟（T0T）；5/15/30/60 | 仅分钟级+日级指数门控 | ⚠️ 无分钟多周期 |
| 尾盘风控 | 14:30 禁新仓/尾盘强平（T0GridTrader等） | EOD 强平（无盘中禁新仓） | ⚠️ 部分缺失 |
| 止损 | T0T 有止损+修正；网格无止损 | 仅移动止损 act0.4/trail0.6 | ⚠️ 无硬止损 |

**对 tpoint 最有价值的 5 个借鉴点**（后续 ML 特征工程将重点验证）：
1. MACD 背离强度过滤（min_hist_diff 0.0→0.15~0.5）— 消融已自证弱背离负 alpha
2. MACD+KDJ 双背离共振 — 可作新特征（KDJ 9/3/3）
3. 离散系数动态阈值 — 偏离度×(1+波动率)，高波动日防过度交易
4. B 信号叠加缩量确认 — 回调+缩量=抛压衰竭
5. 尾盘风控 — 14:30 后禁新仓（信号质量时段特征）
