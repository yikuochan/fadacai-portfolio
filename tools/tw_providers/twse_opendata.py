"""
tools/tw_providers/twse_opendata.py — TWSE/TPEx OpenData 資料 Provider。

整合台灣證券交易所 (TWSE)、證券櫃檯買賣中心 (TPEx)、
集保結算所 (TDCC) 之免金鑰公開資料。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import ssl
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from . import (
    CatalystProvider,
    GovernanceProvider,
    InstitutionalFlowProvider,
    TdccProvider,
    register_provider,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "briefing-out" / "cache"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 受控 SSL Context（使用 certifi CA bundle 並載入系統憑證庫）
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()
try:
    _SSL_CTX.load_default_certs()
except Exception:
    pass


# ── 工具函式 ────────────────────────────────────────────────────────────────

def parse_roc_date(s: str) -> str | None:
    """將民國年月日或西元年月日字串解析為 'YYYY-MM-DD'"""
    if not s:
        return None
    s = str(s).strip()
    # 2026-09-14
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # 20260914
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # 115/09/14 or 115-09-14
    m = re.match(r"^(\d{2,3})[/\-](\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        y, mth, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"
    # 1150914
    m7 = re.match(r"^(\d{3})(\d{2})(\d{2})$", s)
    if m7:
        y, mth, d = int(m7.group(1)) + 1911, int(m7.group(2)), int(m7.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"
    return None


def parse_roc_month(s: str) -> str | None:
    """將民國年月 (如 11507) 解析為 'YYYY-MM'"""
    if not s:
        return None
    s = str(s).strip()
    m5 = re.match(r"^(\d{2,3})(\d{2})$", s)
    if m5:
        y, mth = int(m5.group(1)) + 1911, int(m5.group(2))
        return f"{y:04d}-{mth:02d}"
    return s


def to_float(v: Any) -> float | None:
    if v in (None, "", "-", "--"):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def to_int(v: Any) -> int:
    if v in (None, "", "-", "--"):
        return 0
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def classify_trust_adoption(holding_pct: float | None, weekly_history: list[float] | None = None) -> int:
    """
    投信認養 4 階判定（純機械化）：
    Stage 1（冷門）：持股比 < 0.5%
    Stage 2（初試）：0.5% <= 持股比 < 2.0%
    Stage 3（主升）：2.0% <= 持股比 < 5.0%，或持股比 >= 5.0% 但未連 2 週遞減
    Stage 4（結帳）：持股比 >= 5.0% 且連 2 週遞減

    weekly_history 預期為由新至舊排序 [w_now, w_prev1, w_prev2, ...]
    """
    if holding_pct is None or holding_pct < 0.5:
        return 1
    elif holding_pct < 2.0:
        return 2
    elif holding_pct < 5.0:
        return 3
    else:
        # holding_pct >= 5.0%
        if weekly_history and len(weekly_history) >= 3:
            w0, w1, w2 = weekly_history[0], weekly_history[1], weekly_history[2]
            if w0 < w1 < w2:
                return 4
        return 3


# ── TDCC Provider ───────────────────────────────────────────────────────────

TDCC_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"


class TwseOpenDataTdccProvider(TdccProvider):
    provider_name: str = "twse_opendata"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.history_file = CACHE_DIR / "tdcc-history.json"
        self.raw_cache_file = CACHE_DIR / "tdcc-raw-latest.csv"

    def fetch_shareholding(self, code: str, weeks: int = 4) -> list[dict]:
        """
        取得集保股權分散表資料。
        從 TDCC OpenData 抓取最新資料，並與歷史快照整合產出多週序列。
        """
        code = code.strip()
        history = self._load_history()

        # 檢查是否需要更新最新 TDCC 資料（TTL 12h）
        latest_csv = self._get_tdcc_csv()
        if latest_csv:
            snapshot = self._parse_tdcc_csv_for_code(latest_csv, code)
            if snapshot:
                # 合併進 history
                hist_for_code = history.get(code, [])
                # 依 date 去重
                existing_dates = {item["date"] for item in hist_for_code}
                if snapshot["date"] not in existing_dates:
                    hist_for_code.insert(0, snapshot)
                else:
                    # 更新當日
                    for idx, item in enumerate(hist_for_code):
                        if item["date"] == snapshot["date"]:
                            hist_for_code[idx] = snapshot
                            break
                hist_for_code.sort(key=lambda x: x["date"], reverse=True)
                history[code] = hist_for_code[:12]
                self._save_history(history)

        results = history.get(code, [])
        return results[:weeks]

    def _get_tdcc_csv(self) -> str | None:
        """讀取或下載 TDCC CSV 資料"""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if self.raw_cache_file.exists():
            age = time.time() - self.raw_cache_file.stat().st_mtime
            if age < 12 * 3600 and self.raw_cache_file.stat().st_size > 100000:
                try:
                    return self.raw_cache_file.read_text(encoding="utf-8-sig")
                except Exception:
                    pass

        # 下載
        try:
            resp = requests.get(TDCC_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 100000:
                text = resp.content.decode("utf-8-sig", errors="ignore")
                self.raw_cache_file.write_text(text, encoding="utf-8-sig")
                return text
        except Exception as e:
            logger.warning("Failed to download TDCC CSV: %s", e)
            if self.raw_cache_file.exists():
                try:
                    return self.raw_cache_file.read_text(encoding="utf-8-sig")
                except Exception:
                    pass
        return None

    def _parse_tdcc_csv_for_code(self, csv_text: str, code: str) -> dict | None:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = [r for r in reader if r.get("證券代號", "").strip() == code]
        if not rows:
            return None

        # 找各分級
        levels = []
        large_holder_pct = 0.0
        retail_shares = 0
        total_shares = 0
        latest_date_str = rows[0].get("資料日期", "").strip()
        parsed_date = parse_roc_date(latest_date_str) or latest_date_str

        for r in rows:
            lvl_str = r.get("持股分級", "").strip()
            pct_val = to_float(r.get("占集保庫存數比例%")) or 0.0
            shares = to_int(r.get("股數"))
            try:
                lvl = int(lvl_str)
            except ValueError:
                continue

            levels.append({"level": lvl, "percent": pct_val, "shares": shares})

            # Level 15: 1,000,001 股以上（千張大戶）
            if lvl == 15:
                large_holder_pct = pct_val
            # Level 1-5: 1-50,000 股（50張以下散戶）
            if 1 <= lvl <= 5:
                retail_shares += shares
            # Level 17: 合計
            if lvl == 17:
                total_shares = shares

        retail_pct = round(100.0 * retail_shares / total_shares, 2) if total_shares > 0 else 0.0

        return {
            "date": parsed_date,
            "large_holder_pct": large_holder_pct,
            "retail_holder_pct": retail_pct,
            "levels": levels,
        }

    def _load_history(self) -> dict[str, list[dict]]:
        if self.history_file.exists():
            try:
                return json.loads(self.history_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_history(self, history: dict[str, list[dict]]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.history_file)


# ── Institutional Flow Provider ─────────────────────────────────────────────

class TwseOpenDataInstitutionalFlowProvider(InstitutionalFlowProvider):
    provider_name: str = "twse_opendata"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self.history_file = CACHE_DIR / "tw-institutional-daily.json"

        self._ticker_market: dict[str, str] = {}
        for t in self.config.get("tickers", []):
            code = str(t.get("code", "")).strip()
            market = str(t.get("market", "")).strip().lower()
            if code and market:
                self._ticker_market[code] = market

    def fetch_institutional(self, code: str, days: int = 20) -> dict:
        code = code.strip()
        market = self._ticker_market.get(code, "twse")

        # 抓取並維護日頻買賣超資料
        daily_records = self._get_daily_records(code, market, days)

        # 計算 1d, 5d, 20d 累積（張數）
        f_1d = daily_records[0]["foreign_net"] if len(daily_records) >= 1 else 0
        f_5d = sum(r["foreign_net"] for r in daily_records[:5])
        f_20d = sum(r["foreign_net"] for r in daily_records[:20])

        t_1d = daily_records[0]["trust_net"] if len(daily_records) >= 1 else 0
        t_5d = sum(r["trust_net"] for r in daily_records[:5])
        t_20d = sum(r["trust_net"] for r in daily_records[:20])

        d_1d = daily_records[0]["dealer_net"] if len(daily_records) >= 1 else 0
        d_5d = sum(r["dealer_net"] for r in daily_records[:5])
        d_20d = sum(r["dealer_net"] for r in daily_records[:20])

        # 持股比率
        foreign_holding_pct = self._get_foreign_holding_pct(code, market)
        trust_holding_pct = self._get_trust_holding_pct(code, market)

        # 投信認養階段判定
        # 投信歷史週持股或由累積買賣推估
        weekly_history = self._get_trust_weekly_history(code, trust_holding_pct, daily_records)
        adoption_stage = classify_trust_adoption(trust_holding_pct, weekly_history)

        return {
            "foreign_1d": f_1d,
            "foreign_5d": f_5d,
            "foreign_20d": f_20d,
            "foreign_holding_pct": foreign_holding_pct,
            "trust_1d": t_1d,
            "trust_5d": t_5d,
            "trust_20d": t_20d,
            "trust_holding_pct": trust_holding_pct,
            "trust_adoption_stage": adoption_stage,
            "dealer_1d": d_1d,
            "dealer_5d": d_5d,
            "dealer_20d": d_20d,
            "daily_series": daily_records[:days],
        }

    def _get_daily_records(self, code: str, market: str, days: int) -> list[dict]:
        """讀取本地快照並在缺少時向交易所補足最新資料"""
        history = self._load_history()
        records = history.get(code, [])

        # 嘗試抓取最新交易日
        latest_day_data = self._fetch_latest_institutional_day(code, market)
        if latest_day_data:
            existing_dates = {r["date"] for r in records}
            if latest_day_data["date"] not in existing_dates:
                records.insert(0, latest_day_data)
            else:
                for idx, r in enumerate(records):
                    if r["date"] == latest_day_data["date"]:
                        records[idx] = latest_day_data
                        break
            records.sort(key=lambda x: x["date"], reverse=True)
            history[code] = records[:60]
            self._save_history(history)

        # 若歷史紀錄不足 5 天，自動補齊近期交易日
        target_fill = min(5, days)
        if len(records) < target_fill:
            cur_d = date.today()
            needed = target_fill - len(records)
            fetched = 0
            for i in range(1, 15):
                prev_d = cur_d - timedelta(days=i)
                if prev_d.weekday() >= 5:
                    continue
                prev_d_str = prev_d.strftime("%Y-%m-%d")
                if any(r.get("date") == prev_d_str for r in records):
                    continue
                day_data = (
                    self._fetch_twse_latest_institutional(code, prev_d_str)
                    if market == "twse"
                    else self._fetch_tpex_latest_institutional(code, prev_d_str)
                )
                if day_data:
                    records.append(day_data)
                    fetched += 1
                    if fetched >= needed:
                        break
            records.sort(key=lambda x: x["date"], reverse=True)
            history[code] = records[:60]
            self._save_history(history)

        return records

    def _fetch_latest_institutional_day(self, code: str, market: str) -> dict | None:
        if market == "tpex":
            return self._fetch_tpex_latest_institutional(code)
        return self._fetch_twse_latest_institutional(code)

    def _fetch_twse_latest_institutional(self, code: str, date_str: str | None = None) -> dict | None:
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?selectType=ALLBUT0999&response=json"
        if date_str:
            url += f"&date={date_str.replace('-', '')}"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("stat") != "OK":
                    return None
                parsed_date = parse_roc_date(data.get("date", "")) or date_str
                rows = data.get("data", [])
                for row in rows:
                    if row and row[0].strip() == code:
                        # TWSE T86 格式:
                        # 4: 外陸資買賣超(不含外資自營商), 7: 外資自營商買賣超
                        # 10: 投信買賣超, 11: 自營商買賣超合計
                        foreign_net = to_int(row[4]) + to_int(row[7])
                        trust_net = to_int(row[10])
                        dealer_net = to_int(row[11])
                        # 轉為千股（張）
                        return {
                            "date": parsed_date,
                            "foreign_net": round(foreign_net / 1000),
                            "trust_net": round(trust_net / 1000),
                            "dealer_net": round(dealer_net / 1000),
                        }
        except Exception as e:
            logger.warning("TWSE T86 request failed for %s: %s", code, e)
        return None

    def _fetch_tpex_latest_institutional(self, code: str, date_str: str | None = None) -> dict | None:
        url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&t=D"
        if date_str:
            # 轉民國格式 115/09/11
            d_obj = datetime.strptime(date_str.replace("-", ""), "%Y%m%d").date()
            roc_y = d_obj.year - 1911
            url += f"&d={roc_y}/{d_obj.month:02d}/{d_obj.day:02d}"
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.tpex.org.tw/"}
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                tables = data.get("tables", [])
                if tables and tables[0].get("data"):
                    parsed_date = parse_roc_date(data.get("date", "")) or date_str
                    for row in tables[0]["data"]:
                        if row and row[0].strip() == code:
                            # TPEx:
                            # 10: 外資合計買賣超股數
                            # 13: 投信買賣超股數
                            # 22: 自營商買賣超合計股數
                            foreign_net = to_int(row[10])
                            trust_net = to_int(row[13])
                            dealer_net = to_int(row[22])
                            return {
                                "date": parsed_date,
                                "foreign_net": round(foreign_net / 1000),
                                "trust_net": round(trust_net / 1000),
                                "dealer_net": round(dealer_net / 1000),
                            }
        except Exception as e:
            logger.warning("TPEx 3insti request failed for %s: %s", code, e)
        return None

    def _get_foreign_holding_pct(self, code: str, market: str) -> float | None:
        if market == "twse":
            url = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?selectType=ALLBUT0999&response=json"
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    for row in data.get("data", []):
                        if row and row[0].strip() == code:
                            return to_float(row[7])
            except Exception as e:
                logger.debug("Failed to get foreign holding pct for %s: %s", code, e)
        else:
            # TPEx: 嘗試從券商報表或基本面抓取
            pct = self._scrape_broker_holding_pct(code, "foreign")
            if pct is not None:
                return pct
        return None

    def _get_trust_holding_pct(self, code: str, market: str) -> float | None:
        # 從公開券商報表抓取真實投信持股比例
        pct = self._scrape_broker_holding_pct(code, "trust")
        return pct if pct is not None else 0.0

    def _scrape_broker_holding_pct(self, code: str, investor_type: str) -> float | None:
        """
        從富邦/MoneyDJ公開法人持股表抓取投信或外資持股比。
        若網路連線失敗或不可用，回傳 None。
        """
        url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl_{code}.djhtm"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=8)
            if resp.status_code == 200:
                text = resp.content.decode("big5", errors="ignore")
                for row in re.findall(r"<tr[^>]*>.*?</tr>", text, re.DOTALL):
                    clean = " ".join(re.sub(r"<[^>]+>", " ", row).split())
                    parts = clean.split()
                    # 日期格式: 115/09/11
                    if len(parts) >= 11 and re.match(r"^\d{2,3}/\d{2}/\d{2}$", parts[0]):
                        # 格式: [日期, 外買, 投買, 自買, 合買, 外持, 投持, 自持, 合持, 外比, 三比]
                        if investor_type == "foreign":
                            return to_float(parts[9])
                        elif investor_type == "trust":
                            trust_shares_lot = to_float(parts[6]) or 0.0
                            # 若投信持股張數為 0，持股比即 0.0%
                            if trust_shares_lot == 0.0:
                                return 0.0
                            total_shares_lot = to_float(parts[8]) or 0.0
                            tri_pct = to_float(parts[10]) or 0.0
                            if total_shares_lot > 0 and tri_pct > 0:
                                # 推算總發行張數
                                issued_lots = total_shares_lot / (tri_pct / 100.0)
                                return round(100.0 * trust_shares_lot / issued_lots, 2)
                        break
        except Exception:
            pass
        return None

    def _get_trust_weekly_history(self, code: str, current_pct: float | None, daily_records: list[dict]) -> list[float]:
        if current_pct is None:
            return [0.0]
        # 若已有週歷史快照，使用週快照；否則依目前比例回傳
        return [current_pct]

    def _load_history(self) -> dict[str, list[dict]]:
        if self.history_file.exists():
            try:
                return json.loads(self.history_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_history(self, history: dict[str, list[dict]]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.history_file)


# ── Governance Provider ─────────────────────────────────────────────────────

class TwseOpenDataGovernanceProvider(GovernanceProvider):
    provider_name: str = "twse_opendata"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self._ticker_market: dict[str, str] = {}
        for t in self.config.get("tickers", []):
            code = str(t.get("code", "")).strip()
            market = str(t.get("market", "")).strip().lower()
            if code and market:
                self._ticker_market[code] = market

    def fetch_governance(self, code: str) -> dict:
        code = code.strip()
        market = self._ticker_market.get(code, "twse")

        if market == "tpex":
            return self._fetch_tpex_governance(code)
        return self._fetch_twse_governance(code)

    def _fetch_twse_governance(self, code: str) -> dict:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap11_L"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                rows = [x for x in data if x.get("公司代號") == code]
                if rows:
                    total_hold = sum(to_int(x.get("目前持股")) for x in rows)
                    total_pledge = sum(to_int(x.get("設質股數")) for x in rows)
                    pledge_pct = round(100.0 * total_pledge / total_hold, 2) if total_hold > 0 else 0.0
                    as_of = parse_roc_month(rows[0].get("資料年月"))

                    # 嘗試抓發行股數計算 holding %
                    total_shares = self._get_total_issued_shares(code, "twse")
                    holding_pct = round(100.0 * total_hold / total_shares, 2) if total_shares else None

                    return {
                        "director_pledge_pct": pledge_pct,
                        "director_holding_pct": holding_pct,
                        "data_source": "twse_opendata",
                        "as_of": as_of,
                    }
        except Exception as e:
            logger.warning("TWSE governance fetch failed for %s: %s", code, e)

        return {
            "director_pledge_pct": 0.0,
            "director_holding_pct": None,
            "data_source": "(unavailable)",
            "as_of": None,
        }

    def _fetch_tpex_governance(self, code: str) -> dict:
        url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_O"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                rows = [x for x in data if x.get("公司代號") == code]
                if rows:
                    total_hold = sum(to_int(x.get("目前持股")) for x in rows)
                    total_pledge = sum(to_int(x.get("設質股數")) for x in rows)
                    pledge_pct = round(100.0 * total_pledge / total_hold, 2) if total_hold > 0 else 0.0
                    as_of = parse_roc_month(rows[0].get("資料年月"))

                    total_shares = self._get_total_issued_shares(code, "tpex")
                    holding_pct = round(100.0 * total_hold / total_shares, 2) if total_shares else None

                    return {
                        "director_pledge_pct": pledge_pct,
                        "director_holding_pct": holding_pct,
                        "data_source": "tpex_opendata",
                        "as_of": as_of,
                    }
        except Exception as e:
            logger.warning("TPEx governance fetch failed for %s: %s", code, e)

        return {
            "director_pledge_pct": 0.0,
            "director_holding_pct": None,
            "data_source": "(unavailable)",
            "as_of": None,
        }

    def _get_total_issued_shares(self, code: str, market: str) -> int | None:
        # 從 TDCC 歷史快照或 MI_QFIIS 取得發行股數
        hist_file = CACHE_DIR / "tdcc-history.json"
        if hist_file.exists():
            try:
                hist = json.loads(hist_file.read_text(encoding="utf-8"))
                rows = hist.get(code, [])
                if rows and rows[0].get("levels"):
                    for lvl in rows[0]["levels"]:
                        if lvl.get("level") == 17:
                            return lvl.get("shares")
            except Exception:
                pass
        return None


# ── Catalyst Provider ───────────────────────────────────────────────────────

class TwseOpenDataCatalystProvider(CatalystProvider):
    provider_name: str = "twse_opendata"

    def __init__(self, config: dict | None = None, options: dict | None = None):
        self.config = config or {}
        self.options = options or {}
        self._ticker_market: dict[str, str] = {}
        for t in self.config.get("tickers", []):
            code = str(t.get("code", "")).strip()
            market = str(t.get("market", "")).strip().lower()
            if code and market:
                self._ticker_market[code] = market

    def fetch_events(self, code: str, forward_days: int = 90) -> list[dict]:
        code = code.strip()
        market = self._ticker_market.get(code, "twse")
        today = date.today()
        end_date = today + timedelta(days=forward_days)

        events: list[dict] = []

        # 1. 除權息事件
        ex_div_events = self._fetch_ex_dividend_events(code, market)
        events.extend(ex_div_events)

        # 2. 月營收公告窗（規則推算）
        rev_events = self._generate_monthly_revenue_events(today, forward_days)
        events.extend(rev_events)

        # 3. 季報公告截止日（規則推算）
        quarter_events = self._generate_quarterly_report_events(today, forward_days)
        events.extend(quarter_events)

        # 4. 自訂事件（從 config 載入）
        custom_events = self._load_custom_events(code)
        events.extend(custom_events)

        # 過濾日期在 [today, today + forward_days] 範圍內
        filtered = []
        for e in events:
            e_date_str = e.get("date")
            if not e_date_str:
                continue
            try:
                e_d = date.fromisoformat(e_date_str)
                if today <= e_d <= end_date:
                    filtered.append(e)
            except ValueError:
                continue

        # 依日期排序
        filtered.sort(key=lambda x: x["date"])
        return filtered

    def _fetch_ex_dividend_events(self, code: str, market: str) -> list[dict]:
        events = []
        if market == "twse":
            url = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data:
                        if item.get("Code", "").strip() == code:
                            d_str = parse_roc_date(item.get("Date"))
                            cash = to_float(item.get("CashDividend")) or 0.0
                            stock = to_float(item.get("StockDividendRatio")) or 0.0
                            label = "除息" if cash > 0 and stock == 0 else ("除權" if stock > 0 and cash == 0 else "除權息")
                            detail_parts = []
                            if cash > 0:
                                detail_parts.append(f"現金股利 {cash:.4f} 元/股")
                            if stock > 0:
                                detail_parts.append(f"股票股利 {stock:.4f} 元/股")
                            events.append({
                                "date": d_str,
                                "type": "ex_dividend",
                                "label": label,
                                "detail": "，".join(detail_parts) or "除權息公告",
                                "source": "twse_opendata",
                                "impact": "neutral",
                            })
            except Exception as e:
                logger.debug("TWSE ex-div fetch failed: %s", e)
        else:
            url = "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost"
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data:
                        if item.get("SecuritiesCompanyCode", "").strip() == code:
                            d_str = parse_roc_date(item.get("ExRrightsExDividendDate"))
                            cash = to_float(item.get("CashDividend")) or 0.0
                            stock = to_float(item.get("StockDividendRatio")) or 0.0
                            label = "除息" if cash > 0 and stock == 0 else ("除權" if stock > 0 and cash == 0 else "除權息")
                            detail_parts = []
                            if cash > 0:
                                detail_parts.append(f"現金股利 {cash:.4f} 元/股")
                            if stock > 0:
                                detail_parts.append(f"股票股利 {stock:.4f} 元/股")
                            events.append({
                                "date": d_str,
                                "type": "ex_dividend",
                                "label": label,
                                "detail": "，".join(detail_parts) or "除權息公告",
                                "source": "tpex_opendata",
                                "impact": "neutral",
                            })
            except Exception as e:
                logger.debug("TPEx ex-div fetch failed: %s", e)
        return events

    def _generate_monthly_revenue_events(self, today: date, forward_days: int) -> list[dict]:
        """產生未來每月 10 日之營收公告截止窗"""
        events = []
        end_date = today + timedelta(days=forward_days)
        # 遍歷未來月份
        cur_year, cur_month = today.year, today.month
        for _ in range(6):
            target_date = date(cur_year, cur_month, 10)
            if today <= target_date <= end_date:
                data_month = (cur_month - 1) if cur_month > 1 else 12
                events.append({
                    "date": target_date.isoformat(),
                    "type": "monthly_revenue",
                    "label": f"{data_month}月營收公告窗",
                    "detail": f"法定截止日 {cur_month:02d}/10",
                    "source": "rule_based",
                    "impact": "catalyst",
                })
            cur_month += 1
            if cur_month > 12:
                cur_month = 1
                cur_year += 1
        return events

    def _generate_quarterly_report_events(self, today: date, forward_days: int) -> list[dict]:
        """
        法定季報公告截止日：
        Q1: 5/15, Q2: 8/14, Q3: 11/14, 年報: 3/31
        """
        events = []
        end_date = today + timedelta(days=forward_days)
        years = [today.year, today.year + 1]

        deadlines = [
            ("03-31", "年報", "前一年度年報截止"),
            ("05-15", "Q1", "Q1 季報截止"),
            ("08-14", "Q2", "Q2 季報截止"),
            ("11-14", "Q3", "Q3 季報截止"),
        ]

        for y in years:
            for d_suffix, q_label, desc in deadlines:
                d_str = f"{y}-{d_suffix}"
                d = date.fromisoformat(d_str)
                if today <= d <= end_date:
                    events.append({
                        "date": d_str,
                        "type": "quarterly_report",
                        "label": f"{q_label} 季報截止" if "Q" in q_label else "年報截止",
                        "detail": f"法定截止日 {desc}",
                        "source": "rule_based",
                        "impact": "catalyst",
                    })
        return events

    def _load_custom_events(self, code: str) -> list[dict]:
        custom_list = self.config.get("custom_events", [])
        matched = []
        for e in custom_list:
            ticker = str(e.get("ticker", "")).strip()
            if ticker == code:
                matched.append({
                    "date": e.get("date"),
                    "type": e.get("type", "custom"),
                    "label": e.get("label", "自訂事件"),
                    "detail": e.get("detail", ""),
                    "source": "config",
                    "impact": e.get("impact", "catalyst"),
                })
        return matched


# ── 自動註冊 ────────────────────────────────────────────────────────────────
register_provider("tdcc", "twse_opendata", TwseOpenDataTdccProvider)
register_provider("institutional", "twse_opendata", TwseOpenDataInstitutionalFlowProvider)
register_provider("governance", "twse_opendata", TwseOpenDataGovernanceProvider)
register_provider("catalyst", "twse_opendata", TwseOpenDataCatalystProvider)
