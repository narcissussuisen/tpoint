# 数据源故障运行手册（mootdx / 兜底链）

> 建立于 2026-09-10（mootdx 接口级失效当天）。适用：tpoint 盘中「信号变少/没信号/数据可疑」时，
> 用 5 分钟定位是不是数据源问题。**先定性，再动手；不改参。**

---

## 0. 一句话判据

**console 日志 `兜底成功(...)` 只在 mootdx 返回 <5 行或异常时才打印 ⇒ 兜底率 = mootdx 失败率。**
2026-09-10 起日志格式为 `✅ 兜底成功(sina) <sym>: N 根分钟线`（P1b 之前为 `腾讯分时兜底成功`）。
也可直接看 `data/metrics.json` 的 `fallback_rounds / symbols`。

---

## 1. 五分钟判定：mootdx 是「接口级失效」还是「本机网络问题」

```bash
cd C:\Users\YZP\WorkBuddy\Claw\tpoint

# ① TCP 连通性（服务端可达？）
venv\Scripts\python.exe -c "
import socket,time
S=[('180.153.18.170',7709),('218.75.126.9',7709),('115.238.56.198',7709),
   ('115.238.90.165',7709),('60.12.136.250',7709),('202.108.25.241',7709)]
ok=0
for ip,p in S:
    t=time.time()
    try: socket.create_connection((ip,p),timeout=2).close(); ok+=1; print('TCP OK  ',ip,'%.2fs'%(time.time()-t))
    except Exception as e: print('TCP FAIL',ip,type(e).__name__)
print('可达 %d/%d'%(ok,len(S)))"
```

```bash
# ② 协议层鉴别（关键）：哪类接口活了、哪类死了
venv\Scripts\python.exe -c "
from pytdx.hq import TdxHq_API
api=TdxHq_API(heartbeat=False)
api.connect('180.153.18.170',7709,time_out=5)
print('count深          ', api.get_security_count(0))
print('list前1000        ', len(api.get_security_list(0,0) or []))
print('finance          ', len(api.get_finance_info(1,'600519') or []))
print('minute_time      ', len(api.get_minute_time_data(0,'300010') or []))
print('--- 下面两项是 tpoint 的命脉 ---')
print('bars 1min        ', len(api.get_security_bars(8,0,'300010',0,10) or []))
print('bars day         ', len(api.get_security_bars(9,1,'600519',0,3) or []))
print('quotes           ', len(api.get_security_quotes([(0,'300010')]) or []))"
```

**判读表**

| 现象 | 结论 |
|---|---|
| TCP 全 FAIL | 本机网络/防火墙/断网 |
| TCP 部分 OK，且 `count/list/finance/minute_time` 正常、**`bars`/`quotes` 全 0** | **接口级失效**（2026-09-10 实例；免费公共源无 SLA）|
| 全部接口都正常 | 不是数据源问题，转查算法/状态机 |
| `bars` 全 0 但**换个网络（手机热点）后正常** | 本机出口 IP 被服务端降级/限流 |
| `bars` 全 0 且**热点下同样全 0** | 服务端全局故障 —— 只能等 / 换源，调参无意义 |

> 库版本核对：`pip show mootdx pytdx`。截至 2026-09-10 两者均为 PyPI 最新
> （mootdx 0.11.7 / 2024-05、pytdx 1.72 / 2019-08），**无版本可升级**。

---

## 2. 降级期间的正确姿势

- ✅ **让兜底链继续跑**：交易链路不中断，信号照常产出。
- ✅ **看 `data_source`**：`mootdx`（主源，真实 OHLC）/ `sina`（兜底，真实 OHLC）/ `tencent_synth`（兜底，**合成 OHLC**）/ `mootdx_partial`。
- ⚠️ **口径会漂**：腾讯分时兜底的 OHLC 是合成的（`open=前根收盘, high=max(前收,今收), low=min(前收,今收)`），
  丢弃真实分钟影线 ⇒ **ATR 中位低估 41.8%**（448 标的-日），1.5×ATR 反T止损窄约 42% ⇒ 更容易被打。
  兜底链已于 2026-09-10 改为**新浪 1m 真实 OHLC 优先**，腾讯降为末位；若仍落到 `tencent_synth` 说明新浪也失败了。
