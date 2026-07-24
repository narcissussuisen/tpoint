# tpoint 系统整体架构说明书

> 版本：v9.1.4 ｜ 编写日期：2026-07-21 ｜ 适用范围：tpoint 做T监控（A 股分钟级 B/S 信号 + 飞书推送）
> 读者对象：运维、策略研发、后续接手同学（团队内部传阅）

---

## 一、系统概述

tpoint 是一套 **A 股分钟级做 T（T+0）策略监控与信号推送系统**。核心目标是在交易时段对监控标的的分钟 K 线进行多因子共振计算，识别高胜率的买入（B）/ 卖出（S）/ 出场（X）信号，并通过飞书机器人实时推送到信号群；同时配套看门狗告警、盘前自检、每日复盘等运维闭环。

系统由 **常驻进程（monitor + alert_engine）** 与 **定时/按需任务（盘前自检、复盘报告、回测研究）** 组成，整体为单机部署、无中心服务端，依赖外部行情源与飞书开放平台完成数据输入与输出。

---

## 二、整体运行框架

### 2.1 核心运行流程

`monitor.py` 是主循环（生产者），`alert_engine.py` 是看门狗（消费者），二者通过 `data/metrics.json` 心跳文件解耦。`datasource.py` 负责行情接入，`miji_alpha.py` + `indicators.py` 负责信号计算，`feishu_alert.py` 负责告警卡片。

```
                          ┌──────────────── 外部行情源 ────────────────┐
                          │  mootdx 通达信 TCP:7709 (主)              │
                          │  腾讯财经 HTTP (qt.gtimg.cn / ifzq) (兜底) │
                          └─────────────────────┬─────────────────────┘
                                                │ OHLCV DataFrame（当日分钟K）
                                                ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  monitor.py  (常驻主循环, 每轮 SCAN_INTERVAL≈15s)                  │
        │  1. 读 watchlist.json → 标的清单                                     │
        │  2. 读 risk_override.json → 风控闸门(regime gate)                   │
        │  3. compute(sym): datasource → miji_alpha/indicators → B/S/X 判定   │
        │  4. emit_card() → 飞书信号卡(JSON)                                  │
        │  5. write_metrics() → data/metrics.json（心跳+指标）                │
        │  6. 写 signal.txt (本地留痕)                                        │
        └───────┬──────────────────────────────┬────────────────────────────┘
                │ POST interactive 卡片          │ 原子写 metrics.json
                ▼                                ▼
   飞书 Webhook ①(信号群)             data/metrics.json
   hook=1d241455...                     │
                                         │ 轮询(常驻)
                                         ▼
                          ┌──────────────────────────────────────┐
                          │  alert_engine.py (看门狗 sidecar)      │
                          │  读 metrics.json → 阈值规则评估         │
                          │  service_up / scan_duration / data_lag │
                          │  / errors / signals 突增 …             │
                          │  → 触发则 feishu_alert.send() 告警卡片  │
                          └──────────────┬───────────────────────┘
                                         │ POST 分级卡片
                                         ▼
                                  飞书 Webhook ①(信号群)
                                  hook=1d241455...

   ┌── 定时/按需任务（与主循环并行） ─────────────────────────────────────┐
   │ scripts/selfcheck_daily.py (计划任务 tpoint_selfcheck, 交易日 09:00)  │
   │   读 metrics.json + 系统状态 → 盘前状态卡 → 飞书 Webhook ①            │
   │ scripts/build_161129_report.py (每日复盘)                            │
   │   → 生成 HTML → 经 push_feishu_html.py → 飞书 Webhook ②             │
   │ backtest/* 与 keyfactor/* (离线研究, 非生产链路)                     │
   │   → 里程碑经 keyfactor/feishu_push.py → 飞书 Webhook ①              │
   └────────────────────────────────────────────────────────────────────┘
```

### 2.2 模块调用关系与依赖层次

