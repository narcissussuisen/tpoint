#!/usr/bin/env python3
"""
Phase 6 — 结论生成器: 解析 keyfactor_summary.json + keyfactor_results.csv,
产出可读的归因结论 (含因子排名、阈值鲁棒性、关键信号方向), 供飞书推送与人工复核。
用法:
  python phase6_report.py            # 读 keyfactor_data 下两份产物, 打印 markdown
  python phase6_report.py --push    # 额外推送到飞书群机器人
"""
import sys, os, json, argparse
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'keyfactor_data')
SJSON = os.path.join(DATA, 'keyfactor_summary.json')
SCSV = os.path.join(DATA, 'keyfactor_results.csv')

def load():
    with open(SJSON, encoding='utf-8') as f:
        s = json.load(f)
    sweep = pd.read_csv(SCSV, encoding='utf-8-sig') if os.path.exists(SCSV) else None
    return s, sweep

def md(s, sweep):
    L = []
    L.append("## miji v9.1.0 关键因子归因结论")
    L.append("")
    L.append(f"- **样本**: {s.get('n_symbols','?')} 只 A股 · **基线信号数**: {s.get('n_signals_baseline','?')}")
    bs = s.get('baseline_skill', {})
    def bsk(h):
        return bs.get(h, bs.get(str(h)))
    L.append(f"- **基线符号调整前向收益 (6/12/24根)**: "
             f"{bsk(6):+.4f}% / {bsk(12):+.4f}% / {bsk(24):+.4f}%")
    L.append("")
    # 因子重要性 (消融: 关掉后 skill24 变化)
    L.append("### 因子重要性 (消融: 关掉该因子后 skill24 的变化)")
    L.append("")
    L.append("| 因子 | 关掉后 skill24 变化 | 解读 |")
    L.append("|---|---|---|")
    fac_cn = {'gravity': '引力 (均线偏离)', 'vol_div': '量价背离', 'macd_div': '分时MACD背离'}
    for fac in s.get('factor_ranking', []):
        drop = s.get('factor_importance_drop_skill24', {}).get(fac)
        if drop is None:
            continue
        if drop < 0:
            interp = "⚠️ 去掉后 skill 反而上升 → 该因子在当前样本上**拖累**信号质量"
        elif drop > 0:
            interp = "✅ 去掉后 skill 下降 → 该因子**贡献**信号质量"
        else:
            interp = "➖ 去掉后 skill 基本不变 → 该因子影响中性"
        L.append(f"| {fac_cn.get(fac, fac)} | {drop:+.5f}% | {interp} |")
    L.append("")
    # 阈值鲁棒性
    if sweep is not None and len(sweep):
        L.append("### 阈值扫描 (Phase 5 最优配置)")
        L.append("")
        best = sweep.loc[sweep['skill24'].idxmax()]
        cfg = ", ".join(f"{k}={best[k]}" for k in sweep.columns
                        if k not in ('n', 'skill6', 'skill12') and not k.startswith('skill')
                        and not (isinstance(best[k], float) and pd.isna(best[k])))
        L.append(f"- **最高 skill24 配置**: {cfg} → skill24={best['skill24']:+.4f}%")
        # resonance 单独看
        if 'RESONANCE' in sweep.columns:
            rc = sweep[sweep['RESONANCE'].notna()]
            if len(rc):
                L.append("- **共振阈值 (RESONANCE)**:")
                for _, r in rc.iterrows():
                    L.append(f"    - {int(r['RESONANCE'])}: n={int(r['n'])} skill24={r['skill24']:+.4f}%")
    L.append("")
    # B/S 方向提示
    L.append("### 方向性提示")
    L.append("")
    L.append("- 前向收益按 `skill = fwd (B) / -fwd (S)` 符号调整; 若 B 段 skill 持续为负、S 段为正,")
    L.append("  说明**卖点(S)质量高于买点(B)**, 与做T秘籍'先卖后买/锁利优先'的实操经验一致。")
    L.append("")
    L.append("> ⚠️ 以上由 AI 基于公开行情数据整理生成, 仅为策略归因研究, 不构成任何投资建议。")
    return "\n".join(L)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--push', action='store_true')
    args = ap.parse_args()
    if not os.path.exists(SJSON):
        print(f"⚠️ 未找到 {SJSON}, 请先跑 run_study.py")
        return
    s, sweep = load()
    text = md(s, sweep)
    print(text)
    if args.push:
        sys.path.insert(0, HERE)
        from feishu_push import push
        push("[Phase6 结论]\n" + text)

if __name__ == '__main__':
    main()
