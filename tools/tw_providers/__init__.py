"""
tw_providers — 台股市場資料 Provider 抽象層與工廠。

提供所有台股擴充模組（籌碼、技術面、催化劑、治理）可插拔切換的資料源介面。
新增資料源只需於 tools/tw_providers/ 下實作對應 Protocol 並註冊，
不需更動任何消費端工具程式。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = ROOT / "research" / "tw-market-config.json"
EXAMPLE_CONFIG_FILE = ROOT / "docs" / "tw-market-config.example.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": "2026-09-13",
    "providers": {
        "tdcc": "twse_opendata",
        "institutional": "twse_opendata",
        "price_history": "yahoo_finance",
        "catalyst": "twse_opendata",
        "governance": "twse_opendata",
    },
    "tickers": [
        {"code": "2328", "name": "廣宇", "market": "twse"},
        {"code": "3141", "name": "晶宏", "market": "tpex"},
    ],
    "benchmarks": {
        "twse": "^TWII",
        "tpex": "^TWOII",
    },
    "provider_options": {
        "yahoo_finance": {
            "suffix_twse": ".TW",
            "suffix_tpex": ".TWO",
        },
        "finmind": {
            "api_token_env": "FINMIND_TOKEN",
        },
    },
    "technical_options": {
        "stop_multiplier": 2.0,
        "rs_lookback_days": 63,
        "vcp_min_contractions": 2,
        "breakout_volume_multiple": 1.5,
    },
    "custom_events": [],
}


# ── Provider Protocols ───────────────────────────────────────────────────────

@runtime_checkable
class TwMarketDataProvider(Protocol):
    """台股市場資料 Provider 基底介面"""
    provider_name: str


@runtime_checkable
class TdccProvider(TwMarketDataProvider, Protocol):
    """集保股權分散表 Provider 介面"""
    def fetch_shareholding(self, code: str, weeks: int = 4) -> list[dict]:
        """
        取得集保股權分散資料（依週排序，最新在前）。
        每筆 dict 包含：
          - date: 'YYYY-MM-DD'
          - large_holder_pct: float (千張大戶或 Level 15 持股比例)
          - retail_holder_pct: float (散戶持股比例)
          - levels: list[dict] (分級細節，可選)
        """
        ...


@runtime_checkable
class InstitutionalFlowProvider(TwMarketDataProvider, Protocol):
    """三大法人買賣超 Provider 介面"""
    def fetch_institutional(self, code: str, days: int = 20) -> dict:
        """
        取得三大法人買賣超與持股資訊。
        回傳 dict 包含：
          - foreign_1d, foreign_5d, foreign_20d: int (張數)
          - foreign_holding_pct: float | None
          - trust_1d, trust_5d, trust_20d: int (張數)
          - trust_holding_pct: float | None
          - trust_adoption_stage: int (1-4)
          - dealer_1d, dealer_5d, dealer_20d: int (張數)
          - daily_series: list[dict] (日明細，可選)
        """
        ...


@runtime_checkable
class PriceHistoryProvider(TwMarketDataProvider, Protocol):
    """歷史日K線 Provider 介面"""
    def fetch_daily(self, code: str, days: int = 365) -> list[dict]:
        """
        取得歷史日K線資料（依日期由舊至新排序）。
        每筆 dict 包含：
          - date: 'YYYY-MM-DD'
          - open: float
          - high: float
          - low: float
          - close: float
          - volume: int
        """
        ...


@runtime_checkable
class CatalystProvider(TwMarketDataProvider, Protocol):
    """催化劑事件日曆 Provider 介面"""
    def fetch_events(self, code: str, forward_days: int = 90) -> list[dict]:
        """
        取得未來催化劑事件清單（除權息、營收窗、季報截止、自訂事件等）。
        每筆 dict 包含：
          - date: 'YYYY-MM-DD'
          - type: str ('ex_dividend' | 'monthly_revenue' | 'quarterly_report' | 'custom')
          - label: str
          - detail: str
          - source: str
          - impact: str ('catalyst' | 'neutral' | 'dilution_risk' 等)
        """
        ...


@runtime_checkable
class GovernanceProvider(TwMarketDataProvider, Protocol):
    """公司治理（董監持股與質押）Provider 介面"""
    def fetch_governance(self, code: str) -> dict:
        """
        取得董監事持股與設質比例。
        回傳 dict 包含：
          - director_pledge_pct: float
          - director_holding_pct: float | None
          - data_source: str
          - as_of: str ('YYYY-MM')
        """
        ...


# ── Registry & Factory ──────────────────────────────────────────────────────

_REGISTRY: dict[str, dict[str, type]] = {
    "tdcc": {},
    "institutional": {},
    "price_history": {},
    "catalyst": {},
    "governance": {},
}


def register_provider(category: str, name: str, provider_cls: type) -> None:
    """註冊一個 Provider 實作類別"""
    if category not in _REGISTRY:
        _REGISTRY[category] = {}
    _REGISTRY[category][name] = provider_cls


def get_available_providers(category: str) -> list[str]:
    """列出某類別下所有已註冊的 Provider 名稱"""
    _ensure_builtin_providers_loaded()
    return sorted(_REGISTRY.get(category, {}).keys())


def load_tw_market_config(path: Path | None = None) -> dict[str, Any]:
    """讀取台股市場設定檔，若不存在則回傳預設設定"""
    target = path or CONFIG_FILE
    if target.exists():
        try:
            user_cfg = json.loads(target.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update(user_cfg)
            if "providers" in user_cfg:
                merged["providers"] = {**DEFAULT_CONFIG["providers"], **user_cfg["providers"]}
            if "provider_options" in user_cfg:
                merged["provider_options"] = {**DEFAULT_CONFIG["provider_options"], **user_cfg["provider_options"]}
            if "technical_options" in user_cfg:
                merged["technical_options"] = {**DEFAULT_CONFIG["technical_options"], **user_cfg["technical_options"]}
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def get_provider(category: str, config: dict[str, Any] | None = None) -> Any:
    """
    依設定檔建立指定類別的 Provider 實例。

    :param category: 'tdcc' | 'institutional' | 'price_history' | 'catalyst' | 'governance'
    :param config: 設定檔 dict，若無則自動載入
    :return: 實作對應 Protocol 的 Provider 實例
    """
    _ensure_builtin_providers_loaded()
    cfg = config or load_tw_market_config()
    provider_name = cfg.get("providers", {}).get(category)
    if not provider_name:
        provider_name = DEFAULT_CONFIG["providers"].get(category)

    category_registry = _REGISTRY.get(category, {})
    provider_cls = category_registry.get(provider_name)
    if not provider_cls:
        available = sorted(category_registry.keys())
        raise ValueError(
            f"未知的 Provider '{provider_name}' (類別 '{category}')。"
            f"可用 Provider: {available}"
        )

    options = cfg.get("provider_options", {}).get(provider_name, {})
    return provider_cls(config=cfg, options=options)


_BUILTIN_LOADED = False


def _ensure_builtin_providers_loaded():
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    _BUILTIN_LOADED = True

    # 載入內建 providers 觸發註冊
    try:
        from . import _mock  # noqa: F401
    except ImportError:
        pass

    try:
        from . import twse_opendata  # noqa: F401
    except ImportError:
        pass

    try:
        from . import yahoo_finance  # noqa: F401
    except ImportError:
        pass
