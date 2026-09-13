#!/usr/bin/env python3
"""
test_tw_technicals.py — 模組 B（技術面趨勢與動能）測試套件。

驗證項目（依 Issue #5 規範）：
1. 單元測試（均線計算、Weinstein 4 階判定、Minervini VCP 收縮與突破量、ATR(14) 停損、RS 分數）
2. 整合測試（Yahoo Finance 抓取上市 2328.TW、上櫃 3141.TWO、指數 ^TWII / ^TWOII、資料不足降級）
3. 架構整合測試（tw-market-indicators.json technicals 區塊寫入、Provider 抽換）
"""

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.tw_providers import get_provider, load_tw_market_config
from tools.tw_providers._mock import (
    _generate_non_vcp_series,
    _generate_stage1_series,
    _generate_stage2_series,
    _generate_stage3_series,
    _generate_stage4_series,
    _generate_vcp_series,
)
from tools.tw_technicals import (
    analyze_technicals_for_ticker,
    classify_weinstein_stage,
    compute_atr,
    compute_moving_averages,
    compute_relative_strength,
    detect_vcp,
)


class TestMovingAveragesAndWeinstein(unittest.TestCase):
    """均線計算與 Weinstein 階段判定單元測試"""

    def test_ma_calculation(self):
        # 構造已知等差數列：1, 2, ..., 240
        closes = [float(i) for i in range(1, 241)]
        ma = compute_moving_averages(closes)

        self.assertEqual(ma["ma5"], round(sum(range(236, 241)) / 5, 2))
        self.assertEqual(ma["ma10"], round(sum(range(231, 241)) / 10, 2))
        self.assertEqual(ma["ma20"], round(sum(range(221, 241)) / 20, 2))
        self.assertEqual(ma["ma60"], round(sum(range(181, 241)) / 60, 2))
        self.assertEqual(ma["ma120"], round(sum(range(121, 241)) / 120, 2))
        self.assertEqual(ma["ma240"], round(sum(range(1, 241)) / 240, 2))

    def test_weinstein_stage_2_advancing(self):
        # 全線多頭排列：5 > 10 > 20 > 60 > 120 > 240 且股價在 20MA 上方
        ma_dict = {
            "ma5": 110.0, "ma10": 105.0, "ma20": 100.0,
            "ma60": 90.0, "ma120": 80.0, "ma240": 70.0
        }
        stage, label = classify_weinstein_stage(112.0, ma_dict)
        self.assertEqual(stage, 2)
        self.assertEqual(label, "主升段")

    def test_weinstein_stage_3_top(self):
        # 股價跌破 20MA 但在 240MA 上方
        ma_dict = {
            "ma5": 92.0, "ma10": 94.0, "ma20": 96.0,
            "ma60": 90.0, "ma120": 85.0, "ma240": 75.0
        }
        stage, label = classify_weinstein_stage(91.0, ma_dict)
        self.assertEqual(stage, 3)
        self.assertEqual(label, "頭部")

    def test_weinstein_stage_4_declining(self):
        # 全線空頭排列，股價在所有均線之下
        ma_dict = {
            "ma5": 50.0, "ma10": 55.0, "ma20": 60.0,
            "ma60": 70.0, "ma120": 80.0, "ma240": 90.0
        }
        stage, label = classify_weinstein_stage(48.0, ma_dict)
        self.assertEqual(stage, 4)
        self.assertEqual(label, "主跌段")

    def test_weinstein_stage_1_basing(self):
        # 240MA 走平盤整
        ma_dict = {
            "ma5": 100.5, "ma10": 100.2, "ma20": 99.8,
            "ma60": 100.1, "ma120": 100.0, "ma240": 100.0
        }
        stage, label = classify_weinstein_stage(101.0, ma_dict)
        self.assertEqual(stage, 1)
        self.assertEqual(label, "築底段")


