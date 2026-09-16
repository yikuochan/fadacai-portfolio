#!/usr/bin/env python3
"""
parse_broker_reports.py — 本地券商研報快速檢索與共識數據抽取工具。

針對 research/analyst_reports/ 下的大量券商研報（Markdown 格式），
使用高效檔案過濾與正則/文字解析，零/極低 Token 抽取：
1. 券商名稱 (Broker: 大摩、高盛、富邦、元大、國泰、凱基、玉山等)
2. 報告日期 (Report Date)
3. 投資評級 (Rating: Buy / 買進 / Overweight / Outperform / Neutral 等)
4. 目標價 (Target Price NT$)
5. 預估 EPS (Forward EPS / 2026 EPS / 2027 EPS 等)

提供 CLI 與模組化 Python 介面供 tw_stock_analysis.py 計算 A2/A3 估值錨點。

Usage:
  python3 tools/parse_broker_reports.py --ticker 3665
  python3 tools/parse_broker_reports.py --ticker 3665 --json
  python3 tools/parse_broker_reports.py --ticker 2330 --days 90
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = ROOT / "research" / "analyst_reports"

# 券商名稱對應字典
BROKER_MAPPING: dict[str, list[str]] = {
    "Morgan Stanley": ["Morgan Stanley", "大摩", "MS-", "ms_", "MS."],
    "Goldman Sachs": ["Goldman Sachs", "高盛", "GS-", "GS.", "GS_"],
    "Citi": ["Citi", "花旗", "Citigroup"],
    "BofA": ["BofA", "美銀", "Bank of America"],
    "Daiwa": ["Daiwa", "大和"],
    "Macquarie": ["Macquarie", "麥格理"],
    "UBS": ["UBS", "瑞銀"],
    "JPMorgan": ["JPMorgan", "JP-", "小摩", "摩根大通"],
    "Aletheia": ["Aletheia", "真理資本"],
    "元大": ["元大", "Yuanta"],
    "富邦": ["富邦", "Fubon"],
    "國泰": ["國泰", "Cathay"],
    "凱基": ["凱基", "KGI"],
    "玉山": ["玉山", "E.SUN", "E-SUN", "ESUN"],
    "群益": ["群益", "Capital"],
    "統一": ["統一", "President"],
    "第一金": ["第一金", "First Financial"],
    "華南": ["華南", "Hua Nan", "HNCB"],
    "永豐": ["永豐", "SinoPac", "Sinopac"],
    "中信": ["中信", "CTBC"],
    "兆豐": ["兆豐", "Mega"],
    "康和": ["康和", "Concord"],
    "國票": ["國票", "IBT"],
    "宏遠": ["宏遠", "HonSec"],
    "元富": ["元富", "MasterLink"],
    "台新": ["台新", "Taishin"],
    "合庫": ["合庫", "TCB"],
    "德意志": ["Deutsche", "德意志"],
    "野村": ["Nomura", "野村"],
    "廣發": ["廣發", "GF"],
}

# 評級分類標準化
RATING_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(Overweight|OW|Outperform|增加持股|加碼|強力買進)\b", re.I), "Buy"),
    (re.compile(r"\b(Trading Buy)\b", re.I), "Trading Buy"),
    (re.compile(r"\b(Buy|買進)\b", re.I), "Buy"),
    (re.compile(r"\b(Neutral|中立|Hold|持有|Equal-weight|EW|In-Line|Market-Weight)\b", re.I), "Neutral"),
    (re.compile(r"\b(Underweight|UW|Sell|賣出|降低持股|減碼)\b", re.I), "Sell"),
]

# 非個股研究報告的排除關鍵字 (晨報/日報/產業匯總等)
MACRO_EXCLUDE_KEYWORDS = [
    "日報", "週報", "月報", "特刊", "哈燒新聞", "晨報", "盤後", "每日", "TWDaily", "Daily", "Weekly", "Monthly",
    "ETF", "指數", "審核結果", "策略展望",
]


def extract_broker(filename: str, content_head: str) -> str:
    """識別報告出具之券商名稱"""
    for broker_name, aliases in BROKER_MAPPING.items():
        for alias in aliases:
            if alias.lower() in filename.lower():
                return broker_name

    for broker_name, aliases in BROKER_MAPPING.items():
        for alias in aliases:
            if alias.lower() in content_head.lower():
                return broker_name

    return "Other Broker"


def extract_report_date(filename: str, content_head: str) -> str | None:
    """提取報告發布日期 (格式: YYYY-MM-DD)"""
    # 1. 檔名中標準 YYYYMMDD (例如 20260824, 2026-08-24, 2026_08_24, 2026.08.24)
    m = re.search(r"(202[4-7])[\-_/.]?(0[1-9]|1[0-2])[\-_/.]?(0[1-9]|[12][0-9]|3[01])", filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # 2. 內容前段中文日期：2026 年 8 月 24 日 或 2026/08/24 或 2026.08.24 或 2026-08-24
    m_zh = re.search(r"(202[4-7])[\s年\-/._]+(0?[1-9]|1[0-2])[\s月\-/._]+([12][0-9]|3[01]|0?[1-9])\s*日?", content_head)
    if m_zh:
        y, month, d = int(m_zh.group(1)), int(m_zh.group(2)), int(m_zh.group(3))
        return f"{y:04d}-{month:02d}-{d:02d}"

    # 3. 英文月份格式：September 6, 2026 或 16 July 2026 或 Sep 4, 2026
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m_en1 = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+([0-9]{1,2}),?\s+(202[4-7])\b", content_head, re.I)
    if m_en1:
        m_str = m_en1.group(1).lower()
        if m_str in month_names:
            return f"{int(m_en1.group(3)):04d}-{month_names[m_str]:02d}-{int(m_en1.group(2)):02d}"

    m_en2 = re.search(r"\b([0-9]{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(202[4-7])\b", content_head, re.I)
    if m_en2:
        m_str = m_en2.group(2).lower()
        if m_str in month_names:
            return f"{int(m_en2.group(3)):04d}-{month_names[m_str]:02d}-{int(m_en2.group(1)):02d}"

    # 4. 年月目錄 fallback (若路徑在 202608 之下)
    m_dir = re.search(r"/(202[4-7])(0[1-9]|1[0-2])/", filename)
    if m_dir:
        return f"{m_dir.group(1)}-{m_dir.group(2)}-01"

    return None


def extract_rating(content_head: str) -> str | None:
    """提取投資評等"""
    for pat, label in RATING_MAP:
        if pat.search(content_head):
            return label
    return None


def extract_target_price(content: str) -> float | None:
    """從報告內文中提取目標價 (NT$)"""
    head = content[:4000]

    tp_patterns = [
        # 12-month target TWD4,200.00 / Target: NT$3,100.00 / 目標價 (12 個月)：NT$2850.0
        r"(?:12\s*[-–]?\s*month\s*target|Price\s*target|Target\s*Price|目標價(?:\s*\([^\)]*\))?|12\s*個月目標價)[\s:：|]*(?:NT\\\$|NT\$|TWD|新台幣|\\\$|\$|\bNT\b)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{2,5}(?:\.[0-9]+)?)\s*(?:元|NT\$|TWD)?",
        # 目標價 (NT$) 2850
        r"目標價\s*\([A-Za-z$]+\)[|:\s]*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{2,5}(?:\.[0-9]+)?)",
        # 目標價：2,900 元
        r"目標價[^\d\n]{0,15}([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{2,5}(?:\.[0-9]+)?)\s*元",
        # TP 3,665
        r"\bTP[\s:：|]*(?:NT\\\$|NT\$|TWD|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{3,5}(?:\.[0-9]+)?)",
    ]

    for pat in tp_patterns:
        matches = re.finditer(pat, head, re.I)
        for m in matches:
            raw_val = m.group(1).replace(",", "").strip()
            try:
                val = float(raw_val)
                start, end = m.span()
                surrounding = head[max(0, start - 15):min(len(head), end + 15)]
                # 排除誤抓年份
                if val in (2024.0, 2025.0, 2026.0, 2027.0, 2028.0) and ("年" in surrounding or "FY" in surrounding):
                    continue
                # 排除明顯非目標價的小數值（如 < 15 元除非是特殊低價股，或 > 25000 元）
                if 15.0 <= val <= 25000.0:
                    return val
            except ValueError:
                pass

    return None


def extract_eps_forecasts(content: str) -> dict[str, float]:
    """
    從報告表格或內文中提取預估 EPS (如 2026, 2027, 2028 等)
    """
    head = content[:6000]
    eps_dict: dict[str, float] = {}

    # 1. 匹配損益表或行文中的 EPS：2026 EPS 105.31 / 2027 EPS 135.48
    # 2026 / 2027 年稅後 EPS 分別至 105.31 元 及 135.48 元
    m_split = re.search(r"2026\s*[/、及與,]\s*2027\s*年[^\d\n]*EPS[^\d\n]*([0-9]{1,3}\.[0-9]{1,2})[^\d\n]*([0-9]{1,3}\.[0-9]{1,2})", head, re.I)
    if m_split:
        try:
            eps_dict["2026"] = float(m_split.group(1))
            eps_dict["2027"] = float(m_split.group(2))
        except Exception:
            pass

    # 2. 行文格式：預估 26 年 65.33；27 年則 ... 預估 27 年 104.69 元
    m_single = re.findall(r"(?:預估|估)?\s*(202[5-9]|2[5-9])\s*年[^\d\n]{0,15}(?:EPS|每股盈餘)?[\s:：=]*(?:為)?\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?", head, re.I)
    for y_raw, val_raw in m_single:
        try:
            val = float(val_raw)
            y_clean = "20" + y_raw if len(y_raw) == 2 else y_raw
            if 0.5 <= val <= 500.0 and y_clean not in eps_dict:
                eps_dict[y_clean] = val
        except ValueError:
            pass

    # 3. 大摩 Fiscal Year Ending 表格格式
    m_ms = re.search(r"Fiscal\s*Year\s*Ending[^\n]*\n[^\n]*EPS[^\n]*\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})", head, re.I)
    if m_ms:
        try:
            v25, v26, v27 = float(m_ms.group(1)), float(m_ms.group(2)), float(m_ms.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 4. 高盛 GS Forecast 表格格式：EPS (NT$) New 66.26 109.21 140.91
    m_gs = re.search(r"EPS\s*\([A-Za-z$]+\)\s*New\s*([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})", head, re.I)
    if m_gs:
        try:
            v25, v26, v27 = float(m_gs.group(1)), float(m_gs.group(2)), float(m_gs.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 5. 麥格理 Macquarie 表格格式：EPS rep [TWD] | 66.2 | 110.6 | 159.7
    m_mac = re.search(r"EPS\s*rep[^\n|]*\|\s*([0-9]{1,3}\.[0-9]{1,2})\s*\|\s*([0-9]{1,3}\.[0-9]{1,2})\s*\|\s*([0-9]{1,3}\.[0-9]{1,2})", head, re.I)
    if m_mac:
        try:
            v25, v26, v27 = float(m_mac.group(1)), float(m_mac.group(2)), float(m_mac.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 6. 本土券商簡易損益表：|每股盈餘 EPS(元)| ... |65.33|104.69|
    m_table = re.search(r"(?:每股盈餘|EPS)[^\n|]*\|\s*([0-9]{1,3}\.[0-9]{2})\s*\|\s*([0-9]{1,3}\.[0-9]{2})\s*\|\s*([0-9]{1,3}\.[0-9]{2})", head, re.I)
    if m_table:
        try:
            vals = [float(m_table.group(1)), float(m_table.group(2)), float(m_table.group(3))]
            if "2026" not in eps_dict and vals[1] > 0.5:
                eps_dict["2026"] = vals[1]
            if "2027" not in eps_dict and vals[2] > 0.5:
                eps_dict["2027"] = vals[2]
        except Exception:
            pass

    return eps_dict


def parse_report_file(file_path: Path) -> dict[str, Any] | None:
    """解析單份 Markdown 券商研報"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.debug("Failed to read %s: %s", file_path, e)
        return None

    filename = str(file_path)
    head = content[:2000]

    broker = extract_broker(file_path.name, head)
    rep_date = extract_report_date(filename, head)
    rating = extract_rating(head)
    target_price = extract_target_price(content)
    eps_forecasts = extract_eps_forecasts(content)

    return {
        "file_path": str(file_path),
        "filename": file_path.name,
        "broker": broker,
        "report_date": rep_date,
        "rating": rating,
        "target_price": target_price,
        "eps_forecasts": eps_forecasts,
    }


