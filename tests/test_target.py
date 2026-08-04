import traceback, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from datasource import MootdxDataSource
print("import OK, 创建实例...")
tf = MootdxDataSource()
for sym in ['161129.SZ', '688347.SH']:
    print('\n===', sym, '===')
    try:
        d = tf.klines.get(sym, period='1d', count=2)
        if d is not None and len(d) > 0:
            print('  日K OK:', len(d), '根 close=', float(d['close'].iloc[-1]), 'date=', d['trade_date'].iloc[-1])
        else:
            print('  日K: 空')
    except Exception as e:
        print('  日K失败:', repr(e))
    try:
        m = tf.klines.intraday(sym)
        if m is not None and len(m) > 0:
            print('  分钟K OK:', len(m), '根 末根=', str(m['trade_time'].iloc[-1]))
        else:
            print('  分钟K: 空(monitor无法监控)')
    except Exception as e:
        print('  分钟K失败:', repr(e))
    try:
        q = tf.tencent_realtime(sym)
        if q:
            print('  腾讯快照:', q['name'], 'price=', q['price'], 'pc=', q['prev_close'])
        else:
            print('  腾讯快照: 空')
    except Exception as e:
        print('  腾讯快照失败:', repr(e))
print("\nDONE")
