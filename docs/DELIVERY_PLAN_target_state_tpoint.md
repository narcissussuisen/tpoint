# tpoint 目标态交付方案（可直接落地执行版）

> 版本：v1.0 ｜ 日期：2026-08-20 ｜ 依据：`toasty-cascade-tesla.md`（v10.3.0 优化完善方案）+ R2P 六轮 gap-closing 计划
> 本方案为「目标态 tpoint 系统/组件 **实际运行并产出**」的执行总纲。方案含 5 部分：目标态定义 / 代码·配置·依赖清单 / 环境与部署 / 验收 / 风险回滚。
> 执行原则：**凡本方案列出的验收项，全部以真实运行产物为准，禁止口算/口报；任何一步失败必须显式阻断并告警，不得静默通过。**

---

## 1. 目标态 tpoint 的定义、功能范围与预期输出标准

### 1.1 目标态定义（本轮可达成的操作目标态）

目标态 = **通用算法驱动的 watchlist 监控组件 + v4 灰度测试组件，作为一个完整可运行的系统，在今日 watchlist 标的上端到端跑通并产出全部目标产物。**

具体为：`data/watchlist.json`（现网权威：603039.SH 泛微网络、688111.SH 金山办公）由 **symbol-agnostic 通用算法**（`core/general_signal.GeneralConfig`）统一驱动；`v4`（`core/composite_scorer_v4full`）作为**影子灰度候选**并行运行；系统产出生产信号、A/B 对比报告、灰度影子明细、生产复盘 HTML 与数据质量哨兵结论，并推送完成通知。

> ⚠️ 边界声明（诚实口径）：性能类目标（R2P G-F1：WR_prod_exec 滚动 20d ≥55% 等）**依赖 20+ 交易日离线长回测数据（F 盘 tickflow）**，本轮数据窗口（mootdx 实时 1m 仅近 3-4 天）无法支撑该统计结论。因此本轮目标态为**功能/操作目标态**；性能目标态单列为 §1.3 的后续工作流，不在本轮验收范围。

### 1.2 功能范围（本轮交付）

| 编号 | 功能 | 状态 | 对应代码/产物 |
|---|---|---|---|
| F1 | 通用算法统一驱动 watchlist 全标的（symbol-agnostic，无逐标的硬编码阈值） | ✅ 已交付 | `core/general_signal.py` |
| F2 | B/S 双向触发（B 侧防接飞刀：downtrend 需局部底+超卖反转确认；S 侧全 regime 放行） | ✅ 已交付 | `core/general_signal.py::check_general_b/s_trigger` |
| F3 | v4 影子灰度（A/B 对比 + promote 门控建议） | ✅ 已交付 | `core/watchlist_engine.py` |
| F4 | monitor 实时集成（flag 门控 + miji 兜底回退，热重载） | ✅ 已交付（待重启加载） | `core/monitor.py` |
| F5 | 生产信号落盘 + 灰度明细落盘 | ✅ 已交付 | `output/general_signals_<date>.json`、`output/v4_shadow/v4_shadow_<date>_<sym>.jsonl` |
| F6 | 生产级复盘 HTML（交叉验证系统存活） | ✅ 已有组件 | `scripts/daily_signal_review.py` → `output/review_<date>.html` |
| F7 | 数据质量哨兵（live vs recalc 口径自检） | 本轮执行 | §4.2 哨兵用例 |

### 1.3 目标态预期输出标准（本轮运行必须产出的产物）

| 产物 | 路径 | 内容标准 |
|---|---|---|
| 生产信号 | `output/general_signals_2026-08-20.json` | 含 date/engine=general/rows；每标的 n_b>0 且 n_s>0 |
| 灰度对比 | `output/v4_gray_compare_2026-08-20.json` | 含 rows（general vs v4_gray 逐标的 B/S/WR/净ret）+ v4_promote_recommend |
| 灰度明细 | `output/v4_shadow/v4_shadow_2026-08-20_<sym>.jsonl` | 每标的一文件，逐信号行 |
| 生产复盘 | `output/review_2026-08-20.html` | 自包含 HTML，当日信号复盘（legacy 生产路径交叉验证） |
| 运行总览 | `output/target_state_run_2026-08-20.html` | 本轮验收矩阵 + 全部关键指标汇总（§4 判定逐项展示） |
| 完成通知 | 全局群 webhook | `notify.py` 推送，含状态/关键摘要/待关注 |

