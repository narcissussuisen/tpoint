# -*- coding: utf-8 -*-
"""探测盲 holdout 候选 T0 ETF/LOF 在 F:/keyfactor_data/1m/ 的存在性与日期覆盖。
候选按代码先验确定(不看结果), 排除原 in-sample 8 只 + 159985 holdout。
"""
import os
import pandas as pd

DATA_DIR = r'F:/keyfactor_data/1m'

# 先验候选 T0 ETF/LOF(跨境/商品/黄金/原油LOF 等). 不含货币/债券ETF(波动太低).
CANDIDATES = [
    # 跨境/QDII ETF (SH)
    ('513100.SH', '纳指ETF'), ('513500.SH', '标普500ETF'), ('513090.SH', '中概互联ETF'),
    ('513030.SH', '德国30ETF'), ('513080.SH', '法国CAC40ETF'), ('513520.SH', '日经225ETF'),
    ('513300.SH', '纳斯达克100ETF'), ('513180.SH', '恒生科技ETF'), ('513660.SH', '恒生ETF'),
    ('513550.SH', '港股通50ETF'), ('513060.SH', '恒生医疗ETF'), ('513120.SH', '港股创新药ETF'),
    ('513010.SH', '恒生科技ETF2'), ('513000.SH', '日经225ETF2'),
    # 跨境 ETF (SZ)
    ('159920.SZ', '恒生ETF'), ('159941.SZ', '纳指ETF'), ('159605.SZ', '中概互联ETF'),
    ('159607.SZ', '中概互联ETF2'), ('159892.SZ', '恒生医药ETF'),
    # 黄金 ETF
    ('518880.SH', '黄金ETF'), ('518800.SH', '黄金ETF2'), ('518850.SH', '黄金ETF3'), ('518660.SH', '黄金ETF4'),
    # 商品 ETF
    ('159934.SZ', '黄金ETF5'), ('159937.SZ', '黄金ETF6'), ('159980.SZ', '有色ETF'), ('159981.SZ', '能源化工ETF'),
    # T0 LOF (跨境/商品/原油/白银/REIT)
    ('162411.SZ', '华宝油气LOF'), ('164824.SZ', '印度基金LOF'), ('160723.SZ', '嘉实原油LOF'),
    ('501018.SH', '南方原油LOF'), ('161226.SZ', '国投白银LOF'), ('160140.SZ', '美国REITLOF'),
    ('161815.SZ', '抗通胀LOF'), ('165513.SZ', '信诚商品LOF'), ('160216.SZ', '国泰商品LOF'),
    ('160416.SZ', '华安石油LOF'), ('164701.SZ', '黄金LOF'),
]

# in-sample 评估窗口(与 8 只研究同窗口)
EVAL_START, EVAL_END = '2026-04-17', '2026-07-16'


def main():
    rows = []
    for sym, name in CANDIDATES:
        f = os.path.join(DATA_DIR, f'{sym}_1m.csv')
        if not os.path.exists(f):
            rows.append((sym, name, 'MISSING', '', '', 0, 0))
            continue
        try:
            df = pd.read_csv(f, encoding='utf-8-sig', usecols=['trade_date'])
            ds = df['trade_date'].astype(str)
            d0, d1 = ds.min(), ds.max()
            n_days = ds.nunique()
            in_win = int(((ds >= EVAL_START) & (ds <= EVAL_END)).sum())
            nrows = len(df)
            rows.append((sym, name, 'OK', d0, d1, n_days, in_win))
        except Exception as e:
            rows.append((sym, name, f'ERR:{e}', '', '', 0, 0))
    print(f"{'sym':<12}{'name':<14}{'status':<8}{'d0':<12}{'d1':<12}{'ndays':>6}{'inWin':>7}")
    ok = 0
    for r in rows:
        print(f"{r[0]:<12}{r[1]:<14}{r[2]:<8}{str(r[3]):<12}{str(r[4]):<12}{r[5]:>6}{r[6]:>7}")
        if r[2] == 'OK' and r[6] >= 40:  # 至少覆盖窗口大部分
            ok += 1
    print(f"\n可用(且覆盖窗口>=40天)候选: {ok} / {len(CANDIDATES)}")


if __name__ == '__main__':
    main()
