# -*- coding: utf-8 -*-
"""
build_gap_analysis.py — tpoint vs 卡方 T0 目标系统差距分析报告生成器
输入：内部固化的差距矩阵（代码核对证据 + PPT/xlsx 解析事实）
产出：output/gap_analysis_YYYY-MM-DD.html（深色主题单文件）
"""
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
OUT = os.path.join(BASE, 'output', f'gap_analysis_{DATE}.html')


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ========== 差距矩阵数据 ==========
# 维度: (维度名, 目标能力, 当前能力, 差距评级, 证据)
MATRIX = [
    ('策略因子', '动量(6/12月)+行业+情绪+波动率+流动性 5因子预测模型 (PPT S4)',
     'VWAP引力(dev=0.6)+MACD背离+价格地板/天花板；量价背离因子已禁用(VOL_DIV_ENABLED=False)',
     'P0', 'miji_alpha.py:26-51, 399'),
    ('周期框架', '短周期持股10min + 长周期1h 双周期 (PPT S14-16)',
     '仅 1m 单周期扫描(15s轮询)；5m/15m 多周期共振研究(v9.3.0)已证伪不进入生产',
     'P1', 'monitor.py:124 SCAN_INTERVAL=15'),
    ('标的筛选', '三条件筛选(近1月日均成交额≥50亿/换手5-15%/振幅5-20%)+40只名单 (PPT S9)',
     'watchlist.json 手工维护 4 只(持仓驱动)；无全市场筛选工具',
     'P0', 'data/watchlist.json'),
    ('绩效统计', '20日/5日/当日收益率+开仓率+胜率+Level星级 (xlsx 17列)',
     'aggregate_metrics 仅笔数/胜率/均盈/均亏/盈亏比/复利净值/平均持仓',
     'P0', 'exit_manager.py:149-176'),
    ('回测能力', '全市场 5002 只批量回测 (xlsx)',
     '仅 watchlist 4 只回测；无全市场批量回测(数据/算力受限)',
     'P0', 'backtest/'),
    ('自动执行', '批量自动下单(委托单执行, 客户案例永鼎股份全天22笔)',
     '仅飞书信号推送+人工执行；无券商接口',
     'P1', 'monitor.py emit_signal'),
    ('风控', '收盘/开盘股数一致校验、风险敞口控制 (PPT S1)',
     'risk_override.json 顶层闸门(regime/action/risk_score)+数据源中断静默告警+静默零信号检测',
     'P2', 'monitor.py:679, 1262-1288'),
    ('数据流程', '全市场实时行情(机构级)',
     'mootdx TCP 7709 主源 + 腾讯分时兜底(无真实OHLC, open=前收)；盘中 DNS 间歇失败无退避重试',
     'P0', 'datasource.py:306-368'),
    ('监控投递', '程序化批量执行+状态同步',
     '飞书卡片推送+push_audit.jsonl审计+push_pending.jsonl补发(REPLAY_MAX_AGE_S=600新鲜度闸门)',
     'P2', 'monitor.py:277-325, 357'),
    ('复盘报告', '日度绩效报告(收益/胜率/开仓率)',
     '3步流水线(daily_signal_review→review_charts→build_review_html)；含实盘/复算对照雏形',
     'P2', 'scripts/daily_signal_review.py'),
]

