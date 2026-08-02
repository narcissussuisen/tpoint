# -*- coding: utf-8 -*-
"""
market_screener.py — 全市场标的筛选器（P0-4 迭代交付）

对齐 PPT S9 标的筛选三条件：
  ① 近1月日均成交额 ≥ 50亿
  ② 换手率 5% - 15%
  ③ 日内振幅 5% - 20%
  另：xlsx 绩效阈值（Level ≥ 3 星 / 20日开仓率 ≥ 50%）可作增强条件。

模式：
  --xlsx <path>   解析卡方绩效 xlsx → 按 Level/开仓率/年化 过滤输出候选池
                  （离线最快，xlsx 5002 只已含全部指标）
  --verify <sym>  从 mootdx 拉近1月日K → 实时计算三条件对照（验证候选池真实性）
  --verify-all    对当前 watchlist + 候选池批量 verify

输出：data/screener_candidates.json（候选池，人工确认后并入 watchlist）

用法示例：
  python scripts/market_screener.py --xlsx "C:/Users/YZP/Downloads/kf_日内回转plus_performance_20260731.xlsx" --min-level 3 --min-open-rate 50 --top 40
  python scripts/market_screener.py --verify 688111.SH
"""
import argparse
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# PPT S9 三条件（可调）
MIN_AMT_YI = 50.0       # 近1月日均成交额 ≥ 50亿
TURNOVER_MIN = 5.0      # 换手率下限 %
TURNOVER_MAX = 15.0     # 换手率上限 %
AMP_MIN = 5.0           # 振幅下限 %
AMP_MAX = 20.0          # 振幅上限 %

OUT_CANDIDATES = os.path.join(BASE, 'data', 'screener_candidates.json')


def parse_xlsx(path):
    """标准库 zipfile 解析 xlsx（无 openpyxl 依赖）。
    返回 list[dict]：Symbol/名称/20日收益率/20日开仓率/年化/Level/20日胜率 等。"""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    z = zipfile.ZipFile(path)
    names = z.namelist()
    # sharedStrings
    ss = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall(f'{NS}si'):
            text = ''.join(t.text or '' for t in si.iter(f'{NS}t'))
            ss.append(text)
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    R = '{http://schemas.openxmlformats.org/package/2006/relationships}'
    rid_map = {r.get('Id'): r.get('Target') for r in rels.findall(f'{R}Relationship')}
    sheet_id = None
    for s in wb.findall(f'{NS}sheets/{NS}sheet'):
        sheet_id = s.get(f'{R}id')
        break
    sheet_file = 'xl/' + rid_map.get(sheet_id, 'worksheets/sheet1.xml').lstrip('/')
    sh = ET.fromstring(z.read(sheet_file))
    rows = []
    for row in sh.findall(f'{NS}sheetData/{NS}row'):
        cells = []
        for c in row.findall(f'{NS}c'):
            t = c.get('t')
            v = c.find(f'{NS}v')
            val = ''
            if v is not None:
                val = v.text or ''
                if t == 's' and val:
                    val = ss[int(val)] if int(val) < len(ss) else ''
            cells.append(val)
        rows.append(cells)
    if not rows:
        return []
    header = rows[0]
    # 列索引
    def col_idx(name):
        for k, h in enumerate(header):
            if str(h).strip() == name:
                return k
        return -1
    i_sym = col_idx('Symbol')
    i_name = col_idx('证券名称')
    i_ret20 = col_idx('20日收益率')
    i_open20 = col_idx('20日开仓率')
    i_ann = col_idx('年化收益率')
    i_level = col_idx('Level')
    i_win20 = col_idx('20日胜率')
    i_ret5 = col_idx('5日收益率')

    def pct(s):
        try:
            return float(str(s).replace('%', ''))
        except (TypeError, ValueError):
            return None

    out = []
    for r in rows[1:]:
        sym = str(r[i_sym]) if i_sym >= 0 and len(r) > i_sym else ''
        if not sym or not sym.strip():
            continue
        rec = {
            'symbol': sym.strip(),
            'name': str(r[i_name]) if i_name >= 0 and len(r) > i_name else '',
            'ret_20d': pct(r[i_ret20]) if i_ret20 >= 0 and len(r) > i_ret20 else None,
            'open_rate_20d': pct(r[i_open20]) if i_open20 >= 0 and len(r) > i_open20 else None,
            'ann_ret': pct(r[i_ann]) if i_ann >= 0 and len(r) > i_ann else None,
            'level': pct(r[i_level]) if i_level >= 0 and len(r) > i_level else None,
            'win_rate_20d': pct(r[i_win20]) if i_win20 >= 0 and len(r) > i_win20 else None,
            'ret_5d': pct(r[i_ret5]) if i_ret5 >= 0 and len(r) > i_ret5 else None,
        }
        out.append(rec)
    return out


