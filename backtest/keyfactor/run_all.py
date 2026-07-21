#!/usr/bin/env python3
"""
关键因子归因研究 — 总编排 (Phase 1 之后):
  25%  Phase1 下载完成 (数据就绪)
  50%  Phase3 归因完成
  75%  Phase4 消融 + Phase5 阈值扫描完成
  100% Phase6 结论生成
依赖: download_1m.py 已落地 keyfactor_data/1m/ ; run_study.py 的 phase3/4/5 ;
        phase6_report.py 的 md/load ; feishu_push.py 的 push。
用法:
  python run_all.py                 # 跑全量 (1m 目录下所有有效 CSV)
  python run_all.py --seedtest     # 仅验证 7 seed (不推送飞书)
  python run_all.py --no-push      # 跑全量但不推送飞书
"""
import sys, os, argparse, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

import run_study as RS
from feishu_push import push
from phase6_report import md, load

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
DEF_IN = KEYFACTOR_1M_DIR

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--indir', default=DEF_IN)
    ap.add_argument('--seedtest', action='store_true')
    ap.add_argument('--no-push', action='store_true', help='不推送飞书')
    args = ap.parse_args()
    do_push = (not args.no_push) and (not args.seedtest)

    indir = os.path.join(HERE, '..', '..', 'backtest', 'backtest_data') if args.seedtest else args.indir
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    if not files:
        print(f"⚠️ {indir} 无 1m 文件")
        return
    # no-failure-mode: 统计有效 (>=1000 根) 文件, 不足则告警但不静默退出
    valid = [f for f in files if (sum(1 for _ in open(f, 'rb')) - 1) >= 1000]
    n_valid = len(valid)
    if n_valid < 250:
        print(f"⚠️ 有效样本 {n_valid} < 250 目标; 继续但结论需谨慎 (no-failure-mode)")
    else:
        print(f"✅ 有效样本 {n_valid} 只 (>=250)")

    if do_push:
        push(f"【25%】Phase1 下载完成：{n_valid} 只有效 A股 1m 历史落地，进入因子归因")

    cache = RS.load_cache(indir)
    base_rows, n_base, base_skill, marginal = RS.phase3(cache)
    if do_push:
        push(f"【50%】Phase3 归因完成：基线信号 {n_base} 个，"
              f"符号调整 skill24={base_skill[24][0]:+.4f}%")

    abl = RS.phase4(cache)
    sweep_df = RS.phase5(cache)
    RS.write_outputs(cache, base_rows, n_base, base_skill, marginal, abl, sweep_df, seedtest=args.seedtest)
    if do_push:
        push(f"【75%】Phase4 消融 + Phase5 阈值扫描完成（{len(sweep_df)} 组配置）")

    # ---- Phase 6 结论 ----
    s, sweep = load()
    text = md(s, sweep)
    print("\n" + text)
    if do_push:
        push("【100%】Phase6 关键因子归因结论：\n" + text)
        print("\n✅ 飞书里程碑已全部推送")

if __name__ == '__main__':
    main()
