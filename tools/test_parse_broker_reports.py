#!/usr/bin/env python3
"""
test_parse_broker_reports.py — 本地券商研報抽取與估值整合單元測試。

使用獨立 Mock 測試資料目錄與 fixture，確保測試不依賴龐大的實際研報檔案，
在任何乾淨 CI / 本地環境下皆可穩定執行與通過。
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tools.parse_broker_reports import (
    extract_broker,
    extract_eps_forecasts,
    extract_rating,
    extract_report_date,
    extract_target_price,
    find_reports_for_ticker,
    get_tw_broker_consensus,
    parse_report_file,
    summarize_broker_consensus,
)
from tools.tw_stock_analysis import (
    compute_taiwan_three_anchors,
    fetch_tw_valuation_inputs,
    format_taiwan_stock_report,
)


class TestParseBrokerReports(unittest.TestCase):
    def setUp(self):
        # 建立臨時測試目錄與 Mock 研報檔案
        self.test_dir = Path(tempfile.mkdtemp())
        self.reports_dir = self.test_dir / "analyst_reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        # Mock 1: 大摩 Morgan Stanley
        (self.reports_dir / "ms_3665.md").write_text(
            """# Morgan Stanley Asia Limited
**Bizlink (3665.TW)**
Date: September 6, 2026
Stock Rating Overweight
Price target NT\\$3,665.00
Fiscal Year Ending 12/25 12/26e 12/27e
EPS (NT\\$) 46.57 69.77 120.77
""",
            encoding="utf-8",
        )

        # Mock 2: 元大
        (self.reports_dir / "貿聯-KY(3665)_元大20260824.md").write_text(
            """# 貿聯-KY (3665 TT)
元大投顧 2026 年 8 月 24 日
評等：買進
目標價 (12 個月)：NT\\$2850.0
2026年EPS 68.4，2027年EPS 113.33
""",
            encoding="utf-8",
        )

        # Mock 3: 富邦
        (self.reports_dir / "富邦-貿聯-KY_(3665_TT_NT2_275_00).md").write_text(
            """# Fubon Research 貿聯-KY (3665 TT)
2026 年 8 月 24 日
維持買進，目標價 3,100 元
2026 年 EPS (元) 69.15
2027 年 EPS (元) 114.51
""",
            encoding="utf-8",
        )

        # Mock 4: 國泰
        (self.reports_dir / "國泰證期研究部貿聯-KY(3665_TT)-買進(-27_5-)-20260824.md").write_text(
            """# 國泰證期研究部 貿聯-KY (3665 TT)
報告日期 2026/08/24
評等 買進
目標價 2,900 元
預估 26 年 65.33；27 年 104.69 元
""",
            encoding="utf-8",
        )

        # Mock 5: 宏觀日報 (應被自動過濾)
        (self.reports_dir / "20260824_3665貿聯_國泰_TWDaily.md").write_text(
            """# 總經日報 2026/08/24
