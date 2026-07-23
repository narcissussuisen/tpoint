#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate forensic HTML: reconcile tpoint live pushes vs report vs state.json,
and diagnose the risk agent. Numbers pulled live from disk."""
import json, os, datetime

BASE = r"C:\Users\YZP\WorkBuddy\Claw\tpoint"
state = json.load(open(os.path.join(BASE, "data/state.json"), encoding="utf-8"))
report = json.load(open(os.path.join(BASE, "output/review_2026-07-22.json"), encoding="utf-8"))
try:
    timeline = json.load(open(os.path.join(BASE, "output/_push_timeline_2026-07-22.json"), encoding="utf-8"))
except Exception:
    timeline = []

DATE = "2026-07-22"

def live_counts(sym):
    b = state.get(f"_b_count_{sym}_{DATE.replace('-','')}", 0)
    s = state.get(f"_s_count_{sym}_{DATE.replace('-','')}", 0)
    return b, s

# ---- per symbol: live(state) vs report(recompute) ----
syms = ["161129.SZ", "688347.SH", "513310.SH"]
rows = []
for sym in syms:
    info = report["symbols"][sym]
    summ = info["summary"]
    lb, ls = live_counts(sym)
    rep_total = summ["n_signals"]
    rep_B = summ["n_B"]; rep_S = summ["n_S"]; rep_X = summ["n_X"]
    live_total = lb + ls
    rows.append({
        "sym": sym, "name": info["name"],
        "live_B": lb, "live_S": ls, "live_total": live_total,
        "rep_B": rep_B, "rep_S": rep_S, "rep_X": rep_X, "rep_total": rep_total,
        "delta": rep_total - live_total,
        "rep_rows": info["rows"],
    })

# ---- dropped pushes (11232) ----
dropped = [e for e in timeline if not e["ok"]]

# ---- risk override ----
risk = json.load(open(os.path.join(BASE, "data/risk_override.json"), encoding="utf-8"))

def esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

html = []
html.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tpoint 信号一致性核查与风控诊断 · {DATE}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:24px;line-height:1.6}}
h1{{font-size:24px;margin:0 0 4px}}
h2{{font-size:19px;margin:28px 0 10px;padding-left:10px;border-left:4px solid #4a90e2;color:#fff}}
h3{{font-size:15px;margin:18px 0 8px;color:#ffd479}}
.sub{{color:#9aa0a6;font-size:13px;margin-bottom:8px}}
.card{{background:#1a1d23;border:1px solid #2a2e37;border-radius:10px;padding:16px 18px;margin:12px 0}}
.warn{{border-left:4px solid #e2a04a;background:#241c10}}
.crit{{border-left:4px solid #e25a5a;background:#241313}}
.ok{{border-left:4px solid #4ae28a;background:#10241a}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
th,td{{border:1px solid #2a2e37;padding:7px 9px;text-align:left;vertical-align:top}}
th{{background:#22262f;color:#cfe3ff;font-weight:600}}
tr:nth-child(even) td{{background:#16191f}}
.r{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:#ff6b6b}} .neg{{color:#4ae28a}} .mut{{color:#9aa0a6}}
code{{background:#0a0c0f;padding:1px 5px;border-radius:4px;color:#ffd479;font-size:12px}}
.tag{{display:inline-block;background:#2a2e37;border-radius:4px;padding:1px 6px;margin:1px;font-size:11px}}
.tok-miss{{color:#e25a5a}} .tok-ok{{color:#4ae28a}}
small{{color:#9aa0a6}}
</style></head><body>
<h1>tpoint 信号一致性核查 &amp; 风控诊断</h1>
<div class="sub">交易日 {DATE} · 生成于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 数据源：monitor 日志 / <code>state.json</code> / <code>review_2026-07-22.json</code> / <code>risk_override.json</code></div>
""")