| 层次 | 模块 | 被依赖方 | 依赖方 |
|------|------|----------|--------|
| 接入层 | `core/datasource.py` | monitor、backtest、keyfactor | mootdx / pytdx / 腾讯 HTTP |
| 算法层 | `core/indicators.py`、`core/miji_alpha.py`、`core/entry_filter.py` | monitor、backtest、selftest | numpy / pandas（纯算法，无 I/O） |
| 出场层 | `core/exit_manager.py` | monitor | 算法层 |
| 编排层 | `core/monitor.py` | run.py / V9Launch.bat | 接入层、算法层、出场层、feishu 推送 |
| 告警层 | `core/alert_engine.py`、`core/feishu_alert.py` | 计划任务/启动脚本 | metrics.json、飞书 HTTP |
| 自检层 | `scripts/selfcheck_daily.py` | 计划任务 tpoint_selfcheck | metrics.json、系统信息、飞书 HTTP |
| 复盘/研究层 | `scripts/build_*.py`、`backtest/*`、`keyfactor/*` | 手动/定时触发 | 接入层、算法层、飞书 HTTP |
| 启动层 | `run.py`、`scripts/*.bat`、`install_*.bat` | 运维入口 | 上述各 Python 模块 |

> 设计纪律：**算法层为纯函数**（inputs=OHLCV 数组 → outputs=指标/信号），不触碰网络与文件，便于离线回测与单测；所有 I/O 收敛到接入层与编排层。

### 2.3 数据流转路径

1. **行情流入**：`core/datasource.MootdxDataSource`（实盘数据源：mootdx TCP 7709 + 腾讯 HTTP 兜底）从 mootdx 拉分钟 K；LOF/T+0 基金（如 161129）分钟 K 稀疏时自动降级到腾讯分时 HTTP 兜底；统一归一化为 `DataFrame[trade_time, trade_date, open, high, low, close, volume]`。为兼容旧接口，`datasource` 模块末尾将 `TickFlow` 设为 `MootdxDataSource` 的别名（`TickFlow = MootdxDataSource`），`monitor` 以 `from datasource import MootdxDataSource as TickFlow` 引用。**注意**：第三方云行情 SDK `tickflow`（独立包，对接 `api.tickflow.org`、需 API Key）与本数据源是两套不同后端，仅在 `backtest/keyfactor/download_tickflow.py` 等离线研究脚本中使用，不参与实盘监控。
2. **计算流转**：`monitor.compute(sym)` 在 `_data_lock` 互斥保护下取数（防止共享 mootdx 单 socket 串标），调 `compute_miji_indicators(...)` 得到因子，再经 `check_b_trigger / check_s_trigger` 判定。
3. **信号流出**：命中 → `emit_card()` 构造交互卡片 → `requests.post(WEBHOOK_URL)`；同时 `emit()` 追加写入 `signal.txt`。
4. **心跳流出**：每轮末 `write_metrics()` 原子写 `metrics.json`（先 `.tmp` 再 `os.replace`）。
5. **告警回流**：`alert_engine` 轮询 `metrics.json`，越阈值则经 `feishu_alert.send()` 推送分级卡片。
6. **状态留存**：`state.json` 记录每日 B/S 计数、冷却、持仓（`pos_*`），跨天自动重置 `bar_*`/`_cooldown_*`/`pos_*`。

---

## 三、对外接口清单

> 说明：tpoint **不暴露任何本地 HTTP/RPC 服务端口**，对外接口分为三类——① 飞书 Webhook（出站 POST）；② 行情数据源（出站 TCP/HTTP）；③ 文件系统契约（运行时文件）；④ CLI 入口（运维/研究）。下表按此分类列出。

### 3.1 飞书 Webhook 推送接口（出站 POST）

