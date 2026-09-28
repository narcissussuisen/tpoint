# -*- coding: utf-8 -*-
r"""test_ml_features.py — core/ml_features.py（2026-09-28 重建）回归

覆盖：
  T1  FEAT_ALL 与 output/ml_versions.json 的 feature_names 逐字一致（两个注册版本都比对）
  T2  build_feature_row：39 维形状 / 无 NaN-inf / float dtype（合成日 + 多个 bar）
  T3  口径抽查：vwap_dev / hist_pct / chg / pos_in_day / bar_idx_frac / mom / rsi_dist
      与手工计算一致；kdj 与 primitives.compute_kdj 一致；macd{p} 与因果版原语一致
  T4  时段哑变量：合成 trade_time 下 is_morning(09:30-10:00) / is_noon(11:00-13:30)
      / is_tail(>=14:30) 正确；无时间戳时 bar 序号近似兜底生效
  T5  ctx：None / 缺键 / 脏值 → 补 0；正常值透传
  T6  fail-open：i 越界 / vwap<=0 / 空 data → None（不抛异常）
  T7  因果性：挂 core/leak_guard.perturbation_test 栅栏（未来扰动 → 历史特征不变）
  T8  F 盘真实日 smoke（文件缺失则 skip 不计失败）

运行：venv\Scripts\python.exe tests\test_ml_features.py
"""
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import ml_features as mf  # noqa: E402
from miji_alpha import compute_miji_indicators, compute_multi_period_macd  # noqa: E402
from primitives import compute_kdj  # noqa: E402
import leak_guard  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS {name}')
    else:
        FAIL += 1
        print(f'  FAIL {name}  {detail}')