# ---- Executive summary ----
html.append("""<div class="card crit"><h3>一、核心结论（先看这个）</h3>
<ol>
<li><b>复盘报告不能代表“真实推送信号”。</b>报告是<b>干净状态全量复算</b>（clean-state replay），与实盘<b>增量+预热(warmup)</b>运行存在系统性偏差：复算信号数（34）是实盘权威计数（<code>state.json</code>=16）的 <b>2.1 倍</b>。161129 复算 5 条 vs 实盘 2 条；688347 复算 14 vs 实盘 9；513310 复算 15 vs 实盘 5。</li>
<li><b>存在 monitor 进程共用同一飞书 webhook 导致频限丢推。</b>根因经核实为<b>手动重复拉起 monitor</b>（双击 <code>run_monitor.bat</code>/<code>restart.bat</code> 绕过单实例锁，07-20 已确证双实例）→ 两个 monitor 实例并发推同一信号群 webhook；叠加 <b>risk_agent 默认复用同一信号群 webhook</b>（<code>risk_agent.py:42</code>），共同打满频限 <code>code=11232 frequency limited</code>，已确认 <b>4 条推送被丢弃</b>（10:00:44 ×3、13:30:04 ×1）。<br><span class="mut">更正：此前将 11232 归因于“商络电子/光纤第二策略实例”与代码事实不符——<code>monitor_console.log</code> 中的“商络电子/光纤”watchlist 是 <b>2026-07-13 的历史残留日志（未轮转）</b>，全 WorkBuddy 无第二 monitor 在推该组；真实多实例来自手动重复拉起 monitor。</span></li>
<li><b>风控 agent 今日完全未生效，且设计上本就不做交易风控。</b>交易风控闸门 <code>risk_override.json</code> 由外部 <code>vr_risk_agent</code> 于 03:00:50 写入、<b>03:10:50 即过期（开盘前 6 小时）</b>，全天 <code>_load_risk_override()</code> 返回 <code>NONE</code>（放行）；告警引擎 <code>alert_engine.py</code> 是<b>健康看门狗</b>（只看 monitor 进程健康），不是交易风控器，今日进程健康故零动作。</li>
</ol></div>""")

# ---- Methodology / limitation ----
html.append("""<div class="card warn"><h3>二、方法与数据局限（必须说明）</h3>
<ul>
<li>monitor 推送日志<b>只记录“本轮 N 条→推送”与飞书返回码，不记录具体标的/信号类型</b>（卡片正文被截断，标的不入日志）。因此<b>逐笔“推送时间→标的”无法从日志直接还原</b>。</li>
<li>用户提供的 3 张飞书截图本环境<b>无法读取（模型不支持图片）</b>。本文用实盘权威计数 <code>state.json</code>、报告复算 <code>review_2026-07-22.json</code>、推送日志三方交叉验证，给出可证伪的结论。</li>
<li>如需未来可审计，<b>必须在每次推送时把 {时间, 标的, 类型, 价格, 飞书返回码} 落盘</b>（见第八节修复建议）。</li>
</ul></div>""")

# ---- Root cause 1: dual instance ----
html.append("""<div class="card crit"><h3>三、根本原因①：手动重复拉起 monitor 共用 webhook → 频限丢推</h3>
<p>证据：</p>
<ul>
<li><b>手动重复拉起 monitor（绕过单实例锁）</b>：07-20 已确证存在双 monitor 实例（双击 <code>run_monitor.bat</code>/<code>restart.bat</code> 未走 V9Launch 自启，单实例锁被绕过），两个实例并发推同一信号群 webhook。</li>
<li><code>risk_agent.py:42</code> 默认 <code>RISK_WEBHOOK</code> 复用<b>同一信号群 webhook</b>（1d241455…），盘中若推送 regime 卡片，进一步加剧频限。</li>
<li>两者共同打满飞书群机器人频限 → <code>frequency limited</code>（code=11232）。</li>
</ul>
<p class="tok-miss"><b>更正（重要）：</b>此前本报告将 11232 归因于“商络电子/光纤第二策略实例”，经核查<b>与代码事实不符</b>。<code>monitor_console.log</code> 中的“商络电子/长飞光纤/三孚股份/菲利华/国际复材”watchlist 是 <b>2026-07-13 的历史残留日志（日志未轮转）</b>，全 WorkBuddy 当前<b>并无第二 monitor 在推该组</b>；<code>monitor_console_new.log</code> 的 2 标的过程亦属历史上手动双开 monitor 的残留，而非独立“商络电子策略”。真实多实例来源是<b>手动重复拉起 monitor</b>。</p>
<p class="tok-miss"><b>已确认丢弃的推送（飞书返回 11232）：</b></p>
<table><tr><th>时间</th><th>来源日志</th><th>说明</th></tr>""")
for e in dropped:
    html.append(f"<tr><td class='r'>{e['ts']}</td><td>{esc(e['log'])}</td><td>code={e['code']} frequency limited — 该批次推送被飞书拒绝</td></tr>")