def filter_xlsx(recs, min_level=3, min_open_rate=50.0, min_ann=0.0, top=None):
    """按卡方绩效阈值过滤 + 排序。返回候选池（已按 Level desc / 年化 desc）。"""
    cand = [r for r in recs
            if r['level'] is not None and r['level'] >= min_level
            and r['open_rate_20d'] is not None and r['open_rate_20d'] >= min_open_rate
            and r['ann_ret'] is not None and r['ann_ret'] >= min_ann]
    cand.sort(key=lambda r: (r['level'], r['ann_ret']), reverse=True)
    if top:
        cand = cand[:top]
    return cand


def verify_from_mootdx(sym):
    """用 mootdx 拉近 1 月日 K，实时计算三条件（成交额/换手率/振幅）。
    返回 dict 或 None。换手率需流通股本（mootdx finance liutongguben）。
    [2026-08-01 修复] 两个单位 bug：
      1. 成交额改用日K权威 amount 字段（元），不再用 close*volume 近似（volume 单位是手，低估100倍）
      2. 换手率 = avg_vol(手)×100 / liutongguben(股) × 100，且用流通股本而非总股本
    """
    try:
        from core.datasource import MootdxDataSource
    except Exception:
        from datasource import MootdxDataSource
    ds = MootdxDataSource()
    df = ds.klines.get(sym, period='1d', count=30)
    if df is None or len(df) == 0:
        return {'symbol': sym, 'error': '无日K数据'}
    recent = df.tail(22)  # 近1月≈22交易日
    # 成交额：优先权威 amount 字段（元），缺失时才退化 close×volume×100 近似
    if 'amount' in recent.columns:
        amt = recent['amount'].mean()
    else:
        amt = (recent['close'] * recent['volume'] * 100.0).mean()  # volume 单位=手
    amt_yi = float(amt) / 1e8
    amp = ((recent['high'] - recent['low']) / recent['close'] * 100.0).mean()
    # 换手率 = 日均成交量(股) / 流通股本(股) × 100
    turnover = None
    try:
        code = sym.split('.')[0]
        market = 0 if sym.endswith('.SZ') else 1
        fin = ds.client.finance(symbol=code, market=market)
        if fin is not None and len(fin):
            # 优先流通股本（换手率定义），缺失退化总股本
            float_share = float(fin.iloc[0].get('liutongguben', 0) or 0)
            total_share = float(fin.iloc[0].get('zongguben', 0) or 0)
            share = float_share if float_share > 0 else total_share
            if share > 0:
                avg_vol = recent['volume'].mean() * 100.0  # 手→股
                turnover = avg_vol / share * 100.0
    except Exception:
        pass
    return {
        'symbol': sym,
        'name': '',
        'amt_yi': round(amt_yi, 2),
        'turnover_pct': round(turnover, 2) if turnover else None,
        'amp_pct': round(amp, 2),
        'pass_amt': bool(amt_yi >= MIN_AMT_YI),
        'pass_turnover': bool(turnover is not None and TURNOVER_MIN <= turnover <= TURNOVER_MAX),
        'pass_amp': bool(AMP_MIN <= amp <= AMP_MAX),
        'checked_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def verify_batch(syms, max_n=None):
    """批量 verify（顺序，避免 mootdx 并发限制）。返回 list[dict]。"""
    out = []
    total = len(syms) if max_n is None else min(max_n, len(syms))
    for i, sym in enumerate(syms[:total], 1):
        r = verify_from_mootdx(sym)
        r['name'] = ''
        r['idx'] = i
        out.append(r)
        amt = r.get('amt_yi'); to = r.get('turnover_pct'); ap = r.get('amp_pct')
        print(f'  [{i}/{total}] {sym:12s} 成交额{amt if amt is not None else "-":>6}亿 '
              f'换手{to if to is not None else "-":>5}% 振幅{ap if ap is not None else "-":>5}% '
              f'pass={r.get("pass_amt")}/{r.get("pass_turnover")}/{r.get("pass_amp")}')
    return out


def main():
    ap = argparse.ArgumentParser(description='全市场标的筛选器（PPT S9 三条件 + xlsx 绩效）')
    ap.add_argument('--xlsx', help='卡方绩效 xlsx 路径')
    ap.add_argument('--min-level', type=int, default=3, help='最低 Level 星级（默认3）')
    ap.add_argument('--min-open-rate', type=float, default=50.0, help='最低 20日开仓率%%（默认50）')
    ap.add_argument('--min-ann', type=float, default=0.0, help='最低年化%%（默认0）')
    ap.add_argument('--top', type=int, default=40, help='输出前 N 只（默认40）')
    ap.add_argument('--verify', help='对单标的从 mootdx 拉日K验证三条件，如 688111.SH')
    ap.add_argument('--verify-all', action='store_true',
                    help='对候选池 data/screener_candidates.json 批量 verify（默认全部40只，可用 --top 限数）')
    ap.add_argument('--save', action='store_true', help='把 xlsx 候选池写入 data/screener_candidates.json')
    args = ap.parse_args()

    if args.verify:
        r = verify_from_mootdx(args.verify)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.verify_all:
        if not os.path.exists(OUT_CANDIDATES):
            print(f'❌ 候选池不存在: {OUT_CANDIDATES}（先跑 --xlsx --save）')
            return
        with open(OUT_CANDIDATES, encoding='utf-8') as f:
            cands = json.load(f).get('candidates', [])
        syms = [c['symbol'] for c in cands]
        name_map = {c['symbol']: c.get('name', '') for c in cands}
        print(f'🎯 批量 verify 候选池 {len(syms)} 只（--top 可限数）...')
        results = verify_batch(syms, max_n=args.top)
        # 合并 xlsx 绩效与实时三条件
        for r in results:
            r['name'] = name_map.get(r['symbol'], '')
        out = {'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'criteria': {'ppt_s9': {'amt_yi': MIN_AMT_YI,
                                       'turnover': [TURNOVER_MIN, TURNOVER_MAX],
                                       'amp': [AMP_MIN, AMP_MAX]}},
               'verified': results}
        out_path = os.path.join(BASE, 'data', 'screener_verified.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        # 三条件全通过数
        full_pass = [r for r in results if r.get('pass_amt') and r.get('pass_turnover') and r.get('pass_amp')]
        amt_pass = sum(1 for r in results if r.get('pass_amt'))
        to_pass = sum(1 for r in results if r.get('pass_turnover'))
        amp_pass = sum(1 for r in results if r.get('pass_amp'))
        print(f'\n✅ 验证完成: {len(results)} 只')
        print(f'  成交额≥{MIN_AMT_YI}亿: {amt_pass} | 换手率{TURNOVER_MIN}-{TURNOVER_MAX}%: {to_pass} | 振幅{AMP_MIN}-{AMP_MAX}%: {amp_pass}')
        print(f'  三条件全过: {len(full_pass)} 只')
        if full_pass:
            print('  ── 三条件全过名单 ──')
            for r in full_pass:
                print(f'  {r["symbol"]:12s} {r.get("name","")} 成交额{r["amt_yi"]}亿 换手{r["turnover_pct"]}% 振幅{r["amp_pct"]}%')
        print(f'💾 已写入 {out_path}')
        return

    if not args.xlsx:
        ap.print_help()
        return

    recs = parse_xlsx(args.xlsx)
    print(f'✅ 解析 xlsx 完成: {len(recs)} 只标的')
    cand = filter_xlsx(recs, min_level=args.min_level,
                       min_open_rate=args.min_open_rate, min_ann=args.min_ann,
                       top=args.top)
    print(f'🎯 满足条件(Level≥{args.min_level} & 开仓率≥{args.min_open_rate}% & 年化≥{args.min_ann}%): {len(cand)} 只')
    for i, r in enumerate(cand, 1):
        print(f'  {i:2d}. {r["symbol"]:12s} {r["name"]:8s} L{r["level"]} 开仓率{r["open_rate_20d"]}% 年化{r["ann_ret"]}% 20日{r["ret_20d"]}% 胜率{r["win_rate_20d"]}%')
    if args.save:
        with open(OUT_CANDIDATES, 'w', encoding='utf-8') as f:
            json.dump({'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'criteria': {'min_level': args.min_level,
                                    'min_open_rate': args.min_open_rate,
                                    'min_ann': args.min_ann,
                                    'ppt_s9': {'amt_yi': MIN_AMT_YI,
                                               'turnover': [TURNOVER_MIN, TURNOVER_MAX],
                                               'amp': [AMP_MIN, AMP_MAX]}},
                       'candidates': cand}, f, ensure_ascii=False, indent=2)
        print(f'💾 候选池已写入 {OUT_CANDIDATES}')


if __name__ == '__main__':
    main()