- 🚫 **禁止在降级期间调止损/trail 参数**：那是拿坏口径做拟合，属过拟合污染。
- ⚠️ **当日复盘要标注数据源**：兜底口径 vs F盘真实 OHLC 回测会产生**假的对账差异**。

## 3. 已知运维成本（2026-09-10 实测）

- **冷启动预热很慢**：mootdx 挂着时，`_warmup_tf()` 3 次重试 × ~172s ≈ **9 分钟**才开始扫描
  （实测 13:19:23 启动 → 13:28:15 进主循环）。期间心跳由 30s 兜底机制维持，**不会误报「服务中断」**
  （v10.9.3 修复）。
- **代价是盘中重启会迟到信号**：预热期间形成的 bar 会在进入主循环后才推送（实测 K:13:24 的 bar 于 13:28:24 推送，迟到 ~4 分钟）。
  ⇒ **mootdx 已挂时，非必要不重启**；必须重启时优先选午休/收盘窗口。
- 预热失败不阻断扫描：`_tf_unhealthy` 只写不判（monitor.py:1859 写 / 2069 清，零门控）。

## 4. 回滚开关

| 开关 | 作用 |
|---|---|
| `TP_INTRADAY_PREFER=tencent` | 兜底链退回「腾讯分时优先」（原行为）；默认 `sina` |
| 哨兵规则 | `config/monitor_config.json` → `alerts[]` 中 `metric=fallback_rate`（阈值 0.5 / 窗口 300s / 告警群 1d241455）；`enabled:false` 即关闭 |

> ⚠️ `config/monitor_config.json` 被 **alert_engine 启动时一次性加载**（alert_engine.py:381）——
> 改规则后必须重启 alert_engine（杀 `data/.alert_engine.pid`，watchdog 30s 内重拉）。

## 5. 第三方核对（agent 会话专用）

tdx-connector 的 `tdx_kline(code, setcode, period="7", wantNum="250")` 返回**真实 1m OHLCV**（实测 ETF 亦覆盖）。

```bash
# ① 在 agent 会话里调 tdx_kline，把 Rows 落成 JSON（示例路径见下）
# ② 再本地核对
venv\Scripts\python.exe scripts\verify_source_fidelity.py \
    --sym 300010.SZ --date 2026-09-10 \
    --bars-json output\tdx_1m_300010_20260910.json
```

⚠️ **硬边界**：tdx-connector / westock-mcp 均为**远端 HTTP MCP**（`txmcp.tdx.com.cn:3001` /
`stockbuddy.qq.com`），鉴权为 OAuth，凭据存于 WorkBuddy 宿主的 **AES-256-GCM 加密库**
（`~/.workbuddy/connectors/<id>/.credentials.v3.json`）。
⇒ **monitor 独立进程永远拿不到可用 token**；强行解密＝绕过宿主安全边界。
**只能作为 agent 侧离线校验源，禁止接入生产路径。**
（若要生产化，须另去服务商申请面向长期运行的独立令牌，单独立项。）

## 6. 历史证据存档（2026-09-10）

| 口径 | 合成/真实 ATR 比值 |
|---|---|
| 今日 09:31–13:07（129 根） | 0.584 |
| 今日 09:31–11:30（120 根，新浪对照） | 0.660 |
| **全历史 448 标的-日（4 只，79–147 天）** | **中位 0.582**（p10–p90 0.451–0.697） |
| F盘 300010 09-09 单日 | 0.733 |
| tdx 连接器 300010 11:19–13:00 | 0.734 |

信号影响（大样本）：总量比 1.077（+7.7%，低于数量级门槛），但**逐日 48.4% 的信号组合发生变化**（总量守恒、位置漂移）。