def _synth_day(n=240, seed=7, pc=10.0):
    """确定性合成交易日（与 leak_guard._demo_ohlcv 同族）。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = pc + 0.002 * t + np.sin(2 * np.pi * t / 30) * 0.15
    c = base + rng.normal(0, 0.03, n)
    o = np.empty(n); o[0] = c[0]; o[1:] = c[:-1]
    h = np.maximum(c, o) + rng.uniform(0.005, 0.02, n)
    lo = np.minimum(c, o) - rng.uniform(0.005, 0.02, n)
    v = rng.uniform(800, 1400, n) + 1500 * np.abs(np.diff(np.concatenate([[c[0]], c])))
    return o, h, lo, c, v


def _trade_times_240():
    """标准 240 bar 的收盘时刻表（09:31-11:30, 13:01-15:00）。"""
    import datetime as _dt
    out = []
    t = _dt.datetime(2026, 9, 25, 9, 31)
    for _ in range(120):
        out.append(t.strftime('%Y-%m-%d %H:%M:%S'))
        t += _dt.timedelta(minutes=1)
    t = _dt.datetime(2026, 9, 25, 13, 1)
    for _ in range(120):
        out.append(t.strftime('%Y-%m-%d %H:%M:%S'))
        t += _dt.timedelta(minutes=1)
    return out


class _FakeDF:
    """最小 DataFrame 替身：仅提供 ['trade_time'] 列 + iloc。"""
    def __init__(self, times):
        self._col = _FakeCol(times)

    def __getitem__(self, key):
        assert key == 'trade_time'
        return self._col


class _FakeCol:
    def __init__(self, vals):
        self._vals = list(vals)

    @property
    def iloc(self):
        return self

    def __getitem__(self, i):
        return self._vals[i]


print('== T1 FEAT_ALL 与 ml_versions.json 逐字一致 ==')
try:
    with open(os.path.join(ROOT, 'output', 'ml_versions.json'), encoding='utf-8') as fh:
        vers = json.load(fh)
    check('T1.1 注册版本数>=1', len(vers) >= 1)
    ok_all = True
    for ent in vers:
        if list(ent['feature_names']) != mf.FEAT_ALL:
            ok_all = False
            print(f"    版本 {ent.get('ver')} feature_names 与 FEAT_ALL 不一致")
    check('T1.2 全版本 feature_names == FEAT_ALL（逐字逐序）', ok_all)
    check('T1.3 FEAT_ALL 长度 39 且无重复',
          len(mf.FEAT_ALL) == 39 and len(set(mf.FEAT_ALL)) == 39)
except Exception as e:
    check('T1 ml_versions.json 读取', False, repr(e))

print('== T2 形状 / 无 NaN-inf ==')
o, h, lo, c, v = _synth_day()
pc = 10.0
data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
sub = _FakeDF(_trade_times_240())
ctx = {'g_factor': 1.0, 'v_factor': 0.0, 'm_factor': 1.0, 'resonance': 2.0}
ok_shape = True
for i in (0, 1, 15, 60, 119, 120, 180, 239):
    row = mf.build_feature_row(data, pc, i, sub, ctx)
    if row is None or row.shape != (39,) or not np.all(np.isfinite(row)):
        ok_shape = False
        print(f'    bar {i}: row={row}')
check('T2.1 8 个抽查 bar 均为 (39,) 有限行', ok_shape)
row = mf.build_feature_row(data, pc, 100, sub, ctx)
check('T2.2 dtype 为 float', row is not None and row.dtype == np.float64)

print('== T3 口径抽查 ==')
i = 100
row = mf.build_feature_row(data, pc, i, sub, ctx)
f = dict(zip(mf.FEAT_ALL, row))
exp_vwap_dev = (c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0
check('T3.1 vwap_dev=(c-vwap)/vwap*100', abs(f['vwap_dev'] - exp_vwap_dev) < 1e-9)
exp_hist_pct = data['hist'][i] / data['vwap'][i] * 100.0
check('T3.2 hist_pct=hist/vwap*100', abs(f['hist_pct'] - exp_hist_pct) < 1e-9)
exp_chg = (c[i] - pc) / pc * 100.0
check('T3.3 chg=(c-pc)/pc*100', abs(f['chg'] - exp_chg) < 1e-9)
check('T3.4 pos_in_day=(i+1)/240', abs(f['pos_in_day'] - (i + 1) / 240.0) < 1e-12)
check('T3.5 bar_idx_frac=i/n', abs(f['bar_idx_frac'] - i / 240.0) < 1e-12)
check('T3.6 mom_5 收益率口径',
      abs(f['mom_5'] - (c[i] / c[i - 5] - 1.0) * 100.0) < 1e-9)
check('T3.7 rsi_dist_30/70=rsi-30/rsi-70',
      abs(f['rsi_dist_30'] - (data['rsi'][i] - 30.0)) < 1e-9
      and abs(f['rsi_dist_70'] - (data['rsi'][i] - 70.0)) < 1e-9)
kk, dd, jj = compute_kdj(h, lo, c)
check('T3.8 kdj 与 primitives 原语一致',
      abs(f['kdj_k'] - kk[i]) < 1e-9 and abs(f['kdj_d'] - dd[i]) < 1e-9
      and abs(f['kdj_j'] - jj[i]) < 1e-9)
mp = compute_multi_period_macd(c, periods=(5, 15, 30, 60))
ok_mp = True
for p in (5, 15, 30, 60):
    if abs(f[f'macd{p}_dif'] - mp[p]['dif'][i]) > 1e-9 \
            or abs(f[f'macd{p}_hist'] - mp[p]['hist'][i]) > 1e-9:
        ok_mp = False
check('T3.9 多周期 MACD 与因果版原语一致', ok_mp)
a = data['atr'][i]
check('T3.10 atr_chan 轨外 ATR 倍数口径',
      abs(f['atr_chan_up1'] - (c[i] - (data['vwap'][i] + a)) / a) < 1e-9
      and abs(f['atr_chan_dn1'] - ((data['vwap'][i] - a) - c[i]) / a) < 1e-9)
check('T3.11 ctx 四列透传',
      f['g_factor'] == 1.0 and f['v_factor'] == 0.0
      and f['m_factor'] == 1.0 and f['resonance'] == 2.0)
check('T3.12 直取字段一致（dif/dea/hist/trend/rsi/vol_ratio/temp/atr_pct）',
      f['dif'] == data['dif'][i] and f['dea'] == data['dea'][i]
      and f['hist'] == data['hist'][i] and f['trend'] == data['trend'][i]
      and f['trend_strong'] == data['trend_strong'][i]
      and f['rsi'] == data['rsi'][i] and f['vol_ratio'] == data['vol_ratio'][i]
      and f['temp'] == data['temp'][i]
      and abs(f['atr_pct'] - a / c[i] * 100.0) < 1e-9)

print('== T4 时段哑变量 ==')
# bar 5 = 09:36 → morning；bar 100 = 11:10 → noon；bar 130 = 13:11 → noon；
# bar 60 = 10:31 → 三者皆否；bar 220 = 14:40 → tail
r5 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, 5, sub, ctx)))
r60 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, 60, sub, ctx)))
r100 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, 100, sub, ctx)))
r130 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, 130, sub, ctx)))
r220 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, 220, sub, ctx)))
check('T4.1 09:36 → is_morning=1 其余=0',
      r5['is_morning'] == 1.0 and r5['is_noon'] == 0.0 and r5['is_tail'] == 0.0)
check('T4.2 10:31 → 三者皆 0',
      r60['is_morning'] == 0.0 and r60['is_noon'] == 0.0 and r60['is_tail'] == 0.0)
check('T4.3 11:10 → is_noon=1', r100['is_noon'] == 1.0 and r100['is_morning'] == 0.0)
check('T4.4 13:11 → is_noon=1（午后首段）', r130['is_noon'] == 1.0)
check('T4.5 14:40 → is_tail=1 其余=0',
      r220['is_tail'] == 1.0 and r220['is_morning'] == 0.0 and r220['is_noon'] == 0.0)
# 无时间戳兜底（sub=None，data 无 is_morning 键 → 序号近似）
data_nodf = dict(data)
data_nodf.pop('df', None)
data_nodf.pop('is_morning', None)
r5b = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data_nodf, pc, 5, None, ctx)))
r220b = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data_nodf, pc, 220, None, ctx)))
check('T4.6 无时间戳序号近似兜底（bar5→morning, bar220→tail）',
      r5b['is_morning'] == 1.0 and r220b['is_tail'] == 1.0)
# 与 monitor 生产 is_morning 数组口径对齐（data['is_morning'] 存在且无 sub 时采用）
data_mon = dict(data_nodf)
hhmm = np.array([t[11:16] for t in _trade_times_240()])
data_mon['is_morning'] = ((hhmm >= '09:30') & (hhmm < '10:00')).astype(int)
r28 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data_mon, pc, 28, None, ctx)))
r29 = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data_mon, pc, 29, None, ctx)))
check('T4.7 data[is_morning] 数组优先于序号近似（bar28=09:59→1, bar29=10:00→0）',
      r28['is_morning'] == 1.0 and r29['is_morning'] == 0.0)

print('== T5 ctx 健壮性 ==')
r_none = dict(zip(mf.FEAT_ALL, mf.build_feature_row(data, pc, i, sub, None)))
r_dirty = dict(zip(mf.FEAT_ALL, mf.build_feature_row(
    data, pc, i, sub, {'g_factor': None, 'v_factor': 'bad', 'm_factor': 0.5})))
check('T5.1 ctx=None → 四列补 0',
      all(r_none[k] == 0.0 for k in ('g_factor', 'v_factor', 'm_factor', 'resonance')))
check('T5.2 脏值容错（None/字符串→0，数值透传）',
      r_dirty['g_factor'] == 0.0 and r_dirty['v_factor'] == 0.0
      and r_dirty['m_factor'] == 0.5)

print('== T6 fail-open ==')
check('T6.1 i 越界 → None', mf.build_feature_row(data, pc, 999, sub, ctx) is None
      and mf.build_feature_row(data, pc, -1, sub, ctx) is None)
data_bad = dict(data)
data_bad['vwap'] = np.zeros_like(data['vwap'])
check('T6.2 vwap=0 → None（除零防护）',
      mf.build_feature_row(data_bad, pc, i, sub, ctx) is None)
check('T6.3 空 dict → None（不抛异常）', mf.build_feature_row({}, pc, 0, None, None) is None)

print('== T7 因果性栅栏（leak_guard 未来扰动） ==')


def _ml_feat_fn(o_, h_, lo_, c_, v_):
    """把 build_feature_matrix 包装成 leak_guard feat_fn 口径（ctx 全 0，无时间戳）。"""
    d = compute_miji_indicators(o_, h_, lo_, c_, v_, float(c_[0]), has_vol=True)
    d.pop('df', None)
    mat = mf.build_feature_matrix(d, float(c_[0]), None, None)
    return {name: mat[:, k] for k, name in enumerate(mf.FEAT_ALL)}


try:
    rep = leak_guard.perturbation_test(_ml_feat_fn, _synth_day(n=240), tol=1e-6)
    check('T7.1 39 特征全链路无前视（未来扰动历史不变）', rep['ok'],
          f"worst={rep['worst']}")
except AssertionError as e:
    check('T7.1 39 特征全链路无前视', False, str(e)[:200])

print('== T8 F 盘真实日 smoke ==')
_REAL = r'F:\keyfactor_data\1m\300010.SZ_1m.csv'
if os.path.isfile(_REAL):
    try:
        import csv as _csv
        rows = {}
        with open(_REAL, encoding='utf-8') as fh:
            rd = _csv.DictReader(fh)
            for r in rd:
                d = r.get('date') or r.get('trade_date') or ''
                rows.setdefault(d, []).append(r)
        day = sorted(rows)[-1]
        recs = sorted(rows[day], key=lambda r: r.get('time', r.get('trade_time', '')))
        def _col(key):
            return np.array([float(r[key]) for r in recs])
        c_r, h_r, lo_r, o_r = _col('close'), _col('high'), _col('low'), _col('open')
        v_r = _col('volume') if 'volume' in recs[0] else None
        pc_r = float(rows[sorted(rows)[-2]][-1]['close']) if len(rows) >= 2 else c_r[0]
        d_r = compute_miji_indicators(o_r, h_r, lo_r, c_r, v_r, pc_r,
                                      has_vol=v_r is not None)
        tkey = 'time' if 'time' in recs[0] else 'trade_time'
        sub_r = _FakeDF([f"{day} {r[tkey]}:00" if len(str(r[tkey])) == 5
                         else str(r[tkey]) for r in recs])
        n_ok, n_none = 0, 0
        for j in range(len(c_r)):
            rr = mf.build_feature_row(d_r, pc_r, j, sub_r, None)
            if rr is None:
                n_none += 1
            elif rr.shape == (39,) and np.all(np.isfinite(rr)):
                n_ok += 1
        check(f'T8.1 真实日 {day}（{len(c_r)} bars）：全部行有限或 None '
              f'(ok={n_ok}, none={n_none})',
              n_ok + n_none == len(c_r) and n_ok > len(c_r) * 0.9)
    except Exception as e:
        check('T8.1 真实日 smoke', False, repr(e))
else:
    print(f'  SKIP T8（{_REAL} 不存在）')

print(f'\n===== {PASS} PASS / {FAIL} FAIL =====')
sys.exit(0 if FAIL == 0 else 1)
