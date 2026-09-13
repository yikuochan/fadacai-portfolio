#!/usr/bin/env python3
"""
fetch_tw_chips.py — 台股籌碼結構分析工具 (Module A)。

提供三大法人累計買賣超 (1d/5d/20d)、集保千張大戶趨勢、散戶退散率、
投信認養 4 階生命週期、董監質押率等微觀結構指標。

輸出: briefing-out/cache/tw-market-indicators.json 之 chips 區塊。
所有指標皆為 display-only（記錄不阻擋）。

Usage:
  python3 tools/fetch_tw_chips.py                        # TTL 內跳過 (預設 6h)
  python3 tools/fetch_tw_chips.py --force                # 強制刷新
  python3 tools/fetch_tw_chips.py --force --ticker 3141  # 只刷指定標的
  python3 tools/fetch_tw_chips.py --days 20              # 自訂交易日天數
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.tw_providers import get_provider, load_tw_market_config

CACHE_DIR = ROOT / "briefing-out" / "cache"
CACHE_FILE = CACHE_DIR / "tw-market-indicators.json"
CACHE_TTL_HOURS = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def is_cache_fresh(path: Path, block_name: str, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data.get(block_name)
        if not block or block.get("status") != "ok":
            return False
        gen_at = block.get("generated_at")
        if not gen_at:
            return False
        # 解析 ISO 時間戳
        t = datetime.fromisoformat(gen_at)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t).total_seconds()
        return age < ttl_hours * 3600
    except Exception:
        return False


def atomic_update_cache(block_name: str, block_data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if CACHE_FILE.exists():
        try:
            current = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            current = {}

    current["version"] = current.get("version", "2026-09-13")
    current["generated_at"] = datetime.now(timezone.utc).isoformat()
    current[block_name] = block_data

    # 整體狀態統計
    sub_statuses = [
        current.get(b, {}).get("status")
        for b in ("chips", "technicals", "catalyst")
        if b in current
    ]
    if sub_statuses and all(s == "ok" for s in sub_statuses):
        current["status"] = "ok"
    elif any(s == "ok" for s in sub_statuses):
        current["status"] = "partial"
    else:
        current["status"] = "warming_up"

    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def analyze_tdcc_trend(shareholding_list: list[dict]) -> dict[str, Any]:
    """
    分析集保股權分散變化趨勢。
    shareholding_list 為最新在前 [w_now, w_prev1, w_prev2, ...]
    """
    if not shareholding_list:
        return {
            "latest_date": None,
            "large_holder_pct": None,
            "large_holder_pct_prev_week": None,
            "retail_change_pct": None,
            "trend_weeks": 0,
            "trend_direction": "neutral",
        }

    latest = shareholding_list[0]
    large_curr = latest.get("large_holder_pct", 0.0)
    retail_curr = latest.get("retail_holder_pct", 0.0)
    date_str = latest.get("date")

    if len(shareholding_list) < 2:
        return {
            "latest_date": date_str,
            "large_holder_pct": large_curr,
            "large_holder_pct_prev_week": None,
            "retail_change_pct": None,
            "trend_weeks": 1,
            "trend_direction": "neutral",
        }

    prev = shareholding_list[1]
    large_prev = prev.get("large_holder_pct", 0.0)
    retail_prev = prev.get("retail_holder_pct", 0.0)
    retail_change = round(retail_curr - retail_prev, 2)

    # 判斷最新一週趨勢方向
    if large_curr > large_prev and retail_curr < retail_prev:
        direction = "concentrating"
    elif large_curr < large_prev and retail_curr > retail_prev:
        direction = "dispersing"
    else:
        direction = "neutral"

    # 計算此趨勢已連續幾週
    trend_weeks = 1
    for i in range(1, len(shareholding_list) - 1):
        w_newer = shareholding_list[i]
        w_older = shareholding_list[i + 1]
        l_newer, l_older = w_newer.get("large_holder_pct", 0.0), w_older.get("large_holder_pct", 0.0)
        r_newer, r_older = w_newer.get("retail_holder_pct", 0.0), w_older.get("retail_holder_pct", 0.0)

        if direction == "concentrating" and l_newer > l_older and r_newer < r_older:
            trend_weeks += 1
        elif direction == "dispersing" and l_newer < l_older and r_newer > r_older:
            trend_weeks += 1
        else:
            break

    return {
        "latest_date": date_str,
        "large_holder_pct": round(large_curr, 2),
        "large_holder_pct_prev_week": round(large_prev, 2),
        "retail_change_pct": retail_change,
        "trend_weeks": trend_weeks,
        "trend_direction": direction,
    }


def fetch_chips_for_ticker(
    code: str,
    tdcc_prov: Any,
    inst_prov: Any,
    gov_prov: Any,
    days: int = 20,
) -> dict[str, Any]:
    """抓取單一標的籌碼面指標"""
    # 1. TDCC
    try:
        shares_hist = tdcc_prov.fetch_shareholding(code, weeks=4)
        tdcc_data = analyze_tdcc_trend(shares_hist)
    except Exception as e:
        logger.warning("[%s] TDCC fetch failed: %s", code, e)
        tdcc_data = {
            "latest_date": None,
            "large_holder_pct": None,
            "large_holder_pct_prev_week": None,
            "retail_change_pct": None,
            "trend_weeks": 0,
            "trend_direction": "unavailable",
            "error": str(e),
        }

    # 2. Institutional
    try:
        inst_data = inst_prov.fetch_institutional(code, days=days)
    except Exception as e:
        logger.warning("[%s] Institutional fetch failed: %s", code, e)
        inst_data = {
            "foreign_1d": 0,
            "foreign_5d": 0,
            "foreign_20d": 0,
            "foreign_holding_pct": None,
            "trust_1d": 0,
            "trust_5d": 0,
            "trust_20d": 0,
            "trust_holding_pct": None,
            "trust_adoption_stage": 1,
            "dealer_1d": 0,
            "dealer_5d": 0,
            "dealer_20d": 0,
            "status": "(unavailable)",
            "error": str(e),
        }

    # 3. Governance
    try:
        gov_data = gov_prov.fetch_governance(code)
    except Exception as e:
        logger.warning("[%s] Governance fetch failed: %s", code, e)
        gov_data = {
            "director_pledge_pct": 0.0,
            "director_holding_pct": None,
            "data_source": "(unavailable)",
            "as_of": None,
            "error": str(e),
        }

    return {
        "tdcc": tdcc_data,
        "institutional": inst_data,
        "governance": gov_data,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="台股籌碼結構分析 (Module A)")
    parser.add_argument("--force", action="store_true", help="強制刷新快取")
    parser.add_argument("--ticker", type=str, default="", help="指定單一標的代碼 (如 3141)")
    parser.add_argument("--days", type=int, default=20, help="法人累積天數 (預設 20)")
    args = parser.parse_args()

    if not args.force and not args.ticker and is_cache_fresh(CACHE_FILE, "chips", CACHE_TTL_HOURS):
        logger.info("✅ chips cache fresh (< %sh), skipping", CACHE_TTL_HOURS)
        return 0

    cfg = load_tw_market_config()
    tdcc_prov = get_provider("tdcc", cfg)
    inst_prov = get_provider("institutional", cfg)
    gov_prov = get_provider("governance", cfg)

    # 確定要處理的標的清單
    tickers_cfg = cfg.get("tickers", [])
    if args.ticker:
        target_codes = [args.ticker.strip()]
    else:
        target_codes = [str(t.get("code")).strip() for t in tickers_cfg if t.get("code")]

    if not target_codes:
        target_codes = ["2328", "3141"]

    logger.info("Fetching chips for %d tickers via %s / %s...",
                len(target_codes), tdcc_prov.provider_name, inst_prov.provider_name)

    tickers_output: dict[str, Any] = {}

    # 若指定單一標的，保留其他標的既有結果
    if args.ticker and CACHE_FILE.exists():
        try:
            cached_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_chips = cached_data.get("chips", {}).get("tickers", {})
            tickers_output.update(cached_chips)
        except Exception:
            pass

    for code in target_codes:
        logger.info("Processing chips for %s...", code)
        tickers_output[code] = fetch_chips_for_ticker(
            code=code,
            tdcc_prov=tdcc_prov,
            inst_prov=inst_prov,
            gov_prov=gov_prov,
            days=args.days,
        )

    block_payload = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": tdcc_prov.provider_name,
        "tickers": tickers_output,
        "display_only": True,
    }

    atomic_update_cache("chips", block_payload)
    logger.info("💾 Wrote chips to %s (status=ok, %d tickers)", CACHE_FILE.relative_to(ROOT), len(tickers_output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
