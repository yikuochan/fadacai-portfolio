#!/usr/bin/env python3
"""
tw_catalyst_calendar.py — 台股催化劑事件日曆 (Module C)。

自動追蹤除權息公告、月營收公告窗（每月 10 日前）、
季報公告法定截止日（Q1: 5/15、Q2: 8/14、Q3: 11/14、年報: 3/31）
與 Config 自訂事件（法說會、可轉債轉換、擴產等）。

輸出: briefing-out/cache/tw-market-indicators.json 之 catalyst 區塊。
所有指標皆為 display-only（記錄不阻擋）。

Usage:
  python3 tools/tw_catalyst_calendar.py                  # 全追蹤標的 (TTL 12h)
  python3 tools/tw_catalyst_calendar.py --force          # 強制刷新
  python3 tools/tw_catalyst_calendar.py --ticker 3141    # 單一標的
  python3 tools/tw_catalyst_calendar.py --horizon 60     # 自訂展望天數 (預設 90)
"""

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.tw_providers import get_provider, load_tw_market_config

CACHE_DIR = ROOT / "briefing-out" / "cache"
CACHE_FILE = CACHE_DIR / "tw-market-indicators.json"
CACHE_TTL_HOURS = 12

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


def compute_next_event(events: list[dict], as_of: date) -> dict[str, Any] | None:
    """從事件清單中挑選最接近之未來事件"""
    future_events = []
    for e in events:
        d_str = e.get("date")
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
            if d >= as_of:
                future_events.append((d, e))
        except ValueError:
            continue

    if not future_events:
        return None

    future_events.sort(key=lambda x: x[0])
    closest_date, closest_event = future_events[0]
    days_until = (closest_date - as_of).days

    return {
        "date": closest_event.get("date"),
        "type": closest_event.get("type"),
        "label": closest_event.get("label"),
        "days_until": days_until,
    }


def analyze_catalyst_for_ticker(
    code: str,
    cat_prov: Any,
    forward_days: int = 90,
    as_of: date | None = None,
) -> dict[str, Any]:
    today = as_of or date.today()
    events = cat_prov.fetch_events(code, forward_days=forward_days)
    next_ev = compute_next_event(events, today)

    return {
        "events": events,
        "next_event": next_ev,
        "forward_horizon_days": forward_days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="台股催化劑事件日曆 (Module C)")
    parser.add_argument("--force", action="store_true", help="強制刷新快取")
    parser.add_argument("--ticker", type=str, default="", help="指定單一標的代碼 (如 3141)")
    parser.add_argument("--horizon", type=int, default=90, help="展望天數 (預設 90)")
    args = parser.parse_args()

    if not args.force and not args.ticker and is_cache_fresh(CACHE_FILE, "catalyst", CACHE_TTL_HOURS):
        logger.info("✅ catalyst cache fresh (< %sh), skipping", CACHE_TTL_HOURS)
        return 0

    cfg = load_tw_market_config()
    cat_prov = get_provider("catalyst", cfg)

    tickers_cfg = cfg.get("tickers", [])
    if args.ticker:
        target_list = [t for t in tickers_cfg if str(t.get("code")).strip() == args.ticker.strip()]
        if not target_list:
            target_list = [{"code": args.ticker.strip(), "market": "twse"}]
    else:
        target_list = tickers_cfg

    logger.info("Generating catalyst calendar for %d tickers via %s (horizon=%dd)...",
                len(target_list), cat_prov.provider_name, args.horizon)

    tickers_output: dict[str, Any] = {}
    if args.ticker and CACHE_FILE.exists():
        try:
            cached_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_cat = cached_data.get("catalyst", {}).get("tickers", {})
            tickers_output.update(cached_cat)
        except Exception:
            pass

    for item in target_list:
        code = str(item.get("code")).strip()
        logger.info("Processing catalyst events for %s...", code)
        try:
            res = analyze_catalyst_for_ticker(code, cat_prov, forward_days=args.horizon)
            tickers_output[code] = res
        except Exception as e:
            logger.warning("Catalyst calendar failed for %s: %s", code, e)
            tickers_output[code] = {
                "events": [],
                "next_event": None,
                "status": "(unavailable)",
                "error": str(e),
            }

    block_payload = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": cat_prov.provider_name,
        "tickers": tickers_output,
        "display_only": True,
    }

    atomic_update_cache("catalyst", block_payload)
    logger.info("💾 Wrote catalyst calendar to %s (status=ok, %d tickers)", CACHE_FILE.relative_to(ROOT), len(tickers_output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