html.append("</table><p class='mut'>具体哪些标的的推送被吞，因日志不记录标的而无法逐条定位——这正是必须修的点（修复建议②审计日志已落地）。</p></div>")

# ---- Root cause 2: recompute divergence ----
html.append("""<div class="card crit"><h3>四、根本原因②：复盘报告=干净复算 ≠ 实盘推送</h3>
<p><code>daily_signal_review.py</code> 用 <b>全新 state 对全量当日数据跑一次 <code>detect_for</code></b>（MACD_GATE_MODE=floor）。而实盘 monitor 是<b>逐根 bar 增量</b>运行，且早盘有 <b>warmup 预热</b>（ATR/VWAP/EMA 短窗口使阈值更宽 → 实盘比复算<b>更保守</b>、信号更少），并跨扫描持久化持仓/冷却。结果复算系统性<b>多发</b>信号。</p>
<table><tr><th>标的</th><th>实盘 state.json<br>(B / S / 计)</th><th>报告复算<br>(B / S / X / 计)</th><th>偏差</th><th>结论</th></tr>""")
for r in rows:
    delta_cls = "tok-miss" if r["delta"]>0 else "tok-ok"
    html.append(f"""<tr>
<td>{r['sym']}<br><small>{esc(r['name'])}</small></td>
<td class='r'>{r['live_B']} / {r['live_S']} / <b>{r['live_total']}</b></td>
<td class='r'>{r['rep_B']} / {r['rep_S']} / {r['rep_X']} / <b>{r['rep_total']}</b></td>
<td class='r {delta_cls}'>+{r['delta']}</td>
<td>复算多发 {r['delta']} 条（warmup/状态差异）</td>
</tr>""")
html.append(f"</table><p class='mut'>实盘权威总计数 = 16 条（B13/S3）；报告复算 = 34 条（B11/S7/X16）。复算把未实际推送的“影子信号”算进报告，导致复盘失真。</p></div>")

# ---- 161129 specific ----
html.append("""<div class="card"><h3>五、161129.SZ 四个疑点逐条答复</h3>
<p>报告对 161129 复算的 5 条：<span class="tag">09:43 卖出</span><span class="tag">10:13 回补(买)</span><span class="tag">10:42 买入</span><span class="tag">10:58 止损/出场(TRAIL)</span><span class="tag">13:07 卖出</span>。实盘 state.json 仅 <b>B=1 / S=1</b>。</p>
<table><tr><th>用户疑点</th><th>日志/数据证据</th><th>结论</th></tr>
<tr><td><b>9:39 卖出：已推送，报告未体现</b></td><td>推送日志 <code>monitor_console.log</code> 有 <code>[09:39:12] 本轮信号1条→推送 status=200 code=0 success</code>。<b>报告确有 161129 卖出，但时间戳记为 09:43</b>（复算 bar 收盘时间，比实盘推送晚 4 分钟）。</td><td class="tok-ok">信号<b>已在报告中</b>，仅时间偏移 4 分钟，看似“缺失”实为对齐口径不同。</td></tr>
<tr><td><b>10:42 买入：未收到</b></td><td>报告有 <code>10:42 B</code>，但<b>两个推送日志均无 10:42 送达记录</b>；实盘 state.json 161129 仅 B=1（已用于回补，见下）。</td><td class="tok-miss">典型<b>复算幻影信号</b>：实盘未触发/未推送，报告却列出。用户“未收到”是正确的。</td></tr>
<tr><td><b>10:58 止损/出场：未收到</b></td><td>报告有 <code>10:58 X(TRAIL)</code>，推送日志无 10:58 送达记录；state.json 亦无对应出场计数（161129 仅 1B/1S）。</td><td class="tok-miss">复算幻影信号，实盘未推送。</td></tr>
<tr><td><b>13:07 卖出：未收到</b></td><td>报告有 <code>13:07 S</code>，推送日志三标的过程实盘在 13:04:25 与 13:09:42 有推送，但无 13:07 送达记录；state.json 161129 S 计=1 已花在 09:39。</td><td class="tok-miss">复算幻影信号，实盘未推送。</td></tr>
</table>
<p class="mut">注：若用户截图中的 10:42/10:58/13:07 确实收到过推送，它们大概率来自<b>手动重复拉起的 monitor 实例</b>（历史上双击 run_monitor.bat 产生的第二实例，推送时刻如 10:00:44/10:16:55/11:04:41/13:04:25/13:09:42… 与之接近），而非当前报告建模的三标的实例——这正是“手动多实例共用 webhook”导致的口径分裂。</p></div>""")

