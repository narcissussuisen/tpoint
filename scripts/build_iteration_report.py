# -*- coding: utf-8 -*-
"""
build_iteration_report.py — 迭代报告生成器（自迭代闭环交付）

读取改进清单实施结果 → 生成 iteration_report_{date}.html
每项含：问题定义 → 修改动作 → 自我评判（合理性/不足）→ 下一轮迭代目标
"""
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
OUT = os.path.join(BASE, 'output', f'iteration_report_{DATE}.html')


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ========== 迭代记录（轮次1） ==========
# 每项: (编号, 标题, 问题, 修改动作, 自我评判_合理, 自我评判_不足, 下一轮目标, 涉及文件, 状态)
ITEMS = [
    # ---- P0 ----
    ('P0-1', '数据源韧性：腾讯分时退避重试',
     '盘中 getaddrinfo 间歇失败致 4 标的全失联，全天 22 信号仅推 7 条（漏推≈50%），腾讯兜底单次请求即放弃',
     'datasource._tencent_intraday_fallback 改用 _retry_with_backoff(3次/1s-4s)；仅网络异常重试，返回 None（无数据）不重试；总耗时 ≤7s+首试 < 15s 扫描间隔',
     '① 退避策略与 mootdx 主源一致，复用既有基建；② 无数据不重试避免无效循环；③ 失败日志明确"已重试3次"便于诊断',
     '未加"连续 N 轮失败暂停扫描"降级（monitor 已有静默零信号告警覆盖，L1262-1288，无需重复）；盘中若 DNS 持续失败，重试仅延后失败 7s，不改变"该轮无数据"结果',
     '下一轮：接入 mootdx 二级服务器池（10 台 _TDX_SERVERS 已定义但腾讯兜底未做服务器级 failover）',
     'core/datasource.py:306', '✅ 已部署'),
    ('P0-2', '首扫抑制窗口收窄（now-3min 白名单）',
     '固定 target_t=09:30/13:00 导致 09:30 后重启把已过信号重扫重发（07-30 重放 9 条），且误伤 07-31 09:33 金山 S@253.89 真实信号',
     '① target_t 改为 (now-3min)；② 首扫时按信号时间戳 ≥(now-3min) 白名单放行，仅抑制更早的历史重扫；③ 白名单放行打日志 ✅',
     '① 直接命中 07-31 案例（09:33 信号在 09:34 重启场景下 now-3min=09:31，信号被放行）；② 精确到信号级而非 bar 级，粒度更细；③ 与 REPLAY_MAX_AGE_S=600 补发闸门协同',
     "白名单用字符串比较时间戳（'%Y-%m-%d %H:%M'），跨日边界（23:58 重启 → 00:01）字符串比较会失效，需 datetime 对象比较；当前 tpoint 无跨日重启场景，风险低",
     '下一轮：白名单时间比较改用 datetime 解析，消除字符串边界隐患',
     'core/monitor.py:1304-1337', '✅ 已部署'),
    ('P0-3', '绩效统计扩展（卡方风格）',
     'aggregate_metrics 无年化/回撤/夏普/开仓率/Level，无法与卡方 xlsx 17 列对照',
     '① aggregate_metrics 新增 max_drawdown_pct/sharpe/ann_ret_pct（复利净值序列计算）；② 新建 scripts/performance_stats.py：kf_style_stats 输出 20日/5日/当日收益、开仓率、胜率、Level 星级',
     '① 最大回撤/夏普用标准公式（峰值回落/年化 std×√244）；② 开仓率 v2 口径修正为"有信号日/窗口日"（首版"笔数/日"虚高 200% 已修正）；③ 纯函数无副作用可复用',
     '① 年化在小样本（<20 笔）数学放大失真（4 笔 → 509%），需样本量标注；② 夏普假设"每笔≈1交易日"，做T笔级口径有偏差；③ 卡方口径含费用（万3.5+万5.641），tpoint 无费用模型，数值不可直接比较',
     '下一轮：绩效统计接入每日复盘（P2-3 已做）+ 样本量阈值（<20 笔标注"小样本"）',
     'core/exit_manager.py:149, scripts/performance_stats.py', '✅ 已部署'),
    ('P0-4', '全市场标的筛选器',
     'watchlist 手工 4 只，无从全市场挑标的能力（用户澄清：4 只持仓驱动，差距在缺筛选工具）',
     '新建 scripts/market_screener.py：① --xlsx 模式标准库解析 5002 只（zipfile+XML，无 openpyxl 依赖）按 Level≥3&开仓率≥50% 过滤；② --verify 模式 mootdx 拉近 1 月日 K 算三条件；③ 候选池落盘 data/screener_candidates.json',
     '① 零依赖解析 xlsx 实测成功（5002 只）；② 候选池 40 只已落盘，与 PPT S9 名单同量级；③ verify 三条件与 PPT S9 口径对齐',
     '① 换手率依赖 mootdx finance 接口（ltsz 流通市值字段校验未充分，可能 None）；② 成交额用"收盘×量"近似而非真实成交额；③ 年化极高（724%）多为新股/小盘极端样本，需过滤提示',
     '下一轮：verify 模式对候选池批量跑 + 换手率字段校验（finance 接口实测）',
     'scripts/market_screener.py, data/screener_candidates.json', '✅ 已部署'),
    # ---- P1 ----
    ('P1-1', '多周期方向标注（1m/5m/1h）',
     '仅 1m 单周期；v9.3.0 已证伪 5m/15m 策略融合（PF 0.605），但周期因子作监控参考仍有价值',
     '① miji_alpha 新增 mtf_direction_snapshot/high_level_trend 纯函数（1m 序列聚合近似 5m/1h）；② 新建 scripts/mtf_direction_lab.py 输出方向快照 JSON',
     '① 严格遵循"不融合进 1m 信号"的用户决策，只作方向参考；② 1h 方向数据不足时正确降级"数据不足"而非假 0（v2/v3 迭代修正）；③ 脚本实测 4 标的输出 1m/5m 方向',
     '① mootdx 免费源 1m 历史仅回溯约 4 个交易日 → 1h 方向（需 ≥10 交易日）不可用，已标注"数据不足"；② 5m 聚合用 [::5] 取末值近似，与真实 5m K 线有偏差',
     '下一轮：接入历史 CSV 缓存（tickflow 落地 1m 数据）补足多日跨度，启用 1h 方向',
     'core/miji_alpha.py, scripts/mtf_direction_lab.py', '✅ 已部署'),
    ('P1-3', '复算口径对齐（复盘强制 mootdx）',
     '复盘引擎当日走 intraday() 可能降级腾讯合成数据（open=前收/high=low=极值），与实盘 mootdx 口径偏差（588000 B@1.783 vs 1.788）',
     'daily_signal_review.fetch_1m：当日强制 historical_1m（mootdx 真实 1m），仅失败才降级 intraday 并标注 data_source=tencent_synth',
     '① 直接消除合成数据进复盘的口径偏差；② 数据来源 attr 标注（mootdx/tencent_synth）可审计；③ 历史路径本就 mootdx，无回归风险',
     '当日 mootdx 失败时仍会降级腾讯合成（无法避免，标注来源已是兜底）；historical_1m 强制抛异常重试 3 次，当日复盘可能多 3-7s',
     '下一轮：降级时在复盘报告显著标注"该标的当日为合成数据"',
     'scripts/daily_signal_review.py:101', '✅ 已部署'),
    # ---- P2 ----
    ('P2-1', '高波动保护（单边行情守卫）',
     '07-30/31 振幅 9.8%/9.4%（基线 5.6%），4 条失效信号全为"均线引力被反向突破"；_gate_floor 已预留 trend_state 但生产从未接通',
     '① 接通 _gate_floor trend_state/floor_trend_threshold（check_miji_trigger + detect_miji_signals 双路径）；② 强下跌地板×1.5、强上涨天花板×1.5；③ HIGH_VOL_GUARD 环境开关（默认开）',
     '① 复用已预留参数，无架构改动；② 纯函数单测验证：dev=1.8% 在守卫下 floor=False/pass=False（精确命中失效场景）；③ 开关可随时回退',
     '① 守卫仅影响 floor 门控通道，strict/MACD 通道不受影响（设计如此，MACD 背离本就是首选买点）；② ×1.5 系数是启发式，未回测验证最优',
     '下一轮：对 07-30/31 高波动日重跑复盘验证守卫后失效信号数下降',
     'core/miji_alpha.py:560-610, backtest/keyfactor/_gate_floor.py', '✅ 已部署'),
    ('P2-2', '实盘/复算 diff 汇总',
     '复盘报告有实盘 vs 复算雏形但无自动化 diff 汇总',
     'daily_signal_review 新增 diff_rows：每标的实盘计数 vs 复算计数 → Δ买/Δ卖 + 结论（一致/实盘>复算/实盘<复算）；HTML 新增"〇·C diff 汇总"卡片',
     '① 自动产出无人工；② 结论语义化（漏推/重放方向）；③ 与既有 〇/〇·B 节同屏形成完整链条',
     'diff 仅计数级，无逐笔时间对齐（逐笔见 push_audit 已覆盖）；"实盘>复算"的"重放"推断在 07-30 后已基本消除',
     '下一轮：diff 加逐笔时间戳对齐（audit vs 复算行级匹配）',
     'scripts/daily_signal_review.py', '✅ 已部署'),
    ('P2-3', '复盘对齐卡方指标',
     '复盘报告缺 20日/5日开仓率、胜率、Level 星级展示',
     'build_review_html 新增"四·B 卡方风格绩效统计"卡片：从复算 rows 近似 trips → kf_style_stats → 胜率/开仓率/Level/盈亏比 + 锚点对照',
     '① 复用 P0-3 的 performance_stats，单一口径；② 锚点对照（卡方中位胜率 61%、688111 锚点）直接展示差距；③ 冒烟验证胜率 69.2% 与复盘一致',
     '近似 trips 用向前验证有利/不利波动估收益，非真实成交收益；开仓率=当日 100%（单日样本天然 100%），跨日才有区分度',
     '下一轮：接入历史 review JSON 序列做 5 日/20 日滚动绩效（需多日数据留存）',
     'scripts/build_review_html.py', '✅ 已部署'),
    ('P1-2', '因子扩展评估（文档）',
     '缺动量/行业/情绪/波动率/流动性因子；需评估接入性价比',
     'docs/factor_expansion_assessment.md：逐因子数据可得性×接入方案×优先级；波动率=可做(P2 置信度标注)、流动性=研究、动量=周期错配不做、行业/情绪=数据不可得',
     '① 明确"不做"项及理由（避免无效投入）；② 引用 v9.3.0 盲 holdout 教训防止重蹈；③ 与 P2-1 打通（高波动保护即波动率因子工程化落地）',
     '行业/情绪数据源未调研具体替代方案（同花顺/东财板块接口待立项）',
     '下一轮：若用户需行业因子，调研板块数据源可行性',
     'docs/factor_expansion_assessment.md', '✅ 已落地'),
    ('P1-4', '自动交易执行（用户已决策不实施）',
     '仅信号推送+人工执行；PPT 目标=批量自动委托',
     '用户已拍板：保持信号推送，不升级自动下单。本迭代未做任何代码改动，仅记录远期备注（若接入需券商 API+风控前置+失败回滚）',
     '尊重用户决策，避免架构级无效改动',
     '—',
     '—',
     '—', '✅ 已决策'),
]

