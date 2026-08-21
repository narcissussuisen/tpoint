# tpoint v10.5.0 交付说明（R2P 全链路闭环版）

> 交付日期：2026-08-21 ｜ 分支：`feat/intraday-capture-v10.2.0` ｜ 引擎：做T策略 v5 / GT-1.0
> 配套方法论：`docs/methodology_framework.md` (METHODOLOGY_VERSION=1.1.0) ｜ 评判标准：`tpoint_signal_validity_criteria_v1.md` (DET Framework)

## 0. 一句话定位
v10.5.0 是「Research-to-Prod gap-closing」6 阶段路线图的**固化交付版**：以 DET 框架确认信号质量系统性成立为地基，逐阶段补齐噪声门控、出场盈亏比、双向反T、regime 门控，并经量化专家 agent 逐阶段评审 gate（PASS）后固化。每个阶段独立可回滚，整条路线图已闭环。

## 1. 路线图与阶段验收（loop engineering）
| Phase | 动作 | 评审 gate | 结论 |
|---|---|---|---|
| P0 地基 | DET 框架 + v5 baseline 测量 | — | 信号质量系统性成立（DA@60 69.5/72.7%，TEP 100% 正） |
| P1.1 | signal_gap 6→8 | DET 重测 | 冗余↓，质量持平 |
| P1.2 | δ=0.5% 主判据 | 评判口径 | 零风险 |
| P1.3 | 量比门控复核 | DET 三重证据 | 证伪旧收益 → 移除 |
| P2 出场盈亏比 | FIXSTOP=1.5% + EOD 强平 + exit_v3 | 量化专家 PASS | 尾端封 -1.5%，EOD 闭环 |
| P3 配对/反T | bidirectional + side-aware 出场 | 量化专家 PASS | 反T 净 +51.7% ≥0 |
| P4 regime 门控 | 持续下行抑制 B + OOS | 量化专家 PASS | OOS 净 +38.99pp |
| P5 固化 | 统一提交 + 监控 + 本说明 | — | 本版交付 |

## 2. 架构
- **引擎**：`core/general_signal.py` —— symbol-agnostic 连续评分（比率口径：tanh(VWAP偏离/ATR)、RSI 中性映射、MACD 柱收敛、量比缩量），全 A 股 1m 通用，无逐标的硬编码。
- **实时入口**：`core/monitor.py::detect_for` 通过 `USE_GENERAL_ENGINE` 热插拔调用 `check_general_b_trigger/check_general_s_trigger`，异常回退 miji。
- **出场**：`core/exit_manager.py`（`make_config` 可配：硬止损/趋势止损/移动止损/时间止损/S信号自然出场/FIXSTOP）；反T 镜像于 `core/simulate_bidirectional.py`。
- **配置**：`data/monitor_config.json`（`_global.general_algorithm` 驱动 GeneralConfig，热重载；`_global.bidirectional_enable` / `regime_gate` 门控）。
- **监控**：`scripts/selfcheck_daily.py` 周期自检（误报消解已迭代）；`run()` 15:00 EOD 强平兜底。

## 3. 本版关键配置（monitor_config.json._global）
```json
"use_general_engine": true,
"bidirectional_enable": true,            // P3 反T层启用
"v4_gray_enable": true,                  // v4 仅影子对照，不干预
"general_algorithm": {
  "strategy_version": "v5", "engine": "GT-1.0",
  "buy_threshold": 0.45, "sell_threshold": 0.45,
  "signal_gap": 8,                        // P1.1 降冗余
  "b_downtrend_reversal": true,           // 防接飞刀
  "s_uptrend_guard": false,               // S 全 regime 放行
  "vol_ratio_b_max": null,                // P1.3 已移除量比门控
  "regime_gate": true,                    // P4 启用
  "regime_lookback": 40, "regime_downtrend_suppress": true, "regime_downtrend_thresh": 0.5
}
```
出场：`EXIT_CFG`(正T PROD: trail0.4/0.6 + FIXSTOP1.5) / `EXIT_CFG_SHORT`(反T DEFAULT: 硬止损atr1.5+时间90+trail0.4/0.6, FIXSTOP off)。

