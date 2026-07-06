from datetime import date, datetime
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from etf import (
    LocalETFStrategy,
    StateStore,
    calculate_momentum_score,
    calculate_volume_ratio,
    clean_name_with_group,
    joinquant_pool_from_log,
    laplace_filter,
    momentum_within_source_tick,
)
from etf_data import (
    DataSourceError,
    HISTORY_CACHE_VERSION,
    MarketDataHub,
    SPOT_ARCHIVE_PREFIX,
    intraday_covers_signal,
    normalize_history,
    normalize_intraday,
    volume_to_shares,
)
from etf_config import StrategyConfig


class ETFStrategyTests(unittest.TestCase):
    def test_momentum_score_for_steady_uptrend(self):
        prices = np.linspace(1.0, 1.3, 30)
        score, annualized, r_squared = calculate_momentum_score(prices, 25)
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)
        self.assertGreater(annualized, 0)
        self.assertGreater(r_squared, 0.95)

    def test_volume_ratio_projects_partial_session(self):
        history = np.array([100.0] * 5)
        result = calculate_volume_ratio(
            history,
            today_volume=50,
            now=datetime(2026, 7, 2, 10, 30),
            lookback_days=5,
        )
        self.assertEqual(result, 2.0)

    def test_laplace_filter_tracks_price_without_overshoot(self):
        prices = np.array([1.0, 2.0, 3.0])
        result = laplace_filter(prices)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], 1.0)
        self.assertTrue(1.0 < result[-1] < 3.0)

    def test_clean_fund_name(self):
        self.assertEqual(
            clean_name_with_group("华夏芯片ETF基金", None), "芯片"
        )

    def test_quote_volume_is_normalized_from_lots_to_shares(self):
        self.assertEqual(
            volume_to_shares(10_000, 100_000_000, 100),
            1_000_000,
        )

    def test_history_volume_keeps_share_unit(self):
        frame = pd.DataFrame({
            "date": ["2026-07-02"],
            "open": [1.0],
            "close": [1.0],
            "high": [1.0],
            "low": [1.0],
            "volume": [1_000_000],
            "amount": [1_000_000],
        })
        result = normalize_history(frame)
        self.assertEqual(result.iloc[0]["volume"], 1_000_000)

    def test_history_cache_version_requires_adjusted_prices(self):
        self.assertIn("qfq", HISTORY_CACHE_VERSION)

    def test_momentum_upper_bound_allows_only_one_quote_tick(self):
        self.assertTrue(
            momentum_within_source_tick(5.0079, 4.9811, 0.0, 5.0)
        )
        self.assertFalse(
            momentum_within_source_tick(5.0149, 5.0059, 0.0, 5.0)
        )
        self.assertFalse(
            momentum_within_source_tick(-0.0001, -0.01, 0.0, 5.0)
        )
        self.assertFalse(
            momentum_within_source_tick(10.0, -1.0, 0.0, 5.0)
        )

    def test_archived_market_amounts_use_previous_three_days(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            for raw_day, first, second in [
                ("2026-06-30", 30.0, 15.0),
                ("2026-07-01", 60.0, 30.0),
                ("2026-07-02", 90.0, 45.0),
            ]:
                pd.DataFrame({
                    "代码": ["510300", "159667"],
                    "名称": ["沪深300ETF", "工业母机ETF"],
                    "成交额": [first, second],
                }).to_csv(
                    cache_dir / f"{SPOT_ARCHIVE_PREFIX}{raw_day}.csv",
                    index=False,
                )
            hub = MarketDataHub(cache_dir)
            frame, days, totals = hub.get_archived_market_amounts(
                date(2026, 7, 3), 3
            )
            amounts = dict(zip(frame["代码"], frame["日均成交额"]))
            self.assertEqual(days[0], date(2026, 6, 30))
            self.assertEqual(days[-1], date(2026, 7, 2))
            self.assertEqual(totals, [45.0, 90.0, 135.0])
            self.assertEqual(amounts["510300"], 60.0)
            self.assertEqual(amounts["159667"], 30.0)

    def test_real_archive_overrides_bootstrap_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "cache"
            bootstrap_dir = root / "bootstrap"
            cache_dir.mkdir()
            bootstrap_dir.mkdir()
            filename = f"{SPOT_ARCHIVE_PREFIX}2026-07-03.csv"
            pd.DataFrame({
                "代码": ["510300"],
                "名称": ["沪深300ETF"],
                "成交额": [10.0],
            }).to_csv(bootstrap_dir / filename, index=False)
            pd.DataFrame({
                "代码": ["510300"],
                "名称": ["沪深300ETF"],
                "成交额": [30.0],
            }).to_csv(cache_dir / filename, index=False)

            hub = MarketDataHub(cache_dir, bootstrap_dir=bootstrap_dir)
            frame, days, totals = hub.get_archived_market_amounts(
                date(2026, 7, 6), 3
            )

            self.assertEqual(days, [date(2026, 7, 3)])
            self.assertEqual(totals, [30.0])
            self.assertEqual(frame.iloc[0]["日均成交额"], 30.0)
            self.assertNotIn("种子收盘快照", hub.source_summary())

    def test_archived_amount_mean_ignores_missing_snapshot_days(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            pd.DataFrame({
                "代码": ["510300"],
                "名称": ["沪深300ETF"],
                "成交额": [30.0],
            }).to_csv(
                cache_dir / f"{SPOT_ARCHIVE_PREFIX}2026-07-02.csv",
                index=False,
            )
            pd.DataFrame({
                "代码": ["510300", "501018"],
                "名称": ["沪深300ETF", "南方原油LOF"],
                "成交额": [60.0, 20.0],
            }).to_csv(
                cache_dir / f"{SPOT_ARCHIVE_PREFIX}2026-07-03.csv",
                index=False,
            )

            hub = MarketDataHub(cache_dir)
            frame, _, _ = hub.get_archived_market_amounts(
                date(2026, 7, 6), 3
            )
            amounts = dict(zip(frame["代码"], frame["日均成交额"]))

            self.assertEqual(amounts["510300"], 45.0)
            self.assertEqual(amounts["501018"], 20.0)

    def test_archive_rejects_intraday_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = MarketDataHub(Path(directory))
            hub.get_eastmoney_spot = lambda force=False: pd.DataFrame({
                "代码": ["510300"],
                "名称": ["沪深300ETF"],
                "成交额": [100.0],
                "数据日期": ["2026-07-03"],
                "更新时间": ["2026-07-03 13:10:00"],
            })
            with self.assertRaises(DataSourceError):
                hub.archive_eastmoney_spot(date(2026, 7, 3))

    def test_fixed_pool_liquidity_uses_archived_amounts(self):
        class FakeData:
            @staticmethod
            def get_archived_market_amounts(before_day, count):
                return (
                    pd.DataFrame({
                        "代码": ["510300", "159667"],
                        "名称": ["沪深300ETF", "工业母机ETF"],
                        "日均成交额": [20.0, 5.0],
                    }),
                    [
                        date(2026, 6, 30),
                        date(2026, 7, 1),
                        date(2026, 7, 2),
                    ],
                    [100.0, 100.0, 100.0],
                )

        strategy = object.__new__(LocalETFStrategy)
        strategy.config = StrategyConfig()
        strategy.data = FakeData()
        selected = strategy._filter_by_liquidity(
            ["510300.XSHG", "159667.XSHE"],
            {},
            10.0,
            date(2026, 7, 3),
        )
        self.assertEqual(selected, ["510300.XSHG"])

    def test_joinquant_reference_log_restores_ranked_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "joinquant.log"
            path.write_text(
                "\n".join([
                    "2026-07-03 13:10:00 - INFO - start",
                    ">>> 第一步：所有ETF按动量得分从大到小排序 <<<",
                    "513290.XSHG 纳指生物: 动量得分: 4.7",
                    "159502.XSHE 生物科技: 动量得分: 13.3",
                    ">>> 第二步：符合全部过滤条件的ETF <<<",
                ]),
                encoding="utf-8",
            )
            self.assertEqual(
                joinquant_pool_from_log(path, date(2026, 7, 3)),
                ["513290.XSHG", "159502.XSHE"],
            )

    def test_cached_minute_lot_volume_is_normalized_to_shares(self):
        frame = pd.DataFrame({
            "time": ["2026-07-03 13:10:00"],
            "open": [1.0],
            "close": [1.0],
            "high": [1.0],
            "low": [1.0],
            "volume": [10_000],
            "amount": [1_000_000],
            "average": [1.0],
        })
        result = normalize_intraday(frame)
        self.assertEqual(result.iloc[0]["volume"], 1_000_000)

    def test_partial_afternoon_minute_cache_is_rejected(self):
        target_day = date(2026, 6, 29)
        partial = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-06-29 13:48:00",
                "2026-06-29 15:00:00",
            ])
        })
        complete = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-06-29 09:31:00",
                "2026-06-29 13:10:00",
            ])
        })
        self.assertFalse(intraday_covers_signal(partial, target_day))
        self.assertTrue(intraday_covers_signal(complete, target_day))

    def test_signal_quotes_are_cut_off_at_1310(self):
        class FakeData:
            @staticmethod
            def get_intraday_histories(codes, target_day):
                return {
                    codes[0]: pd.DataFrame({
                        "time": pd.to_datetime([
                            f"{target_day} 09:30:00",
                            f"{target_day} 13:10:00",
                            f"{target_day} 13:11:00",
                        ]),
                        "open": [1.0, 1.1, 1.2],
                        "close": [1.0, 1.1, 1.2],
                        "high": [1.0, 1.1, 1.2],
                        "low": [1.0, 1.1, 1.2],
                        "volume": [100.0, 200.0, 400.0],
                        "amount": [100.0, 220.0, 480.0],
                        "source": ["fixture"] * 3,
                    })
                }

            @staticmethod
            def get_quotes(codes):
                raise AssertionError(f"不应实时回退: {codes}")

        strategy = object.__new__(LocalETFStrategy)
        strategy.data = FakeData()
        strategy.state = type(
            "State", (), {"data": {"name_map": {"513290": "纳指生物"}}}
        )()
        code = "513290.XSHG"
        histories = {
            code: pd.DataFrame({
                "date": [datetime(2026, 7, 2).date()],
                "close": [1.0],
            })
        }
        quotes, exact, fallback = strategy._signal_quotes(
            [code], histories, datetime(2026, 7, 3, 13, 10)
        )
        self.assertEqual(exact, 1)
        self.assertEqual(fallback, 0)
        self.assertEqual(quotes[code].price, 1.1)
        self.assertEqual(quotes[code].volume, 300.0)

    def test_state_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = StateStore(path)
            state.data["weak"]["active"] = True
            state.save()
            self.assertTrue(StateStore(path).data["weak"]["active"])

    def test_historical_weak_state_uses_only_prior_days(self):
        dates = np.array([
            datetime(2026, 6, day).date() for day in range(15, 30)
        ])
        frame = {
            "date": dates,
            "close": np.linspace(100, 114, len(dates)),
        }
        history = __import__("pandas").DataFrame(frame)
        strategy = object.__new__(LocalETFStrategy)
        strategy.config = __import__("etf_config").StrategyConfig()
        histories = {
            code: history.copy()
            for code in ("000300.XSHG", "399101.XSHE", "399006.XSHE",
                         "000510.XSHG")
        }
        lines, weak = strategy._weak_state_from_histories(
            histories, datetime(2026, 6, 30).date()
        )
        self.assertFalse(weak)
        self.assertIn("站上MA：4/4", lines[-2])

    def test_historical_weak_state_carries_across_days(self):
        dates = pd.bdate_range("2026-06-08", "2026-06-29").date
        falling = np.linspace(120, 90, len(dates))
        recovering = falling.copy()
        recovering[-1] = 130
        rising = np.linspace(90, 120, len(dates))
        histories = {
            "000300.XSHG": pd.DataFrame({"date": dates, "close": falling}),
            "399101.XSHE": pd.DataFrame({"date": dates, "close": falling}),
            "399006.XSHE": pd.DataFrame({"date": dates, "close": recovering}),
            "000510.XSHG": pd.DataFrame({"date": dates, "close": rising}),
        }
        strategy = object.__new__(LocalETFStrategy)
        strategy.config = __import__("etf_config").StrategyConfig()
        lines, weak = strategy._weak_state_from_histories(
            histories, datetime(2026, 6, 30).date()
        )
        self.assertTrue(weak)
        self.assertIn("低于MA：2/4", lines[-2])
        self.assertIn("延续走弱期", lines[-1])


if __name__ == "__main__":
    unittest.main()
