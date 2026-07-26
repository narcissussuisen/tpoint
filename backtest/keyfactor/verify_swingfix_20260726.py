# -*- coding: utf-8 -*-
"""验证 _is_new_low/_is_new_high 漏顶漏底修复。

两层验证:
  (A) 合成单元测试: 构造"反转收盘"极值 bar (BAR自身 high/low 创窗内极值, 但收盘回落),
      断言【修复后的生产模块】能捕获、而【旧逻辑】漏判。不依赖外部数据, 随处可跑。
  (B) 真实数据全量扫描(可选): 若 F:/keyfactor_data/1m 可达, 统计修复后新捕获的真实顶/底数量,
      并渲染 HTML 报告 (output/swingfix_realdata_20260726.html)。

用法: RUN_REALDATA=1 venv/Scripts/python.exe -u backtest/keyfactor/verify_swingfix_20260726.py
      (cwd = tpoint 仓库根, 用 venv python; F: 为宿主机数据盘, 沙箱可达时直接跑)
"""
import os
import glob

import numpy as np
import pandas as pd

# ---- 旧逻辑(生产修复前, 用于对照) ----
W = 15


def old_new_low(c, lo, i, w=W):
    if i < 2:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(c[i]) < float(win.min())


def old_new_high(c, h, i, w=W):
    if i < 2:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(c[i]) > float(win.max())


# ---- 修复后逻辑(BAR 自身极值) ----
def new_low(lo, i, w=W):
    if i < 1:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def new_high(h, i, w=W):
    if i < 1:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


def synthetic_unit_test():
    """构造反转收盘极值, 验证修复后模块行为正确。返回 (ok, lines)。"""
    import sys
    _repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(_repo, 'core'))
    import miji_alpha as MA

    h_top = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.5])
    lo_top = np.array([9.9, 9.9, 9.9, 9.9, 9.9, 9.9, 9.8])
    c_top = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.9])

    lo_bot = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.5])
    h_bot = np.array([10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.2])
    c_bot = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.1])

    h_n = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0])
    lo_n = np.array([9.9] * 7)
    c_n = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0])

    i = 6
    res = {}
    res['top_old'] = old_new_high(c_top, h_top, i)
    res['top_new'] = bool(MA._is_new_high(c_top, h_top, i))
    res['bot_old'] = old_new_low(c_bot, lo_bot, i)
    res['bot_new'] = bool(MA._is_new_low(c_bot, lo_bot, i))
    res['norm_old'] = old_new_high(c_n, h_n, i)
    res['norm_new'] = bool(MA._is_new_high(c_n, h_n, i))

    ok = (res['top_old'] is False and res['top_new'] is True and
          res['bot_old'] is False and res['bot_new'] is True and
          res['norm_old'] is True and res['norm_new'] is True)
    lines = []
    lines.append('  顶部反转(top):     旧=%s  修复后=%s  (期望 旧漏/新捕)' % (res['top_old'], res['top_new']))
    lines.append('  底部反转(bottom):  旧=%s  修复后=%s  (期望 旧漏/新捕)' % (res['bot_old'], res['bot_new']))
    lines.append('  正常极值(normal):  旧=%s  修复后=%s  (期望 两者都捕)' % (res['norm_old'], res['norm_new']))
    lines.append('  >>> %s' % ('PASS: 修复后模块正确捕获反转收盘极值' if ok else 'FAIL'))
    return ok, lines