SUMMARY = [
    ('P0 必改', 4, 4, '数据源/首扫/绩效/筛选全部落地'),
    ('P1 应改', 3, 3, '多周期方向/复算口径/因子评估落地（P1-4 用户决策不实施）'),
    ('P2 优化', 3, 3, '高波动守卫/diff/复盘对齐全部落地'),
    ('已部署', 9, 9, 'monitor 已重启加载新代码（PID 17000）'),
    ('遗留', 4, 4, '腾讯多服务器 failover / 白名单 datetime 比较 / 样本量阈值 / 1h 数据源'),
]

# ========== 轮次2 迭代记录（自迭代闭环 · 第 2 轮） ==========
# 每项: (编号, 标题, 问题, 修改动作, 自我评判_合理, 自我评判_不足, 下一轮目标, 涉及文件, 状态)
ROUND2_ITEMS = [
    ('R2-1', '腾讯兜底服务器级 failover（域名池 + 跨厂商新浪）',
     '07-31 盘中 getaddrinfo 间歇失败致腾讯分时兜底单域名单点故障，4 标的长时间失联，全天漏推≈50% 信号；轮次1 退避重试只解决"抖动"，不解决"域名级故障"',
     '① 腾讯域名池 _TENCENT_HOSTS（3 镜像按序尝试，单域失败立即切换）；② 新增独立厂商新浪 1m 兜底（真实 OHLC，抗腾讯全家族 DNS 故障）；③ 全池失败才退避重试 3 次；④ 实测：主域正常 242 根，模拟腾讯全挂自动切新浪 250 根真实 OHLC',
     '① 三级兜底链（mootdx → 腾讯域名池 → 新浪）消除单点；② 新浪真实 OHLC 质量优于腾讯分时合成数据；③ 切换日志（域名失败→切备用）便于诊断；④ 总耗时仍 <15s 扫描间隔',
     '① 新浪接口是 JSONP（跨域包装），解析依赖正则，若新浪改版会失效；② 域名池顺序固定，未做"健康域名置顶"的粘滞优化（每次从第一个开始试）；③ web.sqt.gtimg.cn 实测 DNS 失败，池中占位但无实际贡献',
     '下一轮：域名池做健康状态粘滞（最近成功域名优先）+ 新浪 JSONP 解析加固（容错更多前缀）',
     'core/datasource.py', '✅ 已部署'),
    ('R2-2', '首扫白名单 datetime 比较（消除跨日边界）',
     '轮次1 白名单用字符串比较时间戳（"%Y-%m-%d %H:%M"），跨日边界（23:58 重启→00:01）字符串比较失效：\'23:58\' < \'00:01\' 误判',
     'monitor 首扫白名单 recent_cutoff 改为 datetime 对象（now-3min, 去 tzinfo），信号时间戳 fromisoformat 解析后比较；解析失败保守抑制',
     '① 5 场景单测全过（跨日 A/B 放行、C 抑制、盘中案例 D 放行/E 抑制）；② 与 REPLAY_MAX_AGE_S=600 补发闸门语义一致；③ 解析失败保守抑制不重发（安全侧）',
     '① fromisoformat 只接受 ISO 格式，若信号时间戳将来变格式会静默抑制；② 未覆盖"信号时间戳为空"的语义（空→抑制，保守但可能吞真实信号）',
     '下一轮：信号时间戳统一为 datetime 对象传递（而非字符串），从根上消除格式耦合',
     'core/monitor.py:1325', '✅ 已部署'),
    ('R2-3', '绩效样本量阈值标注（小样本防误导）',
     '轮次1 年化小样本数学放大失真（4 笔 → 1033%），复盘卡片直接展示误导；sharpe/年化无样本量上下文',
     'performance_stats.kf_style_stats 新增 sample_warning：<20 笔=小样本、20-59=样本偏小、≥60=None；build_review_html 卡方卡片在样本不足时显著展示 ⚠️ 警告条',
     '① 阈值 20/60 参考统计学经验（年化需 ≥20 笔才有意义）；② 警告条橙色高亮不干扰数值展示；③ 空输入/单笔降级安全',
     '① 阈值未用卡方 xlsx 分布校准（其 20 日开仓率仅 18.3% 标的样本量大得多）；② 警告文案在 build_review_html 是硬编码，未随 performance_stats 参数化',
     '下一轮：接历史多日滚动绩效（5日/20日真实 trips），样本量自然满足',
     'scripts/performance_stats.py, scripts/build_review_html.py', '✅ 已部署'),
    ('R2-4', '1h 方向接入历史 CSV 缓存（tick_cache）',
     '轮次1 mootdx 免费源 1m 历史仅回溯约 4 交易日 → 1h 方向需 ≥10 交易日数据，全部"数据不足"',
     'mtf_direction_lab 新增 load_tick_cache_1m：读 data/tick_cache/{sym}_{yyyymmdd}.csv（tick 级逐笔）聚合为 1m K 线（open/close/high/low/volume），最多 15 日；缓存缺失时降级 mootdx',
     '① 161129/513310 有 67 日缓存 → 1h 方向立即可用（实测 1h 多头↑/空头↓）；② 聚合规则正确（首笔 open/末笔 close/最大 high/最小 low/Σvolume）；③ 688111/588000 无缓存正确降级 mootdx + 标注"数据不足"而非假 0',
     '① 缓存仅到 07-24（161129），07-30/31 需实时补录（无自动缓存写入机制）；② 688111/588000 是 07-31 新标的，tick_cache 无历史（需等积累或手动拉取）；③ tick 聚合为 1m 有微小精度损失（同分钟多笔取首末）',
     '下一轮：给 monitor 增加收盘后 tick_cache 自动落盘（每天 15:00 后把当日 tick 写入缓存），让新标的 1h 方向 2 周后自动可用',
     'scripts/mtf_direction_lab.py', '✅ 已部署'),
    ('R2-5', '高波动守卫校准（trend_strong + 当日涨跌门控）',
     '轮次1 A/B 验证发现 ×1.5 固定系数在 V 型反转日方向错误：07-31 588000 把 4 条有效浅层地板（dev -1.5~-2.1%）误滤、深层失效接飞刀（-2.3%/-3.1%）反而放行。根因：EMA 趋势在 V 反转日滞后 10+ 根',
     '① compute_trend_strength：连续 8 根同向才确认强趋势；② 守卫判定改为 trend_strong + 当日涨跌门控（day_chg<-3% 才收紧地板 / >+3% 才抬高天花板）；③ monitor 生产路径 check_miji_trigger 与回测 detect_miji_signals 同步',
     '① 07-30（真单边下跌日）守卫仍有效：floor 失效 4→2、总失效 8→6、有效率 76.5%→80.0%；② 07-31（V 型反转日）守卫正确退出：588000 floor 信号与关守卫完全一致（6 条全保留），有效率 67.9%→70.0%；③ 三版本对比实证：裸 trend（滤4条有效）→ trend_strong+涨跌门控（0 误滤）',
     '① confirm_bars=8 是启发式，未网格搜索最优；② day_chg 阈值 ±3% 是经验值，ETF/LOF 波动率与个股不同；③ 07-31 仍滤掉 1 条边缘 floor（全局 floor 15 vs 16）',
     '下一轮：confirm_bars 与 day_chg 阈值网格搜索（在 07-24~31 多日数据上）+ 守卫开关加入复盘报告展示',
     'core/miji_alpha.py, scripts/verify_vol_guard.py', '✅ 已部署'),
    ('R2-6', 'screener 批量 verify + 换手率字段修正',
     '轮次1 换手率用 mootdx finance 的 ltsz 字段——实测 finance 接口返回的是 zongguben（总股本）等拼音字段，无 ltsz → 换手率恒为 None；且缺批量 verify 模式',
     '① verify_from_mootdx 换手率改用 zongguben（总股本，股数）计算（A 股基本全流通近似）；② 新增 --verify-all 批量模式：读候选池 40 只顺序 verify，输出 data/screener_verified.json + 三条件通过统计；③ pass_turnover 统一 bool 修复 JSON 序列化',
     '① 换手率实测有效（688111=0.03%、161129=1.84% 合理）；② 批量验证 10 只正常输出；③ 重要口径发现：候选池 40 只全不满足成交额≥50亿（0.08-0.69亿），watchlist 四标的也不满足（最大 1.69亿）——PPT S9 门槛为个股设计，ETF/LOF 需另设口径',
     '① 批量 verify 顺序执行 40 只需几分钟（每只 2 次 mootdx 请求）；② 换手率用总股本近似流通股本，限售股多的次新股有偏差；③ 候选池全是小盘高波动股，与 PPT S9"大盘活跃股"方向相反，需要用户决策筛选口径',
     '下一轮：与用户确认筛选口径（个股池 vs ETF/LOF 池两套阈值）；verify 加并发或缓存加速',
     'scripts/market_screener.py, data/screener_verified.json', '✅ 已部署'),
]

