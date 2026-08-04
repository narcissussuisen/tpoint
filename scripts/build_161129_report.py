"""
构建 161129.SZ 2026-07-21 零信号复盘 HTML 报告
数据来源: 复刻生产路径(1m + strict MACD 门控 + PC=昨收) 实拉当日/对比日行情
输出: output/161129_0721_review.html
"""
import sys, json
import numpy as np
import pandas as pd
sys.path.insert(0, 'core')
from datasource import MootdxDataSource
from miji_alpha import (compute_miji_indicators, detect_miji_signals,
                        check_miji_trigger, gravity_signal, macd_divergence_signal,
                        VWAP_DEV_BUY, VWAP_DEV_SELL, LOCAL_W)

SYM = '161129.SZ'
TODAY = '2026-07-21'
COMPARE = ['2026-07-20', '2026-07-17']
ds = MootdxDataSource()

def get_pc(day):
    d = ds.get(SYM, period='1d', count=40)
    if d is None: return None
    m = {dt: cl for dt, cl in zip(d['trade_date'].tolist(), d['close'].tolist())}
    sd = sorted(m)
    if day in m:
        idx = sd.index(day); return float(m[sd[idx-1]]) if idx>0 else float(m[day])
    before = [x for x in sd if x < day]
    return float(m[before[-1]]) if before else float(d['close'].iloc[-1])

def load(day, today=False):
    df = ds.intraday(SYM) if today else ds.historical_1m(SYM, day, offset=1000)
    if df is None or len(df)==0: return None, None, None
    df = df.sort_values('trade_time').reset_index(drop=True)
    o=df['open'].values.astype(float); h=df['high'].values.astype(float)
    lo=df['low'].values.astype(float); c=df['close'].values.astype(float)
    v=df['volume'].values.astype(float)
    has_vol=bool(np.sum(v)>0); pc=get_pc(day)
    data=compute_miji_indicators(o,h,lo,c,v,pc,has_vol=has_vol)
    return df, data, pc

def analyze(day, df, data, pc, today=False):
    n=data['n']; c=data['c']; vwap=data['vwap']; atr=data['atr']
    dev=np.where(vwap>0,(c-vwap)/vwap*100,0.0)
    nb=ns=mb=ms=blk_b=blk_s=early=strict_b=strict_s=0
    band_b_full=[None]*n; band_s_full=[None]*n; near=[]
    off_last_b=-999; off_last_s=-999; off_b=0; off_s=0   # 'off'模式(纯引力)模拟
    mark=[]
    for i in range(2,n):
        if atr[i]<=0: continue
        lower=vwap[i]-VWAP_DEV_BUY*atr[i]; upper=vwap[i]+VWAP_DEV_SELL*atr[i]
        bb=(lower-vwap[i])/vwap[i]*100; bs=(upper-vwap[i])/vwap[i]*100
        band_b_full[i]=bb; band_s_full[i]=bs
        gf,gdev=gravity_signal(c,vwap,atr,i)
        mf,md=macd_divergence_signal(df['high'].values.astype(float),df['low'].values.astype(float),c,data['dif'],data['dea'],data['hist'],i)
        bt,st_,bd,sd_,_=check_miji_trigger(data,i,macd_gate_mode='strict')
        if gf==1:
            nb+=1; mark.append(('B',i,float(dev[i]),float(gdev)))
            if i<LOCAL_W: early+=1
            if mf!=1:
                blk_b+=1
                if len(near)<8: near.append({'t':str(df['trade_time'].iloc[i]),'price':round(float(c[i]),4),'dev':round(float(gdev),3),'macd':md or '无背离','side':'B'})
        elif gf==-1:
            ns+=1; mark.append(('S',i,float(dev[i]),float(gdev)))
            if mf!=-1:
                blk_s+=1
                if len(near)<8: near.append({'t':str(df['trade_time'].iloc[i]),'price':round(float(c[i]),4),'dev':round(float(gdev),3),'macd':md or '无背离','side':'S'})
        if mf==1: mb+=1
        elif mf==-1: ms+=1
        if bt: strict_b+=1
        if st_: strict_s+=1
        # off 模式模拟(纯引力, 含 SIGNAL_GAP=8, 每日上限12)
        if gf==1 and (i-off_last_b)>=8 and off_b<12:
            off_b+=1; off_last_b=i
        if gf==-1 and (i-off_last_s)>=8 and off_s<12:
            off_s+=1; off_last_s=i
    sigs=detect_miji_signals(data,pc,macd_gate_mode='strict')
    day_chg=(c[-1]/pc-1)*100 if pc else 0
    rng=(c.max()-c.min())/c.min()*100 if c.min()>0 else 0
    flat=int(np.sum(df['high'].values.astype(float)==df['low'].values.astype(float)))
    return {
        'day':day,'today':today,'n':n,
        't0':str(df['trade_time'].iloc[0]),'t1':str(df['trade_time'].iloc[-1]),
        'pc':round(pc,4) if pc else None,
        'o':round(float(c[0]),4),'hi':round(float(c.max()),4),'lo':round(float(c.min()),4),'cl':round(float(c[-1]),4),
        'day_chg':round(float(day_chg),3),'range':round(float(rng),3),
        'vwap_last':round(float(vwap[-1]),4),
        'atr_mean':round(float(np.mean(atr[atr>0])),5),'atr_last':round(float(atr[-1]),5),
        'dev_min':round(float(dev.min()),3),'dev_max':round(float(dev.max()),3),'dev_last':round(float(dev[-1]),3),
        'band_b':round(float(np.mean([x for x in band_b_full if x is not None])),3),'band_s':round(float(np.mean([x for x in band_s_full if x is not None])),3),
        'grav_b':nb,'grav_s':ns,'macd_b':mb,'macd_s':ms,
        'blocked_b':blk_b,'blocked_s':blk_s,
        'early_grav':early,'strict_b':strict_b,'strict_s':strict_s,
        'off_b':off_b,'off_s':off_s,
        'n_sig':len(sigs),
        'signals':[{'type':s['type'],'t':str(df['trade_time'].iloc[s['idx']]),'price':s['price'],'chg':s['chg'],'detail':s['detail']} for s in sigs],
        'near':near,
        'flat':flat,
        'series':{'i':list(range(n)),'dev':[round(float(x),3) for x in dev],
                  'bb':[None if x is None else round(float(x),3) for x in band_b_full],'bs':[None if x is None else round(float(x),3) for x in band_s_full],
                  'mark':[{'side':m[0],'i':m[1],'dev':round(m[2],3),'gdev':round(m[3],3)} for m in mark]},
    }