| 名称 | Webhook 地址（hook id） | 用途 | 请求方式 | 消息类型 | 触发方 |
|------|------------------------|------|----------|----------|--------|
| 信号 / 告警 / 盘前自检 / 研究推送 | `https://open.feishu.cn/open-apis/bot/v2/hook/1d241455-447b-4017-b9a3-4ecb61912369` | B/S/X 信号卡、分级告警卡、盘前状态卡、keyfactor 研究里程碑 | POST | `interactive` 卡片（或 `text` 兜底） | monitor / alert_engine / selfcheck / feishu_push |
| 每日复盘 HTML | `https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622` | 每日复盘 HTML 报告的纯文本链接通知（文件经 push_feishu_html.py 上传云空间） | POST | `text` | research/push_feishu_html.py |
| 全局任务状态 | `https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103` | 跨项目任务启动/完成/卡死状态通知 | POST | `text` | `~/.workbuddy/notify.py` |

**飞书 Webhook 请求/响应结构（信号卡示例）**

- 请求体（POST `application/json`）：
  ```json
  {
    "msg_type": "interactive",
    "card": {
      "header": { "template": "green|red|blue", "title": {"tag":"plain_text","content":"161129 买入 3成"} },
      "elements": [
        {"tag":"div","text":{"tag":"lark_md","content":"**原油LOF·买入｜做T·3成 ★☆☆**"}},
        {"tag":"div","text":{"tag":"lark_md","content":"现价 1.921（-3.71%）｜下轨 1.984"}},
        {"tag":"div","text":{"tag":"lark_md","content":"依据：回踩支撑"}},
        {"tag":"div","text":{"tag":"lark_md","content":"信号K：10:28"}},
        {"tag":"hr"},
        {"tag":"note","elements":[{"tag":"plain_text","content":"RSI=.. 温=.. 量比=.. ｜ v9.1.4·仅供参考非投资建议"}]}
      ]
    }
  }
  ```
- 响应体（JSON，关键字段）：
  | 字段 | 类型 | 说明 |
  |------|------|------|
  | `code` | int | `0` 表示成功；`11232` 表示频率限制（feishu_push 会自动退避重试） |
  | `msg` | string | 状态描述 |
  | `data` | object | 可选，消息回执 |
  | `status_code` | int | HTTP 层状态，成功为 `200` |

> 配色约定：**买入=green / 卖出=red / 出场=blue**（与 A 股红涨绿跌相反，此为飞书卡片语义配色，非价格涨跌色）。

### 3.2 行情数据源接口（出站）

| 接口 | 协议 / 方式 | 端点 | 用途 | 关键入参 | 返回 |
|------|-------------|------|------|----------|------|
| 通达信行情（主源） | **TCP**（二进制协议，非 HTTP） | 多组 `host:7709`（如 `180.153.18.170:7709`，内置 10 台 + pytdx hosts 兜底） | 日 K / 分钟 K / 实时五档 / 财务 | `symbol`(6位代码)、`frequency`(8=1m,9=日K)、`market`(0深/1沪) | mootdx `DataFrame`（trade_date/open/close/high/low/volume） |
| 腾讯实时快照（备份） | **HTTP GET** | `https://qt.gtimg.cn/q={sz/sh}{code}`（GBK 返回） | mootdx 挂掉时的实时价备份 | 腾讯代码前缀 `sz161129`/`sh688347` | 文本 `~` 分隔，解析为 `{name,code,price,prev_close,open,volume}` |
| 腾讯分时兜底（LOF 兜底） | **HTTP GET** | `https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sz/sh}{code}`（GBK JSON） | mootdx 分钟 K 不足时（LOF/T+0 基金）拉当日分钟线 | 腾讯代码前缀 | JSON → 归一化 `DataFrame[trade_time,open,close,high,low,volume]` |
| 云行情 SDK（研究用，非生产） | **HTTPS**（云 API） | `https://api.tickflow.org`（需环境变量 `TICKFLOW_API_KEY`） | 离线研究批量下载历史 1m（keyfactor 因子研究）；**替代 mootdx 作研究数据源**，不参与实盘 | API Key + `symbol`/`period`(如 `1m`)/`count`(≤5000) | tickflow `DataFrame`（接口同形 `tf.klines.get` / `tf.klines.intraday`，与 `MootdxDataSource` 兼容） |