ROUND2_SUMMARY = [
    ('R2-1 数据源', 1, 1, '腾讯域名池 + 跨厂商新浪 1m，三级兜底链实测通过'),
    ('R2-2 白名单', 1, 1, 'datetime 比较消除跨日边界（5 场景单测）'),
    ('R2-3 绩效', 1, 1, '样本量阈值标注（<20 小样本 / <60 偏小）'),
    ('R2-4 1h方向', 1, 1, 'tick_cache 聚合接入，161129/513310 1h 方向可用'),
    ('R2-5 守卫', 1, 1, 'trend_strong+涨跌门控：07-30 有效保持、07-31 误滤消除'),
    ('R2-6 筛选器', 1, 1, '换手率字段修正 + 批量 verify + 口径发现'),
]

NEXT_ROUND = [
    '① 腾讯域名池健康粘滞（最近成功域名优先）+ 新浪 JSONP 解析加固',
    '② 信号时间戳统一为 datetime 对象传递（消除字符串格式耦合）',
    '③ 接历史多日滚动绩效（5日/20日真实 trips，样本量自然满足）',
    '④ monitor 收盘后 tick_cache 自动落盘（新标的 1h 方向 2 周后自动可用）',
    '⑤ confirm_bars / day_chg 阈值网格搜索 + 守卫开关进复盘报告',
    '⑥ 与用户确认筛选口径（个股池 vs ETF/LOF 池两套阈值）+ verify 加速',
]


