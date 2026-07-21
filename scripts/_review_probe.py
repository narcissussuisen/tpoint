import sys, time
sys.path.insert(0, r'C:/Users/YZP/WorkBuddy/Claw/tpoint/core')
sys.path.insert(0, r'C:/Users/YZP/WorkBuddy/Claw/tpoint')
from datasource import MootdxDataSource
ds = MootdxDataSource()
for sym in ['161129.SZ','688347.SH']:
    t=time.time()
    try:
        df = ds.intraday(sym)
        el=round(time.time()-t,1)
        if df is None:
            print(sym,'=> None (no data)', f'{el}s'); continue
        print(sym,'=> rows',len(df), f'{el}s')
        print(df[['trade_time','close','high','low','volume']].tail(3).to_string())
    except Exception as e:
        print(sym,'=> EXC', repr(e), f'{round(time.time()-t,1)}s')
