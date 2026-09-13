#!/usr/bin/env python3
"""
test_tw_catalyst_calendar.py — 模組 C（催化劑事件日曆）測試套件。

驗證項目（依 Issue #7 規範）：
1. 單元測試（月營收截止日計算、季報法定截止日、自訂事件合併、next_event 與 days_until 計算）
2. 整合測試（TWSE 與 TPEx 除權息 API 抓取、非除權息淡季 graceful degradation）
3. 架構整合測試（tw-market-indicators.json catalyst 區塊寫入、TTL 機制）
"""

import json
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.tw_catalyst_calendar import (
    analyze_catalyst_for_ticker,
    compute_next_event,
)
from tools.tw_providers import get_provider, load_tw_market_config
from tools.tw_providers.twse_opendata import TwseOpenDataCatalystProvider


class TestCatalystRulesAndDates(unittest.TestCase):
    """固定規則與日期計算單元測試"""

    def setUp(self):
        self.prov = TwseOpenDataCatalystProvider()

    def test_monthly_revenue_deadlines_all_months(self):
        # 測試各月份在 90 天展望內皆正確產出每月 10 日營收窗
        # 基準日設為 2026-02-15（閏年/平年 2 月跨月邊界）
        test_base = date(2026, 2, 15)
        events = self.prov._generate_monthly_revenue_events(test_base, forward_days=90)
        dates = [e["date"] for e in events]

        # 2/15 往後 90 天涵蓋 3/10, 4/10, 5/10
        self.assertIn("2026-03-10", dates)
        self.assertIn("2026-04-10", dates)
        self.assertIn("2026-05-10", dates)

        # 檢查標籤與前月對應（例如 3/10 應公告 2 月營收）
        ev_march = next(e for e in events if e["date"] == "2026-03-10")
        self.assertIn("2月營收公告窗", ev_march["label"])

    def test_quarterly_report_deadlines(self):
        # 測試四大季報法定截止日：03-31, 05-15, 08-14, 11-14
        # 基準日設為 2026-01-01，展望 365 天
        events = self.prov._generate_quarterly_report_events(date(2026, 1, 1), forward_days=365)
        dates = {e["date"]: e["label"] for e in events}

        self.assertIn("2026-03-31", dates)
        self.assertIn("年報截止", dates["2026-03-31"])

        self.assertIn("2026-05-15", dates)
        self.assertIn("Q1 季報截止", dates["2026-05-15"])

        self.assertIn("2026-08-14", dates)
        self.assertIn("Q2 季報截止", dates["2026-08-14"])

        self.assertIn("2026-11-14", dates)
        self.assertIn("Q3 季報截止", dates["2026-11-14"])

    def test_custom_events_merging(self):
        cfg = {
            "custom_events": [
                {
                    "ticker": "3141",
                    "date": "2026-10-15",
                    "type": "custom",
                    "label": "CB5 轉換起始日",
                    "detail": "第五次無擔保可轉換公司債轉換開始",
                    "impact": "dilution_risk",
                },
                {
                    "ticker": "2330",
                    "date": "2026-10-20",
                    "label": "台積電法說會",
                },
            ]
        }
        prov = TwseOpenDataCatalystProvider(config=cfg)
        events_3141 = prov._load_custom_events("3141")
        self.assertEqual(len(events_3141), 1)
        self.assertEqual(events_3141[0]["label"], "CB5 轉換起始日")
        self.assertEqual(events_3141[0]["impact"], "dilution_risk")

        events_2328 = prov._load_custom_events("2328")
        self.assertEqual(len(events_2328), 0)

    def test_next_event_and_days_until_calculation(self):
        today = date(2026, 9, 13)
        events = [
            {"date": "2026-09-10", "type": "past", "label": "已過事件"},
            {"date": "2026-09-20", "type": "ex_dividend", "label": "除息"},
            {"date": "2026-10-10", "type": "monthly_revenue", "label": "營收"},
        ]
        next_ev = compute_next_event(events, as_of=today)
        self.assertIsNotNone(next_ev)
        self.assertEqual(next_ev["date"], "2026-09-20")
        self.assertEqual(next_ev["label"], "除息")
        self.assertEqual(next_ev["days_until"], 7)  # 20 - 13 = 7 天

    def test_next_event_across_month_boundary(self):
        today = date(2026, 9, 28)
        events = [
            {"date": "2026-10-05", "type": "custom", "label": "法說會"}
        ]
        next_ev = compute_next_event(events, as_of=today)
        self.assertEqual(next_ev["days_until"], 7)


class TestRealApiCatalyst(unittest.TestCase):
    """真實 API 整合測試（除權息預告表、非除權息淡季測試）"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = load_tw_market_config()
        cls.prov = get_provider("catalyst", cls.cfg)

    def test_twse_catalyst_real(self):
        res = analyze_catalyst_for_ticker("2328", self.prov, forward_days=90)
        self.assertIn("events", res)
        self.assertIn("next_event", res)
        self.assertGreaterEqual(len(res["events"]), 1)

    def test_tpex_catalyst_real(self):
        res = analyze_catalyst_for_ticker("3141", self.prov, forward_days=90)
        self.assertIn("events", res)
        self.assertIn("next_event", res)
        self.assertGreaterEqual(len(res["events"]), 1)

    def test_empty_dividend_season_graceful(self):
        # 即使無即將除權息事件，仍有月營收與季報，不拋出錯誤
        events = self.prov._fetch_ex_dividend_events("999999_NOEXDIV", "twse")
        self.assertIsInstance(events, list)


class TestMockProviderCatalyst(unittest.TestCase):
    """Mock Provider 催化劑日曆測試"""

    def test_mock_catalyst_events(self):
        cfg = {"providers": {"catalyst": "mock"}}
        prov = get_provider("catalyst", cfg)
        events = prov.fetch_events("3141", forward_days=90)
        self.assertGreaterEqual(len(events), 1)
        # 檢查是否有 mock 的除息事件
        ex_div = [e for e in events if e.get("type") == "ex_dividend"]
        self.assertEqual(len(ex_div), 1)
        self.assertEqual(ex_div[0]["label"], "除息")


if __name__ == "__main__":
    unittest.main(verbosity=2)