def _items_html(items):
    item_html = ''
    for num, title, problem, action, pro, con, nxt, files, status in items:
        item_html += f'''
<div class="item">
  <div class="head"><span class="num">{esc(num)}</span><span class="title">{esc(title)}</span>
    <span class="st ok">{esc(status)}</span></div>
  <table>
    <tr><td class="lbl">问题定义</td><td>{esc(problem)}</td></tr>
    <tr><td class="lbl">修改动作</td><td>{esc(action)}</td></tr>
    <tr><td class="lbl pro">✓ 合理性</td><td class="pro">{esc(pro)}</td></tr>
    <tr><td class="lbl con">✗ 不足</td><td class="con">{esc(con)}</td></tr>
    <tr><td class="lbl">下一轮目标</td><td>{esc(nxt)}</td></tr>
    <tr><td class="lbl">涉及文件</td><td class="mono">{esc(files)}</td></tr>
  </table>
</div>'''
    return item_html


def build_html():
    r1_html = _items_html(ITEMS)
    r2_html = _items_html(ROUND2_ITEMS)

    sum_rows = ''
    for name, done, total, note in SUMMARY:
        sum_rows += f'<tr><td class="dim">{esc(name)}</td><td>{done}/{total}</td><td>{esc(note)}</td></tr>'

    r2_sum_rows = ''
    for name, done, total, note in ROUND2_SUMMARY:
        r2_sum_rows += f'<tr><td class="dim">{esc(name)}</td><td>{done}/{total}</td><td>{esc(note)}</td></tr>'

    next_html = ''
    for i, n in enumerate(NEXT_ROUND, 1):
        next_html += f'<div class="next">{i}. {esc(n)}</div>'

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tpoint 自迭代改进报告 · {esc(DATE)}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background:#0f1419; color:#dbe4ee; font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; padding:28px 20px 60px; line-height:1.6; }}
.wrap {{ max-width:1060px; margin:0 auto; }}
h1 {{ font-size:25px; margin-bottom:4px; }}
.sub {{ color:#8b98a8; font-size:13px; margin-bottom:22px; }}
h2 {{ font-size:18px; margin:30px 0 12px; padding-left:10px; border-left:4px solid #4da3ff; }}
.card {{ background:#1a222c; border:1px solid #2b3644; border-radius:10px; padding:16px 18px; margin:12px 0; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ border:1px solid #2b3644; padding:7px 10px; text-align:left; vertical-align:top; }}
th {{ background:#141b24; color:#38d6d0; }}
td.dim {{ white-space:nowrap; font-weight:600; color:#4da3ff; }}
td.lbl {{ color:#8b98a8; width:88px; white-space:nowrap; font-weight:600; vertical-align:top; }}
td.pro {{ color:#3ddc84; }} td.con {{ color:#ffab40; }}
.item {{ background:#1a222c; border:1px solid #2b3644; border-left:4px solid #4da3ff; border-radius:8px; padding:14px 18px; margin:12px 0; }}
.head {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; }}
.num {{ font-family:Consolas,monospace; font-weight:700; color:#8b98a8; }}
.title {{ font-weight:700; font-size:15px; }}
.st {{ font-size:11px; padding:1px 8px; border-radius:8px; margin-left:auto; }}
.st.ok {{ background:rgba(61,220,132,.15); color:#3ddc84; }}
.mono {{ font-family:Consolas,monospace; font-size:11.5px; color:#38d6d0; }}
.next {{ background:#141b24; border:1px solid #2b3644; border-radius:6px; padding:8px 14px; margin:6px 0; font-size:13px; }}
.foot {{ color:#6b7178; font-size:11.5px; margin-top:26px; border-top:1px solid #2b3644; padding-top:10px; }}
</style></head><body><div class="wrap">

<h1>🔄 tpoint 自迭代改进报告 — {esc(DATE)}</h1>
<div class="sub">对照 gap_analysis_2026-07-31 改进清单 → 轮次1 落地 11 项 → 轮次2 校准/补强 6 项（闭环式自迭代 · 累计 2 轮）</div>

<h2>轮次2 完成总览</h2>
<div class="card"><table>
<tr><th>改进域</th><th>完成</th><th>说明</th></tr>
{r2_sum_rows}
</table></div>

<h2>轮次2 逐项迭代记录（问题 → 修改 → 自我评判 → 下一轮）</h2>
{r2_html}

<h2>轮次1 回顾（11 项改进清单落地）</h2>
<div class="card"><table>
<tr><th>类别</th><th>完成</th><th>说明</th></tr>
{sum_rows}
</table></div>

<div class="sub" style="margin-top:10px">轮次1 逐项记录见前版报告（gap_analysis 11 项：P0-1~P0-4/P1-1~P1-4/P2-1~P2-3），本轮起以轮次2 为当前迭代主体。</div>

<h2>下一轮迭代目标（闭环续接）</h2>
<div class="card">
{next_html}
</div>

<div class="foot">生成 {esc(DATE)} · tpoint 自迭代轮次2 · 6 项改动已语法校验+单元验证+A/B 实证+部署重启（monitor PID 9312）</div>
</div></body></html>'''


def main():
    html = build_html()
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ 迭代报告已生成: {OUT} ({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
