"""
tools/tw_providers/_mock.py — 測試專用 Mock Provider 實作。

支援單元測試與離線驗證，提供確定性數據與邊界情境資料。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import (
    CatalystProvider,
    GovernanceProvider,
    InstitutionalFlowProvider,
    PriceHistoryProvider,
    TdccProvider,
    register_provider,
)


class MockTdccProvider(TdccProvider):
    provider_name: str = "mock"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        # 允許測試直接注入資料
        self.override_data: dict[str, list[dict]] = {}

    def fetch_shareholding(self, code: str, weeks: int = 4) -> list[dict]:
        if code in self.override_data:
            return self.override_data[code][:weeks]

        # 預設模擬資料（最新在前，呈現連續集中趨勢）
        base_date = date.today()
        # 找最近的週五
        days_since_friday = (base_date.weekday() - 4) % 7
        latest_friday = base_date - timedelta(days=days_since_friday)

        if code == "3141":
            # 晶宏：連 3 週大戶集中、散戶減少
            return [
                {
                    "date": (latest_friday).isoformat(),
                    "large_holder_pct": 45.2,
                    "retail_holder_pct": 34.5,
                    "levels": [{"level": 15, "percent": 45.2}],
                },
                {
                    "date": (latest_friday - timedelta(days=7)).isoformat(),
                    "large_holder_pct": 44.8,
                    "retail_holder_pct": 36.0,
                    "levels": [{"level": 15, "percent": 44.8}],
                },
                {
                    "date": (latest_friday - timedelta(days=14)).isoformat(),
                    "large_holder_pct": 44.1,
                    "retail_holder_pct": 37.2,
                    "levels": [{"level": 15, "percent": 44.1}],
                },
                {
                    "date": (latest_friday - timedelta(days=21)).isoformat(),
                    "large_holder_pct": 43.5,
                    "retail_holder_pct": 38.0,
                    "levels": [{"level": 15, "percent": 43.5}],
                },
            ][:weeks]

        elif code == "2328":
            # 廣宇：大戶持股穩定
            return [
                {
                    "date": (latest_friday).isoformat(),
                    "large_holder_pct": 37.19,
                    "retail_holder_pct": 41.5,
                    "levels": [{"level": 15, "percent": 37.19}],
                },
                {
                    "date": (latest_friday - timedelta(days=7)).isoformat(),
                    "large_holder_pct": 37.10,
                    "retail_holder_pct": 41.6,
                    "levels": [{"level": 15, "percent": 37.10}],
                },
                {
                    "date": (latest_friday - timedelta(days=14)).isoformat(),
                    "large_holder_pct": 37.05,
                    "retail_holder_pct": 41.7,
                    "levels": [{"level": 15, "percent": 37.05}],
                },
                {
                    "date": (latest_friday - timedelta(days=21)).isoformat(),
                    "large_holder_pct": 36.95,
                    "retail_holder_pct": 41.9,
                    "levels": [{"level": 15, "percent": 36.95}],
                },
            ][:weeks]

        return [
            {
                "date": (latest_friday - timedelta(days=7 * i)).isoformat(),
                "large_holder_pct": 40.0 + i * 0.5,
                "retail_holder_pct": 35.0 - i * 0.3,
            }
            for i in range(weeks)
        ]


class MockInstitutionalFlowProvider(InstitutionalFlowProvider):
    provider_name: str = "mock"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.override_data: dict[str, dict] = {}

    def fetch_institutional(self, code: str, days: int = 20) -> dict:
        if code in self.override_data:
            return self.override_data[code]

        if code == "3141":
            return {
                "foreign_1d": 850,
                "foreign_5d": 4494,
                "foreign_20d": 8650,
                "foreign_holding_pct": 16.49,
                "trust_1d": 0,
                "trust_5d": 0,
                "trust_20d": 0,
                "trust_holding_pct": 0.0,
                "trust_adoption_stage": 1,
                "dealer_1d": 65,
                "dealer_5d": 11,
                "dealer_20d": 320,
            }
        elif code == "2328":
            return {
                "foreign_1d": 264,
                "foreign_5d": 1200,
                "foreign_20d": 4500,
                "foreign_holding_pct": 9.37,
                "trust_1d": 0,
                "trust_5d": 0,
                "trust_20d": 0,
                "trust_holding_pct": 0.18,
                "trust_adoption_stage": 1,
                "dealer_1d": 49,
                "dealer_5d": 120,
                "dealer_20d": 350,
            }

        return {
            "foreign_1d": 100,
            "foreign_5d": 500,
            "foreign_20d": 2000,
            "foreign_holding_pct": 10.0,
            "trust_1d": 50,
            "trust_5d": 250,
            "trust_20d": 1000,
            "trust_holding_pct": 1.5,
            "trust_adoption_stage": 2,
            "dealer_1d": 10,
            "dealer_5d": 50,
            "dealer_20d": 200,
        }


class MockPriceHistoryProvider(PriceHistoryProvider):
    provider_name: str = "mock"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.override_data: dict[str, list[dict]] = {}

    def fetch_daily(self, code: str, days: int = 365) -> list[dict]:
        if code in self.override_data:
            return self.override_data[code][-days:]

        # 針對特定測試代碼產生合成走勢
        if code.startswith("SYNTH_STAGE2"):
            return _generate_stage2_series(days)
        elif code.startswith("SYNTH_STAGE3"):
            return _generate_stage3_series(days)
        elif code.startswith("SYNTH_STAGE4"):
            return _generate_stage4_series(days)
        elif code.startswith("SYNTH_STAGE1"):
            return _generate_stage1_series(days)
        elif code.startswith("SYNTH_VCP_BREAKOUT"):
            return _generate_vcp_series(days, breakout_volume_ratio=2.0)
        elif code.startswith("SYNTH_VCP_LOWVOL"):
            return _generate_vcp_series(days, breakout_volume_ratio=0.8)
        elif code.startswith("SYNTH_VCP_NOCONTRACT"):
            return _generate_non_vcp_series(days)

        # 預設模擬：2328 與 3141 及指數
        base_price = 45.0 if code == "2328" else 85.0
        if "^" in code:
            base_price = 22000.0 if "TWII" in code else 260.0

        today = date.today()
        series = []
        cur_price = base_price
        for i in range(days):
            d = today - timedelta(days=days - i)
            # 簡單隨機波動 + 微幅向上趨勢
            change = (0.5 - (i % 7) * 0.1) * (base_price * 0.01)
            cur_price = max(1.0, round(cur_price + change, 2))
            high = round(cur_price * 1.015, 2)
            low = round(cur_price * 0.985, 2)
            series.append({
                "date": d.isoformat(),
                "open": cur_price,
                "high": high,
                "low": low,
                "close": cur_price,
                "volume": 1000000 + (i % 10) * 50000,
            })
        return series


class MockCatalystProvider(CatalystProvider):
    provider_name: str = "mock"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.override_events: dict[str, list[dict]] = {}

    def fetch_events(self, code: str, forward_days: int = 90) -> list[dict]:
        if code in self.override_events:
            return self.override_events[code]

        today = date.today()
        events = []

        if code == "3141":
            events.append({
                "date": (today + timedelta(days=7)).isoformat(),
                "type": "ex_dividend",
                "label": "除息",
                "detail": "現金股利 0.3578 元/股",
                "source": "mock",
                "impact": "neutral",
            })
        elif code == "2328":
            events.append({
                "date": (today + timedelta(days=25)).isoformat(),
                "type": "ex_dividend",
                "label": "除息",
                "detail": "現金股利 1.20 元/股",
                "source": "mock",
                "impact": "neutral",
            })

        # 加入規則計算的營收與季報
        next_month_10 = _next_monthly_rev_date(today)
        if (next_month_10 - today).days <= forward_days:
            events.append({
                "date": next_month_10.isoformat(),
                "type": "monthly_revenue",
                "label": f"{_prev_month_label(today)}月營收公告窗",
                "detail": f"法定截止日 {next_month_10.strftime('%m/%d')}",
                "source": "rule_based",
                "impact": "catalyst",
            })

        # 排序
        events.sort(key=lambda e: e["date"])
        return events


class MockGovernanceProvider(GovernanceProvider):
    provider_name: str = "mock"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.override_data: dict[str, dict] = {}

    def fetch_governance(self, code: str) -> dict:
        if code in self.override_data:
            return self.override_data[code]

        if code == "3141":
            return {
                "director_pledge_pct": 23.96,
                "director_holding_pct": 5.53,
                "data_source": "mock",
                "as_of": "2026-07",
            }
        elif code == "2328":
            return {
                "director_pledge_pct": 0.0,
                "director_holding_pct": 20.88,
                "data_source": "mock",
                "as_of": "2026-07",
            }

        return {
            "director_pledge_pct": 0.0,
            "director_holding_pct": 10.0,
            "data_source": "mock",
            "as_of": "2026-07",
        }


# ── Synthetic Series Generator Helpers ─────────────────────────────────────

def _generate_stage2_series(days: int = 300) -> list[dict]:
    """多頭走勢：MA5 > MA10 > MA20 > MA60 > MA120 > MA240，股價持續向上"""
    today = date.today()
    series = []
    price = 50.0
    for i in range(days):
        d = today - timedelta(days=days - i)
        # 連續向上爬升
        price += 0.25 + (i % 3) * 0.05
        high = price + 1.0
        low = price - 0.5
        series.append({
            "date": d.isoformat(),
            "open": price - 0.2,
            "high": high,
            "low": low,
            "close": price,
            "volume": 2000000,
        })
    return series


def _generate_stage3_series(days: int = 300) -> list[dict]:
    """頭部震盪：先漲至高點，近期跌破 MA20，但仍在 MA240 之上"""
    series = _generate_stage2_series(days)
    # 最後 15 天急跌破 20MA
    last_price = series[-16]["close"]
    for i in range(15):
        idx = len(series) - 15 + i
        drop = (i + 1) * 1.5
        p = last_price - drop
        series[idx]["open"] = p + 0.5
        series[idx]["close"] = p
        series[idx]["high"] = p + 1.0
        series[idx]["low"] = p - 0.5
    return series


def _generate_stage4_series(days: int = 300) -> list[dict]:
    """主跌段：空頭排列，股價在所有均線之下"""
    today = date.today()
    series = []
    price = 200.0
    for i in range(days):
        d = today - timedelta(days=days - i)
        price = max(10.0, price - 0.35)
        series.append({
            "date": d.isoformat(),
            "open": price + 0.2,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1500000,
        })
    return series


def _generate_stage1_series(days: int = 300) -> list[dict]:
    """築底段：240MA 走平，股價在 100 附近上下震盪"""
    today = date.today()
    series = []
    import math
    for i in range(days):
        d = today - timedelta(days=days - i)
        p = 100.0 + 3.0 * math.sin(i / 10.0)
        series.append({
            "date": d.isoformat(),
            "open": p,
            "high": p + 0.8,
            "low": p - 0.8,
            "close": p,
            "volume": 800000,
        })
    return series


def _generate_vcp_series(days: int = 200, breakout_volume_ratio: float = 2.0) -> list[dict]:
    """
    Minervini VCP 波動收縮合成走勢：
    3 個收縮波：
      C1: 深度 -20% (100 -> 80 -> 98)
      C2: 深度 -10% (98 -> 88.2 -> 99)
      C3: 深度 -4%  (99 -> 95 -> 100)
    最後一天收盤價突破 pivot (100.0) 達到 101.5，成交量為均量的 breakout_volume_ratio 倍。
    """
    today = date.today()
    series = []
    base_vol = 1000000

    # 前 60 天走平在 100
    prices = [100.0] * 60
    # C1: 100 -> 80 (20 天) -> 98 (20 天)
    for i in range(20):
        prices.append(100.0 - 20.0 * (i + 1) / 20.0)
    for i in range(20):
        prices.append(80.0 + 18.0 * (i + 1) / 20.0)
    # C2: 98 -> 88.2 (15 天) -> 99 (15 天)
    for i in range(15):
        prices.append(98.0 - 9.8 * (i + 1) / 15.0)
    for i in range(15):
        prices.append(88.2 + 10.8 * (i + 1) / 15.0)
    # C3: 99 -> 95.0 (10 天) -> 100 (10 天)
    for i in range(10):
        prices.append(99.0 - 4.0 * (i + 1) / 10.0)
    for i in range(10):
        prices.append(95.0 + 5.0 * (i + 1) / 10.0)

    # 突破日
    prices.append(101.5)

    n = len(prices)
    for i, p in enumerate(prices):
        d = today - timedelta(days=n - i)
        is_breakout = (i == n - 1)
        vol = int(base_vol * breakout_volume_ratio) if is_breakout else base_vol
        series.append({
            "date": d.isoformat(),
            "open": p - 0.2,
            "high": p + 0.3,
            "low": p - 0.3,
            "close": p,
            "volume": vol,
        })
    return series[-days:] if len(series) > days else series


def _generate_non_vcp_series(days: int = 150) -> list[dict]:
    """一般無收縮特徵的隨機走勢"""
    today = date.today()
    series = []
    p = 50.0
    for i in range(days):
        d = today - timedelta(days=days - i)
        p += ((i % 5) - 2) * 0.5
        series.append({
            "date": d.isoformat(),
            "open": p,
            "high": p + 0.5,
            "low": p - 0.5,
            "close": p,
            "volume": 1000000,
        })
    return series


def _next_monthly_rev_date(as_of: date) -> date:
    """計算下一個月營收公告截止日（每月 10 日）"""
    if as_of.day < 10:
        return date(as_of.year, as_of.month, 10)
    if as_of.month == 12:
        return date(as_of.year + 1, 1, 10)
    return date(as_of.year, as_of.month + 1, 10)


def _prev_month_label(as_of: date) -> int:
    """營收公告所屬月份（例如 10/10 公告 9 月營收）"""
    if as_of.day < 10:
        # 當月 10 號前公告的是上上月
        m = as_of.month - 2
        return m if m > 0 else m + 12
    m = as_of.month - 1
    return m if m > 0 else m + 12


# ── 自動註冊 Mock Providers ────────────────────────────────────────────────
register_provider("tdcc", "mock", MockTdccProvider)
register_provider("institutional", "mock", MockInstitutionalFlowProvider)
register_provider("price_history", "mock", MockPriceHistoryProvider)
register_provider("catalyst", "mock", MockCatalystProvider)
register_provider("governance", "mock", MockGovernanceProvider)