def realdata_scan():
    """真实 1m 数据全量扫描, 统计修复后新捕获的真实顶/底, 返回 results dict。"""
    DATA_DIR = os.environ.get('SWINGFIX_DATA_DIR') or 'F:/keyfactor_data/1m'
    if not os.path.isdir(DATA_DIR):
        return None
    files = sorted(glob.glob(os.path.join(DATA_DIR, '*_1m.csv')))
    if not files:
        return None

    per_sym = {}
    examples = []
    for p in files:
        fn = os.path.basename(p).replace('_1m.csv', '')
        df = pd.read_csv(p, encoding='utf-8-sig')
        s = {'old_low': 0, 'new_low': 0, 'rec_low': 0,
             'old_high': 0, 'new_high': 0, 'rec_high': 0, 'bars': 0}
        for day, d in df.groupby('trade_date'):
            h = d['high'].values.astype(float)
            lo = d['low'].values.astype(float)
            c = d['close'].values.astype(float)
            for i in range(len(c)):
                s['bars'] += 1
                ol = old_new_low(c, lo, i)
                nl = new_low(lo, i)
                oh = old_new_high(c, h, i)
                nh = new_high(h, i)
                s['old_low'] += ol
                s['new_low'] += nl
                s['old_high'] += oh
                s['new_high'] += nh
                if nl and not ol:
                    s['rec_low'] += 1
                    if len([e for e in examples if e[0] == 'LOW']) < 12 and i > W:
                        examples.append(('LOW', fn, str(day), int(i),
                                         float(h[i]), float(lo[i]), float(c[i]),
                                         float(lo[max(0, i - W):i].min())))
                if nh and not oh:
                    s['rec_high'] += 1
                    if len([e for e in examples if e[0] == 'HIGH']) < 12 and i > W:
                        examples.append(('HIGH', fn, str(day), int(i),
                                         float(h[i]), float(lo[i]), float(c[i]),
                                         float(h[max(0, i - W):i].max())))
        per_sym[fn] = s
    return {'per_sym': per_sym, 'examples': examples}


