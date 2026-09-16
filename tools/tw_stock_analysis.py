#!/usr/bin/env python3
"""
tw_stock_analysis.py — 台股分析引擎與報告生成器 (Module D)。

整合模組 A (籌碼面 fetch_tw_chips.py)、模組 B (技術面 tw_technicals.py)、
模組 C (催化劑 tw_catalyst_calendar.py) 與基本面月營收、在地化三錨點估值，
產出標準化台股研究報告 (Markdown / 結構化資料)。

遵循規則：
1. Display-only 規範：籌碼面與技術面僅為觀察層 / 風險 flag，不翻轉 Verdict。
2. 三錨點在地化與降級規範：A1 (市場PE)、A2 (PEG成長倍數)、A3 (法人目標價隱含PE)。
   可用錨點 < 2 時標示「⚠️ 估值信心不足」，絕不偽造目標價。
3. 第一性檢查 (Step 0e)：核心 thesis、證偽條件、機率分布 (EV Σ)。

Usage:
  python3 tools/tw_stock_analysis.py 3141
  python3 tools/tw_stock_analysis.py 2328.TW --json
  python3 tools/tw_stock_analysis.py 3141 --force
  python3 tools/tw_stock_analysis.py 3141 --output briefing-out/stock-analysis-3141.md
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.fetch_tw_chips import (
    CACHE_FILE as TW_INDICATORS_FILE,
    CACHE_TTL_HOURS as CHIPS_TTL,
    fetch_chips_for_ticker,
    is_cache_fresh,
)
from tools.parse_broker_reports import get_tw_broker_consensus
from tools.tw_catalyst_calendar import (
    CACHE_TTL_HOURS as CAT_TTL,
    analyze_catalyst_for_ticker,
)
from tools.tw_providers import get_provider, load_tw_market_config
from tools.tw_technicals import (
    CACHE_TTL_HOURS as TECH_TTL,
    analyze_technicals_for_ticker,
)

logger = logging.getLogger(__name__)

CACHE_DIR = ROOT / "briefing-out" / "cache"
LEADING_CACHE_FILE = CACHE_DIR / "leading-indicators.json"

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()
try:
    _SSL_CTX.load_default_certs()
except Exception:
    pass


def _http_get_json(url: str, timeout: int = 15, headers: dict | None = None) -> Any:
    """受控 SSL 驗證之 HTTP GET JSON 輔助函式"""
    req = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Ticker 偵測與正規化 ──────────────────────────────────────────────────────

def is_taiwan_ticker(ticker: str) -> bool:
    """
    判斷 ticker 是否為台股標的：
    - 4 位純數字 (如 2328, 3141)
    - 4 位數字 + .TW (如 2328.TW)
    - 4 位數字 + .TWO (如 3141.TWO)
    - 4 位數字 + .TWO / .TW (不分大小寫)
    - 6 位數字 (可轉債 / 權證) 或 5 位數字
    """
    t = ticker.strip().upper()
    if re.match(r"^\d{4,6}(\.TW|\.TWO)?$", t):
        return True
    return False


def normalize_tw_ticker(ticker: str, default_market: str = "twse") -> tuple[str, str, str]:
    """
    正規化台股代碼。
    回傳 (code, market, full_symbol)
    例如:
      '3141' -> ('3141', 'tpex', '3141.TWO') (若 config 設定為 tpex)
      '2328.TW' -> ('2328', 'twse', '2328.TW')
      '3141.TWO' -> ('3141', 'tpex', '3141.TWO')
    """
    t = ticker.strip().upper()
    market = default_market.lower()
    code = t

    if "." in t:
        code, suffix = t.split(".", 1)
        if suffix in ("TWO", "TWOII"):
            market = "tpex"
        elif suffix in ("TW", "TWII"):
            market = "twse"
    else:
        # 從 config 查
        cfg = load_tw_market_config()
        for item in cfg.get("tickers", []):
            if str(item.get("code")).strip() == code:
                market = str(item.get("market", default_market)).strip().lower()
                break

    suffix = ".TWO" if market == "tpex" else ".TW"
    full_symbol = f"{code}{suffix}"
    return code, market, full_symbol


def get_company_name(code: str, default_name: str = "") -> str:
    """取得台股公司名稱"""
    cfg = load_tw_market_config()
    for item in cfg.get("tickers", []):
        if str(item.get("code")).strip() == code:
            return str(item.get("name", default_name))
    return default_name or code


# ── 月營收資料提取 ────────────────────────────────────────────────────────────

def get_monthly_revenue_data(
    code: str,
    market: str,
    mock_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    從 leading-indicators.json 或 TWSE/TPEx OpenAPI 取得該標的最新月營收資料
    """
    if mock_override is not None:
        return mock_override

    # 1. 嘗試從 leading-indicators.json 讀取
    if LEADING_CACHE_FILE.exists():
        try:
            lead_data = json.loads(LEADING_CACHE_FILE.read_text(encoding="utf-8"))
            tw_monthly = lead_data.get("blocks", {}).get("tw_monthly", {}) or lead_data.get("tw_monthly", {})
            companies = tw_monthly.get("companies", [])
            for c in companies:
                if str(c.get("code")).strip() == code:
                    return {
                        "status": "ok",
                        "source": "cache:leading-indicators.json",
                        "data_month": c.get("data_month"),
                        "revenue_ntd_thousand": c.get("revenue_ntd_thousand"),
                        "revenue_curr_month_k": c.get("revenue_curr_month_k"),
                        "yoy_pct": c.get("yoy_pct"),
                        "mom_pct": c.get("mom_pct"),
                        "cum_yoy_pct": c.get("cum_yoy_pct"),
                        "yoy_history": c.get("yoy_history", []),
                        "accel_flag": c.get("accel_flag"),
                        "turned_negative": c.get("turned_negative"),
                    }
        except Exception:
            pass

    # 2. 備份：即時抓取 TWSE/TPEx 月營收 OpenAPI
    try:
        if market == "tpex":
            url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
            rows = _http_get_json(url, timeout=15)
            if isinstance(rows, list):
                for row in rows:
                    row_code = str(row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("Code") or "").strip()
                    if row_code == code:
                        yoy = None
                        try:
                            val = row.get("營業收入-去年同月增減(%)") or row.get("去年同月增減(%)") or row.get("LastMonthChangeRatio")
                            yoy = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        mom = None
                        try:
                            val = row.get("營業收入-上月比較增減(%)") or row.get("上月比較增減(%)") or row.get("PreMonthChangeRatio")
                            mom = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        rev_k = None
                        try:
                            val = row.get("營業收入-當月營收") or row.get("CurrentMonthRevenue")
                            rev_k = float(str(val).replace(",", "").strip())
                        except Exception:
                            pass
                        cum_yoy = None
                        try:
                            val = row.get("累計營業收入-前期比較增減(%)") or row.get("前期比較增減(%)")
                            cum_yoy = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        month_val = str(row.get("資料年月") or row.get("Date") or row.get("出表日期") or "")
                        return {
                            "status": "ok",
                            "source": "live:tpex_openapi",
                            "data_month": month_val,
                            "revenue_curr_month_k": rev_k,
                            "yoy_pct": yoy,
                            "mom_pct": mom,
                            "cum_yoy_pct": cum_yoy,
                            "yoy_history": [{"month": month_val, "yoy_pct": yoy}] if yoy is not None else [],
                            "accel_flag": None,
                            "turned_negative": None,
                        }
        else:
            url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
            rows = _http_get_json(url, timeout=15)
            if isinstance(rows, list):
                for row in rows:
                    row_code = str(row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("Code") or "").strip()
                    if row_code == code:
                        yoy = None
                        try:
                            val = row.get("營業收入-去年同月增減(%)") or row.get("去年同月增減(%)") or row.get("LastMonthChangeRatio")
                            yoy = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        mom = None
                        try:
                            val = row.get("營業收入-上月比較增減(%)") or row.get("上月比較增減(%)") or row.get("PreMonthChangeRatio")
                            mom = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        rev_k = None
                        try:
                            val = row.get("營業收入-當月營收") or row.get("CurrentMonthRevenue")
                            rev_k = float(str(val).replace(",", "").strip())
                        except Exception:
                            pass
                        cum_yoy = None
                        try:
                            val = row.get("累計營業收入-前期比較增減(%)") or row.get("前期比較增減(%)")
                            cum_yoy = float(str(val).replace("%", "").strip())
                        except Exception:
                            pass
                        month_val = str(row.get("資料年月") or row.get("出表日期") or row.get("Date") or "")
                        return {
                            "status": "ok",
                            "source": "live:twse_openapi",
                            "data_month": month_val,
                            "revenue_curr_month_k": rev_k,
                            "yoy_pct": yoy,
                            "mom_pct": mom,
                            "cum_yoy_pct": cum_yoy,
                            "yoy_history": [{"month": "latest", "yoy_pct": yoy}] if yoy is not None else [],
                            "accel_flag": None,
                            "turned_negative": None,
                        }
    except Exception as e:
        logger.warning("[%s] Monthly revenue fetch failed: %s", code, e)

    return {
        "status": "(unavailable)",
        "source": "none",
        "data_month": None,
        "revenue_curr_month_k": None,
        "yoy_pct": None,
        "mom_pct": None,
        "cum_yoy_pct": None,
        "yoy_history": [],
        "accel_flag": None,
        "turned_negative": None,
    }


