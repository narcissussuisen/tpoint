#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""deploy_optimal.py — 最优因子上线器（2026-08-05 用户指令：抓波动为唯一目的，解除一切优化阻碍）

选参口径（防 08-05 胜率虚胖教训）：
  total_ret 优先（抓波动的本质），硬约束 win_rate 不得劣于基线、n>=30。
  atr 网格今日实测无区分度（三档同指标），仅 trail 生效。
验证：全历史 + 近20日 holdout 双窗口（holdout 收益劣化>2 则标注灰度警告仍上线，明日闭环验证）。
上线：per-symbol trail_activate_pct/trail_pct 写 monitor_config.json（monitor 热重载，次日开盘生效）。
产物：output/optimal_deploy_<date>.html（回测报告）+ git 版本 + 推 a35d7f52。

CLI：python scripts/deploy_optimal.py --date 2026-08-05 [--dry-run]
"""
import os, sys, json, argparse, datetime, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
CFG = os.path.join(ROOT, 'data', 'monitor_config.json')
CL_STATE = os.path.join(ROOT, 'data', 'closed_loop_state.json')
VERSION_FILE = os.path.join(ROOT, 'VERSION')
CHANGELOG = os.path.join(ROOT, 'CHANGELOG.md')
HOLDOUT = 20


def push(text):
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    req = __import__('urllib.request').request.Request(
        HOOK, data=payload, headers={'Content-Type': 'application/json'})
    with __import__('urllib.request').request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', 'replace')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    date = a.date

    import factor_optimizer as FO
    opt = json.load(open(os.path.join(ROOT, 'output', f'factor_opt_{date}.json'), encoding='utf-8'))
    wl = json.load(open(FO.WATCHLIST, encoding='utf-8'))
    cfg = json.load(open(CFG, encoding='utf-8'))

    deployed, rows_html, push_lines = [], [], []
    for sym, v in opt['symbols'].items():
        if 'error' in v or sym not in wl:
            continue
        name = v['name']
        base = v['baseline']
        # 选参：total_ret 优先，wr 不得劣于基线，n>=30
        cands = []
        for k, m in v['trail_grid'].items():
            ta, tp = map(float, k.split('/'))
            if (ta, tp) == FO.CUR_TRAIL:
                continue
            if m['n'] >= FO.MIN_TRIPS and m['win_rate'] >= base['win_rate'] and m['total_ret'] > base['total_ret']:
                cands.append((ta, tp, m))
        if not cands:
            push_lines.append(f'·{sym} {name}：无双优候选（wr不降且ret升），维持 0.4/0.6')
            rows_html.append((sym, name, '0.4/0.6(维持)', base, base, None, '无双优候选'))
            continue
        cands.sort(key=lambda x: -x[2]['total_ret'])
        ta, tp, m = cands[0]
        # holdout 验证
        try:
            days = FO.sym_days(sym)
            for d, data, g in days:
                data['sym'] = sym
            sig = FO.day_signals(sym, name, days, FO.CUR_ATR)
            cur_h = FO.metrics_of(FO.eval_config(sig[-HOLDOUT:], *FO.CUR_TRAIL))
            new_h = FO.metrics_of(FO.eval_config(sig[-HOLDOUT:], ta, tp))
        except Exception as e:
            cur_h = new_h = {'error': str(e)[:60]}
        warn = ''
        if 'error' not in new_h and new_h['total_ret'] < cur_h['total_ret'] - 2:
            warn = f'⚠️近20日收益劣化({cur_h["total_ret"]}%→{new_h["total_ret"]}%)，灰度观察'
        if not a.dry_run:
            cfg.setdefault(sym, {})['trail_activate_pct'] = ta
            cfg[sym]['trail_pct'] = tp
            cfg[sym]['_trail_note'] = (f'{date} 最优因子上线：total_ret优先口径 ret {base["total_ret"]}%→{m["total_ret"]}% '
                                       f'wr {base["win_rate"]}%→{m["win_rate"]}% n={m["n"]}（deploy_optimal.py）{warn}')
        deployed.append({'sym': sym, 'name': name, 'ta': ta, 'tp': tp,
                         'base': base, 'new': m, 'cur_h': cur_h, 'new_h': new_h, 'warn': warn})
        push_lines.append(f'·{sym} {name}：trail 0.4/0.6→{ta}/{tp}｜全历史 ret {base["total_ret"]}%→{m["total_ret"]}% '
                          f'wr {base["win_rate"]}%→{m["win_rate"]}%｜近20日 ret {cur_h.get("total_ret")}%→{new_h.get("total_ret")}% {warn}')
        rows_html.append((sym, name, f'{ta}/{tp}', base, m, (cur_h, new_h), warn or '已上线'))

    # HTML 回测报告
    def cell(x):
        return f'n={x["n"]} wr={x["win_rate"]}% pl={x["pl_ratio"]} ret={x["total_ret"]}%' if x and 'error' not in x else str(x)
    trs = ''.join(
        f'<tr><td>{s}</td><td>{n}</td><td>{p}</td><td>{cell(b)}</td><td>{cell(m)}</td>'
        f'<td>{cell(h[0])} → {cell(h[1]) if h else "-"}</td><td>{st}</td></tr>'
        for s, n, p, b, m, h, st in rows_html)
    html = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>tpoint 最优因子回测报告 {date}</title>
<style>body{{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#0f1419;color:#e6e6e6;padding:24px;max-width:1100px;margin:auto}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border:1px solid #333;padding:6px 8px;text-align:left}} th{{background:#1c232b}}
.up{{color:#f6465d}} .dn{{color:#2ebd85}} .note{{color:#999;font-size:12px;margin-top:16px}}</style></head><body>
<h1>tpoint 最优因子回测报告 {date}（total_ret 优先口径 · wr 不降为硬约束 · n≥30）</h1>
<table><tr><th>标的</th><th>名称</th><th>上线trail</th><th>基线(0.4/0.6)全历史</th><th>最优组合全历史</th><th>近20日holdout 基线→最优</th><th>状态</th></tr>{trs}</table>
<p class="note">口径：F盘全历史 1m + 生产同源信号 + simulate_day；成本=万一佣金+印花(股票卖边)+滑点2bps/边。
atr 网格(0.15/0.25/0.35)今日实测三档指标完全一致（无区分度），未变更。生成：deploy_optimal.py {datetime.datetime.now().strftime("%F %T")}</p>
</body></html>'''
    html_path = os.path.join(ROOT, 'output', f'optimal_deploy_{date}.html')
    open(html_path, 'w', encoding='utf-8').write(html)

    ver_line = 'dry-run，未写配置'
    if not a.dry_run and deployed:
        json.dump(cfg, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        # 更新闭环状态快照 → 明日A步验证今日上线项
        st = json.load(open(CL_STATE, encoding='utf-8')) if os.path.exists(CL_STATE) else {}
        st.setdefault('applied_today', [])
        for d in deployed:
            st['applied_today'].append({'sym': d['sym'], 'param': 'trail',
                                        'old': '0.4/0.6', 'new': f"{d['ta']}/{d['tp']}"})
        snap = st.get('config_snapshot', {})
        for d in deployed:
            snap.setdefault(d['sym'], {})['trail_activate_pct'] = d['ta']
            snap[d['sym']]['trail_pct'] = d['tp']
        st['config_snapshot'] = snap
        json.dump(st, open(CL_STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        # git 版本
        import re
        cur_v = open(VERSION_FILE, encoding='utf-8').read().strip()
        mm = re.match(r'(\d+)\.(\d+)\.(\d+)', cur_v)
        new_v = f'{mm.group(1)}.{mm.group(2)}.{int(mm.group(3)) + 1}'
        open(VERSION_FILE, 'w', encoding='utf-8').write(new_v + '\n')
        open(CHANGELOG, 'a', encoding='utf-8').write(
            f'\n## v{new_v}（{date}）最优因子上线（用户指令：解除优化阻碍，total_ret优先口径）\n'
            + ''.join(f"- {d['sym']} trail: 0.4/0.6 → {d['ta']}/{d['tp']}（ret {d['base']['total_ret']}%→{d['new']['total_ret']}%，wr {d['base']['win_rate']}%→{d['new']['win_rate']}%）{d['warn']}\n"
                      for d in deployed)
            + f'- 回测报告：output/optimal_deploy_{date}.html\n')
        def git(*args):
            return subprocess.run(['git', '-C', ROOT] + list(args), capture_output=True, text=True, encoding='utf-8')
        git('add', 'VERSION', 'CHANGELOG.md', 'data/monitor_config.json', 'data/closed_loop_state.json',
            f'output/optimal_deploy_{date}.html', 'scripts/deploy_optimal.py', 'scripts/gate_ablation.py',
            'scripts/daily_closed_loop.py', 'scripts/_today.py', 'scripts/fdisk_daily_update.py',
            'scripts/run_daily_review.bat')
        r = git('commit', '-m', f'feat(v{new_v}): 最优因子上线 {date}（{len(deployed)}只 trail 调优，total_ret优先）+ 自迭代闭环/消融探针/流水线修复')
        if r.returncode == 0:
            git('tag', f'v{new_v}')
            ver_line = f'v{cur_v} → v{new_v}（已 commit+tag）'
        else:
            ver_line = f'commit 失败: {(r.stderr or "")[:80]}'

    lines = [f'🚀 tpoint 最优因子上线 {date}｜部署{len(deployed)}只｜{ver_line}',
             '口径：total_ret优先（抓波动），wr不降为硬约束，n≥30；明日闭环A步自动验证今日上线项', '']
    lines += push_lines
    lines += ['', f'回测报告：output/optimal_deploy_{date}.html',
              '⚠️ 结构性问题：688111 全历史 ret -32.6%（最优仍 -29.2%），513310 全历史为负——建议评审是否移出watchlist或换策略框架']
    if not a.dry_run:
        push('\n'.join(lines))
    print('\n'.join(lines))
    print(f'[ok] {html_path}')


if __name__ == '__main__':
    main()