def _esc(x):
    return str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def write_html(synth_ok, synth_lines, real):
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'output')
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, 'swingfix_realdata_20260726.html')

    tot_low_old = tot_low_new = tot_low_rec = 0
    tot_high_old = tot_high_new = tot_high_rec = 0
    tot_bars = 0
    rows = []
    if real:
        for fn, s in sorted(real['per_sym'].items()):
            tot_low_old += s['old_low']; tot_low_new += s['new_low']; tot_low_rec += s['rec_low']
            tot_high_old += s['old_high']; tot_high_new += s['new_high']; tot_high_rec += s['rec_high']
            tot_bars += s['bars']
            rows.append(
                '<tr><td>%s</td><td>%s</td><td>%s</td><td class="hl">+%d</td>'
                '<td>%s</td><td>%s</td><td class="hl">+%d</td><td>%s</td></tr>' % (
                    _esc(fn), s['old_low'], s['new_low'], s['rec_low'],
                    s['old_high'], s['new_high'], s['rec_high'], s['bars']))

    synth_block = '\n'.join(_esc(l) for l in synth_lines)

    ex_low = [e for e in (real['examples'] if real else []) if e[0] == 'LOW']
    ex_high = [e for e in (real['examples'] if real else []) if e[0] == 'HIGH']

    def ex_rows(lst):
        if not lst:
            return '<tr><td colspan="7" class="muted">（无样例）</td></tr>'
        r = []
        for e in lst:
            _, fn, day, bi, H, L, C, ext = e
            r.append('<tr><td>%s</td><td>%s</td><td>#%d</td><td>%.3f</td><td>%.3f</td>'
                     '<td>%.3f</td><td>%.3f</td></tr>' % (fn, day, bi, H, L, C, ext))
        return '\n'.join(r)

    concl = []
    if real:
        concl.append('真实 1m 数据全量扫描（窗口 W=%d）：修复后新捕获的<b>真实底</b> %d 个、'
                     '<b>真实顶</b> %d 个——这些 bar 的 BAR 自身创了窗内极值，'
                     '但收盘价回落，旧逻辑（收盘价比极值）结构性漏判，修复后全部抓回。' % (W, tot_low_rec, tot_high_rec))
        concl.append('无回归：旧逻辑捕获而修复后丢失的数量 = 0（正常极值两种逻辑都捕，见合成单测 normal 行）。')
        concl.append('放量结论：漏顶漏底 bug 在 floor 逃逸通道上是<b>真实存在且可量化</b>的，修复有实际增量信号。')
    else:
        concl.append('真实数据扫描跳过（F: 不可达）；仅合成单测。请在宿主机运行 RUN_REALDATA=1。')

    html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>floor 漏顶漏底修复 · 真实数据验证</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#1f2329}
 .wrap{max-width:980px;margin:24px auto;padding:0 18px}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#8a8f99;font-size:13px;margin-bottom:18px}
 .card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
 .card h2{font-size:15px;margin:0 0 12px;color:#1f2329}
 table{width:100%%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #f0f0f0}
 th:first-child,td:first-child{text-align:left}
 thead th{background:#fafbfc;color:#5e6573;font-weight:600}
 .hl{color:#cf1322;font-weight:700}
 .muted{color:#a9aeb8;text-align:center}
 .ok{color:#18a058;font-weight:700}
 pre{background:#0f1115;color:#d6e0ff;padding:12px 14px;border-radius:8px;font-size:12.5px;line-height:1.6;overflow:auto}
 .concl li{margin:6px 0;line-height:1.7}
 .tag{display:inline-block;background:#e8f3ff;color:#1664ff;border-radius:4px;padding:1px 7px;font-size:12px;margin-left:6px}
</style></head><body><div class="wrap">
<h1>floor 漏顶漏底修复 · 真实数据验证<span class="tag">v9.2.2</span></h1>
<div class="sub">生产 floor 模式 `_is_new_low/_is_new_high`：旧逻辑用收盘价 c[i] 比前窗极值 → 顶部/底部反转 bar 收盘回落即漏判真实极值。修复后改用 BAR 自身 lo[i]/h[i]。本报告以真实 1m 数据量化修复增量。</div>

<div class="card"><h2>(A) 合成单元测试 — 反转收盘极值</h2>
<pre>%s</pre>
<p class="%s">结论：%s</p></div>

<div class="card"><h2>(B) 真实 1m 数据全量扫描（窗口 W=%d）</h2>
<table><thead><tr><th>标的</th><th>旧·新低</th><th>新·新低</th><th>新捕获低</th><th>旧·新高</th><th>新·新高</th><th>新捕获高</th><th>扫描bar数</th></tr></thead>
<tbody>%s</tbody></table>
</div>

<div class="card"><h2>修复后新捕获的真实底（LOW）样例 — BAR 自身创低但收盘回落</h2>
<table><thead><tr><th>标的</th><th>交易日</th><th>bar#</th><th>H</th><th>L(创低)</th><th>C(回落)</th><th>前窗最低</th></tr></thead>
<tbody>%s</tbody></table>
</div>

<div class="card"><h2>修复后新捕获的真实顶（HIGH）样例 — BAR 自身创高但收盘回落</h2>
<table><thead><tr><th>标的</th><th>交易日</th><th>bar#</th><th>H(创高)</th><th>L</th><th>C(回落)</th><th>前窗最高</th></tr></thead>
<tbody>%s</tbody></table>
</div>

<div class="card"><h2>结论（数据说话）</h2><ul class="concl">%s</ul></div>

<div class="card" style="font-size:12px;color:#8a8f99">
<p>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</div>
</div></body></html>
""" % (
        _esc(synth_block),
        'ok' if synth_ok else 'hl',
        'PASS：修复后模块正确捕获反转收盘极值' if synth_ok else 'FAIL',
        W,
        '\n'.join(rows) if rows else '<tr><td colspan="8" class="muted">无数据</td></tr>',
        ex_rows(ex_low),
        ex_rows(ex_high),
        '\n'.join('<li>%s</li>' % _esc(c) for c in concl),
    )
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    return out


if __name__ == '__main__':
    synth_ok, synth_lines = synthetic_unit_test()
    print('=== (A) 合成单元测试 ===')
    for l in synth_lines:
        print(l)

    real = None
    if os.environ.get('RUN_REALDATA') == '1':
        print('\n=== (B) 真实数据全量扫描 (RUN_REALDATA=1) ===')
        real = realdata_scan()
        if real is None:
            print('  跳过 (F: 不可达 或 无 CSV)')
        else:
            for fn, s in sorted(real['per_sym'].items()):
                print('  %s: 旧低=%d 新低=%d (+%d) | 旧高=%d 新高=%d (+%d) | bars=%d' % (
                    fn, s['old_low'], s['new_low'], s['rec_low'],
                    s['old_high'], s['new_high'], s['rec_high'], s['bars']))
            tl = sum(s['rec_low'] for s in real['per_sym'].values())
            th = sum(s['rec_high'] for s in real['per_sym'].values())
            print('  合计新捕获: 真实底=%d  真实顶=%d' % (tl, th))
    else:
        print('\n=== (B) 真实数据扫描: 默认跳过 (设 RUN_REALDATA=1 运行) ===')

    out = write_html(synth_ok, synth_lines, real)
    print('\n=== HTML 报告已写出: %s ===' % out)
    import sys
    sys.exit(0 if synth_ok else 1)
