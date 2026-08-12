import sys, traceback
sys.path.insert(0, 'core')
sys.path.insert(0, '.')
import pandas as pd
try:
    from datasource import MootdxDataSource
except Exception as e:
    print("import fail:", e); sys.exit(1)
tf = MootdxDataSource()
for sym in ['300308.SZ','161129.SZ','688111.SH','513310.SH']:
    try:
        df = tf.klines.intraday(sym, as_dataframe=True)
        if df is None:
            print(f"[{sym}] intraday() -> None"); continue
        print(f"[{sym}] rows={len(df)} cols={list(df.columns)}")
        if 'trade_date' in df.columns:
            print(f"   trade_date uniq: {df['trade_date'].unique()[:5]}")
        if 'trade_time' in df.columns:
            print(f"   trade_time head: {df['trade_time'].iloc[0]}  tail: {df['trade_time'].iloc[-1]}")
        if len(df)>=5 and 'trade_date' in df.columns:
            bar_date=str(df['trade_date'].iloc[0])
            today=pd.Timestamp.now().strftime('%Y-%m-%d')
            print(f"   bar_date={bar_date} today={today} match={bar_date==today}")
    except Exception as e:
        print(f"[{sym}] EXC: {e}")
        traceback.print_exc()