R={}
df,data,pc=load(TODAY,today=True)
R[TODAY]=analyze(TODAY,df,data,pc,today=True)
for d in COMPARE:
    f,d2,p=load(d)
    if f is None: continue
    R[d]=analyze(d,f,d2,p)

# ---------- SVG 图表 (今日 dev% + 引力带 + 触发点) ----------
def build_svg(s):
    W,H=920,340; pad=46
    xs=s['series']['i']; dev=s['series']['dev']; bb=s['series']['bb']; bs=s['series']['bs']
    bb2=[x for x in bb if x is not None]; bs2=[x for x in bs if x is not None]
    ymin=min(min(dev),min(bb2))-0.5; ymax=max(max(dev),max(bs2))+0.5
    n=len(xs)
    def X(i): return pad+(W-2*pad)*i/(n-1 if n>1 else 1)
    def Y(v): return pad+(H-2*pad)*(ymax-v)/(ymax-ymin)
    # band polygon (bands defined for i>=2)
    bi=[i for i in range(2,n) if bs[i] is not None and bb[i] is not None]
    top=[f"{X(i):.1f},{Y(bs[i]):.1f}" for i in bi]
    bot=[f"{X(i):.1f},{Y(bb[i]):.1f}" for i in reversed(bi)]
    bandpoly=" ".join(top+bot)
    dev_pts=" ".join(f"{X(i):.1f},{Y(dev[i]):.1f}" for i in range(n))
    svg=[f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="Consolas,monospace">']
    svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0f1420"/>')
    # zero line
    svg.append(f'<line x1="{pad}" y1="{Y(0):.1f}" x2="{W-pad}" y2="{Y(0):.1f}" stroke="#3a4156" stroke-width="1" stroke-dasharray="4 4"/>')
    svg.append(f'<text x="{W-pad+4}" y="{Y(0)+4:.1f}" fill="#7c879b" font-size="11">0%</text>')
    # bands
    if bandpoly:
        svg.append(f'<polygon points="{bandpoly}" fill="#1b3a2e" opacity="0.45"/>')
        svg.append(f'<line x1="{pad}" y1="{Y(bs[bi[0]]):.1f}" x2="{W-pad}" y2="{Y(bs[bi[-1]]):.1f}" stroke="#c0563f" stroke-width="1" stroke-dasharray="3 3"/>')
        svg.append(f'<line x1="{pad}" y1="{Y(bb[bi[0]]):.1f}" x2="{W-pad}" y2="{Y(bb[bi[-1]]):.1f}" stroke="#3f9bc0" stroke-width="1" stroke-dasharray="3 3"/>')
    svg.append(f'<text x="{W-pad+4}" y="{Y(bs[-1])+4:.1f}" fill="#c0563f" font-size="11">上轨(+{abs(s["band_s"])}%)</text>')
    svg.append(f'<text x="{W-pad+4}" y="{Y(bb[-1])+4:.1f}" fill="#3f9bc0" font-size="11">下轨(-{abs(s["band_b"])}%)</text>')
    # dev line
    svg.append(f'<polyline points="{dev_pts}" fill="none" stroke="#d9b44a" stroke-width="1.6"/>')
    # markers: gravity fired bars
    for m in s['series']['mark']:
        col='#ff6b5e' if m['side']=='B' else '#ffd166'
        svg.append(f'<circle cx="{X(m["i"]):.1f}" cy="{Y(m["dev"]):.1f}" r="2.2" fill="{col}" opacity="0.55"/>')
    # axis labels
    svg.append(f'<text x="{pad}" y="{H-12}" fill="#7c879b" font-size="11">09:30</text>')
    svg.append(f'<text x="{W-pad-30}" y="{H-12}" fill="#7c879b" font-size="11">{s["t1"][11:16]}</text>')
    svg.append(f'<text x="6" y="{Y(ymax)+4:.1f}" fill="#7c879b" font-size="11">{ymax:.1f}</text>')
    svg.append(f'<text x="6" y="{Y(ymin)+4:.1f}" fill="#7c879b" font-size="11">{ymin:.1f}</text>')
    svg.append('</svg>')
    return "".join(svg)

svg_today=build_svg(R[TODAY])

def row(d):
    r=R[d]; cls=' class="hl"' if r['today'] else ''
    return (f"<tr{cls}><td>{d}{' (今日/盘中)' if r['today'] else ''}</td>"
            f"<td>{r['n']}</td><td>{r['day_chg']:+}%</td><td>{r['range']}%</td>"
            f"<td>{r['dev_min']}% / {r['dev_max']}%</td>"
            f"<td>{r['early_grav']}</td><td>{r['grav_b']} / {r['grav_s']}</td>"
            f"<td>{r['macd_b']} / {r['macd_s']}</td>"
            f"<td>{r['blocked_b']} / {r['blocked_s']}</td>"
            f"<td>{r['off_b']} / {r['off_s']}</td>"
            f"<td><b>{r['n_sig']}</b></td></tr>")

rows="".join(row(d) for d in [TODAY]+COMPARE if d in R)
t=R[TODAY]
cmp_rows=""
for d in COMPARE:
    if d not in R: continue
    c=R[d]
    cmp_rows+=f"<tr><td>{d}</td><td>{c['n_sig']}</td><td>{c['day_chg']:+}%</td><td>{c['range']}%</td><td>{c['early_grav']}</td><td>{c['macd_b']}/{c['macd_s']}</td><td>{', '.join(s['detail'] for s in c['signals']) or '—'}</td></tr>"

near_rows="".join(
    f"<tr><td>{m['t'][11:16]}</td><td>{'买' if m['side']=='B' else '卖'}</td><td>{m['price']}</td><td>{m['dev']:+}%</td><td>{m['macd']}</td></tr>"
    for m in t['near'])

blocked_total=t['blocked_b']+t['blocked_s']
grav_total=t['grav_b']+t['grav_s']
blocked_pct=round(blocked_total/grav_total*100,1) if grav_total else 0

html=f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>tpoint 161129 2026-07-21 零信号复盘</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0b0f17;color:#d7dde8;font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;line-height:1.6}}
.wrap{{max-width:1040px;margin:0 auto;padding:28px 22px 60px}}
h1{{font-size:23px;margin:0 0 4px;color:#fff}}
h2{{font-size:17px;margin:30px 0 10px;color:#e9c46a;border-left:4px solid #e9c46a;padding-left:10px}}
.sub{{color:#8b94a7;font-size:13px;margin-bottom:18px}}
.card{{background:#131a27;border:1px solid #232c3d;border-radius:10px;padding:16px 18px;margin:12px 0}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:150px;background:#131a27;border:1px solid #232c3d;border-radius:10px;padding:14px}}
.kpi .v{{font-size:24px;font-weight:700;color:#fff}}
.kpi .l{{font-size:12px;color:#8b94a7;margin-top:4px}}
.kpi.bad .v{{color:#ff6b5e}} .kpi.warn .v{{color:#ffd166}} .kpi.ok .v{{color:#4cc38a}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{padding:8px 10px;text-align:center;border-bottom:1px solid #202938}}
th{{color:#9aa6bb;font-weight:600;background:#0f1622}}
tr.hl{{background:#1a2230}}
td.l,th.l{{text-align:left}}
.note{{background:#1c1410;border:1px solid #4a3520;color:#f0c674;border-radius:8px;padding:12px 14px;font-size:13px;margin:12px 0}}
.crit{{background:#241015;border:1px solid #5a2530;color:#ff9b90;border-radius:8px;padding:12px 14px;font-size:13px;margin:12px 0}}
code{{background:#0c1118;padding:1px 6px;border-radius:4px;color:#9fd0ff;font-size:12px}}
.chart{{background:#0f1420;border:1px solid #232c3d;border-radius:10px;padding:10px;margin-top:8px}}
.legend{{font-size:12px;color:#8b94a7;margin-top:6px}}
ul{{margin:6px 0 6px 2px;padding-left:18px}} li{{margin:4px 0}}
.pos{{color:#4cc38a}} .neg{{color:#ff6b5e}}
</style></head><body><div class="wrap">
<h1>tpoint 策略 · 161129.SZ（原油LOF易方达）零信号复盘</h1>
<div class="sub">交易日 2026-07-21（盘中至 {t['t1'][11:16]}）｜ 引擎 v9.1.4 ｜ 生产路径：1分钟 + 严格MACD门控 + PC=昨收（{t['pc']}）+ 量价背离禁用</div>

<div class="crit"><b>🔴 核心结论（运营故障为主因，策略过滤为辅因）：</b><br>
① <b>运营故障（主因，且当前仍在发生）</b>：monitor 今日崩溃/重启多次（09:31→09:35→10:07→10:58 致命 OSError→11:24），10:07 轮对 <code>原油LOF易方达</code> 连续抛 <code>compute exception: 'NoneType' object has no attribute 'klines'</code>（tf=None）。更关键的证据：当前进程 metrics 的 <code>last_bar_ts=1784642220→21:57</code>（<b>未来时间，明显为假</b>）、<code>scan_duration_s=0.12</code>（远快于正常 1-3s 真实抓取）、且 monitor_console.log <b>自 10:06 后再无输出</b>——证明<b>当前 monitor 仍处在 tf=None 快速失败状态，161129 至今未被真正计算</b>。这是零信号的主因。<br>
② <b>策略条件（即使数据完好也近乎零，反事实）</b>：用真实行情复刻生产引擎（数据可正常拉取），今日 161129 共 {grav_total} 根 bar 触发"引力"超跌/超买，但严格MACD门控要求 <code>i≥15</code> 后须有MACD背离配合，结果 <b>{blocked_total} 根（{blocked_pct}%）被门控拦掉</b>。今日早盘无波动（前15根引力0次），错过"早盘降级gravity-only"这一历史主要出信号窗口。即便 monitor 健康，今日也只会出 0~1 笔。</div>

<h2>一、今日市场条件（161129）</h2>
<div class="kpis">
<div class="kpi"><div class="v">{t['o']}→{t['cl']}</div><div class="l">开→收（昨收 {t['pc']}）</div></div>
<div class="kpi {'neg' if t['day_chg']<0 else 'pos'}"><div class="v">{t['day_chg']:+}%</div><div class="l">当日涨跌</div></div>
<div class="kpi"><div class="v">{t['range']}%</div><div class="l">日内振幅</div></div>
<div class="kpi"><div class="v">{t['dev_min']}% / {t['dev_max']}%</div><div class="l">dev% 区间（最低/最高偏离VWAP）</div></div>
<div class="kpi"><div class="v">{t['atr_mean']}</div><div class="l">ATR均值(1m)</div></div>
</div>
<div class="card">价格确实大幅波动（低见 {t['lo']}、较昨收 -3.4%），dev% 最低 {t['dev_min']}%（深度偏离VWAP），<b>并非"无波动"</b>。问题不在波动缺失，而在波动的<b>形态</b>：下跌发生在 10:30 之后（i≥{LOCAL_W}），此时已进入严格MACD门控区，而该段下跌<b>未伴随MACD底背离</b>。</div>

<h2>二、策略参数过滤过严（严格MACD门控）</h2>
<div class="kpis">
<div class="kpi bad"><div class="v">{grav_total}</div><div class="l">引力触发 bar 数 (B{t['grav_b']}/S{t['grav_s']})</div></div>
<div class="kpi bad"><div class="v">{blocked_total}</div><div class="l">被严格MACD门控拦掉</div></div>
<div class="kpi bad"><div class="v">{blocked_pct}%</div><div class="l">引力触发中被过滤比例</div></div>
<div class="kpi warn"><div class="v">{t['macd_b']}/{t['macd_s']}</div><div class="l">真正形成MACD背离的bar</div></div>
<div class="kpi ok"><div class="v">{t['off_b']}/{t['off_s']}</div><div class="l">若改'off'纯引力会触发(上限12)</div></div>
</div>
<div class="note"><b>门控逻辑（生产默认 strict）：</b> <code>i &lt; {LOCAL_W}</code> 时降级为"纯引力"(gravity 即触发)；<code>i ≥ {LOCAL_W}</code> 后 <b>必须 m_factor 同向</b>（价格新低+MACD绿柱收缩/金叉，或价格新高+红柱缩短/死叉）。今日早盘(09:31-09:45)价格贴近VWAP、引力 0 次，未用上降级窗口；10:30 后的深跌又缺MACD背离配合 → 几乎全被拦。</div>
<div class="card"><b>近失案例（引力已触发、MACD未配合 → 被拦）：</b>
<table><thead><tr><th>时间</th><th>方向</th><th>价格</th><th>引力dev</th><th>MACD状态</th></tr></thead><tbody>{near_rows}</tbody></table>
<div class="legend">例：10:37 价格 1.911、偏离VWAP −1.70%（典型超跌买点），但MACD无底背离 → 严格门控拦掉。若用 'off'（纯引力）模式，此类 bar 会直接触发。</div></div>

<h2>三、与近期正常出信号日的差异</h2>
<table><thead><tr><th>日期</th><th>bar数</th><th>当日涨跌</th><th>振幅</th><th>dev%区间</th><th>早盘引力(i&lt;{LOCAL_W})</th><th>引力触发B/S</th><th>MACD背离B/S</th><th>被门控拦B/S</th><th>纯引力会触发B/S</th><th>实发信号</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="card"><b>差异要点：</b>
<ul>
<li><b>07-20（B1/S2）</b>：当日 +9.98% 大涨、早盘即高开波动，<b>前15根就有引力触发</b>（降级窗口生效）产出首笔 B@09:34；午后 S@13:40 由"引力+MACD红柱缩短"共振触发。</li>
<li><b>07-17（5笔）</b>：振幅 6.71%、dev 最低 −4.20%，深度超跌段<b>与MACD绿柱收缩共振</b>（B@10:45 dev−3.34% +绿柱收缩；B@13:43 dev−1.84% +绿柱收缩），既吃到早盘降级窗口也吃到MACD共振。</li>
<li><b>今日 07-21</b>：虽振幅 6.23% 不低，但<b>早盘0波动</b>（错过降级窗口），深跌段(10:30+)缺MACD背离配合 → 降级窗口与共振窗口"两头不靠"。</li>
</ul></div>

<h2>四、数据支撑 · 今日 dev% 与引力带</h2>
<div class="chart">{svg_today}</div>
<div class="legend">黄线=价格偏离VWAP的 dev%；蓝/红虚线=引力触发带(±{abs(t['band_b'])}% 量级，随ATR浮动)；绿色带区=触发带内。橙/红小点=引力触发 bar（红=买超跌 / 黄=卖超买）。可见价格多次跌破下轨（应触发买），但均被严格MACD门控拦掉。</div>

<h2>五、运营故障细节（必须修复）</h2>
<div class="crit">monitor 今日状态机（来自 monitor_lifecycle.log / monitor_fatal.log / monitor_console.log / metrics.json）：
<ul>
<li>09:31:55 启动(pid 7252) → 09:35:17 僵锁清理重启 → 10:07:10 再重启 → <b>10:58:49 致命退出 OSError(22)</b> → 11:24:06 重启(当前 pid 13792)。</li>
<li>10:07 这一轮对 <code>原油LOF易方达</code> 与 <code>华虹宏力</code> 连续打印 <code>compute exception: 'NoneType' object has no attribute 'klines'</code> —— 即 <code>tf</code>(数据源) 为 None，<b>两标的均未计算</b>。</li>
<li><b>当前进程仍在坏状态（铁证）</b>：实时 metrics 显示 <code>last_bar_ts=1784642220→21:57</code>（未来时间，明显为假）、<code>scan_duration_s=0.12</code>（正常真实抓取需 1-3s，0.12s 即快速失败）、<code>signals=0</code>；且 monitor_console.log <b>自 10:06 后再无任何输出</b>。三重证据指向<b>当前 monitor 仍是 tf=None 快速失败</b>，161129 全天未被真正计算。</li>
<li>根因：monitor.py 仅在"当日首次刷新"初始化 tf；中途崩溃重启后 <code>_daily_refreshed_date</code> 已是今日，tf 长期为 None（代码 924-927 已加恢复分支，但今日仍触发，说明恢复分支未稳定生效，疑与 fatal 崩溃循环有关）。</li>
<li>⚠️ <b>自检盲区</b>：11:27 的盘中自检报"健康（27 PASS）"是<b>误报</b>——它只查进程存活 + 心跳，未校验 last_bar_ts 是否合理、是否真算出信号。建议自检增加对 <code>last_bar_ts</code> 时效性（距 now &lt; 5min）与 <code>scan_duration_s</code> 合理性（&gt; 0.5s）的断言。</li>
</ul></div>

<h2>六、结论与建议</h2>
<div class="card">
<b>为什么今天零信号？</b> 直接原因是 monitor 运行故障（tf=None + 崩溃循环）使 161129 未被计算；即使排除故障，严格MACD门控 + 今日"早盘无波动/深跌无背离"的市况组合，也会使实发信号趋近于 0~1 笔。<br><br>
<b>建议：</b>
<ol>
<li><b>立即重启 monitor（关键）</b>：当前进程(13792)仍 tf=None 快速失败，须清理锁文件后重启，并验证三项恢复指标——<code>last_bar_ts</code> 变为真实近期时间戳、<code>scan_duration_s</code> 回升至 1-3s、monitor_console.log 重新开始打印"扫描完成"。否则 161129/688347 盘中均无信号。</li>
<li><b>考虑 161129 的MACD门控模式</b>：该标的是低换手 LOF，MACD背离稀少。若希望更敏感，可对 161129 单独设 <code>MACD_GATE_MODE=off</code>（纯引力）或 <code>floor</code>（strict+价格地板/天花板），今日可多捕获约 {t['off_b']} 笔买 / {t['off_s']} 笔卖。</li>
<li><b>早盘窗口价值</b>：历史信号多来自开盘前15根波动，今日缺失；属偶发市况，非参数问题。</li>
<li><b>加固 tf 生命周期</b>：把"tf is None 则重建"从每轮前置检查改为异常时自动重连，并修复 Windows 控制台 gbk 编码导致的 fatal 崩溃（OSError 22）。</li>
</ol>
</div>
<div class="sub">生成：复刻生产引擎逐bar重放真实行情 · tpoint v9.1.4</div>
</div></body></html>"""

with open('output/161129_0721_review.html','w',encoding='utf-8') as f:
    f.write(html)
with open('output/161129_0721_review.json','w',encoding='utf-8') as f:
    json.dump({k:{kk:vv for kk,vv in v.items() if kk!='series'} for k,v in R.items()}, f, ensure_ascii=False, indent=2)
print("OK -> output/161129_0721_review.html")
print(f"今日: grav_total={grav_total} blocked={blocked_total}({blocked_pct}%) strict_raw={t['strict_b']}/{t['strict_s']} off={t['off_b']}/{t['off_s']} n_sig={t['n_sig']}")