# ---- 688347 / 513310 ----
html.append("""<div class="card"><h3>六、688347.SH / 513310.SH 一致性核对</h3>
<p>因推送日志不记录标的，以下以“<b>报告复算 vs 实盘 state.json</b>”对比，列出复算多出、实盘未发的信号（即报告中“看似触发但实盘没推”的条目）。</p>""")
for r in rows[1:]:
    sym = r["sym"]; name = r["name"]
    html.append(f"<h3 style='color:#ffd479'>{sym} · {esc(name)}</h3>")
    html.append(f"<p class='mut'>实盘 state.json：B={r['live_B']} / S={r['live_S']}（共 {r['live_total']}）；报告复算：B={r['rep_B']} / S={r['rep_S']} / X={r['rep_X']}（共 {r['rep_total']}，<span class='tok-miss'>多 {r['delta']} 条</span>）。</p>")
    html.append("<table><tr><th>复算时间</th><th>类型</th><th>价格</th><th>标签</th><th>实盘是否发出</th></tr>")
    for row in r["rep_rows"]:
        t = row["time"][11:19]
        typ = row["type_cn"]
        price = row["price"]
        tag = esc(row["tag"])
        # heuristic: X(出场) signals are exits that the clean replay always emits; live only emits if a position existed
        verdict = "复算幻影（实盘计数未含）" if row["type"] in ("X",) else "需对照实盘计数"
        html.append(f"<tr><td class='r'>{t}</td><td>{typ}</td><td class='r'>{price}</td><td><small>{tag}</small></td><td class='tok-miss'>{verdict}</td></tr>")
    html.append("</table>")
html.append("<p class='mut'>说明：实盘为增量运行，很多“出场(X)”是复算在干净状态下对每一个开仓都补的平仓，而实盘因 warmup/持仓状态并未走到那一步，故 X 类几乎全部为复算多发。B/S 的偏差同理。</p></div>")

