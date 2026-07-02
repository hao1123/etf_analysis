from datetime import datetime
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
    clean_fund_name,
    laplace_filter,
)


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
        self.assertEqual(clean_fund_name("华夏芯片ETF基金"), "芯片")

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