## 4. 量化验收证据
- **信号质量（DET, 192标的/6024日/90323信号）**：B-DA@60 69.5% / S-DA@60 72.7%；TEP pos_rate 100%（每笔理论净值全正）；B-EHR@0.5% 68.7% / S-EHR@0.5% 63.5%。→ 找极端顶底能力系统性成立，旧「WR 低=信号错」为出场损耗污染假象。
- **反T（P3, faithful 双向解耦, 161129+513310 各79天）**：反T 池级 n=594 WR=37.4% **净 +51.7%**（≥0 PASS）；双向合计净 -77.28% ≥ 正T -128.98%（增量正贡献 PASS）。
- **regime 门控（P4, 时间切 train60%/test40%）**：正T PROD 出场 net —— TRAIN -66.49%→-17.34%(+49.15pp)，TEST(OOS) -54.06%→-15.07%(+38.99pp)；参数固定未寻优、test 方向一致 → 启用。

## 5. 实盘部署要点
1. **FIXSTOP=1.5%**：EV 中性尾端断路器，封尾部暴跌单笔最差 ~-1.5%（不提升每笔期望，仅封尾）。
2. **EOD 强平**：`run()` 检测 wall-clock≥15:00 触发收盘强平，受涨跌停 `can_sell` 门控（真实推送 EXIT 信号），闭环评审条件#1。
3. **反T 需底仓**：`bidirectional_enable=true` 下 S→B 配对；A 股 T+1 实盘须有底仓配合，无底仓时反T 仅作信号质量观测。
4. **regime 门控**：持续下行 regime 自动抑制 B（防接飞刀）；仅减 B 不增 B，fail-open，S 不动。
5. **监控**：`selfcheck_daily.py` 周期自检；飞书日报 15:40 标准化。

## 6. 已知限制 / 上线硬门槛
- **【硬门槛 B5】入账价对账**：对账/recalc 须用「信号 bar close 价」而非 `signal.txt` 推送价（历史差距可达 2.6%）。monitor 实时 `pos['entry_price']` 已取 `c[i]`(bar close) 正确；缺口在**对账工具读取 signal.txt 推送价**处 → 须改对账入口按信号时间戳锚定 bar close。**上线前必做**。
- 个股泛化：P4 OOS 仅 161129/513310（各79天）入池，4 只个股样本<40天未入 OOS，待扩样本复现。
- 长侧（正T）原始信号净仍为负（WR 37.6% / 净 -128.98% raw）：v5 原始信号既有问题，regime 门控已止血至 OOS -15%，长侧 alpha 待 R2P 后续（因子 OOS / 买点确认升级）。
- 因子 OOS 调参：因过拟合风险延后（信号已 DET 验证成立，非强制）。

## 7. 回滚 / 灰度
- 每阶段独立 commit（d1ec1ae P2.2 / ab88137 P3 / 81a9c8c P4），可单阶段 `git revert` 回滚。
- 实时门控（`bidirectional_enable` / `regime_gate` / `vol_ratio_b_max`）均热重载，无需重启即可灰度开关。
- 建议：先在灰度标的观察反T + regime 门控实盘信号质量（T1 全量推送、不设单次往返限制），稳定后再扩大。

## 8. 提交索引（feat/intraday-capture-v10.2.0）
- `d1ec1ae` P2.2 FIXSTOP + EOD 强平
- `ab88137` P3 反T层 + side-aware 出场 + None 守卫
- `81a9c8c` P4 regime 门控 + OOS 验证
- `v10.5.0` 本版：统一提交 P0–P4 + VERSION/CHANGELOG/本说明（详见 `git log`）