> 注意：**mootdx 走 TCP 7709，需放行出站 7709**；海外网络常超时，需国内代理或更新服务器列表。`datasource.tdx_client()` 内置四级兜底（显式列表→pytdx hosts→bestip→裸 factory），并对返回数据做非空校验，规避"连得通但返回空"的僵服务器。

> **`TickFlow` 名称重载提示**：实盘路径中 `TickFlow` 是 `MootdxDataSource` 的兼容别名（指向同一对象，后端为 mootdx/腾讯）；研究路径中 `from tickflow import TickFlow` 指向独立的 `tickflow` 云 SDK 包（后端为 tickflow 云 API）。二者后端不同，勿混淆。

### 3.3 文件系统接口（运行时契约）

| 文件 | 方向 | 读写方 | 结构 / 关键字段 |
|------|------|--------|------------------|
| `data/metrics.json` | 读写 | 写：monitor；读：alert_engine、selfcheck | `{ts, scan_duration_s, signals, errors, symbols, last_bar_ts, status}`；`last_bar_ts` 为最新行情棒 Unix 时间戳（null=非交易时段） |
| `data/signal.txt` | 追加写 | 写：monitor；读：人工/复盘 | 按 `[日期]` 分段，每条信号含时间戳、B/S/X、价格、涨跌幅、触发位、RSI、温度 |
| `data/state.json` | 读写 | monitor | 每日 B/S 计数、冷却时间戳、`pos_*`（持仓）、`_daily_refreshed_date`；跨天自动重置 |
| `data/watchlist.json` | 只读 | monitor、selfcheck | 标的唯一真相源。格式兼容旧 `{"code":"名称"}` 与新 `{"code":{"name":..,"status":"active"|"suspended","suspended_until":"ISO时间"|null}}`；monitor 扫描前过滤 status=suspended 且期限未到的标的（停牌≠数据源中断，不计入 err_count、不报缺数告警） |
| `data/risk_override.json` | 只读 | monitor | 风控闸门（模式②）：`{regime, action, risk_score, expires_at, source}`；缺失/过期/坏→`NONE`（放行） |
| `data/.monitor.lock` / `.monitor.pid` | 读写 | monitor | 单实例锁（Windows `msvcrt.locking`）；防止重复运行 |
| `data/.alert_engine.lock` / `.alert_engine.pid` | 读写 | alert_engine | 看门狗单实例锁；防止告警重发 |
| `VERSION` | 只读 | monitor、selfcheck | 版本号（如 `9.1.4`） |

### 3.4 CLI 入口接口

| 命令 | 请求方式 | 主要入参 | 行为 |
|------|----------|----------|------|
| `python run.py` | 进程启动 | 可选 env `TP_SCAN_INTERVAL` | 自动探测含 mootdx/requests 的 Python 解释器，拉起 `core/monitor.py` |
| `python core/monitor.py` | 常驻进程 | env：`TP_SCAN_INTERVAL`、`TP_WEBHOOK_URL`、`TP_WATCHLIST_FILE` 等 | 主扫描循环（见 2.1） |
| `python core/alert_engine.py [--once\|--dry-run\|--self-test]` | 常驻/单次 | `--once` 单次评估；`--dry-run` 不打请求；`--self-test` 卡片渲染验证 | 看门狗（读 `monitor_config.json` 规则） |
| `python scripts/selfcheck_daily.py [--no-push] [--date YYYY-MM-DD]` | 单次 | `--no-push` 仅本地输出不推送；`--date` 指定日期 | 盘前自检，读 `metrics.json`+系统状态→飞书状态卡 |
| `python scripts/build_161129_report.py` | 单次 | 标的/日期参数（脚本内配置） | 生成每日复盘 HTML（输出 `output/*.html`） |
| `python backtest/keyfactor/feishu_push.py "<文本>" [--critical] [--retries N]` | 进程启动 | 位置参数：消息文本；`--critical` 跳过限速；`--retries` 重试次数 | 经 Webhook ① 推研究里程碑（令牌桶限速+退避） |

