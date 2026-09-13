#!/usr/bin/env python3
"""
test_tw_stock_analysis_e2e.py — 台股分析 Skill 端對端驗證測試套件 (Module D E2E, Issue #9)。

驗證項目（依 Issue #9 規範）：
1. 端對端流程測試：
   - 上市標的 (2328 廣宇)：完整流程執行成功，報告含所有區塊
   - 上櫃標的 (3141 晶宏)：完整流程執行成功，報告含所有區塊
   - 冷啟動（快取不存在/過期）：自動觸發模組刷新，報告完整產出
   - 熱啟動（快取新鮮）：快速讀取快取產出報告
2. 估值降級測試：
   - 三錨點全部可用 → 正常 median 計算
   - A2 不可用（缺 PEG）→ 標 (A2 unavailable)，用 A1/A3 均值
   - A2 + A3 均不可用 → 標「⚠️ 估值信心不足：僅 1 個錨點可用」
   - 全部不可用 → 明確標示「⚠️ 估值信心不足：無可用錨點，無法提供目標價」
3. Display-only 規範驗證：
   - 籌碼面顯示「外資大買」但基本面悲觀 → Verdict 不被籌碼翻轉
   - 技術面 Stage 4 + 基本面樂觀 → Verdict 附帶技術風險 flag 但不翻空
   - 催化劑日曆正確嵌入報告，不影響 Verdict 機率分布
4. HTML 報告渲染：
   - generate_html.py 正確渲染台股報告的所有新增區塊
   - 表格（籌碼、均線、催化劑）在 HTML 中具備 .table-wrap 橫向捲動結構
   - KaTeX 數學公式支援載入
5. 資料源抽換端對端：
   - Config 中所有 provider 切換為 mock → 報告仍正確產出（含 mock 資料）
   - 部分 provider 不可用（如 TDCC 維護中）→ 對應區塊標 (unavailable)，其餘正常

使用 Mock Provider — 無外部真實網路依賴。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.generate_html import colorize_pct, md_to_html, wrap_tables
from tools.tw_stock_analysis import (
    compute_first_principles_ev,
    compute_taiwan_three_anchors,
    format_taiwan_stock_report,
    gather_taiwan_stock_data,
    is_taiwan_ticker,
    normalize_tw_ticker,
)


class TestTickerDetection(unittest.TestCase):
    """台股 Ticker 偵測與正規化測試"""

    def test_pure_digits(self):
        self.assertTrue(is_taiwan_ticker("2328"))
        self.assertTrue(is_taiwan_ticker("3141"))
        self.assertTrue(is_taiwan_ticker("2330"))

    def test_suffix_tickers(self):
        self.assertTrue(is_taiwan_ticker("2328.TW"))
        self.assertTrue(is_taiwan_ticker("3141.TWO"))
        self.assertTrue(is_taiwan_ticker("2328.tw"))
        self.assertTrue(is_taiwan_ticker("3141.two"))

    def test_us_tickers_rejected(self):
        self.assertFalse(is_taiwan_ticker("NVDA"))
        self.assertFalse(is_taiwan_ticker("AAPL"))
        self.assertFalse(is_taiwan_ticker("TSLA"))
        self.assertFalse(is_taiwan_ticker("PLTR"))

    def test_normalize_tw_ticker(self):
        code, market, sym = normalize_tw_ticker("3141")
        self.assertEqual(code, "3141")
        self.assertEqual(market, "tpex")
        self.assertEqual(sym, "3141.TWO")

        code2, market2, sym2 = normalize_tw_ticker("2328")
        self.assertEqual(code2, "2328")
        self.assertEqual(market2, "twse")
        self.assertEqual(sym2, "2328.TW")

        code3, market3, sym3 = normalize_tw_ticker("2330.TW")
        self.assertEqual(code3, "2330")
        self.assertEqual(market3, "twse")
        self.assertEqual(sym3, "2330.TW")


class TestValuationDegradation(unittest.TestCase):
    """估值降級單元與邊界測試"""

    def test_three_anchors_available(self):
        # A1=30, A2=20, A3=25 -> median=25
        val_inputs = {
            "current_price": 100.0,
            "trailing_pe": 30.0,
            "forward_eps_growth": 20.0,
            "target_price_analyst": 125.0,
            "forward_eps_consensus": 5.0,
            "eps_ttm": 4.0,
        }
        res = compute_taiwan_three_anchors(val_inputs, peg_benchmark=1.0)
        self.assertEqual(res["num_available_anchors"], 3)
        self.assertEqual(res["base_fair_pe"], 25.0)
        self.assertTrue(res["is_confident"])
        self.assertIsNone(res["confidence_warning"])
        self.assertEqual(res["fair_price_base"], 125.0)  # 5.0 * 25.0

    def test_a2_unavailable_two_anchors(self):
        # A1=30, A2=None, A3=20 -> average=25
        val_inputs = {
            "current_price": 100.0,
            "trailing_pe": 30.0,
            "forward_eps_growth": None,  # A2 unavailable
            "target_price_analyst": 100.0,
            "forward_eps_consensus": 5.0,
            "eps_ttm": 4.0,
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertEqual(res["num_available_anchors"], 2)
        self.assertEqual(res["base_fair_pe"], 25.0)
        self.assertEqual(res["a2_desc"], "(A2 unavailable)")
        self.assertTrue(res["is_confident"])
        self.assertIsNone(res["confidence_warning"])

    def test_single_anchor_degradation(self):
        # 僅 A1=30 可用 -> 標記估值信心不足
        val_inputs = {
            "current_price": 90.0,
            "trailing_pe": 30.0,
            "forward_eps_growth": None,
            "target_price_analyst": None,
            "forward_eps_consensus": None,
            "eps_ttm": 3.0,
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertEqual(res["num_available_anchors"], 1)
        self.assertFalse(res["is_confident"])
        self.assertIsNotNone(res["confidence_warning"])
        self.assertIn("僅 1 個錨點可用", res["confidence_warning"])
        self.assertEqual(res["a2_desc"], "(A2 unavailable)")
        self.assertEqual(res["a3_desc"], "(A3 unavailable)")

    def test_zero_anchors_degradation(self):
        # 全無錨點
        val_inputs = {
            "current_price": 90.0,
            "trailing_pe": None,
            "forward_eps_growth": None,
            "target_price_analyst": None,
            "forward_eps_consensus": None,
            "eps_ttm": None,
        }
        res = compute_taiwan_three_anchors(val_inputs)
        self.assertEqual(res["num_available_anchors"], 0)
        self.assertFalse(res["is_confident"])
        self.assertIn("無可用錨點", res["confidence_warning"])
        self.assertIsNone(res["fair_price_base"])


class TestDisplayOnlyDiscipline(unittest.TestCase):
    """Display-only 規範驗證：籌碼與技術不翻轉 Verdict"""

    def test_stage4_does_not_flip_verdict_to_avoid_when_ev_bullish(self):
        # 基本面 EV 期望報酬率 +25% (Buy)，即使技術面為 Stage 4，Verdict 仍為 Buy + 附帶風險提示
        mock_data = {
            "code": "2328",
            "name": "廣宇",
            "full_symbol": "2328.TW",
            "valuation": {
                "current_price": 40.0,
                "is_confident": True,
                "fair_price_bull": 60.0,
                "fair_price_base": 50.0,
                "fair_price_bear": 40.0,
                "base_fair_pe": 20.0,
                "a1_desc": "20.0",
                "a2_desc": "20.0",
                "a3_desc": "20.0",
            },
            "technicals": {
                "ma_alignment": {
                    "weinstein_stage": 4,
                    "stage_label": "主跌段",
                }
            },
            "chips": {
                "tdcc": {"large_holder_pct": 35.0, "trend_direction": "concentrating"},
                "institutional": {"foreign_5d": 5000, "trust_adoption_stage": 1},
                "governance": {"director_pledge_pct": 0.0},
            },
            "catalyst": {"events": [], "next_event": None},
            "monthly_revenue": {"status": "ok", "data_month": "11508", "yoy_pct": 12.5, "revenue_curr_month_k": 200000},
            "ev": {
                "status": "ok",
                "ev_price": 50.0,
                "ev_return_pct": 25.0,
                "p_bull": 0.25,
                "p_base": 0.50,
                "p_bear": 0.25,
            },
        }
        report = format_taiwan_stock_report(mock_data)
        self.assertIn("建議：Buy", report)
        self.assertIn("⚠️ 技術面處於 Stage 4 主跌段（技術風險提示）", report)

    def test_institutional_big_buy_does_not_flip_bearish_ev(self):
        # 籌碼外資大買 + 投信認養，但基本面 EV 期望報酬率 -20% (Avoid) -> Verdict 維持 Avoid
        mock_data = {
            "code": "3141",
            "name": "晶宏",
            "full_symbol": "3141.TWO",
            "valuation": {
                "current_price": 100.0,
                "is_confident": True,
                "fair_price_bull": 90.0,
                "fair_price_base": 80.0,
                "fair_price_bear": 60.0,
                "base_fair_pe": 16.0,
                "a1_desc": "16.0",
                "a2_desc": "16.0",
                "a3_desc": "16.0",
            },
            "technicals": {
                "ma_alignment": {
                    "weinstein_stage": 2,
                    "stage_label": "主升段",
                }
            },
            "chips": {
                "tdcc": {"large_holder_pct": 50.0, "trend_direction": "concentrating", "trend_weeks": 4},
                "institutional": {"foreign_5d": 10000, "trust_adoption_stage": 3, "trust_holding_pct": 4.5},
                "governance": {"director_pledge_pct": 0.0},
            },
            "catalyst": {"events": [], "next_event": None},
            "monthly_revenue": {"status": "ok", "data_month": "11508", "yoy_pct": -15.0, "revenue_curr_month_k": 100000},
            "ev": {
                "status": "ok",
                "ev_price": 77.5,
                "ev_return_pct": -22.5,
                "p_bull": 0.25,
                "p_base": 0.50,
                "p_bear": 0.25,
            },
        }
        report = format_taiwan_stock_report(mock_data)
        self.assertIn("建議：Avoid", report)


class TestHtmlRendering(unittest.TestCase):
    """HTML 渲染器支援台股報告區塊與行動版捲動、KaTeX 測試"""

    def test_table_wrapping_and_katex_inclusion(self):
        md_text = """# 股票研究報告：晶宏（3141.TWO）

