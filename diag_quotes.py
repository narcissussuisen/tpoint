import sys; sys.path.insert(0,'core'); sys.path.insert(0,'.')
from datasource import MootdxDataSource
tf=MootdxDataSource()
for sym in ['300308.SZ','161129.SZ','688111.SH','513310.SH','300757.SZ']:
    try:
        q=tf.tencent_realtime(sym)
        if q: print(f"{sym} {q['name']} 现价={q['price']} 昨收={q['prev_close']} 涨跌={round((q['price']/q['prev_close']-1)*100,2)}%")
        else: print(f"{sym} tencent_realtime=None")
    except Exception as e:
        print(f"{sym} EXC {e}")