# ── 基本面與估值資料抓取 ──────────────────────────────────────────────────────

def fetch_tw_valuation_inputs(
    code: str,
    market: str,
    price_prov: Any = None,
    mock_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    抓取台股估值三錨點輸入：
    - A1：現行市場 PE (TWSE/TPEx OpenAPI 即時本益比，或 Yahoo Finance trailingPE)
    - A2：PEG 成長合理倍數 (Forward EPS growth rate 自算；無分析師預估則標 unavailable)
    - A3：法人/研究報告目標價隱含 PE (若有；若無則降級)
    - 現價 (Current Price)
    """
    if mock_inputs:
        return mock_inputs

    current_price = None
    trailing_pe = None
    forward_eps_growth = None
    target_price_analyst = None
    forward_eps_consensus = None
    eps_ttm = None

    a1_unavailable_reason = None
    a2_unavailable_reason = "台股無公開分析師一致預期覆蓋（缺乏未來 Forward EPS 成長率數據）"
    a3_unavailable_reason = "無公開可查證之券商/法人一致預期目標價與預估 EPS"

    # 1. 取得現價
    if price_prov:
        try:
            bars = price_prov.fetch_daily(code, days=5)
            if bars:
                current_price = bars[-1]["close"]
        except Exception:
            pass

    # 2. 從 TWSE / TPEx OpenAPI 抓取官方本益比 (A1)
    a1_fetch_failed = False
    a1_found_but_invalid = False
    if market == "tpex":
        try:
            url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
            rows = _http_get_json(url, timeout=15)
            found_code = False
            if isinstance(rows, list):
                for row in rows:
                    row_code = str(row.get("SecuritiesCompanyCode") or row.get("公司代號") or row.get("Code") or "").strip()
                    if row_code == code:
                        found_code = True
                        pe_str = str(row.get("PriceEarningRatio", "")).replace(",", "").strip()
                        if pe_str and pe_str not in ("-", "--", "0.00", "0"):
                            trailing_pe = float(pe_str)
                        else:
                            a1_found_but_invalid = True
                        break
            if not found_code and not a1_found_but_invalid:
                a1_unavailable_reason = "TPEx 官方清單中無此標的本益比資料"
        except Exception as e:
            logger.warning("[%s] TPEx PE fetch failed: %s", code, e)
            a1_fetch_failed = True
            a1_unavailable_reason = f"TPEx OpenAPI 連線或擷取失敗（{e.__class__.__name__}）"
    else:
        try:
            url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
            rows = _http_get_json(url, timeout=15)
            found_code = False
            if isinstance(rows, list):
                for row in rows:
                    row_code = str(row.get("Code") or row.get("公司代號") or row.get("SecuritiesCompanyCode") or "").strip()
                    if row_code == code:
                        found_code = True
                        pe_str = str(row.get("PEratio", "")).replace(",", "").strip()
                        if pe_str and pe_str not in ("-", "--", "0.00", "0"):
                            trailing_pe = float(pe_str)
                        else:
                            a1_found_but_invalid = True
                        break
            if not found_code and not a1_found_but_invalid:
                a1_unavailable_reason = "TWSE 官方清單中無此標的本益比資料"
        except Exception as e:
            logger.warning("[%s] TWSE PE fetch failed: %s", code, e)
            a1_fetch_failed = True
            a1_unavailable_reason = f"TWSE OpenAPI 連線或擷取失敗（{e.__class__.__name__}）"

    if a1_found_but_invalid:
        a1_unavailable_reason = "官方本益比為虧損或無數值（PE ≤ 0 或為空）"

    # 3. 嘗試 Yahoo Finance 補充現價或 PE
    if current_price is None or trailing_pe is None:
        try:
            suffix = ".TWO" if market == "tpex" else ".TW"
            symbol = f"{code}{suffix}"
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
            data = _http_get_json(url, timeout=10)
            res = data.get("chart", {}).get("result", [])
            if res:
                meta = res[0].get("meta", {})
                if current_price is None:
                    current_price = meta.get("regularMarketPrice")
        except Exception:
            pass

    if trailing_pe is None and a1_unavailable_reason is None:
        a1_unavailable_reason = "無法取得有效市場本益比（TWSE/TPEx 與備用來源均無資料）"

    # 4. 嘗試從本地券商研報庫抽取共識數據 (A2 成長率 & A3 目標價與預估 EPS)
    broker_consensus = None
    target_price_base_eps = None
    try:
        broker_consensus = get_tw_broker_consensus(code)
        if broker_consensus.get("status") == "ok":
            target_price_analyst = broker_consensus.get("median_target_price")
            forward_eps_consensus = broker_consensus.get("forward_eps_consensus")
            target_price_base_eps = broker_consensus.get("target_price_base_eps")
            forward_eps_growth = broker_consensus.get("eps_growth_pct")
            if target_price_analyst is not None and target_price_base_eps is not None:
                a3_unavailable_reason = None
            elif target_price_analyst is not None:
                a3_unavailable_reason = "本地研報庫有目標價但缺乏對應年度 EPS 預估數據"
            else:
                a3_unavailable_reason = "本地研報庫有覆蓋但缺乏目標價資料"
            if forward_eps_growth is not None and forward_eps_growth > 0:
                a2_unavailable_reason = None
            elif forward_eps_growth is not None:
                a2_unavailable_reason = f"本地研報庫顯示 EPS 預估成長率為負或持平（{forward_eps_growth:.1f}%）"
            else:
                a2_unavailable_reason = "本地研報庫有覆蓋但缺乏足夠 EPS 預估數據以計算成長率"
        else:
            a3_unavailable_reason = "本地研報庫無覆蓋"
            a2_unavailable_reason = "本地研報庫無覆蓋（缺乏 Forward EPS 預估數據）"
    except Exception as e:
        logger.warning("[%s] Broker reports parsing failed: %s", code, e)
        a3_unavailable_reason = f"本地研報解析失敗（{e}）"
        a2_unavailable_reason = f"本地研報解析失敗（{e}）"

    return {
        "current_price": current_price,
        "trailing_pe": trailing_pe,
        "forward_eps_growth": forward_eps_growth,
        "target_price_analyst": target_price_analyst,
        "forward_eps_consensus": forward_eps_consensus,
        "target_price_base_eps": target_price_base_eps,
        "eps_ttm": eps_ttm,
        "broker_consensus": broker_consensus,
        "a1_unavailable_reason": a1_unavailable_reason if trailing_pe is None else None,
        "a2_unavailable_reason": a2_unavailable_reason if forward_eps_growth is None else None,
        "a3_unavailable_reason": a3_unavailable_reason if target_price_analyst is None else None,
    }


def compute_taiwan_three_anchors(
    val_inputs: dict[str, Any],
    peg_benchmark: float = 1.0,
) -> dict[str, Any]:
    """
    計算在地化台股三錨點估值：
    - A1: 現行市場 PE (TWSE/TPEx 即時)
    - A2: PEG × 預估成長率 (若無成長率則 (A2 unavailable))
    - A3: 分析師目標價隱含 PE (若無則 (A3 unavailable))

    降級規則：
    - 3 個錨點可用：Base Fair PE = median(A1, A2, A3)
    - 2 個錨點可用：Base Fair PE = median(可用 2 錨)
    - 1 個錨點可用：Base Fair PE = 可用 1 錨，標記「⚠️ 估值信心不足：僅 1 個錨點可用」
    - 0 個錨點可用：全部 unavailable，標記「⚠️ 估值信心不足：無可用錨點，無法提供目標價」
    """
    current_price = val_inputs.get("current_price")
    a1_pe = val_inputs.get("trailing_pe")
    if a1_pe is not None and (a1_pe <= 0 or math.isnan(a1_pe)):
        a1_pe = None

    # 缺位具體原因
    a1_unavail_reason = val_inputs.get("a1_unavailable_reason") or "官方資料源無有效本益比或為虧損"
    a2_unavail_reason = val_inputs.get("a2_unavailable_reason") or "台股無公開分析師一致預期覆蓋（缺乏未來 Forward EPS 成長率數據）"
    a3_unavail_reason = val_inputs.get("a3_unavailable_reason") or "無公開可查證之券商/法人一致預期目標價與預估 EPS"

    # A2: PEG 錨
    growth_pct = val_inputs.get("forward_eps_growth")  # 如 15.0 (%)
    a2_pe = None
    a2_desc = f"(A2 unavailable: {a2_unavail_reason})"
    if growth_pct is not None and growth_pct > 0:
        a2_pe = round(peg_benchmark * growth_pct, 2)
        a2_desc = f"{a2_pe:.1f} (PEG {peg_benchmark} × 成長率 {growth_pct}%)"

    # A3: 法人目標價隱含 PE (分母優先採用與券商目標價年度基準對齊之 EPS，即次年 2027 EPS，無 2027 則退回 2026/TTM)
    target_price = val_inputs.get("target_price_analyst")
    a3_base_eps = val_inputs.get("target_price_base_eps") or val_inputs.get("forward_eps_consensus") or val_inputs.get("eps_ttm")
    a3_pe = None
    a3_desc = f"(A3 unavailable: {a3_unavail_reason})"
    if target_price is not None and a3_base_eps is not None and a3_base_eps > 0:
        a3_pe = round(target_price / a3_base_eps, 2)
        a3_desc = f"{a3_pe:.1f} (法人目標價 ${target_price} ÷ 預估 EPS ${a3_base_eps})"

    a1_desc = f"{a1_pe:.1f} (市場現行本益比)" if a1_pe is not None else f"(A1 unavailable: {a1_unavail_reason})"

    available_anchors = []
    if a1_pe is not None:
        available_anchors.append(("A1", a1_pe))
    if a2_pe is not None:
        available_anchors.append(("A2", a2_pe))
    if a3_pe is not None:
        available_anchors.append(("A3", a3_pe))

    num_available = len(available_anchors)
    base_fair_pe = None
    bull_pe = None
    bear_pe = None
    confidence_warning = None
    is_confident = True

    if num_available >= 3:
        vals = sorted([v for _, v in available_anchors])
        base_fair_pe = round(vals[1], 2)
        bull_pe = round(max(vals) * 1.25, 2)
        bear_pe = round(min(vals) * 0.70, 2)
    elif num_available == 2:
        vals = sorted([v for _, v in available_anchors])
        base_fair_pe = round(sum(vals) / 2.0, 2)
        bull_pe = round(max(vals) * 1.25, 2)
        bear_pe = round(min(vals) * 0.70, 2)
    elif num_available == 1:
        base_fair_pe = round(available_anchors[0][1], 2)
        bull_pe = round(base_fair_pe * 1.25, 2)
        bear_pe = round(base_fair_pe * 0.70, 2)
        confidence_warning = "⚠️ 估值信心不足：僅 1 個錨點可用"
        is_confident = False
    else:
        confidence_warning = "⚠️ 估值信心不足：無可用錨點，無法提供目標價"
        is_confident = False

    # 計算各情境公允價 (若有 current_price 與 PE)
    # 若無 forward EPS，以現價 / A1 倒推 EPS 或用基準倍數計算
    est_eps = val_inputs.get("forward_eps_consensus") or a3_base_eps or val_inputs.get("eps_ttm")
    if est_eps is None and current_price and a1_pe and a1_pe > 0:
        est_eps = round(current_price / a1_pe, 2)

    fair_price_base = None
    fair_price_bull = None
    fair_price_bear = None
    if est_eps is not None and is_confident and base_fair_pe is not None:
        fair_price_base = round(est_eps * base_fair_pe, 2)
        fair_price_bull = round(est_eps * (bull_pe or base_fair_pe * 1.25), 2)
        fair_price_bear = round(est_eps * (bear_pe or base_fair_pe * 0.70), 2)

    return {
        "current_price": current_price,
        "estimated_eps": est_eps,
        "a1_pe": a1_pe,
        "a1_desc": a1_desc,
        "a2_pe": a2_pe,
        "a2_desc": a2_desc,
        "a3_pe": a3_pe,
        "a3_desc": a3_desc,
        "broker_consensus": val_inputs.get("broker_consensus"),
        "num_available_anchors": num_available,
        "base_fair_pe": base_fair_pe,
        "bull_pe": bull_pe,
        "bear_pe": bear_pe,
        "fair_price_base": fair_price_base,
        "fair_price_bull": fair_price_bull,
        "fair_price_bear": fair_price_bear,
        "confidence_warning": confidence_warning,
        "is_confident": is_confident,
    }


# ── 第一性原理與 EV 計算 ──────────────────────────────────────────────────────

def compute_first_principles_ev(
    val_result: dict[str, Any],
    p_bull: float = 0.25,
    p_base: float = 0.50,
    p_bear: float = 0.25,
) -> dict[str, Any]:
    """
    Step 0e: Expected Value (EV) 計算。
    Σ(機率 × 各情境公允價)
    """
    cur_price = val_result.get("current_price")
    fv_bull = val_result.get("fair_price_bull")
    fv_base = val_result.get("fair_price_base")
    fv_bear = val_result.get("fair_price_bear")

    if not val_result.get("is_confident") or not all([fv_bull, fv_base, fv_bear]):
        return {
            "ev_price": None,
            "ev_return_pct": None,
            "p_bull": p_bull,
            "p_base": p_base,
            "p_bear": p_bear,
            "status": "(unavailable)",
        }

    ev_price = round(p_bull * fv_bull + p_base * fv_base + p_bear * fv_bear, 2)
    ev_return_pct = None
    if cur_price and cur_price > 0:
        ev_return_pct = round(((ev_price - cur_price) / cur_price) * 100.0, 1)

    return {
        "ev_price": ev_price,
        "ev_return_pct": ev_return_pct,
        "p_bull": p_bull,
        "p_base": p_base,
        "p_bear": p_bear,
        "status": "ok",
    }


# ── 數據聚合核心 ────────────────────────────────────────────────────────────

def gather_taiwan_stock_data(
    ticker: str,
    force: bool = False,
    config: dict[str, Any] | None = None,
    mock_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    端對端抓取並聚合台股分析所需之完整數據集合：
    1. 代碼正規化 (2328 / 3141)
    2. 模組 A 籌碼結構 (fetch_tw_chips.py / cache)
    3. 模組 B 技術面 (tw_technicals.py / cache)
    4. 模組 C 催化劑日曆 (tw_catalyst_calendar.py / cache)
    5. 基本面月營收趨勢
    6. 三錨點在地化估值
    7. 第一性檢查與 EV
    """
    code, market, full_symbol = normalize_tw_ticker(ticker)
    cfg = config or load_tw_market_config()
    company_name = get_company_name(code)

    # 檢查快取新鮮度
    chips_fresh = is_cache_fresh(TW_INDICATORS_FILE, "chips", CHIPS_TTL)
    tech_fresh = is_cache_fresh(TW_INDICATORS_FILE, "technicals", TECH_TTL)
    cat_fresh = is_cache_fresh(TW_INDICATORS_FILE, "catalyst", CAT_TTL)

    # 讀取既有 tw-market-indicators.json 快取
    cache_data: dict[str, Any] = {}
    if TW_INDICATORS_FILE.exists():
        try:
            cache_data = json.loads(TW_INDICATORS_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache_data = {}

    # 1. 籌碼資料 (Chips)
    chips_ticker_data = cache_data.get("chips", {}).get("tickers", {}).get(code)
    if force or not chips_fresh or not chips_ticker_data or mock_overrides:
        try:
            tdcc_p = get_provider("tdcc", cfg)
            inst_p = get_provider("institutional", cfg)
            gov_p = get_provider("governance", cfg)
            chips_ticker_data = fetch_chips_for_ticker(code, tdcc_p, inst_p, gov_p)
        except Exception as e:
            logger.warning("[%s] Chips live gather failed: %s", code, e)
            chips_ticker_data = {"status": "(unavailable)", "error": str(e)}

    # 2. 技術面資料 (Technicals)
    tech_ticker_data = cache_data.get("technicals", {}).get("tickers", {}).get(code)
    if force or not tech_fresh or not tech_ticker_data or mock_overrides:
        try:
            price_p = get_provider("price_history", cfg)
            benchmarks_cache = {}
            for b_sym in ("^TWII", "^TWOII"):
                try:
                    b_bars = price_p.fetch_daily(b_sym, days=365)
                    if b_bars:
                        benchmarks_cache[b_sym] = b_bars
                except Exception:
                    pass
            tech_options = cfg.get("technical_options", {})
            tech_ticker_data = analyze_technicals_for_ticker(
                code=code,
                market=market,
                price_prov=price_p,
                benchmarks_cache=benchmarks_cache,
                options=tech_options,
            )
        except Exception as e:
            logger.warning("[%s] Technicals live gather failed: %s", code, e)
            tech_ticker_data = {
                "ma_alignment": {"status": "(unavailable)"},
                "vcp": {"status": "(unavailable)"},
                "atr": {"status": "(unavailable)"},
                "relative_strength": {"status": "(unavailable)"},
                "error": str(e),
            }

    # 3. 催化劑資料 (Catalyst)
    cat_ticker_data = cache_data.get("catalyst", {}).get("tickers", {}).get(code)
    if force or not cat_fresh or not cat_ticker_data or mock_overrides:
        try:
            cat_p = get_provider("catalyst", cfg)
            cat_ticker_data = analyze_catalyst_for_ticker(code, cat_p, forward_days=90)
        except Exception as e:
            logger.warning("[%s] Catalyst live gather failed: %s", code, e)
            cat_ticker_data = {"events": [], "next_event": None, "status": "(unavailable)", "error": str(e)}

    # 4. 月營收 (Monthly Revenue)
    monthly_rev_mock = mock_overrides.get("monthly_revenue") if mock_overrides else None
    monthly_rev = get_monthly_revenue_data(code, market, mock_override=monthly_rev_mock)

    # 5. 估值三錨點 (Valuation Anchors)
    price_prov = None
    try:
        price_prov = get_provider("price_history", cfg)
    except Exception:
        pass

    val_mock = mock_overrides.get("valuation") if mock_overrides else None
    val_inputs = fetch_tw_valuation_inputs(code, market, price_prov=price_prov, mock_inputs=val_mock)
    val_result = compute_taiwan_three_anchors(val_inputs)

    # 6. 第一性檢查與 EV
    ev_result = compute_first_principles_ev(val_result)

    return {
        "code": code,
        "name": company_name,
        "market": market,
        "full_symbol": full_symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "monthly_revenue": monthly_rev,
        "chips": chips_ticker_data,
        "technicals": tech_ticker_data,
        "catalyst": cat_ticker_data,
        "valuation": val_result,
        "ev": ev_result,
    }


# ── Markdown 報告生成 ────────────────────────────────────────────────────────

def format_taiwan_stock_report(data: dict[str, Any]) -> str:
    """
    產出符合 Issue #8 規範的台股標準化研究報告 Markdown。
    遵循 Display-only 與 估值降級規範。
    """
    code = data["code"]
    name = data.get("name") or code
    full_symbol = data["full_symbol"]
    cur_price = data.get("valuation", {}).get("current_price")
    price_str = f"（{cur_price:.2f} 元）" if cur_price else ""

    rev = data.get("monthly_revenue", {})
    chips = data.get("chips", {})
    tech = data.get("technicals", {})
    cat = data.get("catalyst", {})
    val = data.get("valuation", {})
    ev = data.get("ev", {})

    lines: list[str] = []
    lines.append(f"# 股票研究報告：{name}（{full_symbol}）{price_str}\n")

    # ── 數據層 ──
    lines.append("## 數據層")

    # 月營收趨勢
    lines.append("### 月營收趨勢（TWSE/TPEx OpenAPI）")
    if rev.get("status") == "ok":
        dm = rev.get("data_month", "最新月份")
        yoy = rev.get("yoy_pct")
        mom = rev.get("mom_pct")
        rev_k = rev.get("revenue_curr_month_k")
        yoy_str = f"{yoy:+.2f}%" if yoy is not None else "(N/A)"
        mom_str = f"{mom:+.2f}%" if mom is not None else "(N/A)"
        rev_str = f"{rev_k:,.0f} 仟元" if rev_k is not None else "(N/A)"
        lines.append(f"- **最新月份 ({dm})**：當月營收 {rev_str} | YoY {yoy_str} | MoM {mom_str}")
        if rev.get("accel_flag"):
            lines.append("- **先行訊號**：🔥 月營收 YoY 連續 2 個月走升（加速中）")
        elif rev.get("turned_negative"):
            lines.append("- **先行訊號**：⚠️ 月營收 YoY 轉負（需求走弱警訊）")
    else:
        lines.append("- 月營收資料：`(unavailable)`")
    lines.append("")

    # 籌碼結構 (模組 A)
    lines.append("### 籌碼結構（模組 A）")
    tdcc = chips.get("tdcc", {})
    inst = chips.get("institutional", {})
    gov = chips.get("governance", {})

    if tdcc.get("trend_direction") != "unavailable" and tdcc.get("large_holder_pct") is not None:
        lh = tdcc.get("large_holder_pct")
        rc = tdcc.get("retail_change_pct")
        tw = tdcc.get("trend_weeks", 1)
        rc_str = f"{rc:+.2f}%" if rc is not None else "—"
        tdir = "大戶集中" if tdcc.get("trend_direction") == "concentrating" else ("散戶退散" if tdcc.get("trend_direction") == "dispersing" else "中性")
        lines.append(f"- **集保大戶趨勢**：千張大戶持股比 {lh:.2f}%（趨勢：{tdir}，連 {tw} 週），散戶變動 {rc_str}")
    else:
        lines.append("- **集保大戶趨勢**：`(unavailable)`")

    if inst.get("foreign_5d") is not None:
        f1 = inst.get("foreign_1d", 0)
        f5 = inst.get("foreign_5d", 0)
        f20 = inst.get("foreign_20d", 0)
        t_stage = inst.get("trust_adoption_stage", 1)
        t_pct = inst.get("trust_holding_pct")
        t_pct_str = f"{t_pct:.2f}%" if t_pct is not None else "0.00%"
        lines.append(f"- **法人動態**：外資 1日 {f1:+d} 張 / 5日 {f5:+d} 張 / 20日 {f20:+d} 張 | 投信認養 Stage {t_stage}（持股比 {t_pct_str}）")
    else:
        lines.append("- **法人動態**：`(unavailable)`")

    if gov.get("director_pledge_pct") is not None:
        pledge = gov.get("director_pledge_pct")
        lines.append(f"- **治理安全**：董監事質押率 {pledge:.2f}%")
    else:
        lines.append("- **治理安全**：`(unavailable)`")
    lines.append("")

    # 技術面定位 (模組 B)
    lines.append("### 技術面定位（模組 B）")
    ma = tech.get("ma_alignment", {})
    vcp = tech.get("vcp", {})
    atr = tech.get("atr", {})
    rs = tech.get("relative_strength", {})

    if ma.get("status") != "(unavailable)" and "weinstein_stage" in ma:
        stg = ma.get("weinstein_stage")
        stg_lbl = ma.get("stage_label", "")
        lines.append(f"- **Weinstein Stage**：Stage {stg} {stg_lbl}")
    else:
        lines.append("- **Weinstein Stage**：`(unavailable)`")

    if vcp.get("status") != "(unavailable)":
        if vcp.get("detected"):
            c_cnt = vcp.get("contractions", 0)
            pv = vcp.get("pivot_price")
            bk = "帶量突破" if vcp.get("breakout_confirmed") else "收縮整理中"
            lines.append(f"- **VCP 型態**：{c_cnt} 次收縮 {bk}，Pivot 關鍵點 {pv} 元")
        else:
            lines.append("- **VCP 型態**：無明顯收縮型態")
    else:
        lines.append("- **VCP 型態**：`(unavailable)`")

    if atr.get("status") != "(unavailable)" and atr.get("atr_14") is not None:
        atr_val = atr.get("atr_14")
        s_stop = atr.get("suggested_stop")
        lines.append(f"- **ATR(14)**：{atr_val:.2f} 元，建議動態停損價 {s_stop:.2f} 元")
    else:
        lines.append("- **ATR(14)**：`(unavailable)`")

    if rs.get("status") != "(unavailable)" and rs.get("rs_percentile") is not None:
        rs_pct = rs.get("rs_percentile")
        bench = rs.get("benchmark", "大盤")
        bench_label = "櫃買指數" if "^TWOII" in bench else "加權指數"
        lines.append(f"- **相對強度 RS**：{rs_pct} 百分位（vs {bench_label}）")
    else:
        lines.append("- **相對強度 RS**：`(unavailable)`")
    lines.append("")

    # 催化劑日曆 (模組 C)
    lines.append("### 催化劑日曆（模組 C）")
    next_ev = cat.get("next_event")
    events = cat.get("events", [])
    if next_ev:
        lines.append(f"- **最近事件**：{next_ev.get('date')} {next_ev.get('label')}（倒數 {next_ev.get('days_until')} 天）")
    else:
        lines.append("- **最近事件**：未來 30 日無重大法定催化劑")

    if events:
        lines.append("\n**未來 90 日催化劑列表：**")
        lines.append("| 日期 | 事件類型 | 說明 | 影響屬性 |")
        lines.append("|------|----------|------|----------|")
        for e in events[:5]:
            lines.append(f"| {e.get('date')} | {e.get('label')} | {e.get('detail')} | {e.get('impact')} |")
    lines.append("")

    # ── 估值層 ──
    lines.append("## 估值層")
    lines.append("### 三錨點公允價（台股在地化）")
    lines.append(f"- **A1 市場 PE**：{val.get('a1_desc')}")
    lines.append(f"- **A2 PEG 成長錨**：{val.get('a2_desc')}")
    lines.append(f"- **A3 分析師目標價隱含 PE**：{val.get('a3_desc')}")

    broker_consensus = val.get("broker_consensus")
    if broker_consensus and broker_consensus.get("status") == "ok":
        b_cnt = broker_consensus.get("coverage_count", 0)
        b_list = ", ".join(broker_consensus.get("brokers", []))
        med_tp = broker_consensus.get("median_target_price")
        min_tp = broker_consensus.get("min_target_price")
        max_tp = broker_consensus.get("max_target_price")
        lines.append(f"- **研報來源**：本地法人研報庫 (共 {b_cnt} 家券商覆蓋: {b_list})")
        lines.append(f"- **目標價區間**：中位數 NT$ {med_tp} (最低 NT$ {min_tp} ~ 最高 NT$ {max_tp})")

    if val.get("confidence_warning"):
        lines.append(f"\n> **{val.get('confidence_warning')}**")

    if val.get("is_confident") and val.get("base_fair_pe") is not None:
        lines.append(f"\n**Base Fair PE** = {val.get('base_fair_pe'):.1f}（median 可用錨點）")
        lines.append(f"- 樂觀公允價：${val.get('fair_price_bull')} 元（Fair PE {val.get('bull_pe')}）")
        lines.append(f"- 基準公允價：${val.get('fair_price_base')} 元（Fair PE {val.get('base_fair_pe')}）")
        lines.append(f"- 悲觀公允價：${val.get('fair_price_bear')} 元（Fair PE {val.get('bear_pe')}）")

    # 錨點說明
    lines.append("\n**【錨點說明】**")
    lines.append("- **A1 市場隱含 PE**：現價 ÷ 過去十二個月每股盈餘（TTM EPS），反映市場現在願意給的倍數。")
    lines.append("- **A2 PEG 成長合理倍數**：依「盈餘成長率」推合理倍數（PEG = PE ÷ 成長率，約 1 倍為合理），需要未來 EPS 成長預估（分析師一致預期）。")
    lines.append("- **A3 分析師目標價隱含 PE**：券商目標價 ÷ 預估 EPS，反映法人對合理倍數的看法。")
    lines.append("- **計算規則**：Base = 可用錨點 median；Bull = max × 1.25；Bear = min × 0.70；可用錨點 < 2 則強制標示信心不足並不給目標價。")
    lines.append("- *註：A4 自建估值錨目前僅適用於美股研究體系，不參與台股估值計算。*")
    lines.append("")

    # 第一性檢查 (Step 0e)
    lines.append("### 第一性檢查（Step 0e）")
    lines.append(f"- **核心 thesis：** [請填寫 1 句可驗證命題，如：{name} 受惠特定製程與終端需求擴張，帶動營收成長]")
    lines.append(f"- **證偽條件：** [請填寫 2-3 個觀察點，如：下次月營收 YoY 跌破 0%、主要客戶拉貨遞延]")

    if ev.get("status") == "ok":
        lines.append("\n| 情境 | 機率 | 公允價 |")
        lines.append("|------|------|--------|")
        lines.append(f"| 樂觀 | {int(ev['p_bull']*100)}% | ${val.get('fair_price_bull')} 元 |")
        lines.append(f"| 基準 | {int(ev['p_base']*100)}% | ${val.get('fair_price_base')} 元 |")
        lines.append(f"| 悲觀 | {int(ev['p_bear']*100)}% | ${val.get('fair_price_bear')} 元 |")
        ret_str = f"{ev.get('ev_return_pct'):+.1f}%" if ev.get('ev_return_pct') is not None else "—"
        lines.append(f"\n**Expected Value (EV)** = ${ev.get('ev_price')} 元（預期報酬率：{ret_str} vs 現價 {cur_price} 元）")
    else:
        lines.append("\n- **機率分布與 EV**：估值錨點不足或缺乏可驗證 EPS，無法計算 EV 數學期望值。")
    lines.append("")

    # ── Verdict ──
    lines.append("## Verdict")
    # Display-only 守則：籌碼與技術不翻轉 Verdict
    risk_flags: list[str] = []
    if tech.get("ma_alignment", {}).get("weinstein_stage") == 4:
        risk_flags.append("⚠️ 技術面處於 Stage 4 主跌段（技術風險提示）")

    if val.get("is_confident") and ev.get("ev_return_pct") is not None:
        ev_ret = ev.get("ev_return_pct", 0.0)
        if ev_ret >= 15.0:
            verdict_word = "Buy"
        elif ev_ret <= -10.0:
            verdict_word = "Avoid"
        else:
            verdict_word = "Hold"
    else:
        verdict_word = "Hold (Data Insufficient)"

    lines.append(f"**建議：{verdict_word}**")
    if risk_flags:
        for rf in risk_flags:
            lines.append(f"- {rf}")
    lines.append("\n*結論依據第一性基本面與三錨點估值驅動；籌碼結構與技術面定位僅作為觀察層與風控參考，不單獨翻轉基本面判定。*")

    return "\n".join(lines) + "\n"


# ── CLI 進入點 ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="台股分析 Skill 整合引擎 (Module D)")
    parser.add_argument("ticker", type=str, help="台股標的代碼 (如 3141, 2328, 3141.TWO)")
    parser.add_argument("--force", action="store_true", help="強制刷新快取")
    parser.add_argument("--json", action="store_true", help="輸出結構化 JSON")
    parser.add_argument("--output", type=str, default="", help="輸出 Markdown 檔案路徑")
    args = parser.parse_args()

    data = gather_taiwan_stock_data(args.ticker, force=args.force)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    md_report = format_taiwan_stock_report(data)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md_report, encoding="utf-8")
        print(f"[tw_stock_analysis] 報告已寫入：{out_path}")
    else:
        print(md_report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