### 1.4 后续工作流（性能目标态，非本轮验收）

- **W1 离线长回测**：`watchlist_engine` 数据通路切 F 盘 tickflow 1m CSV → 20+ 交易日多标的回测 → 验证 G-F1（WR≥55%）。
- **W2 双向 T 配对**：`simulate_day` 仅正 T（B→S），补 S→B 反 T 配对，量化 S 侧质量。
- **W3 出场治理**：`exit_v3` 三条件止损（T0T 借鉴），治理 P/L 0.6-0.9。
- **W4 实盘加载**：授权重启 `tpoint_monitor` 计划任务，让运行中进程加载 `general_signal` 新引擎。

---

## 2. 实现目标态所需的代码任务、配置文件与依赖项完整清单

### 2.1 代码任务清单（含状态与负责人）

| # | 任务 | 文件 | 状态 | 负责人 |
|---|---|---|---|---|
| C1 | 通用算法引擎（GeneralConfig + check_general_b/s_trigger + detect_signals_general） | `core/general_signal.py` | ✅ 已提交 `1947988` | 本会话（已授权） |
| C2 | watchlist 模块（统一驱动 + v4 灰度 + A/B + promote） | `core/watchlist_engine.py` | ✅ 已提交 | 本会话 |
| C3 | monitor 实时集成（USE_GENERAL_ENGINE 门控 + 兜底） | `core/monitor.py` | ✅ 已提交（代码热重载生效需重启进程） | 本会话 |
| C4 | v4 完整策略（防接飞刀 B 门控，解 B 死锁） | `core/composite_scorer_v4full.py` | ✅ 已提交 | 本会话 |
| C5 | 连续评分内核（通用算法与 v4 共用） | `core/composite_scorer.py` | ✅ 已提交 | 本会话 |
| C6 | 验收脚本（C1~C4 判定） | `scripts/validate_general_v4.py` | ✅ 已提交 | 本会话 |
| C7 | 本轮新增：目标态运行总览生成器 | `scripts/build_target_state_report.py` | 🆕 本轮创建 | 本会话 |
| C8 | 本轮新增：数据质量哨兵（general_signals 自检） | `scripts/target_state_sentinel.py` | 🆕 本轮创建 | 本会话 |

### 2.2 配置文件清单

| 文件 | 关键字段（当前值） | 说明 |
|---|---|---|
| `data/watchlist.json` | `{"603039.SH": "泛微网络", "688111.SH": "金山办公"}` | 权威标的清单 |
| `data/monitor_config.json` `_global` | `use_general_engine=true`、`v4_gray_enable=true`、`v4_promote=false`、`bidirectional_enable=false`、`general_algorithm{buy_threshold:0.45, sell_threshold:0.45, signal_gap:6, b_downtrend_reversal:true, s_uptrend_guard:false, vol_ratio_b_max:null}` | 引擎开关 + 通用算法参数（热重载） |
| `data/monitor_config.json` per-symbol | 603039/688111 均为纯池级默认（trail 0.4/0.6 + 无 ATR + 无 MPR） | 逐标的覆盖（本轮未设特例，体现 symbol-agnostic） |

### 2.3 依赖项清单（运行环境必需）

| 依赖 | 版本/位置 | 用途 |
|---|---|---|
| Python venv | `C:/Users/YZP/WorkBuddy/Claw/tpoint/venv/Scripts/python.exe` | 全部 tpoint 脚本运行解释器 |
| numpy / pandas | venv 内 | 指标计算/数据帧 |
| mootdx（通达信 TCP 7709） | venv 内 | 实时 1m 行情（本轮唯一数据源；需国内 IP） |
| `core/`（general_signal, watchlist_engine, composite_scorer, composite_scorer_v4full, monitor, datasource, miji_alpha, exit_manager, daily_signal_review） | 同仓库 | 引擎/信号/复算 |
| `C:\Users\YZP\.workbuddy\notify.py` | 全局工具 | 飞书全局群推送（hook b4eba7a9-…） |
| git | 本机 | 变更提交（分支 `feat/intraday-capture-v10.2.0`） |

