# -*- coding: utf-8 -*-
"""
build_ablation_report.py — 因子消融矩阵 + FLOOR 网格 → HTML 报告

读 output/factor_ablation_YYYY-MM-DD.json，生成深色主题 HTML：
  一、结论速览（每个因子/通道的边际贡献表）
  二、消融矩阵逐组合×逐标的明细
  三、FLOOR_DEV_PCT 网格（含笔级加权汇总 + 每标的胜率/盈亏比/净收益）
  四、方法声明与口径说明

用法：
  python scripts/build_ablation_report.py [--json output/factor_ablation_2026-08-01.json]
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def load_payload(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def calc_agg(matrix):
    """从原始矩阵重算笔级加权汇总（兼容 JSON 中 matrix_agg 缺 pl_ratio 的旧版）。"""
    out = {}
    for name, by_sym in matrix.items():
        tot = wins = ret = 0
        pl_win = pl_loss = 0.0
        for sym, m in by_sym.items():
            if not isinstance(m, dict) or 'error' in m:
                continue
            n = m.get('total') or 0
            tot += n
            wins += n * (m.get('win_rate') or 0) / 100.0
            ret += m.get('total_ret') or 0.0
            pl_win += n * (m.get('avg_win') or 0)
            pl_loss += n * (m.get('avg_loss') or 0)
        pl = round(pl_win / abs(pl_loss), 2) if pl_loss else 99.0
        out[name] = {
            'total': tot,
            'win_rate': round(wins / tot * 100, 1) if tot else 0.0,
            'pl_ratio': pl,
            'total_ret': round(ret, 2),
        }
    return out


def build_conclusion(payload):
    """边际贡献表：基于 matrix 原始数据重算笔级加权。
    注意控制变量：每个边际对比只改一个通道。
      - 地板边际  = prod(m开+地板) − no_floor(m开+无地板)
      - m门控边际 = prod(m开+地板) − floor_only(m关+地板)
      - 早盘g边际 = prod(m开+g开+地板) − no_g_early(m开+g关+地板)
      - vol边际   = vol_gate − prod（唯一差异 vol_in_gate）
    """
    agg = calc_agg(payload.get('matrix', {}))
    rows = []
    def g(name):
        return agg.get(name, {'total': 0, 'win_rate': 0, 'pl_ratio': 0, 'total_ret': 0})
    prod = g('prod')
    def delta(a, b):
        return {'total': a['total'] - b['total'],
                'win_rate': round(a['win_rate'] - b['win_rate'], 1),
                'pl_ratio': round(a['pl_ratio'] - b['pl_ratio'], 2),
                'total_ret': round(a['total_ret'] - b['total_ret'], 2)}
    rows.append(('基线 prod（v9.2.2 floor）', prod['total'], prod['win_rate'],
                 prod['pl_ratio'], prod['total_ret'], '基准'))
    d = delta(prod, g('no_floor'))
    rows.append(('地板/天花板通道（prod−no_floor）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], '负=地板通道有害（m门控下）'))
    d = delta(prod, g('floor_only'))
    rows.append(('MACD 门控边际（prod−floor_only）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], '负=加入m门控有害'))
    d = delta(prod, g('no_g_early'))
    rows.append(('早盘引力（prod−no_g_early）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], '负=去掉早盘g有害(g有益)'))
    d = delta(g('vol_gate'), prod)
    rows.append(('vol 参与放行（vol_gate−prod）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], 'vol真实边际'))
    d = delta(g('g_only'), prod)
    rows.append(('纯引力假设（g_only−prod）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], 'gate=off 全时段g'))
    d = delta(g('v_only'), prod)
    rows.append(('纯vol假设（v_only−prod）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], 'strict+vol 无g/m'))
    d = delta(g('gvm_floor'), prod)
    rows.append(('全开+地板（gvm_floor−prod）', d['total'], d['win_rate'],
                 d['pl_ratio'], d['total_ret'], 'g+v+m 全参与'))
    # 纯地板基线
    n = g('none')
    rows.append(('纯地板/天花板（none，无因子打分）', n['total'], n['win_rate'],
                 n['pl_ratio'], n['total_ret'], '参考：floor通道单独能力'))
    return rows


def build_matrix_table(payload):
    """逐组合×逐标的明细表。"""
    matrix = payload.get('matrix', {})
    combos = payload.get('combos', [])
    names = [c[0] for c in combos] if combos else list(matrix.keys())
    symbols = payload.get('symbols', [])
    # 列头
    thead = '<tr><th>组合</th><th>配置</th>'
    for s in symbols:
        thead += f'<th>{esc(s)}</th>'
    thead += '<th>笔级加权</th></tr>'
    rows = ''
    for name in names:
        by_sym = matrix.get(name, {})
        cfg = ''
        for c in combos:
            if c[0] == name:
                cfg = f'enable={c[1]} vol={c[2]} vig={c[3]} gate={c[4]}'
        rows += f'<tr><td class="c1">{esc(name)}</td><td class="cfg">{esc(cfg)}</td>'
        agg_ret = agg_wr = 0
        tot = 0
        for s in symbols:
            m = by_sym.get(s, {})
            if 'error' in m:
                rows += '<td class="bad">✗</td>'
                continue
            wr = m.get('win_rate', 0)
            ret = m.get('total_ret', 0)
            n = m.get('total', 0)
            # 笔级加权累计（本表只显示 胜率/净收益 两个核心）
            rows += (f'<td>{wr}%<br><span class="sub">{ret}%</span></td>')
            tot += n
            agg_wr += n * (wr or 0)
            agg_ret += ret
        if tot:
            rows += f'<td class="agg">{round(agg_wr/tot,1)}%<br><span class="sub">{round(agg_ret,2)}%</span></td>'
        else:
            rows += '<td class="agg">—</td>'
        rows += '</tr>'
    return thead, rows


def build_floor_table(payload):
    """FLOOR_DEV_PCT 网格表：每档的笔级加权胜率/盈亏比/净收益。"""
    agg = payload.get('floor_agg', {})
    grid = payload.get('floor_grid', [])
    rows = ''
    for fv in grid:
        a = agg.get(str(fv), {})
        n = a.get('total', 0)
        if not n:
            continue
        rows += (f'<tr><td>{fv}</td><td>{n}</td>'
                 f'<td>{a.get("win_rate", 0)}%</td>'
                 f'<td>{a.get("pl_ratio", 0)}</td>'
                 f'<td>{a.get("total_ret", 0)}%</td></tr>')
    return rows


def build_html(payload, date_str):
    concl = build_conclusion(payload)
    thead, matrix_rows = build_matrix_table(payload)
    floor_rows = build_floor_table(payload)
    # 结论表
    concl_rows = ''
    for name, n, wr, pl, ret, note in concl:
        cls = 'pos' if ret >= 0 else 'neg'
        concl_rows += (f'<tr><td>{esc(name)}</td><td>{n}</td>'
                       f'<td>{wr}%</td><td>{pl}</td>'
                       f'<td class="{cls}">{ret}%</td><td class="sub">{esc(note)}</td></tr>')
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>tpoint 因子消融矩阵 + FLOOR 网格 · {date_str}</title>
<style>
:root{{--bg:#11151c;--card:#1a2029;--ink:#d5dae2;--sub:#8a93a6;--line:#2a3140;
--pos:#7ee2a8;--neg:#ff8b8b;--warn:#f5c26b;--accent:#9ec9ff}}
body{{background:var(--bg);color:var(--ink);font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1200px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:var(--accent);font-size:15px;margin-top:30px}}
.card{{background:var(--card);border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line);font-size:12.5px}}
th{{color:var(--sub);font-weight:500}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}} .warn{{color:var(--warn)}}
.sub{{color:var(--sub);font-size:11px}}
.c1{{font-weight:600;color:#fff}} .cfg{{font-size:10.5px;color:var(--sub)}}
.agg{{font-weight:600;color:var(--accent)}}
.sum{{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px}}
.sum div{{background:#232b38;border-radius:8px;padding:10px 16px}}
.sum b{{font-size:20px;display:block;color:#fff}}
.note{{background:#232b38;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:12.5px;line-height:1.7}}
.badge{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;margin-left:6px}}
.b-pos{{background:#1a3a2a;color:#7ee2a8}} .b-neg{{background:#3a1a1a;color:#ff8b8b}}
</style></head><body>
<h1>tpoint 因子消融矩阵 + FLOOR 网格 · {date_str}</h1>
<div class="card">
  <h2>一、结论速览（8 标的 × 146 天 · 万一费率 · MACD_GATE_MODE=floor · 净收益口径）</h2>
  <p class="sub">边际 = 去掉/加入某通道后笔级加权指标变化。正=通道有益（保留），负=通道有害（考虑调整）。</p>
  <table>
    <tr><th>通道</th><th>笔数</th><th>净胜率Δ</th><th>盈亏比Δ</th><th>净收益Δ</th><th>解读</th></tr>
    {concl_rows}
  </table>
  <div class="sum">
    <div><b>{payload.get('generated_at','')[:10]}</b>生成</div>
    <div><b>{len(payload.get('symbols', []))}</b>标的</div>
    <div><b>{len(payload.get('combos', []))}</b>组合</div>
    <div><b>{len(payload.get('floor_grid', []))}</b>floor档</div>
  </div>
</div>
<div class="card">
  <h2>二、消融矩阵逐组合 × 逐标的（胜率% / 净收益%）</h2>
  <table>
    {thead}
    {matrix_rows}
  </table>
</div>
<div class="card">
  <h2>三、FLOOR_DEV_PCT 网格（prod 组合 · 笔级加权）</h2>
  <table>
    <tr><th>FLOOR_DEV_PCT</th><th>总笔数</th><th>净胜率</th><th>盈亏比</th><th>总净收益</th></tr>
    {floor_rows}
  </table>
</div>
<div class="card">
  <h2>四、方法声明</h2>
  <div class="note">
    <b>口径</b>：F盘 tickflow 1m 历史库；引擎 core.miji_alpha.detect_miji_signals（生产同源）；
    成本 cost_for_symbol（万一佣金不免五 / ETF 无印花税 / 北交所千0.575）；出场 = PROD_CONFIG
    （仅移动止损 act0.4/trail0.6 + S信号出场）；ret_pct 为扣双边成本净收益，win_rate 为净胜率。<br>
    <b>消融开关</b>：enable 三元组 (gravity, vol_div, macd_div) 关某因子 → 该因子恒为 0；
    vol_in_gate=True 时 vol 参与门控放行（生产仅记录共振分数不参与放行）；
    gate 显式指定 floor/strict/off。<br>
    <b>样本</b>：688146/600206/688347/688766/688111 各 143 天，161129/513310 各 65 天，600584 20 天。
    未做样本外切分（如需 --oos 重跑）。<br>
    <b>已知限制</b>：simulate_day 单仓位模型 + SIGNAL_GAP=8 强约束，冗余信号只影响候选池不影响成交路径，
    信号数差异可能被成交路径稀释（详见结论解读）。
  </div>
</div>
</body></html>"""
    out_path = os.path.join(BASE, 'output', f'ablation_report_{date_str}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out_path}')
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', default=None)
    args = ap.parse_args()
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    if not args.json:
        args.json = os.path.join(BASE, 'output', f'factor_ablation_{date_str}.json')
    payload = load_payload(args.json)
    build_html(payload, date_str)


if __name__ == '__main__':
    main()
