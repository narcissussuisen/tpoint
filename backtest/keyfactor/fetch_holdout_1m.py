# -*- coding: utf-8 -*-
"""盲 holdout 数据拉取: 拉取一批先验确定的 T0 ETF/LOF 1m 历史到 F:/keyfactor_data/1m/。
这些标的均不在 in-sample 8 只 + 159985 holdout 中, 调参时不可见。
落盘 schema 与 download_1m.py 一致: symbol,name,timestamp,trade_date,trade_time,open,high,low,close,volume,amount
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from datasource import tdx_client
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = r'F:/keyfactor_data/1m'
os.makedirs(DATA_DIR, exist_ok=True)

TARGET = 16000      # ≈67 交易日, 覆盖 in-sample 窗口(2026-04-17..2026-07-16)
PER = 800
PAGES = 22
FREQ = 8            # 1分钟

# (code6位, sym, name) —— 全部 T+0 (跨境/QDII ETF, 黄金/商品ETF, 跨境LOF)
# 排除 in-sample 8 只 + 159985; 513040/518880 已在缓存, 不在此列(由 harness 直接读)
SYMS = [
    ('513100', '513100.SH', '纳指ETF'), ('513500', '513500.SH', '标普500ETF'),
    ('513090', '513090.SH', '中概互联ETF'), ('513030', '513030.SH', '德国30ETF'),
    ('513080', '513080.SH', '法国CAC40ETF'), ('513520', '513520.SH', '日经225ETF'),
    ('513300', '513300.SH', '纳斯达克100ETF'), ('513180', '513180.SH', '恒生科技ETF'),
    ('513660', '513660.SH', '恒生ETF'), ('513550', '513550.SH', '港股通50ETF'),
    ('513060', '513060.SH', '恒生医疗ETF'), ('513120', '513120.SH', '港股创新药ETF'),
    ('513010', '513010.SH', '恒生科技ETF2'), ('513000', '513000.SH', '日经225ETF2'),
    ('159920', '159920.SZ', '恒生ETF'), ('159941', '159941.SZ', '纳指ETF'),
    ('159605', '159605.SZ', '中概互联ETF'), ('159607', '159607.SZ', '中概互联ETF2'),
    ('159892', '159892.SZ', '恒生医药ETF'),
    ('518800', '518800.SH', '黄金ETF2'), ('518850', '518850.SH', '黄金ETF3'),
    ('518660', '518660.SH', '黄金ETF4'), ('159934', '159934.SZ', '黄金ETF5'),
    ('159937', '159937.SZ', '黄金ETF6'),
    ('159980', '159980.SZ', '有色ETF'), ('159981', '159981.SZ', '能源化工ETF'),
    ('162411', '162411.SZ', '华宝油气LOF'), ('164824', '164824.SZ', '印度基金LOF'),
    ('160723', '160723.SZ', '嘉实原油LOF'), ('501018', '501018.SH', '南方原油LOF'),
    ('161226', '161226.SZ', '国投白银LOF'), ('160140', '160140.SZ', '美国REITLOF'),
    ('161815', '161815.SZ', '抗通胀LOF'), ('165513', '165513.SZ', '信诚商品LOF'),
    ('160216', '160216.SZ', '国泰商品LOF'), ('160416', '160416.SZ', '华安石油LOF'),
    ('164701', '164701.SZ', '黄金LOF'),
]


def fetch_one(code, name):
    frames = []
    cli = tdx_client()
    for p in range(PAGES):
        start = p * PER
        try:
            df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER)
        except Exception:
            try:
                cli = tdx_client()
                df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER)
            except Exception as e:
                print(f'  [{code}] page {p} 失败: {e}')
                break
        if df is None or len(df) == 0:
            break
        frames.append(df)
        if len(df) < PER:
            break
        time.sleep(0.05)
    if not frames:
        return None
    big = pd.concat(frames, ignore_index=True)
    big['datetime'] = pd.to_datetime(big['datetime'])
    big = big.drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    big = big.tail(TARGET).reset_index(drop=True)
    out = pd.DataFrame({
        'symbol': code + ('.SH' if code[0] in '56' else '.SZ'),
        'name': name,
        'timestamp': (big['datetime'].astype('int64') // 10**6).astype('int64'),
        'trade_date': big['datetime'].dt.strftime('%Y-%m-%d'),
        'trade_time': big['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S'),
        'open': big['open'].astype(float),
        'high': big['high'].astype(float),
        'low': big['low'].astype(float),
        'close': big['close'].astype(float),
        'volume': big['vol'].astype(float) if 'vol' in big.columns else big.get('volume', 0).astype(float),
        'amount': big['amount'].astype(float) if 'amount' in big.columns else 0.0,
    })
    return out


def main():
    print(f'开始盲 holdout 拉取, 目标 {len(SYMS)} 只 x {TARGET} 根')
    ok = skip = fail = 0
    for code, sym, name in SYMS:
        fpath = os.path.join(DATA_DIR, f'{sym}_1m.csv')
        if os.path.exists(fpath):
            try:
                if sum(1 for _ in open(fpath, 'rb')) - 1 >= TARGET:
                    print(f'  {sym} 已存在且充足, 跳过')
                    skip += 1
                    continue
            except Exception:
                pass
        out = fetch_one(code, name)
        if out is None or len(out) == 0:
            print(f'  {sym} 无数据, 跳过')
            fail += 1
            continue
        out.to_csv(fpath, index=False, encoding='utf-8-sig')
        ok += 1
        print(f'  {sym:12s} {name:10s} {len(out)}根 {out["trade_date"].iloc[0]}~{out["trade_date"].iloc[-1]}')
    print(f'\n=== 拉取完成 ok={ok} skip={skip} fail={fail} / {len(SYMS)} ===')


if __name__ == '__main__':
    main()