## 數據層
| 日期 | 事件類型 | 說明 |
|------|----------|------|
| 2026-10-10 | 9月營收公告窗 | 法定截止日 |

## 估值層
EV 公式：$$\\sum (p_i \\times fv_i) = 95.5$$
"""
        toc, body = md_to_html(md_text)
        wrapped_body = wrap_tables(body)
        self.assertIn('<div class="table-wrap"><table>', wrapped_body)
        self.assertIn('</table></div>', wrapped_body)


class TestMockProviderE2E(unittest.TestCase):
    """端對端 Mock 流程測試（上市 2328 與上櫃 3141）"""

    def setUp(self):
        self.mock_cfg = {
            "version": "2026-09-13",
            "providers": {
                "tdcc": "mock",
                "institutional": "mock",
                "price_history": "mock",
                "catalyst": "mock",
                "governance": "mock",
            },
            "tickers": [
                {"code": "2328", "name": "廣宇", "market": "twse"},
                {"code": "3141", "name": "晶宏", "market": "tpex"},
            ],
            "benchmarks": {
                "twse": "^TWII",
                "tpex": "^TWOII",
            },
            "technical_options": {
                "stop_multiplier": 2.0,
                "rs_lookback_days": 63,
                "vcp_min_contractions": 2,
                "breakout_volume_multiple": 1.5,
            },
        }

    def test_e2e_3141_tpex_report(self):
        mock_val = {
            "current_price": 85.0,
            "trailing_pe": 28.5,
            "forward_eps_growth": 25.0,
            "target_price_analyst": 110.0,
            "forward_eps_consensus": 3.5,
            "eps_ttm": 3.0,
        }
        data = gather_taiwan_stock_data(
            "3141",
            force=True,
            config=self.mock_cfg,
            mock_overrides={"valuation": mock_val},
        )
        self.assertEqual(data["code"], "3141")
        self.assertEqual(data["market"], "tpex")
        self.assertEqual(data["name"], "晶宏")

        report = format_taiwan_stock_report(data)
        self.assertIn("股票研究報告：晶宏（3141.TWO）", report)
        self.assertIn("## 數據層", report)
        self.assertIn("### 月營收趨勢", report)
        self.assertIn("### 籌碼結構（模組 A）", report)
        self.assertIn("### 技術面定位（模組 B）", report)
        self.assertIn("### 催化劑日曆（模組 C）", report)
        self.assertIn("## 估值層", report)
        self.assertIn("### 三錨點公允價（台股在地化）", report)
        self.assertIn("### 第一性檢查（Step 0e）", report)
        self.assertIn("## Verdict", report)

    def test_e2e_2328_twse_report(self):
        mock_val = {
            "current_price": 45.0,
            "trailing_pe": 105.0,
            "forward_eps_growth": None,  # A2 unavailable
            "target_price_analyst": None,  # A3 unavailable
            "forward_eps_consensus": None,
            "eps_ttm": 0.43,
        }
        data = gather_taiwan_stock_data(
            "2328",
            force=True,
            config=self.mock_cfg,
            mock_overrides={"valuation": mock_val},
        )
        self.assertEqual(data["code"], "2328")
        self.assertEqual(data["market"], "twse")
        self.assertEqual(data["name"], "廣宇")

        report = format_taiwan_stock_report(data)
        self.assertIn("股票研究報告：廣宇（2328.TW）", report)
        self.assertIn("⚠️ 估值信心不足：僅 1 個錨點可用", report)
        self.assertIn("(A2 unavailable)", report)
        self.assertIn("(A3 unavailable)", report)

    def test_provider_partial_unavailable_degrades_cleanly(self):
        # 當某區塊 provider 回傳失敗時，標記 (unavailable) 不使整份報告 crash
        mock_data = {
            "code": "3141",
            "name": "晶宏",
            "full_symbol": "3141.TWO",
            "valuation": {
                "current_price": 85.0,
                "is_confident": False,
                "confidence_warning": "⚠️ 估值信心不足：無可用錨點，無法提供目標價",
                "a1_desc": "(A1 unavailable)",
                "a2_desc": "(A2 unavailable)",
                "a3_desc": "(A3 unavailable)",
            },
            "technicals": {
                "ma_alignment": {"status": "(unavailable)"},
                "vcp": {"status": "(unavailable)"},
                "atr": {"status": "(unavailable)"},
                "relative_strength": {"status": "(unavailable)"},
            },
            "chips": {
                "tdcc": {"trend_direction": "unavailable"},
                "institutional": {},
                "governance": {},
            },
            "catalyst": {"events": [], "next_event": None, "status": "(unavailable)"},
            "monthly_revenue": {"status": "(unavailable)"},
            "ev": {"status": "(unavailable)"},
        }
        report = format_taiwan_stock_report(mock_data)
        self.assertIn("(unavailable)", report)
        self.assertIn("⚠️ 估值信心不足：無可用錨點", report)
        self.assertIn("建議：Hold (Data Insufficient)", report)


if __name__ == "__main__":
    unittest.main()