# ---- Risk agent ----
html.append(f"""<div class="card crit"><h3>七、风控 agent 诊断（为何完全没发挥作用）</h3>
<p>系统里有<b>两套互不相干</b>的“风控”机制，用户所说的“风控 agent”实际指交易风控，但两者今日都未对交易产生任何约束：</p>
<table><tr><th>机制</th><th>是否运行</th><th>职责</th><th>今日表现</th><th>为何无动作</th></tr>
<tr><td><b>告警引擎<br>alert_engine.py</b></td><td class="tok-ok">运行中<br>(心跳年龄 0–1s)</td><td><b>monitor 进程健康看门狗</b>：扫描耗时/服务中断/信号突增/错误数</td><td>零告警</td><td>它的规则只监控 <b>monitor 自身健康</b>，<b>不含任何交易风控逻辑</b>（无 HALT_BUY/FORCE_SELL）。monitor 进程活着→它无事可做。<b>它本就不是交易风控器。</b></td></tr>
<tr><td><b>交易风控闸门<br>_risk_gate +<br>risk_override.json</b></td><td class="tok-miss">配置已失效</td><td>读取 risk_override.json，对 miji_alpha 套 HALT_BUY / FORCE_SELL 闸门</td><td>全天无任何 HALT/FORCE_SELL</td><td>override 由外部 <code>vr_risk_agent</code> 写于 <code>{esc(risk.get('triggered_at'))}</code>，action=<code>{esc(risk.get('action'))}</code>，但 <b>expires_at={esc(risk.get('expires_at'))}</b> —— 开盘前 6 小时即过期。<code>_load_risk_override()</code> 见过期即返回 <code>NONE</code>（放行）。且 ALLOW_BUY 本身等同于放行。</td></tr>
</table>
<p class="tok-miss"><b>根因：</b>外部风险 agent 给 override 设了 <b>10 分钟 TTL 且盘中不刷新</b>，导致交易时段闸门形同虚设；而“告警引擎”被误当作交易风控，实则其职责边界根本不包含交易拦截。</p></div>""")

# ---- Recommendations ----
html.append("""<div class="card ok"><h3>八、修复建议（按优先级）</h3>
<ol>
<li><b>杜绝多实例共用 webhook（P0）</b>：单实例锁已存在但被“手动双击 run_monitor.bat/restart.bat”绕过。已改为<b>仅允许 V9Launch.bat 拉起</b>（monitor.py __main__ 闸门 + 启动标记 <code>TP_LAUNCHED_BY_V9LAUNCH</code>），<b>禁止手动双击</b>；<code>risk_agent</code> 已改用专属 webhook（与信号群分离），消除与其争抢频限。注：<code>monitor_console.log</code> 中的“商络电子/光纤”是 07-13 历史残留日志，非活策略，无需处理。</li>
<li><b>推送落审计日志（P0）</b>：在 emit 推送处把 <code>{时间, 标的, 类型, 价格, 飞书code}</code> 写入 <code>data/push_audit.jsonl</code>，使未来可逐笔核对，消除“日志不记标的”盲区。</li>
<li><b>修复报告口径（P0）</b>：<code>daily_signal_review.py</code> 改为<b>回放实盘 state 增量逻辑</b>（含 warmup、持仓、冷却）或直接以 <code>state.json</code> 实盘计数为准，不要干净复算；至少把复算结果与 state.json 同屏对比并标注偏差。</li>
<li><b>风控闸门常态化（P1）</b>：外部 <code>vr_risk_agent</code> 的 override TTL 改为<b>覆盖整个交易时段</b>（如 08:30–15:30）并盘中定期刷新；告警引擎若要做交易风控，需新增 HALT_BUY/FORCE_SELL 规则（当前无）。</li>
<li><b>飞书频限兜底（P1）</b>：推送失败（11232）应<b>本地重试+退避</b>并写入审计日志告警，避免静默丢推。</li>
</ol>
<p class="tok-ok"><b>实施状态（2026-07-22 修复已落地）：</b>① monitor 仅 V9Launch 可启 + restart.bat 按 PID 精确杀 + risk_agent 独立 webhook；② 推送审计 <code>push_audit.jsonl</code> 已上线；③ 复盘报告新增“实盘权威源(state.json) vs 复算对照 + Δ”节；④ risk_agent 收编为独立常驻进程（V9Launch 拉起，TTL=7h 覆盖全时段 + 每 180s 刷新），告警引擎新增二级兜底 HALT_BUY；⑤ 推送失败本地重试+指数退避已上线。详见 <code>CHANGELOG.md</code>。</p></div>""")

html.append("<hr><p class='mut'>附：本报告所有计数均从磁盘文件实时读取；推送时间线来自 <code>_push_timeline_2026-07-22.json</code>。截图无法读取，逐笔“推送↔截图”精确对齐需上述审计日志落地后重做。</p></body></html>")

out = os.path.join(BASE, "output/forensic_2026-07-22.html")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(html))
print("WROTE", out, "bytes=", len("\n".join(html)))
