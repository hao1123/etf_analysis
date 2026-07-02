"""本地 ETF 提醒策略的参数与固定池。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    dynamic_pool_limit: int = 100
    dynamic_prefilter_limit: int = 400
    state_path: Path = Path("data/etf_state.json")
    cache_dir: Path = Path("data/cache")