大盤下跌，提及 3665 貿聯
目標價 5000 元
""",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_extract_broker(self):
        self.assertEqual(extract_broker("ms_3665.md", "Morgan Stanley"), "Morgan Stanley")
        self.assertEqual(extract_broker("富邦-3665.md", ""), "富邦")
        self.assertEqual(extract_broker("2330-高盛.md", ""), "Goldman Sachs")
        self.assertEqual(extract_broker("unknown.md", "Cathay Securities"), "國泰")
        self.assertEqual(extract_broker("unknown.md", "random content"), "Other Broker")

    def test_extract_report_date(self):
        self.assertEqual(extract_report_date("貿聯_20260824.md", ""), "2026-08-24")
        self.assertEqual(extract_report_date("ms_3665.md", "September 6, 2026"), "2026-09-06")
        self.assertEqual(extract_report_date("fubon.md", "報告日期: 2026/08/18"), "2026-08-18")
        self.assertEqual(extract_report_date("cathay.md", "2026 年 7 月 15 日"), "2026-07-15")

    def test_extract_rating(self):
        self.assertEqual(extract_rating("Stock Rating Overweight"), "Buy")
        self.assertEqual(extract_rating("投資評等：買進"), "Buy")
        self.assertEqual(extract_rating("建議 Trading Buy"), "Trading Buy")
        self.assertEqual(extract_rating("維持 Neutral 中立評等"), "Neutral")
        self.assertEqual(extract_rating("評等: Underweight 減碼"), "Sell")

    def test_extract_target_price(self):
        self.assertEqual(extract_target_price("目標價 3,100 元"), 3100.0)
        self.assertEqual(extract_target_price("Price target NT$3,665.00"), 3665.0)
        self.assertEqual(extract_target_price("12-month target TWD4,200.00"), 4200.0)
        self.assertEqual(extract_target_price("目標價 (NT$) 2850"), 2850.0)

    def test_extract_eps_forecasts(self):
        text = "預估 26 年 65.33；27 年 104.69 元"
        eps = extract_eps_forecasts(text)
        self.assertEqual(eps.get("2026"), 65.33)
        self.assertEqual(eps.get("2027"), 104.69)

    def test_find_reports_and_summarize(self):
        consensus = get_tw_broker_consensus("3665", reports_dir=self.reports_dir, days=90, reference_date=date(2026, 9, 10))
        self.assertEqual(consensus.get("status"), "ok")
        self.assertEqual(consensus.get("coverage_count"), 4)  # 4 家券商 (日報被排除)
        self.assertIn("Morgan Stanley", consensus.get("brokers"))
        self.assertIn("元大", consensus.get("brokers"))
        self.assertIn("富邦", consensus.get("brokers"))
        self.assertIn("國泰", consensus.get("brokers"))

        # 目標價中位數: [2850.0, 2900.0, 3100.0, 3665.0] -> (2900 + 3100) / 2 = 3000.0
        self.assertEqual(consensus.get("median_target_price"), 3000.0)
        self.assertEqual(consensus.get("min_target_price"), 2850.0)
        self.assertEqual(consensus.get("max_target_price"), 3665.0)

        # 2026 EPS: [65.33, 68.4, 69.15, 69.77] -> (68.4 + 69.15)/2 = 68.78
        self.assertIsNotNone(consensus.get("eps_2026_consensus"))
        # 2027 EPS: [104.69, 113.33, 114.51, 120.77] -> (113.33 + 114.51)/2 = 113.92
        self.assertIsNotNone(consensus.get("eps_2027_consensus"))
        self.assertEqual(consensus.get("target_price_base_eps"), 113.92)  # A3 目標價分母對齊次年 2027 EPS
        # 成長率: 2027 vs 2026
        self.assertGreater(consensus.get("eps_growth_pct"), 50.0)

    def test_uncovered_ticker_fallback(self):
        consensus = get_tw_broker_consensus("9999", reports_dir=self.reports_dir, days=90)
        self.assertEqual(consensus.get("status"), "(unavailable: 本地研報庫無覆蓋)")
        self.assertEqual(consensus.get("coverage_count"), 0)
        self.assertIsNone(consensus.get("median_target_price"))

    def test_three_anchors_computation_with_broker_consensus(self):
        val_inputs = {
            "current_price": 2000.0,
            "trailing_pe": 35.0,
            "forward_eps_growth": 65.0,
            "target_price_analyst": 3000.0,
            "forward_eps_consensus": 68.78,
            "target_price_base_eps": 113.92,
            "eps_ttm": 57.0,
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 4,
                "brokers": ["Morgan Stanley", "元大", "富邦", "國泰"],
                "median_target_price": 3000.0,
                "min_target_price": 2850.0,
                "max_target_price": 3665.0,
            },
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertTrue(res["is_confident"])
        self.assertEqual(res["num_available_anchors"], 3)
        # A3 PE: 3000 / 113.92 = 26.33 (落在 25~30x 合理區間，而非 43.6x)
        self.assertAlmostEqual(res["a3_pe"], 26.33, places=1)
        self.assertGreaterEqual(res["a3_pe"], 25.0)
        self.assertLessEqual(res["a3_pe"], 30.0)
        self.assertIsNotNone(res["base_fair_pe"])
        self.assertIsNotNone(res["fair_price_base"])
        self.assertGreater(res["fair_price_bull"], res["fair_price_base"])
        self.assertLess(res["fair_price_bear"], res["fair_price_base"])

    def test_report_formatting_with_broker_consensus(self):
        val_inputs = {
            "current_price": 2000.0,
            "trailing_pe": 35.0,
            "forward_eps_growth": 65.0,
            "target_price_analyst": 3000.0,
            "forward_eps_consensus": 68.0,
            "eps_ttm": 57.0,
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 4,
                "brokers": ["大摩", "元大", "富邦", "國泰"],
                "median_target_price": 3000.0,
                "min_target_price": 2850.0,
                "max_target_price": 3665.0,
            },
        }
        val_res = compute_taiwan_three_anchors(val_inputs)
        data = {
            "code": "3665",
            "name": "貿聯-KY",
            "market": "twse",
            "full_symbol": "3665.TW",
            "valuation": val_res,
            "ev": {"status": "ok", "ev_price": 3100.0, "ev_return_pct": 55.0, "p_bull": 0.25, "p_base": 0.5, "p_bear": 0.25},
            "monthly_revenue": {"status": "ok", "data_month": "11508", "revenue_curr_month_k": 8891805, "yoy_pct": 54.0, "mom_pct": -7.6},
            "chips": {},
            "technicals": {},
            "catalyst": {},
        }
        md = format_taiwan_stock_report(data)
        self.assertIn("本地法人研報庫 (共 4 家券商覆蓋: 大摩, 元大, 富邦, 國泰)", md)
        self.assertIn("- **目標價區間**：中位數 NT$ 3000.0 (最低 NT$ 2850.0 ~ 最高 NT$ 3665.0)", md)
        self.assertIn("Base Fair PE", md)


if __name__ == "__main__":
    unittest.main()