# ========== 改进清单 ==========
ITEMS = [
    # (编号, 优先级, 改进项, 现状, 目标差距, 建议方案, 涉及文件)
    ('P0-1', 'P0', '数据源韧性：腾讯分时兜底加退避重试',
     '_tencent_intraday_fallback 单次请求即放弃；07-31 盘中 getaddrinfo 间歇失败 4 标的全失联, 全天 22 信号仅推 7 条(漏推≈50%)',
     '盘中任何单点抖动不得静默吞信号；降级链须可靠',
     '腾讯分时请求复用 _retry_with_backoff(3次/1s-4s)；失败计数与 mootdx 独立；连续 N 轮失败降级为"暂停该标的扫描+告警"而非静默跳过',
     'core/datasource.py:306'),
    ('P0-2', 'P0', '首扫抑制窗口收窄',
     "首扫 target_t='13:00'/'09:30'(monitor.py:1307)只跳过开盘前 bar；09:30 后重启会把已过信号重扫重发(07-30 午后重启爆发9条重放, 已加补发闸门但首扫抑制仍误伤真实信号: 07-31 09:33 金山办公 S@253.89 被吞)",
     '重启不丢真实信号、不重发历史信号',
     "target_t 改为 (now-3min)：首扫只重建持仓状态+跳过 3 分钟前的历史 bar, 3 分钟内的真实新信号正常推送",
     'core/monitor.py:1307'),
    ('P0-3', 'P0', '绩效统计扩展(卡方风格)',
     'aggregate_metrics 无年化/最大回撤/夏普/开仓率/Level星级',
     'xlsx 17 列指标(20日/5日/当日收益、开仓率、胜率、Level)可逐项对照',
     '在 aggregate_metrics 基础上扩展: 年化(按交易日折算)、最大回撤(复利净值序列)、夏普(日收益)、开仓率(信号数/交易bar数)；新增卡方风格周报模块按 20日/5日窗口滚动统计',
     'core/exit_manager.py:149'),
    ('P0-4', 'P0', '全市场标的筛选器',
     'watchlist 手工 4 只；无从全市场挑标的的能力(用户澄清: 4只是持仓驱动, 不代表系统不普适, 差距在缺筛选工具)',
     'PPT S9 三条件: 近1月日均成交额≥50亿/换手率5-15%/振幅5-20% → 40只名单; xlsx 5002只可作候选池',
     '新增 scripts/market_screener.py: 从 mootdx/腾讯拉全市场日K, 按三条件过滤+按 xlsx Level 排序; 输出候选池 JSON, 人工确认后并入 watchlist',
     'scripts/(新增)'),
    ('P1-1', 'P1', '多周期因子',
     '仅 1m 单周期; v9.3.0 已证伪 5m/15m 共振无泛化 edge(PF 0.605), 但那是"策略收益"维度, 周期因子作为监控信号仍有价值',
     '短10min/长1h 双周期(PPT S14-16)',
     '监控场景引入 5m/1h 周期信号作为"大方向参考"标注在飞书卡片上, 不与 1m 信号融合(避免重蹈 v9.3.0 证伪覆辙)',
     'core/miji_alpha.py'),
    ('P1-2', 'P1', '因子扩展评估',
     '仅 VWAP/MACD/价格地板 3 类量价因子; 无动量/行业/情绪/波动率/流动性因子',
     'PPT S4 5因子预测模型',
     '评估波动率因子(ATR%)与流动性因子(量能持续性)接入 miji_alpha 的性价比; 动量/行业/情绪因子需要更多数据源(财报/板块/舆情), 列为中期研究项',
     'core/miji_alpha.py'),
    ('P1-3', 'P1', '复算口径对齐',
     '复算引擎用 mootdx 真实 1m(复盘权威); 但盘中腾讯合成数据(open=前收, high/low=极值)与实盘口径偏差(07-31 588000 复算 09:36 B@1.783 vs 实盘 09:35 B@1.788)',
     '盘中与盘后口径一致, 可逐条对照',
     '复盘引擎当日强制 mootdx 真实数据并缓存 CSV; 腾讯合成数据仅盘中实时用, 复盘时标注"合成数据"来源',
     'scripts/daily_signal_review.py'),
    ('P1-4', 'P1', '自动交易执行评估(远期)',
     '仅信号推送+人工执行; 无券商接口/无盘口挂单',
     'PPT 批量自动委托(客户案例22笔/天)',
     '用户已拍板: 保持信号推送, 不升级自动下单; 远期若接入, 需券商API+风控前置+失败回滚, 列为 P2 远期备注',
     '—'),
    ('P2-1', 'P2', '高波动保护',
     '07-30/31 振幅 9.8%/9.4%(基线5.6%), 金山办公 16%; 4条失效信号全部"均线引力被反向突破"',
     '高波/高换手/高振幅标的下信号仍须有效(PPT S5-6: 高波高换手是T0最好环境, 但需参数自适应)',
     '振幅>8% 时: ①放大 VWAP_DEV 带宽 ②floor 门控 FLOOR_DEV_PCT 提高 ③或暂停引力类信号只保留 MACD 背离',
     'core/miji_alpha.py:26-51'),
    ('P2-2', 'P2', '实盘/复算 diff 报告',
     '复盘报告含实盘 vs 复算对照雏形, 但无自动化 diff 汇总',
     '每次复盘自动生成"实盘推送 vs 复算信号"差异清单(漏推/错推/时间差)',
     'daily_signal_review 输出 diff 表: 实盘 state.json 计数 vs 复算信号清单, 标注缺失/新增/时间偏移, 汇总到复盘 HTML 章节二',
     'scripts/daily_signal_review.py'),
    ('P2-3', 'P2', '复盘报告对齐卡方指标',
     '复盘报告章节: 实盘投递分类/信号清单/有效性/失效原因/整体+5日基线/行情图',
     '增加 20日/5日开仓率、胜率、Level 星级展示',
     '在复盘 HTML 增加"绩效统计"卡片, 复用 P0-3 扩展后的统计输出',
     'scripts/build_review_html.py'),
]

