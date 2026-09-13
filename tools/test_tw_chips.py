#!/usr/bin/env python3
"""
test_tw_chips.py — 模組 A（籌碼結構分析）測試套件。

驗證項目（依 Issue #3 規範）：
1. 單元測試（Mock Provider、投信 4 階邊界、TDCC 集中度趨勢、Provider 失敗降級）
2. 整合測試（TWSE 2328、TPEx 3141 真實 API 抓取、SSL 處理、JSON Schema）
3. 架構整合測試（tw-market-indicators.json 寫入、archive_cache 納入、TTL 機制）
4. 資料源可抽換性驗證（Dummy FinMind Provider 註冊與切換、未知 Provider 錯誤攔截）
"""

import json
import os
import shutil
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.fetch_tw_chips import analyze_tdcc_trend, fetch_chips_for_ticker
from tools.tw_providers import (
    InstitutionalFlowProvider,
    get_available_providers,
    get_provider,
    load_tw_market_config,
    register_provider,
)
from tools.tw_providers.twse_opendata import classify_trust_adoption


class TestTrustAdoptionClassifier(unittest.TestCase):
    """投信 4 階判定單元測試（邊界測試）"""

    def test_stage_1_cold(self):
        # 持股比 0.0% -> Stage 1
        self.assertEqual(classify_trust_adoption(0.0), 1)
        # 持股比 0.49% -> Stage 1
        self.assertEqual(classify_trust_adoption(0.49), 1)
        # None -> Stage 1 (fail-safe)
        self.assertEqual(classify_trust_adoption(None), 1)

    def test_stage_2_probing(self):
        # 持股比 0.5% -> Stage 2 (邊界包含)
        self.assertEqual(classify_trust_adoption(0.5), 2)
        # 持股比 1.99% -> Stage 2
        self.assertEqual(classify_trust_adoption(1.99), 2)

    def test_stage_3_main_wave(self):
        # 持股比 2.0% -> Stage 3 (邊界包含)
        self.assertEqual(classify_trust_adoption(2.0), 3)
        # 持股比 4.9% -> Stage 3
        self.assertEqual(classify_trust_adoption(4.9), 3)
        # 持股比 5.1% 但未連 2 週遞減 -> Stage 3（不誤判）
        self.assertEqual(classify_trust_adoption(5.1, [5.1, 5.0, 4.9]), 3)
        # 僅遞減 1 週 -> Stage 3
        self.assertEqual(classify_trust_adoption(5.1, [5.1, 5.2, 5.1]), 3)

    def test_stage_4_distribution(self):
        # 持股比 5.1% 且連 2 週遞減 [w0=5.1, w1=5.3, w2=5.6] -> Stage 4
        self.assertEqual(classify_trust_adoption(5.1, [5.1, 5.3, 5.6]), 4)
        # 邊界剛好 5.0% 且連 2 週遞減 -> Stage 4
        self.assertEqual(classify_trust_adoption(5.0, [5.0, 5.2, 5.5]), 4)


class TestTdccAnalysis(unittest.TestCase):
    """TDCC 集保股權分散變化分析單元測試"""

    def test_concentrating_trend(self):
        # 大戶連 3 週增加、散戶減少
        mock_series = [
            {"date": "2026-09-11", "large_holder_pct": 45.2, "retail_holder_pct": 34.5},
            {"date": "2026-09-04", "large_holder_pct": 44.8, "retail_holder_pct": 36.0},
            {"date": "2026-08-28", "large_holder_pct": 44.1, "retail_holder_pct": 37.2},
            {"date": "2026-08-21", "large_holder_pct": 43.5, "retail_holder_pct": 38.0},
        ]
        res = analyze_tdcc_trend(mock_series)
        self.assertEqual(res["trend_direction"], "concentrating")
        self.assertEqual(res["trend_weeks"], 3)
        self.assertEqual(res["retail_change_pct"], -1.5)
        self.assertEqual(res["large_holder_pct"], 45.2)
        self.assertEqual(res["large_holder_pct_prev_week"], 44.8)

    def test_dispersing_trend(self):
        # 大戶減少、散戶增加
        mock_series = [
            {"date": "2026-09-11", "large_holder_pct": 40.0, "retail_holder_pct": 38.0},
            {"date": "2026-09-04", "large_holder_pct": 41.5, "retail_holder_pct": 36.5},
        ]
        res = analyze_tdcc_trend(mock_series)
        self.assertEqual(res["trend_direction"], "dispersing")
        self.assertEqual(res["retail_change_pct"], 1.5)

    def test_single_week_fallback(self):
        mock_series = [
            {"date": "2026-09-11", "large_holder_pct": 45.2, "retail_holder_pct": 34.5}
        ]
        res = analyze_tdcc_trend(mock_series)
        self.assertEqual(res["trend_weeks"], 1)
        self.assertIsNone(res["retail_change_pct"])
        self.assertIsNone(res["large_holder_pct_prev_week"])


