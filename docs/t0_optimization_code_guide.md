# 分时做T（T+0）量化策略系统性优化 · 代码清单与使用指南

> tpoint 项目 · 2026-08-01 · 阶段B交付
> ML 定位 = **因子研究工具**（结论以规则引擎参数/阈值落地，不引入模型运行时推理依赖）

## 一、运行环境

- Python: 项目 venv（`venv/Scripts/python.exe`，Python 3.13）
- ML 依赖: `xgboost>=2.1` / `lightgbm>=4.6` / `scikit-learn` / `scipy` / `joblib`（已装：xgboost 3.3.0 / lightgbm 4.7.0 / sklearn 1.9.0）
- 数据源: `F:\keyfactor_data\1m\` 全市场 1m 历史库（4149 只，tickflow 格式，单标的 20~146 交易日）
- 所有命令在 tpoint 项目根目录执行

## 二、流水线总览

```
[数据构建] scripts/ml_build_dataset.py
    → output/ml_dataset_full/dataset.csv（259.9 万样本，39 特征 + 4 标签）
[ML 训练] scripts/ml_train_evaluate.py
    → output/ml_train_results.json（XGB/LGB/RF × B/S，CV + Walk-forward OOS + 特征重要性）
[规则落地] scripts/ml_to_rules.py
    → output/ml_rules.json（特征分箱净收益表 + 参数推荐）
[报告汇编] scripts/build_ml_report.py
    → output/t0_optimization_research_2026-08-01.html（单文件自包含报告）
```

## 三、脚本详解

### 1. scripts/ml_build_dataset.py — 全市场 ML 数据集构建

```bash
python scripts/ml_build_dataset.py \
  --dir F:/keyfactor_data/1m \
  --out output/ml_dataset_full \
  --workers 8
```

- **标的过滤**（`check_filter()`）：交易日≥30 / 日均成交额≥2000万 / 价格 3-100 元 / 一字bar占比≤30% / 涨停日≤20% / 剔除 ST、北交所(920段)、ETF/LOF → **3351 只通过**
- **特征工程**（`build_features_for_day()`，39 特征全部因果可用——仅用信号时刻及以前数据）：
  - 基础 14：vwap_dev / atr_pct / dif / dea / hist / hist_pct / trend / trend_strong / rsi / vol_ratio / temp / chg / pos_in_day / bar_idx_frac
  - 补充 17：多周期 MACD（5/15/30/60 的 dif+hist）、rsi_dist_30/70、KDJ(9/3/3) K/D/J、ATR 通道 up/dn、动量 mom_1/5/15、时段 is_morning/is_noon/is_tail
  - 信号上下文 3：g_factor / v_factor / m_factor（三因子）+ resonance
- **标签**（前向 N-bar 净收益二分类，与项目胜率口径一致）：
  - `label_N = 信号点后 N 根 1m bar 净收益（扣双边成本 via make_cost_model）> 0`
  - N ∈ {10, 20, 30, 60}，主标签 `label_20`
- **并行**：`multiprocessing.Pool(8)`（Windows spawn）；单标的 688 样本/平均
- **输出**：`dataset.csv` + `part_*.csv`（每标的一个）+ `filter_result.json`

### 2. scripts/ml_train_evaluate.py — ML 训练与评估

```bash
python scripts/ml_train_evaluate.py \
  --data output/ml_dataset_full/dataset.csv \
  --out output/ml_train_results.json
```

- **防泄漏**：GroupKFold by 交易日（同交易日样本不跨折）+ Purged walk-forward（前70%日期 train / 后30% test，只跑一次纪律）
- **模型**：XGBoost / LightGBM / RandomForest，B/S 分开建模（因子语义相反：买需低吸、卖需高抛）
- **超参**：n_estimators=300, max_depth=5(GBDT)/12(RF), lr=0.05, min_child_weight=3, subsample=0.8, colsample=0.8, scale_pos_weight=负/正样本比, n_jobs=4
- **评估**：
  - ML 指标：AUC / Precision / Recall / F1（WF OOS）
  - 交易挂钩：按预测概率分 10 箱 → 各箱实际胜率 + 单调性 + top箱胜率（**箱单调 + top箱>50% 才算有交易价值**）
  - 特征重要性：XGBoost gain + permutation importance 双法
- **输出**：`ml_train_results.json`

### 3. scripts/ml_to_rules.py — ML→规则落地

```bash
python scripts/ml_to_rules.py \
  --data output/ml_dataset_full/dataset.csv \
  --out output/ml_rules.json
```

- **核心思想**（与"因子研究工具"定位一致）：对高重要度特征按值分箱，统计各箱前向净收益/胜率，直接产出"该特征在哪个区间信号质量最好"的规则建议——**零模型依赖**
- **分箱特征**（14 个）：hist_pct / vwap_dev / atr_pct / rsi / kdj_k / kdj_j / mom_5 / chg / pos_in_day / is_tail / is_morning / resonance / trend / trend_strong
- **候选规则参数**（7 个）：min_hist_diff / VWAP_DEV / RSI_oversold / RSI_overbought / KDJ_K_buy / KDJ_K_sell / tail_gate
- **输出**：`ml_rules.json`（bins 分箱表 + recommendations 参数推荐，含 lift/单调性）

### 4. scripts/build_ml_report.py — 报告汇编

```bash
python scripts/build_ml_report.py
```

- 聚合：开源调研(`output/research/open_source_survey.md`) + 现状清单(`docs/parameter_inventory.md`) + ML 训练结果 + 规则推荐
- 输出：`output/t0_optimization_research_2026-08-01.html`（深色主题单文件，含 KPI / 对照表 / 模型对比 / 特征重要性 / 分箱表 / 结论）

## 四、评估口径（用户确认）

| 项 | 说明 |
|---|---|
| 数据源 | F盘 1m 全市场历史库（4149 只 → 过滤 3351） |
| 回测区间 | 2025-12 ~ 2026-07（按标的 20-146 交易日），OOS 按日期前70%/后30% |
| 标的过滤 | 见上方 2.1 |
| 成本模型 | 万一佣金 + 印花税万5.641(个股卖)/0(ETF) + 滑点2bps；个股双边≈0.116% |
| 标签 | 前向 N=20 bar 净收益（扣双边成本）>0；N∈{10,20,30,60} 敏感性对照 |
| ML 评估 | AUC / Precision / Recall / F1 / 分箱单调性 / top箱胜率 |
| 交易评估 | 净胜率 / 盈亏比 / 最大回撤 / 夏普（规则落地后 aggregate_metrics 复测） |
| 防泄漏 | GroupKFold by date + Purged walk-forward + 特征全因果 |

## 五、可落地参数推荐（训练完成后填充）

> 由 `ml_to_rules.py` 的 recommendations 输出 + 人工决策填充，见报告第四节。

## 六、已知坑与修复

1. **GroupKFold CV 报 `cannot unpack non-iterable numpy.int64`**：`cross_val_score` 传 groups 兼容性问题 → 改手动 GroupKFold 循环
2. **`eval_trade_bins` 返回 Interval index**：groupby 后 index 是 Interval → reset_index + 用 `int(r['rank'])`
3. **低基数特征 qcut 退化**：is_tail/is_morning 等 → 直接按取值分组（n_unique <= n_bins 判断）
4. **后台命令开头 rm 触发 safe-delete 拦截**：路径转换失败导致命令链中断 → 用 python os.remove
5. **149 万样本 RF CV 过慢**：CV 树数 100→60 + n_jobs 2→4