# ========== xlsx 数据对照 ==========
XLSX_STATS = [
    ('标的池规模', '5002 只全市场批量回测', '仅 4 只 watchlist', 'P0'),
    ('年化收益率分布', '中位 5.25% / 均值 18.55% / P90 71.92% (右偏, 头部极强)',
     'tpoint 无年化统计口径(aggregate_metrics 无此指标)', 'P0'),
    ('20日开仓率', '≥50% 仅 18.3%, ≥80% 仅 4.1% → 大部分标的不适合做T',
     'tpoint 无开仓率统计; 无法判断当前 4 只标的处于什么水平', 'P0'),
    ('20日胜率', '中位 61.0%, ≥60% 占 54.2%',
     'tpoint 复盘胜率 69.2%(07-31) 但样本小且口径不同', 'P1'),
    ('Level 星级×年化', '1星 -7.3% / 2星 2.6% / 3星 24.4% / 4星 41.3% / 5星 118.7%',
     'tpoint 无 Level 评级体系', 'P0'),
    ('PPT S9 名单命中', '40 只目标名单 → xlsx 命中 39 只 (未命中=华虹公司)',
     'tpoint watchlist 4 只 → xlsx 命中仅 688111.SH (Level3, 20日收益1.86%, 年化22.58%, 开仓率57.94%, 胜率50%)', 'P0'),
]

# ========== HTML 模板 ==========
def _items_group(items, prio):
    """按优先级过滤并渲染改进清单条目"""
    out = ''
    for num, p, title, cur, gap, plan, files in items:
        if p != prio:
            continue
        p_cls = {'P0': 'p0', 'P1': 'p1', 'P2': 'p2'}.get(p, 'p2')
        out += f'''<div class="item {p_cls}">
            <div class="item-head">
                <span class="item-num">{esc(num)}</span>
                <span class="item-prio badge-{p}">{esc(p)}</span>
                <span class="item-title">{esc(title)}</span>
            </div>
            <table class="item-table">
                <tr><td class="lbl">现状</td><td>{esc(cur)}</td></tr>
                <tr><td class="lbl">目标差距</td><td>{esc(gap)}</td></tr>
                <tr><td class="lbl">建议方案</td><td>{esc(plan)}</td></tr>
                <tr><td class="lbl">涉及文件</td><td class="mono">{esc(files)}</td></tr>
            </table>
        </div>'''
    return out