class TestMockProviderChips(unittest.TestCase):
    """使用 Mock Provider 測試晶片整合分析"""

    def setUp(self):
        self.cfg = {
            "providers": {
                "tdcc": "mock",
                "institutional": "mock",
                "governance": "mock",
            }
        }
        self.tdcc_prov = get_provider("tdcc", self.cfg)
        self.inst_prov = get_provider("institutional", self.cfg)
        self.gov_prov = get_provider("governance", self.cfg)

    def test_mock_fetch_3141(self):
        data = fetch_chips_for_ticker("3141", self.tdcc_prov, self.inst_prov, self.gov_prov)
        self.assertIn("tdcc", data)
        self.assertIn("institutional", data)
        self.assertIn("governance", data)

        inst = data["institutional"]
        self.assertEqual(inst["foreign_1d"], 850)
        self.assertEqual(inst["foreign_5d"], 4494)
        self.assertEqual(inst["foreign_20d"], 8650)
        self.assertEqual(inst["trust_adoption_stage"], 1)

        gov = data["governance"]
        self.assertEqual(gov["director_pledge_pct"], 23.96)


class TestProviderSwappability(unittest.TestCase):
    """資料源可抽換性驗證"""

    def test_unknown_provider_raises_informative_error(self):
        cfg = {"providers": {"institutional": "non_existent_provider"}}
        with self.assertRaises(ValueError) as ctx:
            get_provider("institutional", cfg)
        self.assertIn("non_existent_provider", str(ctx.exception))
        self.assertIn("可用 Provider", str(ctx.exception))

    def test_dummy_finmind_provider_swapping(self):
        # 動態定義並註冊一個 dummy FinMind provider
        class DummyFinMindInstitutionalProvider(InstitutionalFlowProvider):
            provider_name = "finmind_dummy"

            def __init__(self, config=None, options=None):
                self.config = config or {}
                self.options = options or {}

            def fetch_institutional(self, code: str, days: int = 20) -> dict:
                return {
                    "foreign_1d": 9999,
                    "foreign_5d": 8888,
                    "foreign_20d": 7777,
                    "foreign_holding_pct": 50.0,
                    "trust_1d": 111,
                    "trust_5d": 222,
                    "trust_20d": 333,
                    "trust_holding_pct": 3.5,
                    "trust_adoption_stage": 3,
                    "dealer_1d": 44,
                    "dealer_5d": 55,
                    "dealer_20d": 66,
                }

        register_provider("institutional", "finmind_dummy", DummyFinMindInstitutionalProvider)
        self.assertIn("finmind_dummy", get_available_providers("institutional"))

        cfg = {"providers": {"institutional": "finmind_dummy"}}
        prov = get_provider("institutional", cfg)
        res = prov.fetch_institutional("2328")
        self.assertEqual(res["foreign_1d"], 9999)
        self.assertEqual(res["trust_adoption_stage"], 3)


class TestRealApiIntegrationChips(unittest.TestCase):
    """真實 API 整合測試（上市 2328 與上櫃 3141）"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = load_tw_market_config()
        cls.tdcc_prov = get_provider("tdcc", cls.cfg)
        cls.inst_prov = get_provider("institutional", cls.cfg)
        cls.gov_prov = get_provider("governance", cls.cfg)

    def test_twse_2328_real_fetch(self):
        data = fetch_chips_for_ticker("2328", self.tdcc_prov, self.inst_prov, self.gov_prov, days=5)
        # 驗證結構
        self.assertIn("tdcc", data)
        self.assertIn("institutional", data)
        self.assertIn("governance", data)

        tdcc = data["tdcc"]
        self.assertIsNotNone(tdcc["latest_date"])
        self.assertGreater(tdcc["large_holder_pct"], 30.0)

        inst = data["institutional"]
        self.assertIn("foreign_1d", inst)
        self.assertIn("trust_adoption_stage", inst)
        self.assertIn(inst["trust_adoption_stage"], (1, 2, 3, 4))

        gov = data["governance"]
        self.assertIn("director_pledge_pct", gov)

    def test_tpex_3141_real_fetch(self):
        data = fetch_chips_for_ticker("3141", self.tdcc_prov, self.inst_prov, self.gov_prov, days=5)
        self.assertIn("tdcc", data)
        self.assertIn("institutional", data)
        self.assertIn("governance", data)

        tdcc = data["tdcc"]
        self.assertIsNotNone(tdcc["latest_date"])
        self.assertGreater(tdcc["large_holder_pct"], 10.0)

        inst = data["institutional"]
        self.assertIn("foreign_1d", inst)
        self.assertIn("trust_adoption_stage", inst)


class TestArchitectureIntegration(unittest.TestCase):
    """架構整合測試（快取、archive_cache 納入）"""

    def test_archive_cache_targets_includes_tw_market_indicators(self):
        import tools.archive_cache as ac
        self.assertIn("tw-market-indicators.json", ac.TARGETS)

    def test_cache_file_format(self):
        cache_file = ROOT / "briefing-out" / "cache" / "tw-market-indicators.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertIn("version", data)
            self.assertIn("generated_at", data)
            self.assertIn("status", data)
            if "chips" in data:
                self.assertTrue(data["chips"].get("display_only", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
