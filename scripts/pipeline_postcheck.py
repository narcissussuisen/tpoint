# -*- coding: utf-8 -*-
r"""pipeline_postcheck.py — 15:30 复盘流水线的语义后检（2026-09-29）

背景
----
原架构中，15:30 `run_daily_review.bat`（12 步机械流水线）跑完后，由 WorkBuddy 自动化
会话 `automation-1785721171231`（15:35）做「验证 + 兜底」，每交易日消耗约 9.4 万 token。
该会话自 2026-09-01 起连续 19 个交易日未写执行记忆，且其校验项写死了 `regime_gate=true`
（2026-09-29 已人审关闭为 false）⇒ 每日必报「配置偏差」，而真正的语义校验已无覆盖。
2026-09-29 人审决定：停用该 AI 会话，把它的语义校验脚本化到本模块，接到 bat 末尾。

机械失败（步骤 rc≠0 / 产物缺失）已由 `pipeline_status.py summarize --push-fail` 覆盖
（bat 第 138-143 行，exit /b 2 + 推 b4eba7a9）。**本模块只补语义层**：
产物内容是否当日、报告结构是否完整、成交口径硬门槛、日志 marker、数据质量哨兵。

校验项
------
硬校验（失败 → rc=1，并推飞书）：
  F1 五个 JSON 存在且顶层 `date == 目标日`（review / live_review / reconcile /
     factor_opt / next_day_algo）；缺失或日期不符 → 前置缺失 → rc=2
  F2 `output/review_<date>.html` 的 `<h2>` 恰为 6 节，且序号前缀为 〇一三四五 齐
     （按序号计数，不绑措辞，避免版本耦合）
  F3 B5 硬门槛：`live_review` 每笔 `|entry_price - entry_bar_close| / entry_bar_close
     <= 0.1%`（生产统一按信号 bar close 入账，不得使用推送价）；trips 为空 = N/A 不判失败
  F4 `logs/daily_review.log` 最后一个 `=== tpoint daily review <date> ===` 之后的当日段，
     5 个 marker 齐全（REPORT_PUSH(a35d7f52): OK / GLOBAL_PUSH(b4eba7a9): OK /
     done (daily iterate) / done (closed loop) / done (auto tune)）
  F5 数据质量哨兵（对齐 prod_vs_bt_reconcile 的既有字段）：
     `reconcile.symbols[sym].live_counts.total == 0 且 recalc_n_signals >= 2`
     （疑似落盘断流），或 `live_counts` 内出现 `state_mismatch`（state 与明细不一致）
软校验（失败只记 WARN，不影响 rc，不推送）：
  S1 主 KPI 区 `class="kpi"` 元素 >= 7（布局驱动、信号弱，故不判失败）

返回码
------
  0  = 全部硬校验通过（软校验失败会打印 WARN 并写日志）
  1  = 语义校验失败（已推 a35d7f52 详情 + b4eba7a9 一行）
  2  = 前置产物缺失（五 JSON / HTML / 当日日志段任一缺失；已推告警）
  77 = 非交易日（防御性；bat 已在 15:30 早退 77，正常不走到）

用法
----
  venv/Scripts/python.exe scripts/pipeline_postcheck.py --date 2026-09-29
  venv/Scripts/python.exe scripts/pipeline_postcheck.py --date 2026-09-29 --no-push
  venv/Scripts/python.exe scripts/pipeline_postcheck.py --date 2099-01-05 --ignore-calendar
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')
LOG_PATH = os.path.join(ROOT, 'logs', 'daily_review.log')

# 与 daily_report_push.py / daily_closed_loop.py 保持一致（单一真源未收敛前的既有约定）
HOOK_REPORT = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
HOOK_GLOBAL = 'https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103'

# 需要校验 date 字段的产物（文件名前缀 → 是否 JSON）
JSON_ARTIFACTS = ('review', 'live_review', 'reconcile', 'factor_opt', 'next_day_algo')
HTML_ARTIFACT = 'review'

# F2：六节序号（〇 为 U+3007，非汉字「零」）
EXPECTED_SECTION_ORDINALS = ('〇', '一', '二', '三', '四', '五')

# F4：当日段必须出现的 marker
LOG_MARKERS = (
    'REPORT_PUSH(a35d7f52): OK',
    'GLOBAL_PUSH(b4eba7a9): OK',
    '=== done (daily iterate) ===',
    '=== done (closed loop) ===',
    '=== done (auto tune) ===',
)

B5_TOL = 1e-3          # 0.1%
KPI_MIN = 7

try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load_json(path):
    """返回 (doc, err)。err 非空 = 读取/解析失败。"""
    if not os.path.exists(path):
        return None, '文件不存在'
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f), ''
    except Exception as e:                                  # noqa: BLE001
        return None, f'解析失败 {e!r}'


def _post(hook, text, retries=2):
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    last = ''
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(hook, data=payload,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return True, r.read().decode('utf-8', 'replace')
        except Exception as e:                              # noqa: BLE001
            last = repr(e)
            if i < retries:
                __import__('time').sleep(2)
    return False, last


def _pct(a, b):
    return abs(a - b) / b * 100.0 if b else float('inf')


# --------------------------------------------------------------------------- #
# 校验项
# --------------------------------------------------------------------------- #
def check_prereq(date):
    """F1 前置：五 JSON 存在 + date 相符。返回 (missing_list, detail_lines)。"""
    missing, detail = [], []
    for name in JSON_ARTIFACTS:
        path = os.path.join(OUT, f'{name}_{date}.json')
        doc, err = _load_json(path)
        if err:
            missing.append(f'{name}_{date}.json({err})')
            detail.append(f'  ❌ {name}: {err}')
            continue
        got = doc.get('date')
        if str(got) != date:
            missing.append(f'{name}_{date}.json(date={got})')
            detail.append(f'  ❌ {name}: date={got!r} 期望 {date}')
        else:
            detail.append(f'  ✅ {name}: date={date}')
    html = os.path.join(OUT, f'{HTML_ARTIFACT}_{date}.html')
    if not os.path.exists(html):
        missing.append(f'{HTML_ARTIFACT}_{date}.html(缺失)')
        detail.append('  ❌ review HTML: 文件不存在')
    else:
        detail.append(f'  ✅ review HTML: {os.path.getsize(html)}B')
    return missing, detail


def check_b5(date):
    """F3：B5 硬门槛。返回 (ok, msg, detail_lines)。"""
    doc, err = _load_json(os.path.join(OUT, f'live_review_{date}.json'))
    if err:
        return False, f'live_review 不可读（{err}）', []
    trips = doc.get('trips') or []
    if not trips:
        return True, 'trips=0，N/A（无样本不判失败）', ['  ⚪ B5：trips=0，N/A']
    bad, lines, worst = [], [], 0.0
    for t in trips:
        ep, eb = t.get('entry_price'), t.get('entry_bar_close')
        if ep is None or eb is None or not eb:
            bad.append(f'{t.get("sym")}@{t.get("entry_time")} 字段缺失')
            continue
        dev = _pct(float(ep), float(eb))
        worst = max(worst, dev)
        if dev > B5_TOL * 100:
            bad.append(f'{t.get("sym")}@{t.get("entry_time")} 偏差 {dev:.4f}%')
    lines.append(f'  {"❌" if bad else "✅"} B5 硬门槛：{len(trips) - len(bad)}/{len(trips)} 笔 '
                 f'entry_price==entry_bar_close（最大偏差 {worst:.4f}% ≤0.1%）')
    if bad:
        lines += [f'      · {x}' for x in bad[:5]]
        return False, f'{len(bad)}/{len(trips)} 笔 entry_price 偏离 entry_bar_close', lines
    return True, '', lines


def check_html_sections(date):
    """F2：review HTML 六节。返回 (ok, msg, detail_lines)。"""
    path = os.path.join(OUT, f'{HTML_ARTIFACT}_{date}.html')
    if not os.path.exists(path):
        return False, 'review HTML 缺失', []
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except Exception as e:                                  # noqa: BLE001
        return False, f'HTML 读取失败 {e!r}', []
    h2 = [re.sub(r'<[^>]+>', '', x).strip()
          for x in re.findall(r'<h2[^>]*>(.*?)</h2>', text, re.S)]
    ordinals = [x[0] if x else '' for x in h2]
    ok = len(h2) == 6 and tuple(ordinals) == EXPECTED_SECTION_ORDINALS
    line = f'  {"✅" if ok else "❌"} 报告结构：<h2> {len(h2)} 节，序号 {"".join(ordinals)}' \
           f'（期望 6 节 / {"".join(EXPECTED_SECTION_ORDINALS)}）'
    return ok, ('' if ok else f'<h2> 结构不符（{len(h2)} 节，序号 {"".join(ordinals)}）'), [line]


def _log_section(date):
    """取最后一个 `=== tpoint daily review <date> ===` 之后的文本；无则 None。"""
    marker = f'=== tpoint daily review {date} ==='
    if not os.path.exists(LOG_PATH):
        return None
    try:
        text = open(LOG_PATH, encoding='utf-8', errors='replace').read()
    except Exception:                                       # noqa: BLE001
        return None
    idx = text.rfind(marker)
    return None if idx < 0 else text[idx:]


def check_log_markers(date):
    """F4：当日日志 marker。返回 (ok, msg, detail_lines)。"""
    section = _log_section(date)
    if section is None:
        return False, f'日志缺少当日段（{date}）', []
    missing = [m for m in LOG_MARKERS if m not in section]
    lines = [f'  {"❌" if missing else "✅"} 日志 marker：{len(LOG_MARKERS) - len(missing)}/'
             f'{len(LOG_MARKERS)} 齐']
    if missing:
        lines += [f'      · 缺 {m}' for m in missing]
        return False, f'日志缺 {len(missing)} 个 marker', lines
    return True, '', lines


def check_data_quality(date):
    """F5：数据质量哨兵。返回 (ok, msg, detail_lines)。
    非阻断性观测（对齐 15:35 会话原逻辑）：live 明细断流 或 state 与明细不一致。
    注意：watchlist 标的 live=0 但 recalc>=2 属疑似断流；两者皆 0 属正常（无推送）。"""
    doc, err = _load_json(os.path.join(OUT, f'reconcile_{date}.json'))
    if err:
        return True, '', [f'  ⚪ 数据质量：reconcile 不可读（{err}），跳过']
    issues, lines = [], []
    for sym, rep in (doc.get('symbols') or {}).items():
        lc = rep.get('live_counts') or {}
        total = lc.get('total')
        recalc = rep.get('recalc_n_signals') or 0
        if total == 0 and recalc >= 2:
            issues.append(f'{sym} live=0 vs recalc={recalc}（疑似落盘断流）')
        if 'state_mismatch' in lc:
            issues.append(f'{sym} state_mismatch Δ{lc.get("state_mismatch")} 笔')
    if issues:
        lines.append(f'  ❌ 数据质量哨兵：{len(issues)} 项')
        lines += [f'      · {x}' for x in issues[:5]]
        return False, f'数据质量告警 {len(issues)} 项：{"；".join(issues[:3])}', lines
    lines.append('  ✅ 数据质量哨兵：未触发')
    return True, '', lines


def check_kpi_soft(date):
    """S1 软校验：KPI 卡数。返回 detail_line。"""
    path = os.path.join(OUT, f'{HTML_ARTIFACT}_{date}.html')
    if not os.path.exists(path):
        return '  ⚪ KPI 卡数：HTML 缺失，跳过'
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except Exception:                                       # noqa: BLE001
        return '  ⚪ KPI 卡数：HTML 读取失败，跳过'
    n = len(re.findall(r'class="[^"]*\bkpi\b', text))
    return f'  {"✅" if n >= KPI_MIN else "⚠️"} KPI 卡数：{n}（软阈值 ≥{KPI_MIN}，不判失败）'


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(date, do_push=True, ignore_calendar=False):
    print(f'[pipeline_postcheck] date={date}')

    if not ignore_calendar:
        sys.path.insert(0, os.path.join(ROOT, 'core'))
        try:
            from trading_calendar import is_trading_day
            if not is_trading_day(date):
                print(f'[pipeline_postcheck] 非交易日 {date} → rc=77（防御性跳过）')
                return 77
        except Exception as e:                              # noqa: BLE001
            print(f'[pipeline_postcheck] WARN 交易日历不可用({e!r})，继续校验')

    # ---- 前置 ----
    missing, pre_lines = check_prereq(date)
    print('F1 产物齐备性：')
    print('\n'.join(pre_lines))
    if missing:
        text = (f'🚨 [tpoint 语义后检 前置缺失] date={date} rc=2\n'
                f'  ❌ {len(missing)} 项：{"；".join(missing[:6])}\n'
                '（机械层告警见 pipeline_status summarize --push-fail）')
        print(text)
        if do_push:
            ok1, r1 = _post(HOOK_REPORT, text)
            ok2, r2 = _post(HOOK_GLOBAL,
                            f'🚨 [tpoint 语义后检] {date} rc=2 前置产物缺失 {len(missing)} 项')
            print(f'[push] a35d7f52={r1[:80]} b4eba7a9={r2[:80]}')
        return 2

    # ---- 硬校验 ----
    fails, hard_lines = [], []
    for ok, msg, lines in (check_html_sections(date),
                           check_b5(date),
                           check_log_markers(date),
                           check_data_quality(date)):
        hard_lines += lines
        if not ok and msg:
            fails.append(msg)
    print('F2-F5 语义校验：')
    print('\n'.join(hard_lines))

    print('S1 软校验：')
    print(check_kpi_soft(date))

    if not fails:
        print(f'[pipeline_postcheck] ✅ 全部硬校验通过 rc=0')
        return 0

    text = (f'🚨 [tpoint 语义后检 FAIL] date={date} rc=1\n'
            + '\n'.join(f'  ❌ {x}' for x in fails)
            + '\n语义校验（替代原 15:35 AI 兜底会话）；详情见 logs/daily_review.log')
    print(text)
    if do_push:
        ok1, r1 = _post(HOOK_REPORT, text)
        ok2, r2 = _post(HOOK_GLOBAL,
                        f'🚨 [tpoint 语义后检] {date} rc=1 失败 {len(fails)} 项：'
                        + '；'.join(fails[:3]))
        print(f'[push] a35d7f52={r1[:80]} b4eba7a9={r2[:80]}')
    return 1


def main():
    ap = argparse.ArgumentParser(description='tpoint 15:30 流水线语义后检')
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--no-push', action='store_true', help='只校验不推送（冒烟/调试）')
    ap.add_argument('--ignore-calendar', action='store_true',
                    help='忽略交易日判定（历史回检 / 负路径冒烟）')
    a = ap.parse_args()
    return run(a.date, do_push=not a.no_push, ignore_calendar=a.ignore_calendar)


if __name__ == '__main__':
    sys.exit(main())