def build_html():
    date_disp = DATE
    # 矩阵行
    matrix_rows = ''
    for name, target, cur, grade, ev in MATRIX:
        grade_cls = {'P0': 'g-p0', 'P1': 'g-p1', 'P2': 'g-p2'}.get(grade, 'g-p2')
        matrix_rows += f'''<tr>
            <td class="dim">{esc(name)}</td>
            <td>{esc(target)}</td>
            <td>{esc(cur)}<div class="ev">📎 {esc(ev)}</div></td>
            <td><span class="badge {grade_cls}">{esc(grade)}</span></td>
        </tr>'''

    # 改进清单行（按优先级分组）
    items_html = _items_group(ITEMS, 'P0') + _items_group(ITEMS, 'P1') + _items_group(ITEMS, 'P2')

    # xlsx 对照行
    xlsx_rows = ''
    for name, target, cur, grade in XLSX_STATS:
        g_cls = {'P0': 'g-p0', 'P1': 'g-p1', 'P2': 'g-p2'}.get(grade, 'g-p2')
        xlsx_rows += f'''<tr>
            <td class="dim">{esc(name)}</td>
            <td>{esc(target)}</td>
            <td>{esc(cur)}</td>
            <td><span class="badge {g_cls}">{esc(grade)}</span></td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tpoint × 卡方 T0 差距分析 · {esc(date_disp)}</title>
<style>
:root {{
  --bg: #0f1419; --panel: #1a222c; --panel2: #141b24;
  --line: #2b3644; --txt: #dbe4ee; --dim: #8b98a8;
  --red: #ff5f5f; --green: #3ddc84; --orange: #ffab40;
  --blue: #4da3ff; --purple: #b07bff; --cyan: #38d6d0;
  --p0: #ff5f5f; --p1: #ffab40; --p2: #4da3ff;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--bg); color: var(--txt);
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  line-height: 1.6; padding: 28px 20px 60px; }}
.wrap {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-size: 26px; margin-bottom: 4px; }}
.sub {{ color: var(--dim); font-size: 13px; margin-bottom: 24px; }}
h2 {{ font-size: 19px; margin: 36px 0 14px; padding-left: 10px;
  border-left: 4px solid var(--blue); }}
h3 {{ font-size: 15px; margin: 18px 0 8px; color: var(--cyan); }}
.card {{ background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }}
.kpi-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 10px 0 4px; }}
.kpi {{ flex: 1; min-width: 150px; background: var(--panel2);
  border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }}
.kpi .v {{ font-size: 22px; font-weight: 700; }}
.kpi .k {{ font-size: 12px; color: var(--dim); margin-top: 2px; }}
.v-red {{ color: var(--red); }} .v-green {{ color: var(--green); }}
.v-orange {{ color: var(--orange); }} .v-blue {{ color: var(--blue); }}
.v-purple {{ color: var(--purple); }} .v-cyan {{ color: var(--cyan); }}

table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ border: 1px solid var(--line); padding: 8px 10px; text-align: left;
  vertical-align: top; }}
th {{ background: var(--panel2); color: var(--cyan); font-weight: 600;
  white-space: nowrap; }}
td.dim {{ white-space: nowrap; font-weight: 600; color: var(--blue); }}
.ev {{ color: var(--dim); font-size: 11px; margin-top: 4px; font-family: Consolas, monospace; }}
.badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 700; }}
.g-p0 {{ background: rgba(255,95,95,.15); color: var(--p0); border: 1px solid var(--p0); }}
.g-p1 {{ background: rgba(255,171,64,.15); color: var(--p1); border: 1px solid var(--p1); }}
.g-p2 {{ background: rgba(77,163,255,.15); color: var(--p2); border: 1px solid var(--p2); }}

.arch {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
.arch-col {{ background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 14px 16px; }}
.arch-col.tpoint {{ border-color: var(--blue); }}
.arch-col.kf {{ border-color: var(--purple); }}
.arch-col h3 {{ margin-top: 0; }}
.arch-col ul {{ list-style: none; font-size: 12.5px; }}
.arch-col li {{ padding: 5px 0 5px 16px; position: relative; border-bottom: 1px dashed var(--line); }}
.arch-col li:last-child {{ border-bottom: none; }}
.arch-col li::before {{ content: "▸"; position: absolute; left: 0; color: var(--dim); }}
.arch-col.tpoint li::before {{ color: var(--blue); }}
.arch-col.kf li::before {{ color: var(--purple); }}
.arch-col .tag {{ font-size: 10.5px; color: var(--dim); }}

.item {{ background: var(--panel); border: 1px solid var(--line);
  border-left: 4px solid var(--blue); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 12px; }}
.item.p0 {{ border-left-color: var(--p0); }}
.item.p1 {{ border-left-color: var(--p1); }}
.item.p2 {{ border-left-color: var(--p2); }}
.item-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
.item-num {{ font-family: Consolas, monospace; font-weight: 700; color: var(--dim); }}
.item-prio {{ font-size: 11px; font-weight: 700; padding: 0 7px; border-radius: 8px; }}
.badge-P0 {{ background: rgba(255,95,95,.2); color: var(--p0); }}
.badge-P1 {{ background: rgba(255,171,64,.2); color: var(--p1); }}
.badge-P2 {{ background: rgba(77,163,255,.2); color: var(--p2); }}
.item-title {{ font-weight: 700; font-size: 14.5px; }}
.item-table td {{ border: none; padding: 3px 6px; font-size: 12.5px; }}
.item-table td.lbl {{ color: var(--dim); white-space: nowrap; width: 76px;
  font-weight: 600; vertical-align: top; }}
.mono {{ font-family: Consolas, monospace; font-size: 11.5px; color: var(--cyan); }}

.callout {{ background: rgba(255,171,64,.08); border: 1px solid rgba(255,171,64,.35);
  border-radius: 8px; padding: 12px 16px; font-size: 13px; margin: 10px 0; }}
.callout.warn {{ background: rgba(255,95,95,.08); border-color: rgba(255,95,95,.4); }}
.callout.ok {{ background: rgba(61,220,132,.07); border-color: rgba(61,220,132,.35); }}
.footer {{ color: var(--dim); font-size: 11.5px; margin-top: 30px;
  border-top: 1px solid var(--line); padding-top: 12px; }}
@media (max-width: 800px) {{ .arch {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="wrap">

<h1>🔍 tpoint × 卡方 T0 目标系统差距分析</h1>
<div class="sub">{esc(date_disp)} · 输入：T0震荡行情下的科技赋能(1).pptx（16页目标系统） + kf_日内回转plus_performance_20260731.xlsx（5002只×17列回测绩效） · 产出：九维差距矩阵 + P0/P1/P2 改进清单</div>

<div class="card">
<h3>核心结论</h3>
<div class="kpi-row">
  <div class="kpi"><div class="v v-blue">10</div><div class="k">差距维度（九维矩阵）</div></div>
  <div class="kpi"><div class="v v-red">4</div><div class="k">P0 必改（数据源/首扫/绩效/筛选）</div></div>
  <div class="kpi"><div class="v v-orange">4</div><div class="k">P1 应改（多周期/因子/口径）</div></div>
  <div class="kpi"><div class="v v-green">3</div><div class="k">P2 优化（高波动/复盘对齐）</div></div>
</div>
<div class="callout warn" style="margin-top:12px">
  ⚠️ <b>对标边界</b>：v9.3.0 盲 holdout 已证伪 floord/VWAP 信号为可交易策略（池化 PF=0.605，PF&gt;1 仅 6/40 只）。
  PPT 的 9.21% 年化 / 夏普 22.42 / 盈利天数 97.46% 是卡方自营策略收益，<b>不应作为 tpoint 策略收益对标目标</b>。
  本次差距分析对标的是<b>系统能力</b>（筛选/统计/数据/投递/复盘），而非策略收益承诺。
</div>
<div class="callout ok">
  ✅ <b>用户决策已确认</b>（2026-07-31）：① 保持信号推送，不升级自动下单（自动执行列为远期 P2 备注）；② watchlist 4 只为持仓驱动选择（2只T0观察+2只持有做T），
  不代表系统缺乏普适性——tpoint 架构支持任意 watchlist，差距在<b>缺一个全市场标的筛选工具</b>；③ 改进项按 P0/P1/P2 排序执行。
</div>
</div>

<h2>一、双栏架构图：目标系统 vs tpoint</h2>
<div class="arch">
  <div class="arch-col kf">
    <h3>🎯 卡方 T0 目标系统（PPT）</h3>
    <ul>
      <li><b>预测模型</b>：动量(6/12月累计收益) + 行业 + 情绪 + 波动率 + 流动性 5因子 <span class="tag">S4</span></li>
      <li><b>标的筛选</b>：近1月日均成交额≥50亿 + 换手率5-15% + 振幅5-20% → 40只名单 <span class="tag">S9</span></li>
      <li><b>双周期</b>：短周期持股10min + 长周期1h <span class="tag">S14-16</span></li>
      <li><b>绩效回测</b>：全市场批量回测；年化/回撤/夏普/开仓率/胜率/Level星级 <span class="tag">S7+xlsx</span></li>
      <li><b>执行</b>：程序化批量委托（客户案例：永鼎股份全天22笔） <span class="tag">S14-16</span></li>
      <li><b>风控</b>：收盘/开盘股数一致、风险敞口控制 <span class="tag">S1</span></li>
      <li><b>适用场景</b>：震荡市/高振幅/流动性充足；高波高换手高振幅收益最好 <span class="tag">S5-6</span></li>
    </ul>
  </div>
  <div class="arch-col tpoint">
    <h3>🛠 tpoint 现状</h3>
    <ul>
      <li><b>信号引擎</b>：VWAP引力(dev=0.6) + MACD背离 + 价格地板/天花板(floor门控)；量价背离禁用</li>
      <li><b>标的池</b>：watchlist.json 手工 4 只（161129/513310/688111/588000）</li>
      <li><b>单周期</b>：1m K线 15s 轮询扫描；5m/15m 共振研究已证伪(v9.3.0)</li>
      <li><b>回测统计</b>：aggregate_metrics 仅笔数/胜率/盈亏比/复利净值，无年化/回撤/夏普/开仓率</li>
      <li><b>执行</b>：飞书信号推送 + 人工执行（无券商接口）</li>
      <li><b>风控</b>：risk_override.json 顶层闸门 + 静默零信号检测 + 数据源中断告警</li>
      <li><b>投递</b>：飞书卡片 + push_audit 审计 + push_pending 补发(新鲜度闸门)</li>
      <li><b>复盘</b>：daily_signal_review → review_charts → build_review_html 3步流水线</li>
    </ul>
  </div>
</div>

<h2>二、九维差距对照矩阵</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>维度</th><th>目标能力（PPT/xlsx）</th><th>当前能力（tpoint）</th><th>评级</th></tr>
{matrix_rows}
</table>
</div>

<h2>三、xlsx 回测数据对照</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>对照项</th><th>卡方全市场回测（xlsx 5002只）</th><th>tpoint 现状</th><th>评级</th></tr>
{xlsx_rows}
</table>
</div>
<div class="callout">
  📌 <b>个体锚点</b>：tpoint watchlist 4 只中仅 <b>688111.SH 金山办公</b> 命中 xlsx（Level 3 星｜20日收益 1.86%｜年化 22.58%｜20日开仓率 57.94%｜20日胜率 50%）。
  161129(原油LOF)/513310(中韩半导体ETF)/588000(科创50ETF) 为 ETF/LOF，xlsx 表内全为个股不适用（表外）。
  PPT S9 的 40 只名单命中 xlsx 39 只（唯一未命中=华虹公司 688347），可作未来选股参考池。
</div>
<div class="callout warn">
  ⚠️ <b>口径警示</b>：xlsx 为卡方内部回测口径（含双边费用 万3.5 买 + 万5.641 卖，见备注列），与 tpoint aggregate_metrics
  （等权每笔、无费用模型）口径不同，<b>数值不可直接横向比较</b>，仅作能力与分布对照。
</div>

<h2>四、改进清单（P0 / P1 / P2）</h2>
{items_html}

<div class="footer">
生成：{esc(date_disp)} · tpoint 差距分析脚本 build_gap_analysis.py · 证据文件行号已内嵌
</div>
</div>
</body>
</html>'''
    return html


def main():
    html = build_html()
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ 差距分析报告已生成: {OUT} ({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