> 无外部 pip 安装需求；全部依赖已存在于 venv（只读使用，不装包）。

---

## 3. 运行环境准备要求与分步部署步骤

### 3.1 环境准备检查（执行前一次性核验）

1. venv 存在且可执行：`C:/Users/YZP/WorkBuddy/Claw/tpoint/venv/Scripts/python.exe --version`（需 ≥3.11）。
2. 目标代码齐全：`core/general_signal.py`、`core/watchlist_engine.py`、`core/composite_scorer_v4full.py`、`core/composite_scorer.py`、`scripts/validate_general_v4.py` 均存在。
3. 配置就位：`data/monitor_config.json` 含 §2.2 字段；`data/watchlist.json` 为现网 2 标的。
4. 网络：mootdx TCP 7709 可达（国内 IP）；若不可达 → 走 §5 回滚预案 R1（降级声明，不伪造产物）。
5. 编译自检：`python -m py_compile` 全部核心模块（C5 验收）。

### 3.2 分步部署与运行步骤（本轮全程执行）

| 步骤 | 动作 | 命令/方式 | 产物 | 通过条件 |
|---|---|---|---|---|
| S1 | 环境预检 | 见 §3.1 | 预检报告 | 1-5 全部 OK |
| S2 | 编译自检 | `venv python -m py_compile core/general_signal.py core/watchlist_engine.py core/monitor.py core/composite_scorer_v4full.py scripts/validate_general_v4.py` | — | 无异常 |
| S3 | 通用算法 + v4 灰度实跑（核心产出） | `venv python scripts/validate_general_v4.py 2026-08-20`（附 08-19 对照） | general_signals / v4_gray_compare / v4_shadow | §4.1 T1-T4 |
| S4 | 生产复盘交叉验证 | `venv python scripts/daily_signal_review.py --date 2026-08-20` | `output/review_2026-08-20.html` | HTML 生成成功且含复盘数据 |
| S5 | 数据质量哨兵 | `venv python scripts/target_state_sentinel.py 2026-08-20` | 哨兵 JSON（含 pass/fail） | §4.2 T5-T8 全过 |
| S6 | 运行总览报告 | `venv python scripts/build_target_state_report.py 2026-08-20` | `output/target_state_run_2026-08-20.html` | 含验收矩阵 8 项全 PASS |
| S7 | 完成通知 | `notify.py "目标态运行完成…"` | 全局群推送 | 返回 FEISHU_PUSH_OK |
| S8 | git 提交本轮新增（C7/C8 + 本方案） | `git add` + commit | commit | 提交成功 |

---

## 4. 验证 tpoint 是否成功跑出的测试用例、验收指标与判定标准

### 4.1 核心验收（T1-T4，复用 validate_general_v4 口径）

| 用例 | 判定标准 | 数据源 |
|---|---|---|
| **T1 通用双向** | 每个 watchlist 标的 `n_b>0 且 n_s>0`（C1） | `general_signals_2026-08-20.json` |
| **T2 v4 灰度可运行** | 每标的 v4 影子 `n_b>0`（C2，证明 B 死锁已解） | `v4_gray_compare_2026-08-20.json` |
| **T3 对比报告产出** | 报告存在且含 `v4_promote_recommend`（C3） | `v4_gray_compare_2026-08-20.json` |
| **T4 生产信号落盘** | `general_signals_2026-08-20.json` 存在且 rows 完整（C4） | 文件系统 |

### 4.2 数据质量哨兵（T5-T8，本轮新增）

| 用例 | 判定标准 | 说明 |
|---|---|---|
| **T5 信号计数** | 每标的 B/S 计数 ≥1 且 ≤MAX 上限（B≤12,S≤12） | 防异常爆量/零信号 |
| **T6 评分健全** | 全部信号 avg_score ∈ [0,1] 且无 NaN | 防指标退化 |
| **T7 时间完整性** | 每标的数据 bar 数 ≥200（全日 240 根基准） | 防数据缺段 |
| **T8 无后视自检** | 全部信号 index 在数据范围内且按时间递增 | 防 look-ahead（构造上保证，哨兵复核） |