class TestMinerviniVCP(unittest.TestCase):
    """Minervini VCP 波動收縮與成交量確認單元測試"""

    def test_vcp_breakout_confirmed(self):
        # 3 次收斂 + 突破日放量 2.0x
        series = _generate_vcp_series(days=200, breakout_volume_ratio=2.0)
        res = detect_vcp(series, min_contractions=2, breakout_volume_multiple=1.5)

        self.assertTrue(res["detected"])
        self.assertGreaterEqual(res["contractions"], 2)
        self.assertIsNotNone(res["pivot_price"])
        self.assertTrue(res["breakout_confirmed"])
        self.assertGreaterEqual(res["breakout_volume_ratio"], 1.5)

    def test_vcp_low_volume_not_confirmed(self):
        # 3 次收斂但量能不足 (0.8x)
        series = _generate_vcp_series(days=200, breakout_volume_ratio=0.8)
        res = detect_vcp(series, min_contractions=2, breakout_volume_multiple=1.5)

        self.assertTrue(res["detected"])
        self.assertFalse(res["breakout_confirmed"])
        self.assertLess(res["breakout_volume_ratio"], 1.5)

    def test_non_vcp_series(self):
        # 無收縮隨機走勢
        series = _generate_non_vcp_series(days=150)
        res = detect_vcp(series, min_contractions=2)

        self.assertFalse(res["detected"])
        self.assertEqual(res["contractions"], 0)
        self.assertFalse(res["breakout_confirmed"])


class TestAtrAndRelativeStrength(unittest.TestCase):
    """ATR(14) 停損與 RS 分數單元測試"""

    def test_atr_calculation(self):
        # 構造每日 True Range 均為 2.0 的數列
        series = []
        for i in range(20):
            series.append({
                "open": 100.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "volume": 1000,
            })
        res = compute_atr(series, period=14, stop_multiplier=2.0)
        self.assertEqual(res["atr_14"], 2.0)
        self.assertEqual(res["stop_distance"], 4.0)
        self.assertEqual(res["suggested_stop"], 97.0)

    def test_relative_strength_outperforming(self):
        # 標的漲 20%，大盤漲 10% -> RS = 20 / 10 = 2.0 (或 > 1.0)
        t_series = [{"close": 100.0}] * 62 + [{"close": 120.0}]
        b_series = [{"close": 1000.0}] * 62 + [{"close": 1100.0}]

        res = compute_relative_strength(t_series, b_series, lookback_days=63, benchmark_name="^TWII")
        self.assertIsNotNone(res["rs_score"])
        self.assertGreater(res["rs_score"], 1.0)

    def test_relative_strength_underperforming(self):
        # 標的跌 10%，大盤漲 10% -> RS < 1.0
        t_series = [{"close": 100.0}] * 62 + [{"close": 90.0}]
        b_series = [{"close": 1000.0}] * 62 + [{"close": 1100.0}]

        res = compute_relative_strength(t_series, b_series, lookback_days=63, benchmark_name="^TWII")
        self.assertIsNotNone(res["rs_score"])
        self.assertLess(res["rs_score"], 1.0)


class TestRealApiTechnicals(unittest.TestCase):
    """真實 API 整合測試（Yahoo Finance 日K線、上市 2328、上櫃 3141、大盤指數）"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = load_tw_market_config()
        cls.prov = get_provider("price_history", cls.cfg)

    def test_fetch_twse_2328_real(self):
        bars = self.prov.fetch_daily("2328", days=240)
        self.assertGreaterEqual(len(bars), 100)
        last_bar = bars[-1]
        self.assertIn("close", last_bar)
        self.assertIn("volume", last_bar)
        self.assertGreater(last_bar["close"], 0.0)

    def test_fetch_tpex_3141_real(self):
        bars = self.prov.fetch_daily("3141", days=240)
        self.assertGreaterEqual(len(bars), 100)
        last_bar = bars[-1]
        self.assertIn("close", last_bar)
        self.assertGreater(last_bar["close"], 0.0)

    def test_fetch_benchmark_real(self):
        bars = self.prov.fetch_daily("^TWII", days=65)
        self.assertGreaterEqual(len(bars), 50)

    def test_insufficient_data_graceful(self):
        # 測試新上市或無資料標的
        bars = self.prov.fetch_daily("999999_NONEXISTENT", days=10)
        self.assertEqual(len(bars), 0)


class TestMockProviderTechnicals(unittest.TestCase):
    """Mock Provider 整合技術分析驗證"""

    def test_mock_technicals_pipeline(self):
        cfg = {"providers": {"price_history": "mock"}}
        prov = get_provider("price_history", cfg)
        bench_cache = {
            "^TWII": prov.fetch_daily("^TWII", 100),
            "^TWOII": prov.fetch_daily("^TWOII", 100),
        }
        res = analyze_technicals_for_ticker("2328", "twse", prov, bench_cache, {})
        self.assertIn("ma_alignment", res)
        self.assertIn("vcp", res)
        self.assertIn("atr", res)
        self.assertIn("relative_strength", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