---

## 四、模块划分

| 模块（文件） | 核心职责 | 边界（不负责） |
|--------------|----------|----------------|
| `core/datasource.py` | 行情接入与归一化；mootdx 四级兜底、腾讯实时/分时兜底、字段对齐、异常 datetime 清洗 | 不计算信号、不推送 |
| `core/indicators.py` | 纯算法：VWAP 价值中枢、ATR 波动带、EMA/ADX 趋势三态、量比、情绪温度计、星级 | 不触网络/文件；被 monitor 与回测共用 |
| `core/miji_alpha.py` | 三因子共振信号引擎（gravity + MACD 背离 + 量价）；`compute_miji_indicators`、`check_b/s_trigger` | 不负责数据拉取 |
| `core/entry_filter.py` | 进场过滤（附加条件收敛误发） | 不负责出场 |
| `core/exit_manager.py` | 出场管理：移动止损、硬止损、时间止损；返回 EXIT 双口径（当日/持仓盈亏） | 不负责入场 |
| `core/monitor.py` | **编排核心**：标的加载、风控闸门、并发/串行 compute、信号判定、卡片推送、心跳写盘、单实例锁、跨天状态重置、收盘 keepalive | 不直接判定告警阈值（交 alert_engine） |
| `core/alert_engine.py` | 看门狗：轮询 `metrics.json`，按 `monitor_config.json` 规则评估，触发分级告警；单实例锁 | 不生成信号、不拉行情 |
| `core/feishu_alert.py` | 通用飞书交互卡片构造与发送；三档配色；可选 HMAC 签名；dry-run | 不含业务规则 |
| `scripts/selfcheck_daily.py` | 盘前自检：组件健康、扫描时效断言（last_bar_ts / scan_duration / errors）、风险告警、交互卡片模板 | 不改生产配置 |
| `scripts/build_*.py` | 复盘/分析 HTML 生成 | 非生产链路 |
| `backtest/*`、`keyfactor/*` | 离线回测与因子研究 | 不影响生产 monitor |
| `run.py`、`scripts/*.bat` | 启动器、重启、单实例拉起、UTF-8/stdout 重定向 | 不含业务逻辑 |

---

## 五、部署与运行环境

### 5.1 部署方式（单机常驻）

- **唯一自启入口**：Windows 启动目录 `V9Launch.bat`（登录时拉起 monitor + alert_engine，venv python，免管理员）。标准重启 = 登录后等待约 15s，勿手动双击 `run_monitor.bat` 等造成重复实例。
- **进程模型**：monitor 与 alert_engine 各为独立进程，经 `:loop` 自愈重启；二者通过文件心跳解耦，互不阻塞。
- **盘前自检**：Windows 计划任务 `tpoint_selfcheck`（周一至五 09:00）调用 `run_selfcheck.bat → selfcheck_daily.py`，结果经 Webhook ① 推送。
- **冷启动**：`run.py`（自动探测解释器）或 `scripts/restart.bat` 一键重启。
- 部署目标原为 Linux（`openclaw` 服务器），当前实际运行于 **Windows**（路径、锁机制已适配 `msvcrt`）。

### 5.2 依赖的基础设施与中间件

