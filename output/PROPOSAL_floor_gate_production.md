# 决策建议：生产 MACD 门控参数（MACD_GATE_MODE / REQUIRE_MACD）调整

- **状态**：建议稿（待人工拍板）
- **适用版本**：tpoint 做T秘籍 v9.1.4 信号引擎
- **隔离声明**：本建议基于沙箱回测（`backtest/keyfactor/compare_macd_gate.py`）与隔离脚本（`scripts/floor_signals_today.py`，零生产依赖）的验证结果，**不影响现生产运行**；任何参数改动须另行评审 + 回测 + 灰度。

---

## 0. TL;DR 推荐

1. **立刻可做（低风险，单一环境变量翻转）**：把生产门控从 `strict` 改为 `floor`——生产由环境变量 `MACD_GATE_MODE` 控制（`core/miji_alpha.py:43`，默认 `strict`），只需在 `run_monitor.bat` / `run_engine.bat` 设 `MACD_GATE_MODE=floor`。
2. **必须同步修复（正确性 / 诚实性）**：`RESONANCE_THRESHOLD=2` 当前在两套引擎里都是**死参数**——信号实际是"单因子触发"，与 MD 文档"≥2项共振才执行"不符。要么在代码里落地强制、要么改文档/注释，二选一。
3. **不推荐直接翻 `off`（= `REQUIRE_MACD=False`）**：信号泛滥、每信号净效率最低（沙箱 0.085），94% 胜率是均值回归方法学内生性，不可作实盘依据。
4. **不建议**在没有新回测的前提下把 `REQUIRE_MACD` 直接置 false，理由同上。

---

## 1. "floor 单因子触发" 究竟是什么 —— 引擎语义差异（已代码确认）

`detect_miji_signals`（研究态）与 `check_miji_trigger`（生产实时路径）的触发判定**只取决于 `macd_gate_mode` 门控**，从不用 `score >= min_resonance` 做硬闸：

```
buy_pass 仅由 macd_gate_mode 决定：
  off     : B iff g_factor==+1
  strict  : i<LOCAL_W -> g_factor==+1 ; i>=LOCAL_W -> m_factor==+1
  floor   : strict 规则  OR  (价格新低 且 g_dev<=-FLOOR_DEV_PCT)
sell_pass 同构（方向相反）
```

- `buy_score = sum(f==+1 for f in [g,v,m])`、`sell_score` 同样计算，但**仅存入 `resonance_score` 元信息字段，从不参与 `if buy_pass:` 判定**。
- 参数 `min_resonance`（默认 `RESONANCE_THRESHOLD=2`）被函数签名接收，**从未在触发分支中使用**。

**代码位置**
- 研究态：`backtest/keyfactor/miji_engine.py:383-407`(B) / `:429-447`(S)，实时 `:533-558`
- 生产态：`core/miji_alpha.py:422-447`(B) / `:468-487`(S)，实时 `check_miji_trigger:533-558`
- 死参数声明：`core/miji_alpha.py:38` `RESONANCE_THRESHOLD = 2  # >=2因子同向 -> 触发信号`（**注释与实际行为矛盾**）

**结论**：当前所有门控模式都是"单因子触发"。`strict`（warmup 后）实际 =「MACD背离单因子」，`off` =「引力单因子」，`floor` =「MACD背离单因子 OR 价格地板/天花板单因子」。生产文档声称的"≥2因子共振"从未被强制。用户观察到的"floor 单因子触发"是系统性的，且 strict/off 同样单因子。

---

## 2. 证据

### 2.1 沙箱三模式对比（106 只 × 历史 1m，次根 K 成交 + 双边各 0.02% 成本）

| 指标 | strict（生产默认） | off（纯引力） | floor（价格地板） |
|---|---|---|---|
| 信号数 | 78,937 | 250,868 | 100,558 |
| 每信号净 T | 0.189 | 0.085 | **0.216** |
| 均净 T% | 0.700 | 0.715 | **0.913** |
| 累计净 T% | 14,914 | 21,444 | **21,744** |
| T 胜率 | 80.4% | 94.0%（虚高） | 87.6% |

### 2.2 今日实时 floor 跑（两标的，仅喂当日已收盘棒，因果无未来函数）

