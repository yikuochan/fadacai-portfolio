#!/usr/bin/env python3
"""
tw_technicals.py — 台股技術面趨勢與動能分析工具 (Module B)。

提供多週期均線排列 (5/10/20/60/120/240 MA)、Stan Weinstein 4 階段循環判定、
Minervini VCP (波動收縮型態) 偵測與突破量確認、ATR(14) 動態停損、
相對強度 (RS) 分數與大盤對比百分位排名。

輸出: briefing-out/cache/tw-market-indicators.json 之 technicals 區塊。
所有指標皆為 display-only（記錄不阻擋）。

Usage:
  python3 tools/tw_technicals.py                        # 全追蹤標的 (TTL 6h)
  python3 tools/tw_technicals.py --force                # 強制刷新
  python3 tools/tw_technicals.py --force --ticker 3141  # 單一標的
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


# ── 計算核心 ────────────────────────────────────────────────────────────────

def compute_moving_averages(closes: list[float]) -> dict[str, float | None]:
    """計算 5/10/20/60/120/240 均線"""
    periods = [5, 10, 20, 60, 120, 240]
    res: dict[str, float | None] = {}
    n = len(closes)
    for p in periods:
        if n >= p:
            res[f"ma{p}"] = round(sum(closes[-p:]) / p, 2)
        else:
            res[f"ma{p}"] = None
    return res


def classify_weinstein_stage(
    price: float,
    ma_dict: dict[str, float | None],
    closes: list[float] | None = None,
) -> tuple[int, str]:
    """
    Stan Weinstein 4 階段循環判定：
    Stage 1（築底）：240MA 走平，股價在 240MA 附近盤整糾結
    Stage 2（主升）：短均線 > 長均線，均線多頭排列
    Stage 3（頭部）：股價跌破 20MA 但在 240MA 上方，均線開始糾結
    Stage 4（主跌）：股價在所有均線下方，空頭排列
    """
    ma5, ma10, ma20 = ma_dict.get("ma5"), ma_dict.get("ma10"), ma_dict.get("ma20")
    ma60, ma120, ma240 = ma_dict.get("ma60"), ma_dict.get("ma120"), ma_dict.get("ma240")

    # 若歷史不足以計算 240MA
    if ma240 is None:
        if ma20 and ma60:
            if price > ma20 > ma60:
                return 2, "主升段"
            elif price < ma20 < ma60:
                return 4, "主跌段"
        return 1, "築底段"

    all_bullish = bool(
        ma5 and ma10 and ma20 and ma60 and ma120 and ma240 and
        (ma5 > ma10 > ma20 > ma60 > ma120 > ma240)
    )
    all_bearish = bool(
        ma5 and ma10 and ma20 and ma60 and ma120 and ma240 and
        (ma5 < ma10 < ma20 < ma60 < ma120 < ma240)
    )

    # 1. 全線多頭排列
    if all_bullish and price >= ma20:
        return 2, "主升段"

    # 2. 空頭排列，股價在 240MA 之下
    if all_bearish or (price < ma240 and ma20 and ma60 and ma20 < ma60 < ma240):
        return 4, "主跌段"

    # 3. 股價跌破 20MA 但仍在 240MA 上方
    if ma20 and price < ma20 and price > ma240:
        return 3, "頭部"

    # 4. 240MA 走平盤整
    # 計算 240MA 斜率（若有足夠收盤價）
    slope_flat = True
    if closes and len(closes) >= 260:
        ma240_prev20 = sum(closes[-260:-20]) / 240
        slope_240 = abs(ma240 - ma240_prev20) / ma240_prev20
        slope_flat = slope_240 < 0.03

    if slope_flat and abs(price - ma240) / ma240 < 0.10:
        return 1, "築底段"

    # 備份趨勢判斷
    if price >= ma240 and ma20 and ma60 and ma20 >= ma60:
        return 2, "主升段"
    elif price < ma240:
        return 4, "主跌段"

    return 1, "築底段"


def detect_vcp(
    series: list[dict],
    min_contractions: int = 2,
    breakout_volume_multiple: float = 1.5,
) -> dict[str, Any]:
    """
    Minervini VCP (Volatility Contraction Pattern) 波動收縮型態偵測。
    """
    if len(series) < 30:
        return {
            "detected": False,
            "contractions": 0,
            "pivot_price": None,
            "breakout_confirmed": False,
            "breakout_volume_ratio": 0.0,
        }

    # 50 日均量計算
    volumes = [b["volume"] for b in series]
    vol_lookback = min(50, len(volumes) - 1)
    if vol_lookback > 0:
        ma50_vol = sum(volumes[-vol_lookback - 1 : -1]) / vol_lookback
    else:
        ma50_vol = volumes[-1]
    latest_vol = volumes[-1]
    vol_ratio = round(latest_vol / ma50_vol, 2) if ma50_vol > 0 else 1.0

    # 掃描近 130 根 K 線波段高低點
    lookback = min(len(series), 130)
    sub = series[-lookback:]

    highs = [b["high"] for b in sub]
    lows = [b["low"] for b in sub]
    closes = [b["close"] for b in sub]

    k = 3
    swings: list[tuple[int, str, float]] = []
    for i in range(k, len(sub) - k):
        is_high = highs[i] == max(highs[i - k : i + k + 1]) and highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
        is_low = lows[i] == min(lows[i - k : i + k + 1]) and lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
        if is_high:
            swings.append((i, "H", highs[i]))
        elif is_low:
            swings.append((i, "L", lows[i]))

    if len(swings) < 4:
        # 放寬條件再試
        for i in range(k, len(sub) - k):
            if highs[i] == max(highs[i - k : i + k + 1]):
                swings.append((i, "H", highs[i]))
            elif lows[i] == min(lows[i - k : i + k + 1]):
                swings.append((i, "L", lows[i]))

    # 合併連續 H 或連續 L
    merged_swings: list[tuple[int, str, float]] = []
    for s in swings:
        if not merged_swings:
            merged_swings.append(s)
            continue
        last = merged_swings[-1]
        if s[1] == last[1]:
            if s[1] == "H" and s[2] > last[2]:
                merged_swings[-1] = s
            elif s[1] == "L" and s[2] < last[2]:
                merged_swings[-1] = s
        else:
            merged_swings.append(s)

    # 計算收縮深幅度 (H -> L)
    contractions: list[tuple[float, float, float]] = []
    for i in range(len(merged_swings) - 1):
        if merged_swings[i][1] == "H" and merged_swings[i + 1][1] == "L":
            h, l = merged_swings[i][2], merged_swings[i + 1][2]
            depth = (h - l) / h
            if depth > 0.02:
                contractions.append((h, l, depth))

    # 驗證收縮序列：每次收縮幅度小於前次之 0.85 (至少 15-25% 收縮)
    valid_c_count = 0
    if len(contractions) >= 2:
        seq = [contractions[-1]]
        for c in reversed(contractions[:-1]):
            prev_c = seq[-1]
            if prev_c[2] <= c[2] * 0.85:
                seq.append(c)
            else:
                break
        if len(seq) >= min_contractions:
            valid_c_count = len(seq)

    detected = valid_c_count >= min_contractions
    pivot_price = round(contractions[-1][0], 2) if contractions else None
    latest_close = closes[-1]
    breakout_confirmed = bool(
        detected and (pivot_price is not None) and
        (latest_close >= pivot_price) and
        (vol_ratio >= breakout_volume_multiple)
    )

    return {
        "detected": detected,
        "contractions": valid_c_count,
        "pivot_price": pivot_price,
        "breakout_confirmed": breakout_confirmed,
        "breakout_volume_ratio": vol_ratio,
    }


def compute_atr(
    series: list[dict],
    period: int = 14,
    stop_multiplier: float = 2.0,
) -> dict[str, float | None]:
    """計算 ATR(14) 及動態停損距離與建議停損價"""
    if len(series) < period + 1:
        return {
            "atr_14": None,
            "stop_multiplier": stop_multiplier,
            "stop_distance": None,
            "suggested_stop": None,
        }

    tr_list: list[float] = []
    for i in range(1, len(series)):
        h = series[i]["high"]
        l = series[i]["low"]
        prev_c = series[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

    atr_14 = round(sum(tr_list[-period:]) / period, 2)
    latest_close = series[-1]["close"]
    stop_distance = round(stop_multiplier * atr_14, 2)
    suggested_stop = round(latest_close - stop_distance, 2)

    return {
        "atr_14": atr_14,
        "stop_multiplier": stop_multiplier,
        "stop_distance": stop_distance,
        "suggested_stop": suggested_stop,
    }


def compute_relative_strength(
    ticker_series: list[dict],
    bench_series: list[dict],
    lookback_days: int = 63,
    benchmark_name: str = "^TWII",
) -> dict[str, Any]:
    """計算相對於大盤基準之 RS 分數"""
    if len(ticker_series) < lookback_days or len(bench_series) < lookback_days:
        return {
            "rs_score": None,
            "rs_percentile": None,
            "benchmark": benchmark_name,
            "lookback_days": lookback_days,
            "status": "(insufficient_data)",
        }

    t_start, t_end = ticker_series[-lookback_days]["close"], ticker_series[-1]["close"]
    b_start, b_end = bench_series[-lookback_days]["close"], bench_series[-1]["close"]

    r_stock = (t_end - t_start) / t_start
    r_bench = (b_end - b_start) / b_start

    # RS 計算：若基準漲幅為正，使用報酬率比值；若基準為負，使用 (1+r_stock)/(1+r_bench)
    if r_bench > 0 and r_stock > 0:
        rs_score = round(r_stock / r_bench, 2)
    else:
        rs_score = round((1.0 + r_stock) / max(0.01, 1.0 + r_bench), 2)

    return {
        "rs_score": rs_score,
        "rs_percentile": 50,  # 後續由所有標的排名更新
        "benchmark": benchmark_name,
        "lookback_days": lookback_days,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────

def analyze_technicals_for_ticker(
    code: str,
    market: str,
    price_prov: Any,
    benchmarks_cache: dict[str, list[dict]],
    options: dict[str, Any],
) -> dict[str, Any]:
    """計算單一標的完整技術面指標"""
    bars = price_prov.fetch_daily(code, days=365)
    if not bars:
        return {
            "ma_alignment": {"status": "(unavailable)"},
            "vcp": {"detected": False, "status": "(unavailable)"},
            "atr": {"status": "(unavailable)"},
            "relative_strength": {"status": "(unavailable)"},
        }

    closes = [b["close"] for b in bars]
    price = closes[-1]

    # 1. 均線排列
    ma_dict = compute_moving_averages(closes)
    all_bullish = bool(
        ma_dict.get("ma5") and ma_dict.get("ma10") and ma_dict.get("ma20") and
        ma_dict.get("ma60") and ma_dict.get("ma120") and ma_dict.get("ma240") and
        (ma_dict["ma5"] > ma_dict["ma10"] > ma_dict["ma20"] >
         ma_dict["ma60"] > ma_dict["ma120"] > ma_dict["ma240"])
    )
    stage, stage_label = classify_weinstein_stage(price, ma_dict, closes)
    ma_alignment = {
        **ma_dict,
        "all_bullish": all_bullish,
        "weinstein_stage": stage,
        "stage_label": stage_label,
    }

    # 2. VCP
    min_c = options.get("vcp_min_contractions", 2)
    vol_mult = options.get("breakout_volume_multiple", 1.5)
    vcp_result = detect_vcp(bars, min_contractions=min_c, breakout_volume_multiple=vol_mult)

    # 3. ATR
    stop_mult = options.get("stop_multiplier", 2.0)
    atr_result = compute_atr(bars, period=14, stop_multiplier=stop_mult)

    # 4. Relative Strength
    bench_sym = "^TWOII" if market == "tpex" else "^TWII"
    bench_bars = benchmarks_cache.get(bench_sym, [])
    lookback = options.get("rs_lookback_days", 63)
    rs_result = compute_relative_strength(bars, bench_bars, lookback_days=lookback, benchmark_name=bench_sym)

    return {
        "ma_alignment": ma_alignment,
        "vcp": vcp_result,
        "atr": atr_result,
        "relative_strength": rs_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="台股技術面趨勢與動能 (Module B)")
    parser.add_argument("--force", action="store_true", help="強制刷新快取")
    parser.add_argument("--ticker", type=str, default="", help="指定單一標的代碼 (如 3141)")
    args = parser.parse_args()

    if not args.force and not args.ticker and is_cache_fresh(CACHE_FILE, "technicals", CACHE_TTL_HOURS):
        logger.info("✅ technicals cache fresh (< %sh), skipping", CACHE_TTL_HOURS)
        return 0

    cfg = load_tw_market_config()
    price_prov = get_provider("price_history", cfg)
    tech_options = cfg.get("technical_options", {})

    # 建立標的與基準快取
    benchmarks_cache: dict[str, list[dict]] = {}
    for bench_sym in ("^TWII", "^TWOII"):
        try:
            b_bars = price_prov.fetch_daily(bench_sym, days=365)
            if b_bars:
                benchmarks_cache[bench_sym] = b_bars
        except Exception as e:
            logger.warning("Failed to fetch benchmark %s: %s", bench_sym, e)

    tickers_cfg = cfg.get("tickers", [])
    if args.ticker:
        target_list = [t for t in tickers_cfg if str(t.get("code")).strip() == args.ticker.strip()]
        if not target_list:
            target_list = [{"code": args.ticker.strip(), "market": "twse"}]
    else:
        target_list = tickers_cfg

    logger.info("Calculating technicals for %d tickers via %s...", len(target_list), price_prov.provider_name)

    tickers_output: dict[str, Any] = {}
    if args.ticker and CACHE_FILE.exists():
        try:
            cached_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_tech = cached_data.get("technicals", {}).get("tickers", {})
            tickers_output.update(cached_tech)
        except Exception:
            pass

    for item in target_list:
        code = str(item.get("code")).strip()
        mkt = str(item.get("market", "twse")).strip().lower()
        logger.info("Processing technicals for %s (%s)...", code, mkt)
        try:
            res = analyze_technicals_for_ticker(
                code=code,
                market=mkt,
                price_prov=price_prov,
                benchmarks_cache=benchmarks_cache,
                options=tech_options,
            )
            tickers_output[code] = res
        except Exception as e:
            logger.warning("Technical analysis failed for %s: %s", code, e)
            tickers_output[code] = {
                "ma_alignment": {"status": "(unavailable)"},
                "vcp": {"detected": False, "status": "(unavailable)"},
                "atr": {"status": "(unavailable)"},
                "relative_strength": {"status": "(unavailable)"},
                "error": str(e),
            }

    # 計算全組合 RS 百分位排名 (0-99)
    valid_rs = [
        (code, d["relative_strength"]["rs_score"])
        for code, d in tickers_output.items()
        if d.get("relative_strength", {}).get("rs_score") is not None
    ]
    if len(valid_rs) > 1:
        valid_rs.sort(key=lambda x: x[1])
        n = len(valid_rs)
        for rank, (code, _) in enumerate(valid_rs):
            pct = int(round(100.0 * rank / (n - 1)))
            tickers_output[code]["relative_strength"]["rs_percentile"] = min(99, pct)
    elif len(valid_rs) == 1:
        tickers_output[valid_rs[0][0]]["relative_strength"]["rs_percentile"] = 50

    block_payload = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": price_prov.provider_name,
        "tickers": tickers_output,
        "display_only": True,
    }

    atomic_update_cache("technicals", block_payload)
    logger.info("💾 Wrote technicals to %s (status=ok, %d tickers)", CACHE_FILE.relative_to(ROOT), len(tickers_output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
