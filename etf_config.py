"""本地 ETF 提醒策略的参数与固定池。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


FUND_COMPANIES = sorted({
    "易方达", "广发", "华夏", "华安", "嘉实", "富国", "招商", "鹏华", "南方",
    "汇添富", "国泰", "平安", "银华", "天弘", "建信", "工银", "华泰柏瑞",
    "博时", "景顺长城", "景顺", "华宝", "申万菱信", "万家", "中欧", "兴证全球",
    "浙商", "诺安", "前海开源", "泰康", "泰达宏利", "农银汇理", "交银", "东方红",
    "财通", "华商", "国联", "永赢", "金鹰", "德邦", "创金合信", "西部利得",
    "圆信永丰", "泓德", "汇安", "诺德", "恒生前海", "华润元大", "大成", "海富通",
    "摩根", "华泰", "中信", "中银", "兴全", "国信", "长城", "中金", "浙商证券",
    "东海", "东吴", "浦银安盛", "信达澳亚", "中加", "中航", "中融", "中邮",
    "中庚", "中信保诚", "中信建投", "中银国际", "中银证券", "九泰", "交银施罗德",
    "光大保德信", "兴银", "农银", "国投瑞银", "国海富兰克林", "国联安", "国金",
    "太平", "方正富邦", "民生加银", "汇丰晋信", "银河", "长信", "长安", "长盛",
    "长江证券", "鹏扬",
}, key=len, reverse=True)

NOISE_WORDS = sorted({
    "6666", "8888", "9999", "A类", "AH", "B", "BS", "C", "C类", "CS", "DB",
    "E", "E类", "ETF", "ETF基金", "ETF联接", "FG", "G60", "GF", "GT", "HGS",
    "LOF", "LOF基金", "LOF联接", "SG", "SZ", "TF", "TK", "WJ", "YH", "ZS",
    "ZZ", "板块", "策略", "产业", "场内", "场外", "低波", "基本面", "基金",
    "精选", "联接", "联接基金", "量化", "龙头", "民企", "民营", "国企", "央企",
    "智能", "全指", "上市开放式", "指基", "指增", "指数", "指数A", "指数C",
    "指数ETF", "指数基金", "主题", "增强", "上海", "黄", "30", "50", "100",
    "300", "500", "1000", "2000", "大", "新", "四川", "浙江", "湖北",
}, key=len, reverse=True)

EXCLUDED_DYNAMIC_KEYWORDS = sorted({
    "300", "500", "1000", "2000", "800", "30", "50", "100", "180", "200",
    "沪深", "中证", "上证", "深证", "深成", "A50", "A100", "A500", "深100",
    "短融", "可转债", "转债", "双债", "利率债", "国债", "地债", "政金债",
    "国开债", "基准国债", "新综债", "信用债", "企业债", "公司债", "城投债",
    "城投", "美元债", "沪公司债", "科创债", "科债", "科创AAA",
    "自由现金流", "现金流", "现金流E", "现金流基", "现金流TF", "现金流全",
    "300现金流", "800现金流", "货币", "现金", "快线", "快钱", "中银现金",
    "500现金", "800现金", "现金800", "现金自由", "现金指数", "全指现金",
    "现金全指", "ESG", "MSCI", "MS", "债",
}, key=len, reverse=True)

SPECIAL_GROUPS: list[dict[str, Any]] = sorted([
    {"name": "香港组",
     "keywords": sorted(["恒生", "恒指", "港股", "港股通", "H股", "香港", "港", "HKC", "HK", "HGS", "H", "中概", "HS科技"], key=len, reverse=True),
     "remove_words": sorted(["恒生", "恒指", "港股", "港股通", "H股", "香港", "港", "HKC", "HK", "HGS", "H", "中概", "HS"], key=len, reverse=True)},
    {"name": "科创组",
     "keywords": sorted(["科创", "科创板", "科综", "KC", "K C", "双创", "科创创业", "创创"], key=len, reverse=True),
     "remove_words": sorted(["科创", "科创板", "科综", "KC", "K C", "双创", "科创创业", "创创", "债券", "债汇", "债指", "债沪", "债易", "债基", "债兴", "债摩", "债", "AAA"], key=len, reverse=True)},
    {"name": "创业组",
     "keywords": sorted(["创业板", "创业", "创板", "创成长"], key=len, reverse=True),
     "remove_words": sorted(["创业板", "创业", "创板", "创成长"], key=len, reverse=True)},
    {"name": "美指组",
     "keywords": sorted(["标普", "纳指", "纳斯达克"], key=len, reverse=True),
     "remove_words": sorted(["标普", "纳指", "纳斯达克"], key=len, reverse=True)},
], key=lambda x: max(len(kw) for kw in x["keywords"]), reverse=True)


GLOBAL_ETF_POOL = [
    "518880.XSHG", "501018.XSHG", "161226.XSHE", "159985.XSHE",
    "159980.XSHE", "513310.XSHG", "159518.XSHE", "159509.XSHE",
    "513100.XSHG", "513520.XSHG", "513500.XSHG", "159502.XSHE",
    "513400.XSHG", "513030.XSHG", "513290.XSHG", "520830.XSHG",
    "159529.XSHE", "159570.XSHE", "513160.XSHG",
]

CHINA_ETF_POOL = [
    "513090.XSHG", "513120.XSHG", "513180.XSHG", "513330.XSHG",
    "513750.XSHG", "159892.XSHE", "513190.XSHG", "159605.XSHE",
    "513630.XSHG", "159323.XSHE", "510900.XSHG", "513920.XSHG",
    "513970.XSHG", "511380.XSHG", "512050.XSHG", "510500.XSHG",
    "159915.XSHE", "510300.XSHG", "512100.XSHG", "159949.XSHE",
    "588080.XSHG", "159967.XSHE", "588220.XSHG", "563300.XSHG",
    "510760.XSHG", "588200.XSHG", "515880.XSHG", "159981.XSHE",
    "512880.XSHG", "513350.XSHG", "159326.XSHE", "159516.XSHE",
    "159206.XSHE", "512480.XSHG", "159363.XSHE", "159870.XSHE",
    "512400.XSHG", "159755.XSHE", "588170.XSHG", "159992.XSHE",
    "159995.XSHE", "512890.XSHG", "515220.XSHG", "159566.XSHE",
    "159819.XSHE", "512800.XSHG", "512690.XSHG", "515050.XSHG",
    "562500.XSHG", "512170.XSHG", "517520.XSHG", "159869.XSHE",
    "512070.XSHG", "159611.XSHE", "562800.XSHG", "515120.XSHG",
    "512010.XSHG", "510880.XSHG", "515790.XSHG", "515980.XSHG",
    "512660.XSHG", "159928.XSHE", "512710.XSHG", "560860.XSHG",
    "515030.XSHG", "159766.XSHE", "159218.XSHE", "159852.XSHE",
    "516160.XSHG", "516150.XSHG", "159227.XSHE", "159583.XSHE",
    "588790.XSHG", "159865.XSHE", "512980.XSHG", "159851.XSHE",
    "561360.XSHG", "561980.XSHG", "562590.XSHG", "512200.XSHG",
    "159732.XSHE", "159667.XSHE", "516510.XSHG", "159840.XSHE",
    "159998.XSHE", "159825.XSHE", "512670.XSHG", "159883.XSHE",
    "515210.XSHG", "515400.XSHG", "159256.XSHE", "561330.XSHG",
    "515170.XSHG", "159638.XSHE", "516520.XSHG", "513360.XSHG",
    "516190.XSHG", "159647.XSHE", "159867.XSHE", "159745.XSHE",
    "516530.XSHG",
]


@dataclass(frozen=True)
class StrategyConfig:
    fixed_pool: tuple[str, ...] = tuple(GLOBAL_ETF_POOL + CHINA_ETF_POOL)
    global_pool: tuple[str, ...] = tuple(GLOBAL_ETF_POOL)
    defensive_etf: str = "511880.XSHG"
    holdings_num: int = 1
    lookback_days: int = 25
    weak_period_ma_lookback: int = 10
    max_weak_days: int = 20
    min_score_threshold: float = 0.0
    max_score_threshold: float = 5.0
    score_threshold_ratio: float = 0.9
    r2_threshold: float = 0.4
    ma_lookback: int = 10
    ma_threshold: float = 1.0
    volume_lookback: int = 5
    volume_threshold: float = 1.8
    loss_ratio: float = 0.97
    fixed_stop_loss_ratio: float = 0.95
    enable_r2_filter: bool = True
    enable_ma_filter: bool = True
    enable_volume_check: bool = True
    enable_loss_filter: bool = True
    enable_premium_filter: bool = False
    max_premium_rate: float = 30.0
    enable_laplace_filter: bool = True
    laplace_s_param: float = 0.05
    laplace_min_slope: float = 0.002
    fallback_liquidity_threshold: float = 10_000_000.0
    liquidity_lookback_days: int = 3
    dynamic_pool_limit: int = 100
    dynamic_prefilter_limit: int = 400
    state_path: Path = Path("data/etf_state.json")
    cache_dir: Path = Path("data/cache")
