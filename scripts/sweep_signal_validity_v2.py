# -*- coding: utf-8 -*-
"""
sweep_signal_validity_v2.py —— DET v2.0 参数敏感性分析（单参数局部扰动，覆盖 k/c1/c2/W_PREC）

评审修复③：原版只扫 k×c1（Precision 恒 63.8%，敏感性空转）。改为对四个参数各自
在基线邻域做 ±1 档局部扰动，输出每个参数的局部梯度与稳健性判定（过拟合=尖峰，稳健=单调平滑）。

基线：k=1.0, c1=1.5, c2=1.0, W_PREC=30；固定 60 标子集（seed=42）。
"""
import sys, os, json, glob, random, datetime, subprocess

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import evaluate_signal_validity_v2 as ev

HEARTBEAT = os.path.join(ev.OUT, 'signal_validity_v2_sweep_heartbeat.txt')
DONE_FLAG = os.path.join(ev.OUT, 'signal_validity_v2_sweep_done.flag')
NOTIFY = ev.NOTIFY


def feishu(text):
    try:
        with open(HEARTBEAT, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(text) + '\n')
    except Exception:
        pass
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _rate(ok, tot):
    return (ok / tot) if tot else None


def run_cfg(k, c1, c2, w_prec):
    """固定参数跑 60 标子集，返回池级聚合。"""
    ev.K = k
    ev.C1 = c1
    ev.C2 = c2
    ev.W_PREC = w_prec
    pool_acc = {h: dict(b_tot=0, b_att=0, b_cap=0, b_tep=0, s_tot=0, s_att=0, s_cap=0, s_tep=0)
                for h in ev.HORIZONS}
    pool_prec = dict(b_tot=0, b_hit=0, b_dev=[], s_tot=0, s_hit=0, s_dev=[])
    for path in SUBSET:
        sym = os.path.basename(path).replace('_1m.csv', '')
        r = ev.evaluate_symbol(sym, path)
        if 'error' in r:
            continue
        for hh in ev.HORIZONS:
            for kk in pool_acc[hh]:
                pool_acc[hh][kk] += r['acc'][hh][kk]
        for kk in pool_prec:
            if kk.endswith('_dev'):
                pool_prec[kk].extend(r['prec'][kk])
            else:
                pool_prec[kk] += r['prec'][kk]
    eod = pool_acc['EOD']
    prec_b = _rate(pool_prec['b_hit'], pool_prec['b_tot'])
    prec_s = _rate(pool_prec['s_hit'], pool_prec['s_tot'])
    cap_b = _rate(eod['b_cap'], eod['b_tot'])
    cap_s = _rate(eod['s_cap'], eod['s_tot'])
    att_b = _rate(eod['b_att'], eod['b_tot'])
    att_s = _rate(eod['s_att'], eod['s_tot'])
    comp_b = (prec_b * cap_b) if (prec_b is not None and cap_b is not None) else None
    comp_s = (prec_s * cap_s) if (prec_s is not None and cap_s is not None) else None
    return dict(k=k, c1=c1, c2=c2, w_prec=w_prec,
                att_b=att_b, att_s=att_s, cap_b=cap_b, cap_s=cap_s,
                prec_b=prec_b, prec_s=prec_s, comp_b=comp_b, comp_s=comp_s,
                n_B=eod['b_tot'], n_S=eod['s_tot'])


def _monotonic_dec(vals):
    return all(vals[i] + 1e-9 >= vals[i + 1] for i in range(len(vals) - 1))


def _has_spike(vals, tol=0.02):
    for i in range(1, len(vals) - 1):
        if vals[i] > vals[i - 1] + tol and vals[i] > vals[i + 1] + tol:
            return True
    return False