| 类别 | 依赖 | 说明 |
|------|------|------|
| 运行时 | Python 3.11（项目 `venv`，`venv/Scripts/python.exe`） | 依赖隔离在 venv，不污染全局 |
| 第三方库 | `requests`、`numpy`、`pandas`、`mootdx>=0.11`、`pytdx` | 见 `requirements.txt` |
| 行情中间件 | 通达信行情服务器（TCP 7709）+ 腾讯财经 HTTP | 无 Key、免费；需放行出站 7709 |
| 消息中间件 | 飞书开放平台群机器人 Webhook（3 个 hook） | 仅需 Webhook URL，无需自建服务 |
| 文件/锁 | 本地文件系统 + `msvcrt`(Win)/`fcntl`(Linux) 文件锁 | 单实例互斥、状态留存 |
| 云空间（复盘） | 飞书云空间（经 push_feishu_html.py 的 `lark-cli`） | 仅复盘 HTML 上传用，非生产依赖 |

### 5.3 启动脚本与运行参数

- `scripts/run_monitor.bat`：设 `PYTHONIOENCODING=utf-8` + `PYTHONUNBUFFERED=1`，stdout/stderr 重定向到 `logs/monitor_console.log`，`:loop` 每 30s 自愈。
- `scripts/run_engine.bat`：常驻拉起 `alert_engine.py`，`:loop` 每 5s 自愈。
- `scripts/run_selfcheck.bat`：用 WorkBuddy 托管 Python 3.13 跑自检。
- 可调环境变量：`TP_SCAN_INTERVAL`（扫描间隔）、`TP_WEBHOOK_URL`、`TP_WATCHLIST_FILE`、`SIGNAL_FILE`/`STATE_FILE`/`METRICS_FILE` 等（见 `monitor._cfg`）。

### 5.4 关键配置

| 配置文件 | 作用方 | 内容 |
|----------|--------|------|
| `config/monitor_config.json` | alert_engine | 飞书 `webhook_url`/`secret`、心跳轮询间隔、`service_stale_s`、告警规则列表（阈值/严重等级/cooldown） |
| `data/watchlist.json` | monitor | 监控标的唯一真相源（**改之→热加载，下一轮生效**；新增 `status`/`suspended_until` 字段可让 monitor 自动跳过停牌标的，省去每次停牌手动改列表） |
| `data/risk_override.json` | monitor | 风控闸门（外部风险 Agent 写入，过期失效） |
| `VERSION` | 全局 | 版本号 |
| `docs/design.md`、`docs/deploy.md`、`docs/t0_playbook.md` | 人 | 设计/部署/操作手册 |

---

## 六、关键设计决策与运维要点

1. **生产者/消费者解耦**：monitor 崩溃不直接中断告警，仅心跳过期由 alert_engine 感知，避免单点耦合。
2. **算法层纯函数化**：`indicators.py` / `miji_alpha.py` 无 I/O，可离线回测与单测，保证生产与研究同源。
3. **数据源四级兜底 + LOF 腾讯兜底**：解决 mootdx 服务器失效与基金分钟 K 稀疏两类生产痛点。
4. **单实例锁防重发**：monitor/engine 各自持锁，防止重启叠加导致飞书告警/信号重发。
5. **心跳原子写**：`write_metrics` 先写 `.tmp` 再 `os.replace`，消除 Windows 文件锁竞争导致的"误报无心跳"。
6. **风控闸门 fail-open**：`risk_override.json` 缺失/过期/坏 → `NONE` 放行，永不因风控文件异常误伤生产做 T。
7. **红涨绿跌 vs 卡片配色**：A 股价格涨跌用红涨绿跌（区域约定）；飞书卡片的 green/red/blue 仅表示买/卖/出场语义，二者不可混淆。
8. **告警阈值集中化**：所有阈值/规则在 `monitor_config.json`，热改无需改代码；盘前自检断言在 `selfcheck_daily.py` 内（与 alert_engine 互补，分别覆盖"运行健康"与"扫描时效"）。

> 投资有风险，系统所有信号与推送仅供参考，不构成投资建议。

---

*— 文档结束 —*
