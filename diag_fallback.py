import sys, traceback, time
sys.path.insert(0, 'core'); sys.path.insert(0, '.')
import pandas as pd
from datasource import MootdxDataSource
tf = MootdxDataSource()
sym='300308.SZ'
# 直接测腾讯/新浪兜底路径
try:
    fb = tf._tencent_intraday_fallback(sym)
    if fb is None:
        print("[fallback] -> None"); 
    else:
        print(f"[fallback] rows={len(fb)} cols={list(fb.columns)}")
        print(f"   trade_date uniq: {fb['trade_date'].unique()[:3]}")
        print(f"   dtypes trade_date: {fb['trade_date'].dtype}")
        today=pd.Timestamp.now().strftime('%Y-%m-%d')
        sub=fb[fb['trade_date']==today]
        print(f"   after date filter rows={len(sub)} today={today}")
except Exception as e:
    print("[fallback] EXC:", repr(e)); traceback.print_exc()