def find_reports_for_ticker(
    ticker: str,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    days: int = 90,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    """
    依個股代碼搜尋近 N 天內的研報並解析。
    若同券商有多份報告，保留最新的一份。
    """
    if not reports_dir.exists():
        return []

    code = ticker.strip().upper().replace(".TW", "").replace(".TWO", "")
    ref_d = reference_date or date.today()
    cutoff_d = ref_d - timedelta(days=days)
    code_pattern = re.compile(r"(?<!\d)" + re.escape(code) + r"(?!\d)")

    matched_files: list[Path] = []
    for p in reports_dir.rglob("*.md"):
        if p.name in ("INDEX.md", "MOC.md", "convert_progress.log"):
            continue
        # 排除總經產業匯總與新聞日報
        if any(ex in p.name for ex in MACRO_EXCLUDE_KEYWORDS):
            continue
        # 檢查個股代碼是否在檔名中 (確保是該標的的個股報告，避免誤命中日期/價格/他股代碼中的數字子字串)
        if code_pattern.search(p.name):
            matched_files.append(p)

    parsed_reports: list[dict[str, Any]] = []
    for fp in matched_files:
        rep = parse_report_file(fp)
        if not rep:
            continue

        rep_d_str = rep.get("report_date")
        if rep_d_str:
            try:
                rd = datetime.strptime(rep_d_str, "%Y-%m-%d").date()
                if rd < cutoff_d:
                    continue
            except Exception:
                pass

        parsed_reports.append(rep)

    # 排序：依報告日期降序
    parsed_reports.sort(key=lambda r: r.get("report_date") or "1970-01-01", reverse=True)

    # 依券商去重，只留該券商最新的一份報告
    unique_by_broker: dict[str, dict[str, Any]] = {}
    for r in parsed_reports:
        b = r["broker"]
        if b not in unique_by_broker:
            unique_by_broker[b] = r

    return list(unique_by_broker.values())


def summarize_broker_consensus(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """
    彙整券商研報共識數據：
    - 目標價中位數 (Median Target Price)
    - 目標價區間 (Min ~ Max)
    - 覆蓋券商家數與明細
    - 共識 Forward EPS (2026/2027)
    - A3 目標價估值分母 EPS (target_price_base_eps，對齊目標價年度基準，優先 2027 -> 2026)
    - 隱含成長率 CAGR / YoY%
    """
    if not reports:
        return {
            "status": "(unavailable: 本地研報庫無覆蓋)",
            "coverage_count": 0,
            "brokers": [],
            "target_prices": [],
            "median_target_price": None,
            "min_target_price": None,
            "max_target_price": None,
            "forward_eps_consensus": None,
            "eps_2026_consensus": None,
            "eps_2027_consensus": None,
            "eps_growth_pct": None,
            "latest_report_date": None,
        }

    target_prices: list[float] = []
    eps_2026_list: list[float] = []
    eps_2027_list: list[float] = []
    broker_details: list[dict[str, Any]] = []
    dates: list[str] = []

    for r in reports:
        b = r["broker"]
        tp = r["target_price"]
        rtg = r["rating"]
        rd = r["report_date"]
        eps_f = r.get("eps_forecasts", {})

        if rd:
            dates.append(rd)
        if tp is not None:
            target_prices.append(tp)
        if "2026" in eps_f:
            eps_2026_list.append(eps_f["2026"])
        if "2027" in eps_f:
            eps_2027_list.append(eps_f["2027"])

        broker_details.append({
            "broker": b,
            "report_date": rd,
            "rating": rtg,
            "target_price": tp,
            "eps_forecasts": eps_f,
        })

    # 計算統計量
    median_tp = None
    min_tp = None
    max_tp = None
    if target_prices:
        sorted_tp = sorted(target_prices)
        n = len(sorted_tp)
        median_tp = round(sorted_tp[n // 2] if n % 2 == 1 else (sorted_tp[n // 2 - 1] + sorted_tp[n // 2]) / 2.0, 1)
        min_tp = min(sorted_tp)
        max_tp = max(sorted_tp)

    eps_2026_med = None
    if eps_2026_list:
        sorted_26 = sorted(eps_2026_list)
        n = len(sorted_26)
        eps_2026_med = round(sorted_26[n // 2] if n % 2 == 1 else (sorted_26[n // 2 - 1] + sorted_26[n // 2]) / 2.0, 2)

    eps_2027_med = None
    if eps_2027_list:
        sorted_27 = sorted(eps_2027_list)
        n = len(sorted_27)
        eps_2027_med = round(sorted_27[n // 2] if n % 2 == 1 else (sorted_27[n // 2 - 1] + sorted_27[n // 2]) / 2.0, 2)

    # 成長率預估 (若有 2026 與 2027 EPS，計算 YoY 成長率)
    eps_growth_pct = None
    if eps_2026_med and eps_2027_med and eps_2026_med > 0:
        eps_growth_pct = round(((eps_2027_med - eps_2026_med) / eps_2026_med) * 100.0, 1)

    # 基準 Forward EPS（一般用途）：優先 2026 -> 2027
    fwd_eps = eps_2026_med or eps_2027_med

    # A3 估值專用基準 EPS（目標價年度基準對齊）：券商 12 個月目標價多以次年 (2027) 獲利為基礎推導，故優先 2027 -> 2026
    target_price_base_eps = eps_2027_med or eps_2026_med

    latest_date = max(dates) if dates else None

    return {
        "status": "ok",
        "coverage_count": len(reports),
        "brokers": [r["broker"] for r in reports],
        "broker_details": broker_details,
        "target_prices": target_prices,
        "median_target_price": median_tp,
        "min_target_price": min_tp,
        "max_target_price": max_tp,
        "forward_eps_consensus": fwd_eps,
        "target_price_base_eps": target_price_base_eps,
        "eps_2026_consensus": eps_2026_med,
        "eps_2027_consensus": eps_2027_med,
        "eps_growth_pct": eps_growth_pct,
        "latest_report_date": latest_date,
    }


def get_tw_broker_consensus(
    ticker: str,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    days: int = 90,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """
    高階整合函式：直接傳入代碼，搜尋本地券商研報並輸出彙整數據。
    """
    reports = find_reports_for_ticker(ticker, reports_dir=reports_dir, days=days, reference_date=reference_date)
    return summarize_broker_consensus(reports)


# ── CLI 介面 ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="本地券商研報快速檢索與共識數據抽取工具")
    parser.add_argument("--ticker", "-t", required=True, help="台股代碼 (如 3665, 2330)")
    parser.add_argument("--days", "-d", type=int, default=90, help="掃描報告天數 (預設 90 天)")
    parser.add_argument("--dir", type=str, default=str(DEFAULT_REPORTS_DIR), help="研報存放目錄")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式輸出")
    args = parser.parse_args()

    reports_dir = Path(args.dir)
    consensus = get_tw_broker_consensus(args.ticker, reports_dir=reports_dir, days=args.days)

    if args.json:
        print(json.dumps(consensus, indent=2, ensure_ascii=False))
        return

    print(f"\n=======================================================")
    print(f" 台股券商研報共識摘要：{args.ticker}")
    print(f"=======================================================")
    if consensus.get("status") != "ok":
        print(f"狀態：{consensus.get('status')}")
        return

    print(f"覆蓋券商數：{consensus['coverage_count']} 家 ({', '.join(consensus['brokers'])})")
    print(f"最新研報日期：{consensus['latest_report_date']}")
    print(f"目標價中位數：NT$ {consensus['median_target_price']} (區間: NT$ {consensus['min_target_price']} ~ {consensus['max_target_price']})")
    print(f"2026 EPS 共識：NT$ {consensus['eps_2026_consensus']}")
    print(f"2027 EPS 共識：NT$ {consensus['eps_2027_consensus']}")
    print(f"預估盈餘成長率：{consensus['eps_growth_pct']}%\n")
    print("各券商評等與目標價明細：")
    print("-" * 55)
    for b in consensus.get("broker_details", []):
        tp_str = f"NT$ {b['target_price']}" if b['target_price'] else "(未給目標價)"
        rtg_str = b['rating'] or "(未標評等)"
        eps_str = ", ".join([f"{k}: ${v}" for k, v in b.get("eps_forecasts", {}).items()]) or "—"
        print(f"- {b['broker']:<15} | 日期: {b['report_date']} | 評等: {rtg_str:<8} | 目標價: {tp_str:<12} | EPS: {eps_str}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
