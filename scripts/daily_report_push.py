#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tpoint 自迭代日报生成+推送（阶段2对账评估 + 阶段3日报推送，确定性脚本化）
2026-08-04 新增：原由 WorkBuddy 自动化 agent 逐日手工聚合，存在「卡回合不推送/推错群」风险，
现固化为 run_daily_review.bat 第8步，agent 只负责验证+兜底。

口径严格照 docs/daily_report_template.md：
- roll20 = data/roundtrip/*.jsonl 最近20个交易日；WR_prod_exec=live中ret_pct>0占比；
  WR_recalc同口径；G1=WR_recalc-WR_prod_exec；任一侧n<10 → 累积中，G1标--
- P = Σ已完成轮次权重 + 当前轮权重×轮内完成度（权重 R0=5/R1=15/R2=30/R3=20/R4=20/R5=10）
- 达标：R0=reconcile当日自动生成；R1起加 |G1|≤3pp（滚动）

推送目的地（2026-08-04 用户指定）：
- 日报全文 → tpoint自迭代报告群 a35d7f52
- 一行状态 → 全局通知群 b4eba7a9（全局规则二：任务状态不可静默）

用法: python scripts/daily_report_push.py --date 2026-08-04
"""
import argparse
import datetime
import glob
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE, 'data', 'iteration_state.json')
RT_GLOB = os.path.join(BASE, 'data', 'roundtrip', '*.jsonl')

REPORT_HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
GLOBAL_HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103'


def pipeline_health(today):
    """T1 失败语义透传（2026-09-03 自迭代闭环硬化方案 v2）：
    只读当日 step_status 中当前 run_id 的已有终态记录；任一 FAILED/DEGRADED/INTERRUPTED
    → 日报标题加 [DEGRADED] 前缀 + 风险节列明细。
    注意：本脚本在流水线中段执行（daily_iterate/closed_loop/auto_tune 尚未跑），
    NOT_RUN 不算异常；无 step_status 记录（T1 上线前历史重跑）时静默跳过。"""
    try:
        cur_path = os.path.join(BASE, 'data', 'step_status', 'current_run.json')
        with open(cur_path, encoding='utf-8') as f:
            rid = json.load(f).get('run_id')
        if not rid or not str(rid).startswith(today.replace('-', '')):
            return '', []
        day_path = os.path.join(BASE, 'data', 'step_status', f'{today}.jsonl')
        if not os.path.exists(day_path):
            return '', []
        finals = {}
        with open(day_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('run_id') == rid:
                    finals[r.get('step')] = r
        bad = []
        for step, rec in finals.items():
            st = rec.get('status')
            if st in ('FAILED', 'INTERRUPTED'):
                rc = rec.get('rc')
                bad.append(f"{step}={st}" + (f"(rc={rc})" if rc is not None else ''))
            elif st == 'DEGRADED':
                miss = [os.path.basename(m) for m in (rec.get('missing_outputs') or [])]
                bad.append(f"{step}=DEGRADED" + (f"(缺产物:{','.join(miss)})" if miss else ''))
            elif st == 'RUNNING' and step != 'daily_report':
                # 本步骤之前的步骤残留 RUNNING = record 丢失（进程中断）；daily_report 自身正在运行，排除
                bad.append(f'{step}=RUNNING残留(疑似中断)')
        if not bad:
            return '', []
        return '[DEGRADED] ', [f"⚠️ 流水线异常步骤（run={rid}）：{'、'.join(bad)}"]
    except Exception:
        return '', []

HOLIDAYS_2026 = {
    '2026-01-01', '2026-01-02', '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
    '2026-02-02', '2026-02-03', '2026-04-06', '2026-05-01', '2026-05-04', '2026-05-05',
    '2026-06-19', '2026-06-22', '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
    '2026-12-25',
}
WEEK_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

NEXT_PLAN = {
    'R0': ['R0 D{n}：验收 reconcile 当日自动生成（连续3日验收项）',
           '记录实盘/复算信号差，为 R1 首扫抑制修复积累基线'],
    'R1': ['R1 D{n}：首扫白名单 startup_suppress_min 灰度 + 重启去重指纹幂等',
           '监控每标的信号数差 ≤1笔/日、滚动 |G1|≤3pp'],
    'R2': ['R2 D{n}：mhd_mode=pct 阈值重标定 / ATR 相对阈值 grid（两段式验证）'],
    'R3': ['R3 D{n}：ML boost 灰度（p≥0.60 加分排序，不抑制信号），跟踪 boost 子集胜率'],
    'R4': ['R4 D{n}：exit_v3 三条件止损两段式验证'],
    'R5': ['R5 D{n}：regime 门控降频 + 每周漂移监控'],
}
WEEKLY_REVIEW_LINE = '（周五）任务B 16:30 周评审：汇总本周 reconcile + 探针漂移，决策维持/推进/回滚'


def push(hook, text, retries=2):
    """推文本到飞书群机器人；失败重试（今日实测 DNS 瞬时故障重试即恢复）。返回 (ok, resp)。"""
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(hook, data=payload,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return True, r.read().decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001
            last = repr(e)
            if i < retries:
                time.sleep(3)
    return False, last


def is_trade_day(ds):
    d = datetime.date.fromisoformat(ds)
    return d.weekday() < 5 and ds not in HOLIDAYS_2026


def trade_days_between(d0, d1):
    """[d0, d1] 区间内交易日列表（含端点）。"""
    out = []
    d = datetime.date.fromisoformat(d0)
    e = datetime.date.fromisoformat(d1)
    while d <= e:
        ds = d.isoformat()
        if is_trade_day(ds):
            out.append(ds)
        d += datetime.timedelta(days=1)
    return out


def load_roll20(today):
    files = sorted(glob.glob(RT_GLOB))[-20:]
    live, recalc, days = [], [], []
    for fp in files:
        ds = os.path.basename(fp).replace('.jsonl', '')
        if ds > today:
            continue
        days.append(ds)
        with open(fp, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                (live if r.get('source') == 'live' else recalc).append(r)
    def wr(rows):
        if len(rows) < 10:
            return None
        return round(sum(1 for r in rows if (r.get('ret_pct') or 0) > 0) / len(rows) * 100, 1)
    return wr(live), wr(recalc), len(live), len(recalc), days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    a = ap.parse_args()
    today = a.date
    wd = datetime.date.fromisoformat(today).weekday()
    week_cn = WEEK_CN[wd]
    is_friday = (wd == 4)

    state = json.load(open(STATE_PATH, encoding='utf-8'))
    rnd = state.get('current_round', 'R0')
    rname = state.get('round_name', '')
    started = state.get('round_started', today)
    round_days = trade_days_between(started, today)
    d_in_round = len(round_days)

    # ---------- 阶段2：滚动20日聚合 ----------
    wr_live, wr_recalc, n_live, n_recalc, rt_days = load_roll20(today)
    g1 = round(wr_recalc - wr_live, 1) if (wr_live is not None and wr_recalc is not None) else None

    # ---------- 达标判定 ----------
    rec_path = os.path.join(BASE, 'output', f'reconcile_{today}.json')
    rec_ok = os.path.exists(rec_path)
    pool = {}
    if rec_ok:
        try:
            pool = json.load(open(rec_path, encoding='utf-8')).get('pool', {})
        except Exception:  # noqa: BLE001
            pass
    if rnd == 'R0':
        passed = rec_ok
    else:
        passed = rec_ok and (g1 is None or abs(g1) <= 3)
    already_ran = str(state.get('last_run', ''))[:10] == today  # 幂等：同日重跑不重复累计
    if not already_ran:
        state['consecutive_pass_days'] = (state.get('consecutive_pass_days', 0) + 1) if passed else 0
    elif not passed:
        state['consecutive_pass_days'] = 0
    k = state['consecutive_pass_days']

    # ---------- 达成度 ----------
    weights = state.get('goal_progress', {}).get('round_weights',
              {'R0': 5, 'R1': 15, 'R2': 30, 'R3': 20, 'R4': 20, 'R5': 10})
    order = ['R0', 'R1', 'R2', 'R3', 'R4', 'R5']
    done_w = sum(weights[r] for r in order if r < rnd)
    gp = state.setdefault('goal_progress', {})
    if rnd == 'R0':
        r0a = gp.setdefault('r0_acceptance', {'replay_0731_reproduced': True, 'reconcile_3d_streak': 0})
        streak = 0
        for ds in reversed(trade_days_between('2026-08-03', today)):
            if os.path.exists(os.path.join(BASE, 'output', f'reconcile_{ds}.json')):
                streak += 1
            else:
                break
        r0a['reconcile_3d_streak'] = min(3, streak)
        inner = ((1.0 if r0a.get('replay_0731_reproduced') else 0.0) + min(1.0, streak / 3)) / 2
    else:
        inner = min(1.0, k / 5)
    p_pct = round(done_w + weights.get(rnd, 0) * inner, 1)
    bar = '█' * int(p_pct // 10) + '░' * (10 - int(p_pct // 10))

    # ---------- 写回 state ----------
    state['roll20'] = {
        'wr_prod_exec': wr_live, 'wr_recalc': wr_recalc, 'g1_pp': g1,
        'n_live': n_live, 'n_recalc': n_recalc, 'as_of': today,
        'note': '双侧n<10 累积中' if (wr_live is None or wr_recalc is None) else '满窗',
    }
    gp['p_pct'] = p_pct
    gp['as_of'] = today
    state['last_run'] = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    hist = state.setdefault('history', [])
    if not any(h.get('date') == today and '日报' in h.get('event', '') for h in hist):
        hist.append({'date': today, 'event': f'{rnd} D{d_in_round} 日报',
                     'note': f'reconcile={"OK" if rec_ok else "缺失"}; roll20 live n={n_live}/recalc n={n_recalc}; '
                             f'G1={g1 if g1 is not None else "--"}; 连续达标{k}日; P={p_pct}%'})
    json.dump(state, open(STATE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    # ---------- 阶段3：生成日报 ----------
    q = f'{round(wr_live / 55 * 100, 1)}' if wr_live is not None else '--'
    wr_live_s = f'{wr_live}%（n={n_live}）' if wr_live is not None else f'累积中（n={n_live}）'
    wr_re_s = f'{wr_recalc}%（n={n_recalc}）' if wr_recalc is not None else f'累积中（n={n_recalc}）'
    g1_s = f'{g1:+}pp' if g1 is not None else '--'
    pool_wr = pool.get('wr_recalc')
    pool_line = (f"当日对账：复算{pool.get('n_recalc_trips', 0)}笔 WR={pool_wr}%、"
                 f"实盘配对{pool.get('n_live_trips', 0)}笔") if pool else '当日 reconcile 缺失'
    last_note = hist[-2]['note'] if len(hist) >= 2 else hist[-1]['note']

    nxt_days_placeholder = None  # 次日交易日按下述 while 计算
    d_next = datetime.date.fromisoformat(today) + datetime.timedelta(days=1)
    while not is_trade_day(d_next.isoformat()):
        d_next += datetime.timedelta(days=1)
    nxt_label = f"{d_next.strftime('%m-%d')} {WEEK_CN[d_next.weekday()]}"
    plans = [p.replace('{n}', str(d_in_round + 1)) for p in NEXT_PLAN.get(rnd, ['按计划当前轮推进'])]
    sec4_title = '四、下周工作计划（含周一首个交易日动作）' if is_friday else f'四、次日工作计划（{nxt_label}）'
    if is_friday:
        plans = [p.replace('次日', '下周一') for p in plans] + [WEEKLY_REVIEW_LINE]

    dg_prefix, dg_lines = pipeline_health(today)
    lines = [
        f'{dg_prefix}【tpoint 自迭代日报】{today}（{week_cn}·交易日）｜当前轮次 {rnd} {rname} D{d_in_round}',
        '',
        '■ 一、目标达成度',
        '· 终极目标（阶段一）：WR_prod_exec(滚动20日)≥55% 且 |G1|≤1pp（对齐回测 C_prod 56.2%）',
        f'· 综合达成度：{bar} {p_pct}%（轮次加权 {done_w}% + 本轮 {round(inner * 100, 1)}%×权重{weights.get(rnd, 0)}）',
        '· 核心指标（滚动20交易日）：',
        f'  WR_prod_exec {wr_live_s}｜WR_recalc {wr_re_s}｜G1 {g1_s}',
        f'· 指标侧校验：WR_prod_exec/55% = {q}%｜连续达标 {k}/5 日（进下一轮门槛）',
        '  ※ 样本<10笔标「累积中」；数据缺口（F盘07-17~07-30）致满窗不早于 2026-08-24',
        '',
        '■ 二、当日已完成关键任务（量化）',
        f'1. 复盘流水线+对账：reconcile {"自动生成" if rec_ok else "缺失"}；{pool_line}',
        f'2. 迭代状态：{last_note}',
        '',
        '■ 三、主要阻碍/风险',
        ('1. 无' if rec_ok else '1. 当日 reconcile 缺失 → 需人工检查 run_daily_review.bat 日志'),
        *dg_lines,
        '',
        f'■ {sec4_title}',
        *[f'{i + 1}. {p}' for i, p in enumerate(plans)],
        '',
        '■ 五、需要您决策/协调的事项',
        '1. 无',
    ]
    report = '\n'.join(lines)
    if k >= 5:
        report = '✅ 本轮验收达标，待周五评审决策进下一轮\n' + report

    # ---------- 推送 ----------
    if g1 is not None and g1 > 5 and n_live >= 10:
        ok_a, resp_a = push(REPORT_HOOK, f'⚠️ tpoint 执行差距告警 {today}：G1={g1:+}pp > 5pp（滚动 n={n_live}），请关注首扫抑制/重放问题')
        print('ALERT_PUSH:', 'OK' if ok_a else f'FAIL {resp_a}')
    ok1, resp1 = push(REPORT_HOOK, report)
    print('REPORT_PUSH(a35d7f52):', 'OK' if ok1 else f'FAIL {resp1}')
    brief = (f'[任务状态] tpoint每日复盘+对账 {today}：{"成功" if rec_ok and ok1 else "异常"} '
             f'| {rnd} D{d_in_round} P={p_pct}% 连续达标{k}/5日 '
             f'| roll20 live n={n_live} recalc n={n_recalc} G1={g1_s} '
             f'| 日报已推 tpoint自迭代报告群' + ('' if ok1 else f'（日报推送失败: {resp1}）'))
    ok2, resp2 = push(GLOBAL_HOOK, brief)
    print('GLOBAL_PUSH(b4eba7a9):', 'OK' if ok2 else f'FAIL {resp2}')
    if not (ok1 and ok2):
        sys.exit(1)


if __name__ == '__main__':
    main()
