# -*- coding: utf-8 -*-
r"""tests/test_rsi_loop.py — RSI 循环守卫逻辑测试（P2.3，2026-09-28）

覆盖：
  1. spec_freeze：hash 稳定性 / FROZEN_SURFACE 黑名单拦截 / spec_hash 漂移拒绝 /
     结构缺字段拒绝 / 仓库外路径拒绝
  2. gates.compare：闸一容差（net_wr −1pp / mean_net 不恶化）/ 闸二保留率 /
     闸四 z 不降——合成 bench JSON 判定
  3. rsi_agent：state 断点续跑语义（mark/step）
不碰真实 git/飞书/monitor；全部合成数据。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, os.path.join(ROOT, 'scripts', 'ai_loop'))

import spec_freeze  # noqa: E402
import gates  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail else ""))


def write_patch(fp, files, spec_hash_expected='X' * 64, **kw):
    doc = {'proposal_id': 'T-TEST', 'hypothesis': 'h', 'knowledge_clause': 'k',
           'files': files, 'validation_plan': 'v', 'rollback': 'r',
           'spec_hash_expected': spec_hash_expected}
    doc.update(kw)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    return fp


def main():
    tmp = tempfile.mkdtemp(prefix='rsi_test_')
    try:
        # ---- spec_freeze ----
        h1 = spec_freeze.spec_hash()
        h2 = spec_freeze.spec_hash()
        check("R1 spec_hash 稳定（同输入同 hash）", h1 == h2 and len(h1) == 64)

        # 正常 patch（scripts/ 内文件 + 正确 spec_hash）
        good = write_patch(os.path.join(tmp, 'good.json'),
                           ['scripts/research/some_analysis.py'], spec_hash_expected=h1)
        ok, reasons, _ = spec_freeze.validate_patch(good)
        check("R2 正常 patch 过（scripts/ 文件 + hash 匹配）", ok, str(reasons))

        # 黑名单拦截
        for frozen in ('core/monitor.py', 'data/watchlist.json',
                       'data/monitor_config.json'):
            p = write_patch(os.path.join(tmp, 'frozen.json'), [frozen])
            ok, reasons, _ = spec_freeze.validate_patch(p)
            check(f"R3 黑名单拦截 {frozen}", not ok and any('黑名单' in r for r in reasons))

        # spec_hash 漂移拒绝
        p = write_patch(os.path.join(tmp, 'drift.json'),
                        ['scripts/research/x.py'], spec_hash_expected='0' * 64)
        ok, reasons, _ = spec_freeze.validate_patch(p)
        check("R4 spec_hash 漂移拒绝（锁人审）",
              not ok and any('spec_hash 漂移' in r for r in reasons), str(reasons))

        # 缺字段
        with open(os.path.join(tmp, 'bad.json'), 'w') as f:
            json.dump({'proposal_id': 'T'}, f)
        ok, reasons, _ = spec_freeze.validate_patch(os.path.join(tmp, 'bad.json'))
        check("R5 缺必填字段拒绝", not ok and any('必填字段' in r for r in reasons))

        # 仓库外路径
        p = write_patch(os.path.join(tmp, 'abs.json'), [r'C:\Windows\system32\x.py'])
        ok, reasons, _ = spec_freeze.validate_patch(p)
        check("R6 绝对路径拒绝", not ok and any('仓库外' in r for r in reasons))
        p = write_patch(os.path.join(tmp, 'esc.json'), ['../outside.py'])
        ok, reasons, _ = spec_freeze.validate_patch(p)
        check("R7 上跳路径拒绝", not ok and any('仓库外' in r for r in reasons))

        # ---- gates.compare（合成 bench） ----
        def mk_bench(net_wr, mean_net, n_trips, z):
            return {'pooled': {'net_wr': net_wr, 'mean_net': mean_net, 'n_trips': n_trips},
                    'random_control': {'z_netwr': z, 'p_netwr': 0.001}}

        base_fp = os.path.join(tmp, 'base.json')
        with open(base_fp, 'w') as f:
            json.dump(mk_bench(0.5213, -0.124, 7499, 62.8), f)

        # 全过：wr 微降 0.5pp（容差内）、mean_net 改善、z 不降
        cand_fp = os.path.join(tmp, 'cand_ok.json')
        with open(cand_fp, 'w') as f:
            json.dump(mk_bench(0.5163, -0.110, 7300, 63.0), f)
        ok, _ = gates.compare(cand_fp, base_fp)
        check("R8 compare：容差内改善 PASS", ok)

        # 闸一 FAIL：wr 降 2pp（超容差）
        cand_fp2 = os.path.join(tmp, 'cand_wr.json')
        with open(cand_fp2, 'w') as f:
            json.dump(mk_bench(0.5013, -0.110, 7300, 63.0), f)
        ok, _ = gates.compare(cand_fp2, base_fp)
        check("R9 compare：net_wr 降 2pp FAIL", not ok)

        # 闸一 FAIL：mean_net 恶化
        cand_fp3 = os.path.join(tmp, 'cand_mn.json')
        with open(cand_fp3, 'w') as f:
            json.dump(mk_bench(0.5213, -0.200, 7300, 63.0), f)
        ok, _ = gates.compare(cand_fp3, base_fp)
        check("R10 compare：mean_net 恶化 FAIL", not ok)

        # 闸二 FAIL：保留率 <90%
        cand_fp4 = os.path.join(tmp, 'cand_keep.json')
        with open(cand_fp4, 'w') as f:
            json.dump(mk_bench(0.5213, -0.110, 6000, 63.0), f)
        ok, _ = gates.compare(cand_fp4, base_fp)
        check("R11 compare：保留率 80% FAIL", not ok)

        # 闸四 FAIL：z 降低
        cand_fp5 = os.path.join(tmp, 'cand_z.json')
        with open(cand_fp5, 'w') as f:
            json.dump(mk_bench(0.5213, -0.110, 7300, 55.0), f)
        ok, _ = gates.compare(cand_fp5, base_fp)
        check("R12 compare：z 降低 FAIL", not ok)

        # ---- rsi_agent state 断点续跑 ----
        import rsi_agent  # noqa: E402
        # 重定向 RSI_DIR 到 tmp（不污染真实 data/rsi）
        rsi_agent.RSI_DIR = tmp
        rsi_agent.state_path = lambda d: os.path.join(tmp, f'{d}.state.json')
        rsi_agent.mark_step('2026-09-28', 'selfcheck', 0, 'note')
        rsi_agent.mark_step('2026-09-28', 'measure', 0, 'light')
        st = rsi_agent.step_status('2026-09-28', 'selfcheck')
        check("R13 state mark/step 语义", st and st['rc'] == 0)
        st2 = rsi_agent.step_status('2026-09-28', 'measure')
        check("R14 多步共存不覆盖", st2 and st2['note'] == 'light')
        st3 = rsi_agent.step_status('2026-09-28', 'apply-code')
        check("R15 未执行步 status=None", st3 is None)

        # 队列语义：apply-code 频控（合成队列；日期必须动态取今天——硬编码日期跨午夜即假败，2026-09-29 实证）
        import datetime as _dt
        _today = _dt.date.today().strftime('%Y-%m-%d')
        qfp = os.path.join(tmp, 'validity_queue.json')
        with open(qfp, 'w') as f:
            json.dump([{'proposal_id': 'X', 'status': 'active',
                        'applied_at': f'{_today} 10:00:00'}], f)
        rsi_agent.QUEUE_FP = qfp
        n_today = sum(1 for q in json.load(open(qfp))
                      if q.get('status') == 'active'
                      and q.get('applied_at', '').startswith(_today))
        check("R16 每日代码合入频控判定", n_today == 1 and n_today >= rsi_agent.DAILY_CODE_MERGE_MAX)

        # metric_trend 趋势基板（Q1 测量，2026-09-29）：合成 bench_baseline → 行字段齐 + 幂等
        with open(os.path.join(tmp, 'bench_baseline.json'), 'w') as f:
            json.dump({'tag': 't', 'generated_at': 'x',
                       'pooled': {'n_trips': 100, 'net_wr': 0.52, 'mean_net': -0.1,
                                  'pl_ratio': 0.6},
                       'random_control': {'z_netwr': 10.0}}, f)
        rsi_agent.TREND_FP = os.path.join(tmp, 'metric_trend.jsonl')
        ok1 = rsi_agent.append_metric_trend('2099-01-05')
        rows = [json.loads(l) for l in open(rsi_agent.TREND_FP, encoding='utf-8') if l.strip()]
        r0 = rows[0]
        check("R17 metric_trend 行字段齐",
              ok1 and len(rows) == 1 and r0['pool']['net_wr_pct'] == 52.0
              and r0['pool']['mean_net_pp'] == -0.1
              and r0['t1_gap']['net_wr_gap_pp'] == 3.0
              and r0['t1_gap']['mean_net_positive'] is False
              and r0['regime_gate'] is not None)
        ok2 = rsi_agent.append_metric_trend('2099-01-05')
        rows2 = [l for l in open(rsi_agent.TREND_FP, encoding='utf-8') if l.strip()]
        check("R18 metric_trend 同日幂等不重复", (not ok2) and len(rows2) == 1)

        # live 补全升级：已有行 live=None，出现 live_review 后原位更新（不增行）
        live_dir = os.path.join(tmp, 'output')
        os.makedirs(live_dir, exist_ok=True)
        with open(os.path.join(live_dir, 'live_review_2099-01-05.json'), 'w') as f:
            json.dump({'summary': {'n_trips': 3, 'valid_rate_pct': 66.7,
                                   'net_sum_pct': 0.5, 'avg_net_pct': 0.17}}, f)
        _orig_root = rsi_agent.ROOT
        rsi_agent.ROOT = tmp  # build_trend_row 读 ROOT/output 与 ROOT/data
        os.makedirs(os.path.join(tmp, 'data'), exist_ok=True)
        with open(os.path.join(tmp, 'data', 'monitor_config.json'), 'w') as f:
            json.dump({'_global': {'general_algorithm': {'regime_gate': True}}}, f)
        ok3 = rsi_agent.append_metric_trend('2099-01-05')
        rows3 = [json.loads(l) for l in open(rsi_agent.TREND_FP, encoding='utf-8') if l.strip()]
        check("R19 live 补全升级原位更新",
              ok3 and len(rows3) == 1 and rows3[0]['live']['n_trips'] == 3
              and rows3[0]['regime_gate'] is True
              and 'live_backfilled_at' in rows3[0])
        # 升级后再次调用 → 幂等跳过
        ok4 = rsi_agent.append_metric_trend('2099-01-05')
        rows4 = [l for l in open(rsi_agent.TREND_FP, encoding='utf-8') if l.strip()]
        check("R20 升级后幂等", (not ok4) and len(rows4) == 1)
        rsi_agent.ROOT = _orig_root
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
