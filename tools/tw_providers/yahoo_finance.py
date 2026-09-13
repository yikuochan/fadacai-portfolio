"""
tools/tw_providers/yahoo_finance.py — Yahoo Finance 價格歷史 Provider。

使用免金鑰之 Yahoo Finance Chart API 抓取台股日K線資料（支援上市 .TW 與上櫃 .TWO）。
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

from . import PriceHistoryProvider, register_provider

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class YahooFinancePriceHistoryProvider(PriceHistoryProvider):
    provider_name: str = "yahoo_finance"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.suffix_twse = self.options.get("suffix_twse", ".TW")
        self.suffix_tpex = self.options.get("suffix_tpex", ".TWO")

        # 建立 ticker -> market 快查表
        self._ticker_market: dict[str, str] = {}
        for t in self.config.get("tickers", []):
            code = str(t.get("code", "")).strip()
            market = str(t.get("market", "")).strip().lower()
            if code and market:
                self._ticker_market[code] = market

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def resolve_symbol(self, code: str) -> str:
        """將台股代碼解析為 Yahoo Finance 符號"""
        code = code.strip()
        if code.startswith("^") or "." in code:
            return code

        # 指數別名處理
        if code.upper() in ("TWII", "TAIEX"):
            return "^TWII"
        elif code.upper() in ("TWOII", "TPEX"):
            return "^TWOII"

        # 從 config 查所屬市場
        market = self._ticker_market.get(code)
        if market == "tpex":
            return f"{code}{self.suffix_tpex}"
        elif market == "twse":
            return f"{code}{self.suffix_twse}"

        # 預設上市
        return f"{code}{self.suffix_twse}"

    def fetch_daily(self, code: str, days: int = 365) -> list[dict]:
        symbol = self.resolve_symbol(code)
        if days <= 30:
            range_param = "1mo"
        elif days <= 90:
            range_param = "3mo"
        elif days <= 180:
            range_param = "6mo"
        elif days <= 370:
            range_param = "1y"
        elif days <= 730:
            range_param = "2y"
        else:
            range_param = "5y"

        data = self._request_chart(symbol, range_param)
        if not data and symbol.endswith(self.suffix_twse) and code not in self._ticker_market:
            # 若預設 .TW 失敗且未指定市場，自動嘗試 .TWO (上櫃)
            alt_symbol = f"{code}{self.suffix_tpex}"
            alt_data = self._request_chart(alt_symbol, range_param)
            if alt_data:
                data = alt_data
                symbol = alt_symbol

        if not data:
            return []

        return self._parse_chart_data(data, days)

    def _request_chart(self, symbol: str, range_param: str, max_retries: int = 3) -> dict | None:
        hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
        encoded_sym = urllib.parse.quote(symbol)

        for attempt in range(1, max_retries + 1):
            host = hosts[(attempt - 1) % len(hosts)]
            url = f"https://{host}/v8/finance/chart/{encoded_sym}?range={range_param}&interval=1d"
            try:
                resp = self.session.get(url, timeout=12)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 404:
                    return None
            except Exception as e:
                logger.debug("Attempt %d failed for %s: %s", attempt, url, e)
            time.sleep(0.5 * attempt)
        return None

    def _parse_chart_data(self, data: dict, days: int) -> list[dict]:
        chart = data.get("chart", {})
        results = chart.get("result")
        if not results:
            return []

        item = results[0]
        timestamps = item.get("timestamp", [])
        indicators = item.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]

        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])

        series = []
        for i, ts in enumerate(timestamps):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue

            o = opens[i] if i < len(opens) and opens[i] is not None else c
            h = highs[i] if i < len(highs) and highs[i] is not None else max(o, c)
            l = lows[i] if i < len(lows) and lows[i] is not None else min(o, c)
            v = int(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0

            d_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            series.append({
                "date": d_str,
                "open": round(float(o), 2),
                "high": round(float(h), 2),
                "low": round(float(l), 2),
                "close": round(float(c), 2),
                "volume": v,
            })

        series.sort(key=lambda x: x["date"])
        return series[-days:] if len(series) > days else series


# ── 自動註冊 ────────────────────────────────────────────────────────────────
register_provider("price_history", "yahoo_finance", YahooFinancePriceHistoryProvider)