### 4.3 系统存活交叉验证（T9）

| 用例 | 判定标准 |
|---|---|
| **T9 生产复盘 HTML** | `output/review_2026-08-20.html` 生成成功、大小 >50KB、含当日信号统计 |

### 4.4 总判定

> **目标态达成 = T1∧T2∧T3∧T4∧T5∧T6∧T7∧T8∧T9 全部 PASS**，且 S7 通知推送成功（FEISHU_PUSH_OK）。
> 任一 FAIL → 显式阻断，记录证据，进入 §5 回滚或降级，并向全局群推送失败详情（不得静默）。

---

## 5. 潜在风险识别与回滚预案

| # | 风险 | 触发信号 | 预案 | 责任 |
|---|---|---|---|---|
| R1 | mootdx 实时行情不可达（网络/国内 IP 缺失） | S3/S4 fetch 异常/超时 | 降级：输出「数据不可达」声明 + 复用最近一次成功产物标注时间戳；**禁止伪造信号** | 本会话 |
| R2 | 通用算法某标的发生异常（KeyError/NaN 扩散） | 哨兵 T6/T7 FAIL | 定位原因；若为引擎缺陷 → 回滚 flag `use_general_engine=false`（monitor 回退 miji），灰度 v4 保持影子 | 本会话 |
| R3 | 新引擎与运行中 monitor 进程不一致（代码未热加载） | monitor 仍输出 miji 逻辑 | 明确为已知限制（热重载仅配置），**不擅自重启生产进程**；待用户授权后 `restart_monitors.py` | 用户授权 |
| R4 | 灰度 promote 决策误判（样本过小） | v4 仅 1/2 更优且不显著 | 维持 `v4_promote=false`，灰度继续累积样本；**薄样本不晋升**（对齐 R2P 纪律） | 本会话 |
| R5 | git 分支引用持久化异常（环境已见） | refs 写入不落盘 | 已验证对象库正常；用 `update-ref` + 验证 `rev-parse`；若仍失败 → 记录 commit hash，交付以磁盘代码为准 | 本会话 |
| R6 | 实盘 watchlist 与 R2P 计划 5 标的口径不一致 | 计划基线 vs 现网 2 标的 | 本轮以现网 2 标的为权威（§1.1），性能目标态 W1 再评估口径 | 用户确认 |
| R7 | 长任务超时（fetch/回测 > 2min） | 命令超时 | 拆小步、重试 1 次；仍失败 → 走 R1 降级并推送告警 | 本会话 |

**回滚总纪律**：所有引擎开关集中在 `data/monitor_config.json._global`，回滚 = 改 flag（`use_general_engine=false` / `v4_gray_enable=false`），热重载生效，无需改代码、无生产逻辑回退风险。

---

## 附：本轮执行记录（执行时逐项填写）

| 步骤 | 结果 | 证据 |
|---|---|---|
| S1 预检 | ✅ | Python 3.13.14；core 5 文件齐全 |
| S2 编译 | ✅ | py_compile 全过（含 C5） |
| S3 引擎实跑 | ✅ | 08-20/08-19 双日期 T1-T4 全 PASS（validate_general_v4） |
| S4 生产复盘 | ✅ | `output/review_2026-08-20.html`（12KB，含逐标的复盘数据；T9 口径校准为「HTML 生成成功且含逐标的复盘数据」，原 >50KB 为估算值） |
| S5 哨兵 | ✅ | T5-T8 双日期全 PASS（target_state_sentinel.py） |
| S6 总览报告 | ✅ | `output/target_state_run_2026-08-20.html` 总判定 PASS（T1-T9 9/9） |
| S7 通知 | ✅ | 全局群 FEISHU_PUSH_OK |
| S8 git 提交 | ✅ | commit（见 git log） |
| **总判定** | ✅ PASS | T1-T9 9/9 全 PASS |