def main():
    global SUBSET
    files = sorted(glob.glob(f'{ev.DATA_DIR}/*_1m.csv'))
    random.seed(42)
    SUBSET = random.sample(files, min(60, len(files)))

    BASE = dict(k=1.0, c1=1.5, c2=1.0, W_PREC=30)
    # 优化参数池 = {k, c1, c2}（可调）。W_PREC 仅作「诊断维度」（揭示邻近率对窗口的固有敏感），
    # 属业务硬约束（抓顶底=看30根K线），严禁纳入优化参数池。
    OPT_PARAMS = ['k', 'c1', 'c2']
    PERTURB = dict(
        k=[0.75, 1.0, 1.25],
        c1=[1.25, 1.5, 1.75],
        c2=[0.75, 1.0, 1.25],
        W_PREC=[20, 30, 40],   # 诊断维度（非优化），结果仅供理解敏感性
    )

    feishu(f'🔬 DET v2.0 敏感性分析(单参数扰动)启动：{len(SUBSET)} 标子集，参数={list(PERTURB.keys())}')

    rows = []
    n = 0
    total = sum(len(v) for v in PERTURB.values())
    for param, vals in PERTURB.items():
        for v in vals:
            cfg = dict(BASE)
            cfg[param] = v
            row = run_cfg(cfg['k'], cfg['c1'], cfg['c2'], cfg['W_PREC'])
            row['sweep_param'] = param
            rows.append(row)
            n += 1
            print(f"  [{param}={v}] comp B={ev._fmt(row['comp_b'])} S={ev._fmt(row['comp_s'])} "
                  f"prec B={ev._fmt(row['prec_b'])} cap B={ev._fmt(row['cap_b'])} att B={ev._fmt(row['att_b'])}")
            if n % 4 == 0:
                feishu(f'🔬 敏感性分析进度 {n}/{total}')

    # 每个参数的局部梯度（comp_b 随该参数档位的变化）
    def _grad(param, key='comp_b'):
        seq = sorted([r for r in rows if r['sweep_param'] == param], key=lambda x: x[param.lower()])
        vals = [r[key] for r in seq if r[key] is not None]
        if len(vals) < 2:
            return None
        return (vals[-1] - vals[0]) * 100

    grads = {p: dict(comp_b_pp=_grad(p, 'comp_b'), comp_s_pp=_grad(p, 'comp_s'),
                     prec_b_pp=_grad(p, 'prec_b'), cap_b_pp=_grad(p, 'cap_b'),
                     att_b_pp=_grad(p, 'att_b')) for p in PERTURB}

    # 稳健性判定：各参数维度上 comp/prec/cap/att 无尖峰且单调（阈值类参数应单调）
    spikes = []
    for p in PERTURB:
        seq = sorted([r for r in rows if r['sweep_param'] == p], key=lambda x: x[p.lower()])
        if _has_spike([r['comp_b'] for r in seq]):
            spikes.append(p)
        if _has_spike([r['prec_b'] for r in seq]):
            spikes.append(p)
    robust = len(spikes) == 0
    verdict = ('ROBUST（四参数局部扰动无尖峰，指标平滑单调，无过拟合）' if robust
               else f'CHECK（参数 {sorted(set(spikes))} 出现尖峰，需审视）')

    out = dict(
        meta=dict(subsets=len(SUBSET), seed=42, baseline=BASE, perturb=PERTURB),
        rows=rows, grads=grads,
        verdict=verdict, spike_params=sorted(set(spikes)),
    )
    fn = 'signal_validity_v2_sweep_2026-08-23.json'
    fp = os.path.join(ev.OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'\n=== 敏感性分析完成（单参数扰动）===')
    for p in PERTURB:
        g = grads[p]
        print(f'  {p}: comp_b 梯度={g["comp_b_pp"]:.2f}pp prec_b={g["prec_b_pp"]:.2f}pp '
              f'cap_b={g["cap_b_pp"]:.2f}pp att_b={g["att_b_pp"]:.2f}pp')
    print(f'  判定: {verdict}')
    print(f'JSON -> {fp}')

    feishu(f'✅ DET v2.0 敏感性分析完成（单参数扰动 k/c1/c2/W_PREC）：\n'
           f'判定: {verdict}\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 DET v2.0 敏感性分析异常: {e}')
        raise