- **161129.SZ**：6 信号（1B / 5S），**全部 `resonance_score=1`**（仅引力 + 价格地板/天花板），md 全程 = 0。
- **688347.SH**：16 信号（12B / 4S），多为 score=1；3 个含 md（10:14 B `1/0/1`、11:28 B `0/0/1`、14:48 S `0/0/-1`）。

两标的的 score 分布进一步印证：引擎不强制 ≥2，单因子即可出信号。

---

## 3. 当前 strict 模式的风险（关键）

沙箱因子归因：`md`（MACD 背离）命中时 12min 前向收益**为负**、未命中时**为正**（三模式一致）→ 本样本中 md 因子**反向预测**。
- 生产默认 `strict` 在 warmup 后强制 `m_factor==+1` 才买 → 等于**把反向预测的因子当正信号筛入**，把不靠 md 的更好子集排除。
- 这与"REQUIRE_MACD=True 提升信号质量"的初衷相反。

---

## 4. 三个生产参数选项

### 选项 A — 保持单因子门控，翻转 `MACD_GATE_MODE: strict → floor`（推荐，低风险）
- **改动**：设环境变量 `MACD_GATE_MODE=floor`（`run_monitor.bat` / `run_engine.bat`），单一翻转，无需动算法代码。
- **收益**：floor 在沙箱效率最高（每信号净 T 0.216 / 均净 T 0.913%）；且 floor **不再强制 md**——md 信号只是"可选之一"，不靠 md 的价格地板/天花板单因子也能触发，绕开 md 反向预测陷阱。
- **风险**：floor 仍允许 md-only 信号通过（如 688347 的 14:48 S）。若要彻底剔除 md 反向影响，需选项 C 的代码改动。
- **验证**：flip 前建议以 `floor` 跑沙箱 OOS 时间切分 + 今日两标的肉眼核对，确认无回归。

### 选项 B — 翻 `REQUIRE_MACD=False`（等价于 off，不推荐）
- off 信号量最大（沙箱 25 万）、每信号净效率最低（0.085），纯引力含大量均值回归噪声；94% 胜率是配对方法学内生性，非实盘 alpha。直接置 false 会显著放大信号噪声与推送量。

### 选项 C — 落地"≥2 因子共振"强制门控（需代码改动，中高风险）
- **改动**：`if buy_pass:` 前加 `and buy_score >= min_resonance`（S 同构）；研究态 + 生产态两处同步。
- **收益**：让引擎与 MD 文档语义一致，信号质量更高（理论上）。
- **风险**：① 信号量可能骤降（单因子信号全砍）；② 当前 md 反向预测，强制共振后主要剩「引力 + 量能」，需**重新回测**确认 skill；③ 沙箱对比从未在"强制共振"语义下跑过，原结论不适用。
- **建议**：作为独立实验分支（`exp/keyfactor-resonance`）验证，勿直接 flip 生产。

---

## 5. 建议执行顺序

1. **【立刻 / 低风险】** 修 `RESONANCE_THRESHOLD` 死参数问题：要么在 `detect_miji_signals` / `check_miji_trigger` 加 `>= min_resonance` 强制（研究态 + 生产态同步），要么把常量注释 / MD 文档改为"单因子门控（由 `macd_gate_mode` 决定）"，消除误导。**诚实性优先**。
2. **【本周 / 中低风险】** 将 `MACD_GATE_MODE` 从 `strict` 切到 `floor`：先做沙箱 OOS 时间切分重跑 + 今日两标的对照，确认 floor 在样本外仍优于 strict，再在 bat 里翻转。
3. **【实验 / 中高风险】** 另开分支验证"强制 ≥2 因子共振"是否真提升 OOS skill，再决定是否进生产。
4. **【监控】** flip 后观察 monitor 推送信号量 / 方向分布是否异常（尤其 md-only 信号占比），必要时回退。

---

## 6. 诚实声明 / 局限

- 沙箱对比本身在"死参数单因子"语义下生成，结论仅比较 strict / off / floor **三档门控的相对优劣**，**不验证"2 因子共振"**（该语义未被测试）。
- 方向准确率 45–47%（≈抛硬币）；高 T 胜率含均值回归 tautology；md 反向预测为样本内观察，需 OOS 确认。
- 本报告为决策依据整理，**非生产变更**；任何参数调整须人工评审 + 回测 + 灰度。

> ⚠️ 以上为基于隔离验证的算法 / 参数决策建议，仅供研究参考，不构成任何投资建议或交易依据。
