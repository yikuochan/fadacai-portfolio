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
    ROOT,
    extract_broker,
    extract_eps_forecasts,
    extract_rating,
    extract_report_date,
    extract_target_price,
    extract_title,
    find_reports_for_ticker,
    format_rel_report_path,
    get_tw_broker_consensus,
    parse_report_file,
    summarize_broker_consensus,
)
from tools.tw_stock_analysis import (
    compute_first_principles_ev,
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

    def test_extract_title(self):
        self.assertEqual(extract_title("# Morgan Stanley Research\nContent", "fallback"), "Morgan Stanley Research")
        self.assertEqual(extract_title("No heading content", "fallback"), "fallback")

    def test_format_rel_report_path(self):
        # 1. 專案根目錄下的絕對路徑 -> 相對路徑
        abs_p = ROOT / "research" / "analyst_reports" / "202609" / "MS-3665_20260902.md"
        self.assertEqual(format_rel_report_path(abs_p), "research/analyst_reports/202609/MS-3665_20260902.md")
        # 2. 原本就是相對路徑
        rel_p = "research/analyst_reports/202609/MS-3665_20260902.md"
        self.assertEqual(format_rel_report_path(rel_p), "research/analyst_reports/202609/MS-3665_20260902.md")
        # 3. 測試目錄 fallback
        temp_p = "/tmp/test_dir/analyst_reports/ms_3665.md"
        self.assertEqual(format_rel_report_path(temp_p), "analyst_reports/ms_3665.md")
        # 4. 空值
        self.assertEqual(format_rel_report_path(None), "")
        self.assertEqual(format_rel_report_path(""), "")

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

        # 驗證 broker_details 包含 file_path, filename 與 title
        details = consensus.get("broker_details", [])
        self.assertEqual(len(details), 4)
        for b in details:
            self.assertIn("file_path", b)
            self.assertIsNotNone(b["file_path"])
            self.assertTrue(b["file_path"].endswith(".md"))
            self.assertIn("filename", b)
            self.assertIn("title", b)
            self.assertTrue(len(b["title"]) > 0)

    def test_uncovered_ticker_fallback(self):
        consensus = get_tw_broker_consensus("9999", reports_dir=self.reports_dir, days=90)
        self.assertEqual(consensus.get("status"), "(unavailable: 本地研報庫無覆蓋)")
        self.assertEqual(consensus.get("coverage_count"), 0)
        self.assertIsNone(consensus.get("median_target_price"))

    def test_three_anchors_computation_with_broker_consensus(self):
        val_inputs = {
            "current_price": 2000.0,
            "trailing_pe": 25.0,
            "forward_eps_growth": 30.0,
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
                "broker_details": [
                    {
                        "broker": "Morgan Stanley",
                        "title": "Morgan Stanley Asia Limited",
                        "report_date": "2026-09-06",
                        "rating": "Buy",
                        "target_price": 3665.0,
                        "eps_forecasts": {"2026": 69.77, "2027": 120.77},
                        "file_path": str(ROOT / "research" / "analyst_reports" / "202609" / "ms_3665.md"),
                    },
                    {
                        "broker": "元大",
                        "title": "貿聯-KY (3665 TT)",
                        "report_date": "2026-08-24",
                        "rating": "Buy",
                        "target_price": 2850.0,
                        "eps_forecasts": {"2026": 68.4, "2027": 113.33},
                        "file_path": "research/analyst_reports/202608/貿聯-KY(3665)_元大20260824.md",
                    },
                ],
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

        # 驗證新增的質化檔案索引區塊
        self.assertIn("### 📑 本地研報引用與質化檔案索引", md)
        self.assertIn("若需深入質化研究或比對各家論點分歧，可直接讀取上列檔案路徑取得完整研報內文。", md)
        self.assertIn("| 券商 | 報告標題 | 報告日期 | 評等 | 目標價 | 預估 EPS | 原始檔案路徑 |", md)
        self.assertIn("`research/analyst_reports/202609/ms_3665.md`", md)
        self.assertIn("`research/analyst_reports/202608/貿聯-KY(3665)_元大20260824.md`", md)

    def test_report_formatting_without_broker_consensus(self):
        val_inputs = {
            "current_price": 2000.0,
            "trailing_pe": 35.0,
            "forward_eps_growth": None,
            "target_price_analyst": None,
            "forward_eps_consensus": None,
            "eps_ttm": 57.0,
            "broker_consensus": {
                "status": "(unavailable: 本地研報庫無覆蓋)",
                "coverage_count": 0,
                "brokers": [],
                "broker_details": [],
            },
        }
        val_res = compute_taiwan_three_anchors(val_inputs)
        data = {
            "code": "9999",
            "name": "未知個股",
            "market": "twse",
            "full_symbol": "9999.TW",
            "valuation": val_res,
            "ev": {"status": "error"},
            "monthly_revenue": {"status": "ok"},
            "chips": {},
            "technicals": {},
            "catalyst": {},
        }
        md = format_taiwan_stock_report(data)
        # 無本地研報覆蓋時優雅降級，不印該節
        self.assertNotIn("質化檔案索引", md)


    def test_bull_cap_with_broker_consensus_over_limit(self):
        # 模擬 3665：A2=69x，未封頂前 Bull PE=86.25x -> 公允價 (114.18*86.25=9848 或 67.56*86.25=5827.05)，但券商最高目標價為 3665.0
        val_inputs = {
            "current_price": 1980.0,
            "trailing_pe": 36.36,
            "forward_eps_growth": 69.0,
            "target_price_analyst": 3100.0,
            "forward_eps_consensus": 67.56,
            "target_price_base_eps": 114.18,
            "eps_ttm": 54.45,
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 10,
                "median_target_price": 3100.0,
                "min_target_price": 2850.0,
                "max_target_price": 3665.0,
            },
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertTrue(res["is_confident"])
        self.assertTrue(res["bull_capped"])
        self.assertEqual(res["fair_price_bull"], 3665.0)
        self.assertIn("NT$3,665 封頂", res["bull_cap_reason"])
        # Bull PE 被反推鎖定
        self.assertAlmostEqual(res["bull_pe"], 3665.0 / 114.18, places=2)

        # 檢驗報告文字與 EV
        ev_res = compute_first_principles_ev(res)
        self.assertEqual(ev_res["status"], "ok")
        self.assertLessEqual(ev_res["ev_price"], 3665.0)

        data = {
            "code": "3665",
            "name": "貿聯-KY",
            "market": "twse",
            "full_symbol": "3665.TW",
            "valuation": res,
            "ev": ev_res,
            "monthly_revenue": {"status": "ok"},
            "chips": {},
            "technicals": {},
            "catalyst": {},
        }
        md = format_taiwan_stock_report(data)
        self.assertIn("（Bull 已依券商最高目標價 NT$3,665 封頂）", md)
        self.assertIn("$3665.0 元", md)

    def test_bull_cap_without_broker_consensus_over_limit(self):
        # 無券商覆蓋，A1=20.0，A2=50.0 -> base=(20+50)/2=35.0, raw bull_pe=50*1.25=62.5
        # 依規則 Bull PE 上限為 A1 PE * 1.50 = 30.0x
        val_inputs = {
            "current_price": 100.0,
            "trailing_pe": 20.0,
            "forward_eps_growth": 50.0,
            "target_price_analyst": None,
            "forward_eps_consensus": 5.0,
            "eps_ttm": 5.0,
            "broker_consensus": None,
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertTrue(res["is_confident"])
        self.assertTrue(res["bull_capped"])
        self.assertEqual(res["bull_pe"], 30.0)  # 20.0 * 1.5
        self.assertEqual(res["fair_price_bull"], 150.0)  # 5.0 * 30.0
        self.assertIn("A1 市場 PE 1.5 倍", res["bull_cap_reason"])

    def test_bull_not_capped_when_within_limit(self):
        # 有券商覆蓋，max_target_price=200.0，A1=20, A2=20, A3=110/5=22 -> max=22 -> bull_pe=22*1.25=27.5 -> raw bull price=137.5 (< 200.0) -> 不觸發封頂
        val_inputs = {
            "current_price": 100.0,
            "trailing_pe": 20.0,
            "forward_eps_growth": 20.0,
            "target_price_analyst": 110.0,
            "forward_eps_consensus": 5.0,
            "target_price_base_eps": 5.0,
            "eps_ttm": 5.0,
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 2,
                "median_target_price": 110.0,
                "min_target_price": 100.0,
                "max_target_price": 200.0,
            },
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertTrue(res["is_confident"])
        self.assertFalse(res["bull_capped"])
        self.assertIsNone(res["bull_cap_reason"])
        self.assertEqual(res["bull_pe"], 27.5)  # max(20, 20, 22) * 1.25
        self.assertEqual(res["fair_price_bull"], 137.5)  # 5.0 * 27.5

    def test_single_source_eps_outlier_filtering(self):
        # (a) 單券商多年期 EPS 離群剔除測試
        from tools.parse_broker_reports import filter_eps_series_single_source
        # 模擬 2481 強茂合庫研報：2025: 3.13, 2026: 11.81 (OCR 錯抓), 2027: 4.80
        raw_eps = {"2025": 3.13, "2026": 11.81, "2027": 4.80}
        filtered = filter_eps_series_single_source(raw_eps, max_ratio=2.5)
        self.assertNotIn("2026", filtered)
        self.assertEqual(filtered["2025"], 3.13)
        self.assertEqual(filtered["2027"], 4.80)

        # 正常成長股不應被剔除
        normal_eps = {"2025": 66.25, "2026": 100.21, "2027": 130.08}
        self.assertEqual(filter_eps_series_single_source(normal_eps), normal_eps)

    def test_cross_broker_eps_outlier_filtering(self):
        # (b) 跨券商同年度 EPS 離群剔除測試
        from tools.parse_broker_reports import filter_cross_broker_eps_outliers
        # 4 家券商預估 2026 EPS: 5.07, 5.18, 4.80，另外某家誤抓 11.81
        eps_list = [5.07, 5.18, 4.80, 11.81]
        cleaned = filter_cross_broker_eps_outliers(eps_list, max_dev_ratio=2.0)
        self.assertNotIn(11.81, cleaned)
        self.assertEqual(len(cleaned), 3)

    def test_base_bear_ladder_clamping(self):
        # (c) Base/Bear 對券商目標價區間的約束測試
        val_inputs = {
            "current_price": 150.0,
            "trailing_pe": 40.0,
            "forward_eps_growth": 50.0,
            "target_price_analyst": 200.0,
            "forward_eps_consensus": 5.0,
            "target_price_base_eps": 7.5,
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 4,
                "median_target_price": 200.0,
                "min_target_price": 190.0,
                "max_target_price": 240.0,
            },
        }
        # Base fair pe = median(40, 50, 200/7.5=26.67) = 40.0
        # raw base price = 7.5 * 40.0 = 300.0 (> max_tp 240.0) -> clamp to 240.0
        # raw bear price = 7.5 * (26.67 * 0.70) = 140.0 -> Bear floor = 190 * 0.70 = 133.0 (< 140 -> 不保底)
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertTrue(res["base_capped"])
        self.assertEqual(res["fair_price_base"], 240.0)
        self.assertIn("Base 已依券商最高目標價 NT$240 封頂", res["base_cap_reason"])

        # 測試 Bear 保底情境：若 raw bear price < 133.0
        val_inputs_bear = dict(val_inputs)
        val_inputs_bear["forward_eps_growth"] = 10.0  # A2 = 10.0 -> bear pe = 10 * 0.70 = 7.0 -> raw bear = 7.5 * 7 = 52.5
        res_bear = compute_taiwan_three_anchors(val_inputs_bear)
        self.assertTrue(res_bear["bear_floored"])
        self.assertEqual(res_bear["fair_price_bear"], 133.0)  # 190 * 0.70
        self.assertIn("Bear 已依券商最低目標價 70%", res_bear["bear_floor_reason"])

    def test_valuation_anomaly_warning(self):
        # (d) Base 遠超券商目標價區間時發出警告
        val_inputs = {
            "current_price": 150.0,
            "trailing_pe": 40.0,
            "forward_eps_growth": 50.0,
            "target_price_analyst": 200.0,
            "forward_eps_consensus": 5.0,
            "target_price_base_eps": 10.0,  # 導致 raw base = 10 * 40 = 400.0 (> 240 * 1.25 = 300)
            "broker_consensus": {
                "status": "ok",
                "coverage_count": 4,
                "median_target_price": 200.0,
                "min_target_price": 190.0,
                "max_target_price": 240.0,
            },
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertIsNotNone(res["valuation_anomaly_warning"])
        self.assertIn("估值輸入異常警告", res["valuation_anomaly_warning"])

        data = {
            "code": "2481",
            "name": "強茂",
            "market": "twse",
            "full_symbol": "2481.TW",
            "valuation": res,
            "ev": {"status": "ok", "ev_price": 220.0, "ev_return_pct": 46.7, "p_bull": 0.25, "p_base": 0.5, "p_bear": 0.25},
            "monthly_revenue": {"status": "ok"},
            "chips": {},
            "technicals": {},
            "catalyst": {},
        }
        md = format_taiwan_stock_report(data)
        self.assertIn("⚠️ 估值輸入異常警告", md)


if __name__ == "__main__":
    unittest.main()
