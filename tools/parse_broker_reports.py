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

# 評級分類標準化 (支援中英文字詞，中文不使用 \\b 邊界)
RATING_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:\b(?:Overweight|OW|Outperform)\b|增加持股|加碼|強力買進)", re.I), "Buy"),
    (re.compile(r"(?:\bTrading Buy\b)", re.I), "Trading Buy"),
    (re.compile(r"(?:\bBuy\b|買進)", re.I), "Buy"),
    (re.compile(r"(?:\b(?:Neutral|Hold|Equal-weight|EW|In-Line|Market-Weight)\b|中立|持有)", re.I), "Neutral"),
    (re.compile(r"(?:\b(?:Underweight|UW|Sell)\b|賣出|降低持股|減碼)", re.I), "Sell"),
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


def extract_report_date(filename_or_path: str, content_head: str) -> str | None:
    """
    提取報告發布日期 (格式: YYYY-MM-DD)。
    注意：僅使用檔名 (Path(filename_or_path).name) 比對日期，避免目錄名數字與檔名代碼數字混淆。
    """
    filename = Path(filename_or_path).name

    # 1. 檔名中標準 YYYYMMDD (例如 20260824, 2026-08-24, 2026_08_24, 2026.08.24)
    m = re.search(r"(202[4-7])[\-_/.]?(0[1-9]|1[0-2])[\-_/.]?(0[1-9]|[12][0-9]|3[01])", filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # 2. 檔名支援台灣民國年格式 (例如 1150819 -> 2026-08-19, 1141225 -> 2025-12-25)
    m_roc = re.search(r"(?<!\d)(11[4-7])(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(?!\d)", filename)
    if m_roc:
        y = int(m_roc.group(1)) + 1911
        return f"{y:04d}-{m_roc.group(2)}-{m_roc.group(3)}"

    # 3. 內容前段中文日期：2026 年 8 月 24 日 或 2026/08/24 或 2026.08.24 或 2026-08-24
    m_zh = re.search(r"(202[4-7])[\s年\-/._]+(0?[1-9]|1[0-2])[\s月\-/._]+([12][0-9]|3[01]|0?[1-9])\s*日?", content_head)
    if m_zh:
        y, month, d = int(m_zh.group(1)), int(m_zh.group(2)), int(m_zh.group(3))
        return f"{y:04d}-{month:02d}-{d:02d}"

    # 4. 內容前段民國年中文日期：115 年 8 月 19 日 或 115/08/19
    m_roc_zh = re.search(r"(?<!\d)(11[4-7])[\s年\-/._]+(0?[1-9]|1[0-2])[\s月\-/._]+([12][0-9]|3[01]|0?[1-9])\s*日?", content_head)
    if m_roc_zh:
        y = int(m_roc_zh.group(1)) + 1911
        month = int(m_roc_zh.group(2))
        d = int(m_roc_zh.group(3))
        return f"{y:04d}-{month:02d}-{d:02d}"

    # 5. 英文月份格式：September 6, 2026 或 16 July 2026 或 Sep 4, 2026
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

    # 6. 年月目錄 fallback (若檔案所在目錄為 202608 格式，且檔名/內文無日期)
    p = Path(filename_or_path)
    for parent in p.parents:
        m_dir = re.search(r"^(202[4-7])(0[1-9]|1[0-2])$", parent.name)
        if m_dir:
            return f"{m_dir.group(1)}-{m_dir.group(2)}-01"

    return None


def extract_title(content_head: str, fallback: str) -> str:
    """提取 Markdown 報告主標題 (第一行 # 標題或檔名)"""
    for line in content_head.splitlines():
        line = line.strip()
        if line.startswith("# ") and len(line) > 2:
            return line[2:].strip()
    return fallback


def format_rel_report_path(file_path: str | Path | None, root_dir: Path = ROOT) -> str:
    """將研報檔案路徑轉為相對專案根的相對路徑"""
    if not file_path:
        return ""
    p = Path(file_path)
    if not p.is_absolute():
        return str(p)
    try:
        return str(p.resolve().relative_to(root_dir.resolve()))
    except (ValueError, RuntimeError):
        p_str = str(p)
        for marker in ("research/analyst_reports", "analyst_reports"):
            idx = p_str.find(marker)
            if idx != -1:
                return p_str[idx:]
        return p.name


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
def extract_table_eps(head: str) -> dict[str, float]:
    """從 Markdown 表格中抽取多年期 EPS"""
    eps_dict: dict[str, float] = {}
    table_lines = head.splitlines()

    # 1. 橫向年份表格: 表頭含有 2024, 2025, 2026 等
    for i, line in enumerate(table_lines):
        if re.search(r"202[4-7]", line) and "|" in line:
            headers = [c.strip() for c in line.split("|")]
            # 排除非表格長文字段落
            if any(len(c) > 40 for c in headers):
                continue
            year_cols: dict[int, str] = {}
            for col_idx, h in enumerate(headers):
                m_y = re.search(r"(202[4-9])", h)
                if m_y:
                    year_cols[col_idx] = m_y.group(1)
            if len(year_cols) >= 2:
                for j in range(i + 1, min(len(table_lines), i + 15)):
                    row = table_lines[j]
                    if re.search(r"(?:每股盈餘|EPS|稅後\s*EPS)", row, re.I):
                        cols = [c.strip() for c in row.split("|")]
                        row_eps: dict[str, float] = {}
                        for c_idx, y in year_cols.items():
                            if c_idx < len(cols):
                                val_str = cols[c_idx]
                                try:
                                    v = float(val_str)
                                    if 0.1 <= v <= 1000.0:
                                        row_eps[y] = v
                                except ValueError:
                                    pass
                        if len(row_eps) >= len(eps_dict):
                            eps_dict.update(row_eps)
                if eps_dict:
                    return eps_dict

    # 2. 直向年份表格: 每行第一非空欄是 2023, 2024, 2025, 2026(F), 2027(F)
    for line in table_lines:
        if "|" in line:
            cols = [c.strip() for c in line.split("|")]
            valid_cols = [c for c in cols if c]
            if valid_cols:
                m_y = re.search(r"^(202[4-9])", valid_cols[0])
                if m_y:
                    y = m_y.group(1)
                    try:
                        v = float(valid_cols[-1])
                        if 0.1 <= v <= 1000.0:
                            eps_dict[y] = v
                    except ValueError:
                        pass

    return eps_dict


def filter_eps_series_single_source(eps_dict: dict[str, float], max_ratio: float = 2.5) -> dict[str, float]:
    """
    單一券商多年度 EPS 合理性過濾（防 OCR/解析錯抓離群值）：
    - 數值邊界：0.1 <= EPS <= 1000.0
    - 相鄰年度變動檢查：相鄰年度獲利倍數落差超過 max_ratio（預設 2.5x，即成長 >150% 或衰退 >60%）
      若有 >=3 個年度，嘗試找出並剔除破壞趨勢一致性的離群年度；
      若僅有 2 個年度且差距超過 3.0x，則因無法判斷誰對，予以清空。
    """
    valid = {k: v for k, v in eps_dict.items() if 0.1 <= v <= 1000.0}
    if len(valid) <= 1:
        return valid

    sorted_years = sorted(valid.keys(), key=lambda y: int(y) if y.isdigit() else 9999)

    def get_max_adjacent_ratio(years: list[str], d: dict[str, float]) -> float:
        r_max = 1.0
        for i in range(len(years) - 1):
            v1, v2 = d[years[i]], d[years[i + 1]]
            if v1 <= 0 or v2 <= 0:
                return 9999.0
            r = max(v1 / v2, v2 / v1)
            if r > r_max:
                r_max = r
        return r_max

    curr_max_r = get_max_adjacent_ratio(sorted_years, valid)
    if curr_max_r <= max_ratio:
        return valid

    if len(sorted_years) == 2:
        # 兩年度落差超過 max_ratio，若超過 3.0 視為衝突不可靠
        if curr_max_r > 3.0:
            return {}
        return valid

    # 當 >= 3 個年度時，找出剔除哪 1 個年度後相鄰比值最低且 <= max_ratio
    min_max_ratio = 9999.0
    best_subset = valid
    for y_drop in sorted_years:
        rem_years = [y for y in sorted_years if y != y_drop]
        r = get_max_adjacent_ratio(rem_years, valid)
        if r < min_max_ratio:
            min_max_ratio = r
            best_subset = {y: valid[y] for y in rem_years}

    if min_max_ratio <= max_ratio:
        return best_subset

    return {}


def filter_cross_broker_eps_outliers(eps_list: list[float], max_dev_ratio: float = 2.0) -> list[float]:
    """
    跨券商同年度 EPS 離群剔除：
    - 排除明顯離群（相對於中位數偏差超過 max_dev_ratio，如 2.0x 倍數差距）
    - 避免單一券商的異常值污染共識中位數
    """
    valid = [v for v in eps_list if v is not None and v > 0]
    if len(valid) <= 2:
        if len(valid) == 2:
            r = max(valid[0] / valid[1], valid[1] / valid[0])
            if r > 2.5:
                return [min(valid)]
        return valid

    sorted_v = sorted(valid)
    n = len(sorted_v)
    med = sorted_v[n // 2] if n % 2 == 1 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2.0

    filtered = []
    for v in valid:
        ratio = max(v / med, med / v)
        if ratio <= max_dev_ratio:
            filtered.append(v)

    return filtered or valid


def extract_eps_forecasts(content: str) -> dict[str, float]:
    """
    從報告表格或內文中提取預估 EPS (如 2026, 2027, 2028 等)
    並自動執行單來源相鄰年度合理性檢查與離群剔除。
    """
    head = content[:6000]
    eps_dict: dict[str, float] = extract_table_eps(head)

    # 1. 雙年度 / 連續年度 EPS：2026/2027 年 EPS 分別為 5.18 元 及 7.93 元
    pat_pair1 = re.finditer(
        r"(202[5-9])\s*[/、及與,\-]\s*(?:20)?(2[5-9])\s*年[^\d\n,，;；]{0,15}(?:EPS|每股盈餘)[^\d\n,，;；]{0,10}(?:為|至|來到|分別為)?\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?\s*[/、及與,]\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?",
        head,
        re.I,
    )
    for m in pat_pair1:
        y1 = m.group(1)
        y2_short = m.group(2)
        y2 = "20" + y2_short if len(y2_short) == 2 else y2_short
        v1, v2 = float(m.group(3)), float(m.group(4))
        if 0.1 <= v1 <= 500.0 and 0.1 <= v2 <= 500.0:
            if y1 not in eps_dict:
                eps_dict[y1] = v1
            if y2 not in eps_dict:
                eps_dict[y2] = v2

    pat_pair2 = re.finditer(
        r"(?:EPS|每股盈餘)[^\d\n,，;；]{0,15}(202[5-9])\s*[/、及與,\-]\s*(?:20)?(2[5-9])\s*年[^\d\n,，;；]{0,10}(?:為|至|來到|分別為)?\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?\s*[/、及與,]\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?",
        head,
        re.I,
    )
    for m in pat_pair2:
        y1 = m.group(1)
        y2_short = m.group(2)
        y2 = "20" + y2_short if len(y2_short) == 2 else y2_short
        v1, v2 = float(m.group(3)), float(m.group(4))
        if 0.1 <= v1 <= 500.0 and 0.1 <= v2 <= 500.0:
            if y1 not in eps_dict:
                eps_dict[y1] = v1
            if y2 not in eps_dict:
                eps_dict[y2] = v2

    pat_pair3 = re.finditer(
        r"(202[5-9])\s*年[^\d\n,，;；]{0,10}(?:EPS|每股盈餘)[\s:：=]*(?:為|至|來到)?\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?[/、及與,]\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?",
        head,
        re.I,
    )
    for m in pat_pair3:
        y_end = int(m.group(1))
        y_start = y_end - 1
        v1, v2 = float(m.group(2)), float(m.group(3))
        if 0.1 <= v1 <= 500.0 and 0.1 <= v2 <= 500.0:
            if str(y_start) not in eps_dict:
                eps_dict[str(y_start)] = v1
            if str(y_end) not in eps_dict:
                eps_dict[str(y_end)] = v2

    # 2. 跨年度並列格式：預估 26 年 65.33；27 年 104.69 元
    m_multi_year = re.finditer(
        r"(202[5-9]|2[5-9])\s*年[^\d\n,，;；]{0,10}([0-9]{1,3}\.[0-9]{1,2})\s*元?[;；,，]?\s*(202[5-9]|2[5-9])\s*年[^\d\n,，;；]{0,10}([0-9]{1,3}\.[0-9]{1,2})",
        head,
        re.I,
    )
    for m in m_multi_year:
        y1_raw, v1_raw, y2_raw, v2_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        y1_clean = "20" + y1_raw if len(y1_raw) == 2 else y1_raw
        y2_clean = "20" + y2_raw if len(y2_raw) == 2 else y2_raw
        try:
            v1, v2 = float(v1_raw), float(v2_raw)
            if 0.1 <= v1 <= 500.0 and 0.1 <= v2 <= 500.0:
                if y1_clean not in eps_dict:
                    eps_dict[y1_clean] = v1
                if y2_clean not in eps_dict:
                    eps_dict[y2_clean] = v2
        except ValueError:
            pass

    # 3. 元富格式：2026F EPS ... 2027F EPS ... 本次 5.07 ... 本次 7.60
    m_yf = re.search(
        r"2026[F\s]*EPS[^\n]*2027[F\s]*EPS[^\n]*\n+([0-9]{1,3}\.[0-9]{2})\s*本次\n+([0-9]{1,3}\.[0-9]{2})\s*前次\n+([0-9]{1,3}\.[0-9]{2})\s*本次\n+([0-9]{1,3}\.[0-9]{2})",
        head,
        re.I,
    )
    if m_yf:
        try:
            v26_curr = float(m_yf.group(2))
            v27_curr = float(m_yf.group(4))
            if 0.1 <= v26_curr <= 500.0 and "2026" not in eps_dict:
                eps_dict["2026"] = v26_curr
            if 0.1 <= v27_curr <= 500.0 and "2027" not in eps_dict:
                eps_dict["2027"] = v27_curr
        except Exception:
            pass

    # 4. 行文單一年度格式：預估 2026 年 EPS 為 4.8 元 或 2026年EPS 68.4 或 26年EPS預估為5.27
    m_single = re.finditer(
        r"(?:預估|估)?\s*(202[5-9]|2[5-9])\s*年[^\d\n,，;；]{0,8}(?:EPS|每股盈餘)[\s:：=]*(?:為|至|來到|預估為|預估至)?\s*([0-9]{1,3}\.[0-9]{1,2})\s*元?",
        head,
        re.I,
    )
    for m in m_single:
        try:
            val = float(m.group(2))
            y_raw = m.group(1)
            y_clean = "20" + y_raw if len(y_raw) == 2 else y_raw
            if 0.5 <= val <= 500.0 and y_clean not in eps_dict:
                eps_dict[y_clean] = val
        except ValueError:
            pass

    # 5. 大摩 Fiscal Year Ending 表格格式
    m_ms = re.search(
        r"Fiscal\s*Year\s*Ending[^\n]*\n[^\n]*EPS[^\n]*\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})",
        head,
        re.I,
    )
    if m_ms:
        try:
            v25, v26, v27 = float(m_ms.group(1)), float(m_ms.group(2)), float(m_ms.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 5. 高盛 GS Forecast 表格格式：EPS (NT$) New 66.26 109.21 140.91
    m_gs = re.search(
        r"EPS\s*\([A-Za-z$]+\)\s*New\s*([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})\s+([0-9]{1,3}\.[0-9]{2})",
        head,
        re.I,
    )
    if m_gs:
        try:
            v25, v26, v27 = float(m_gs.group(1)), float(m_gs.group(2)), float(m_gs.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 6. 麥格理 Macquarie 表格格式：EPS rep [TWD] | 66.2 | 110.6 | 159.7
    m_mac = re.search(
        r"EPS\s*rep[^\n|]*\|\s*([0-9]{1,3}\.[0-9]{1,2})\s*\|\s*([0-9]{1,3}\.[0-9]{1,2})\s*\|\s*([0-9]{1,3}\.[0-9]{1,2})",
        head,
        re.I,
    )
    if m_mac:
        try:
            v25, v26, v27 = float(m_mac.group(1)), float(m_mac.group(2)), float(m_mac.group(3))
            if "2026" not in eps_dict:
                eps_dict["2026"] = v26
            if "2027" not in eps_dict:
                eps_dict["2027"] = v27
        except Exception:
            pass

    # 7. 本土券商簡易損益表：|每股盈餘 EPS(元)| ... |65.33|104.69|
    m_table = re.search(
        r"(?:每股盈餘|EPS)[^\n|]*\|\s*([0-9]{1,3}\.[0-9]{2})\s*\|\s*([0-9]{1,3}\.[0-9]{2})\s*\|\s*([0-9]{1,3}\.[0-9]{2})",
        head,
        re.I,
    )
    if m_table:
        try:
            vals = [float(m_table.group(1)), float(m_table.group(2)), float(m_table.group(3))]
            if "2026" not in eps_dict and vals[1] > 0.5:
                eps_dict["2026"] = vals[1]
            if "2027" not in eps_dict and vals[2] > 0.5:
                eps_dict["2027"] = vals[2]
        except Exception:
            pass

    # 執行單一來源相鄰年度合理性檢查與離群剔除
    return filter_eps_series_single_source(eps_dict)


# 常見台股代碼與別名/中文名對照 (可透過 research/tw-market-config.json 動態補充)
DEFAULT_TICKER_ALIASES: dict[str, list[str]] = {
    "2481": ["強茂", "PANJIT", "PanJIT"],
    "3665": ["貿聯", "貿聯-KY", "BizLink", "Bizlink"],
    "2330": ["台積電", "台積", "TSMC"],
    "2454": ["聯發科", "MediaTek"],
    "2317": ["鴻海", "Hon Hai", "Foxconn"],
    "2382": ["廣達", "Quanta"],
    "3037": ["欣興", "Unimicron"],
    "3081": ["聯亞", "LandMark"],
    "3443": ["創意", "Global Unichip", "GUC"],
    "3661": ["世芯", "世芯-KY", "Alchip"],
    "5274": ["信驊", "ASPEED", "Aspeed"],
    "6223": ["旺矽", "MPI"],
    "6669": ["緯穎", "Wiwynn"],
    "3533": ["嘉澤", "Lotes"],
    "3189": ["景碩", "Kinsus"],
    "2383": ["台光電", "EMC"],
    "6274": ["台燿", "TUC"],
    "8210": ["勤誠", "Chenbro"],
    "1560": ["中砂", "Kinik"],
    "2308": ["台達電", "Delta"],
    "2328": ["廣宇"],
    "3141": ["晶宏"],
}


def get_ticker_aliases(ticker: str) -> list[str]:
    """取得標的的別名與中文名稱清單"""
    code = ticker.strip().upper().replace(".TW", "").replace(".TWO", "")
    aliases = list(DEFAULT_TICKER_ALIASES.get(code, []))

    # 嘗試從 research/tw-market-config.json 讀取配置
    config_file = ROOT / "research" / "tw-market-config.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for item in cfg.get("tickers", []):
                if item.get("code") == code:
                    name = item.get("name")
                    if name and name not in aliases:
                        aliases.append(name)
        except Exception:
            pass

    return aliases


def parse_report_file(file_path: Path) -> dict[str, Any] | None:
    """解析單份 Markdown 券商研報"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.debug("Failed to read %s: %s", file_path, e)
        return None

    head = content[:2000]

    broker = extract_broker(file_path.name, head)
    rep_date = extract_report_date(str(file_path), head)
    rating = extract_rating(head)
    target_price = extract_target_price(content)
    eps_forecasts = extract_eps_forecasts(content)
    title = extract_title(head, file_path.stem)

    return {
        "file_path": str(file_path),
        "filename": file_path.name,
        "title": title,
        "broker": broker,
        "report_date": rep_date,
        "rating": rating,
        "target_price": target_price,
        "eps_forecasts": eps_forecasts,
    }


def parse_multi_target_report(file_path: Path, code: str, aliases: list[str]) -> list[dict[str, Any]]:
    """
    從多標的文件（論壇演講、評等彙整表、綜合報告等）中抽取特定標的的評等/目標價/EPS。
    僅在對該標的有實質評等、目標價或預估 EPS 時納入。
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.debug("Failed to read %s: %s", file_path, e)
        return []

    filename = file_path.name
    rep_date = extract_report_date(str(file_path), content[:2000])
    doc_broker = extract_broker(filename, content[:2000])

    search_terms = [code] + aliases
    found_nodes: list[dict[str, Any]] = []

    # 1. Markdown 章節解析：# 標題含 代碼 或 中文/英文別名
    section_splits = list(re.finditer(r"(?m)^(#{1,3})\s+(.+)$", content))
    for i, m in enumerate(section_splits):
        header_level = len(m.group(1))
        header_text = m.group(2).strip()

        # 檢查該章節標題是否為目標標的 (例如: "# 強茂 (2481 TT)" 或 "# 2481")
        has_code = re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", header_text) is not None
        has_alias = any(re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", header_text, re.I) for alias in aliases)
        if not (has_code or has_alias):
            continue

        start_pos = m.end()
        end_pos = len(content)
        for next_m in section_splits[i + 1:]:
            if len(next_m.group(1)) <= header_level:
                end_pos = next_m.start()
                break

        sec_body = content[start_pos:end_pos]
        # 章節標題若為個股，提取該章節內的券商、評等、目標價、EPS
        sec_broker = extract_broker(filename, sec_body[:2000])
        if sec_broker == "Other Broker":
            sec_broker = doc_broker

        sec_rating = extract_rating(sec_body[:2000])
        sec_tp = extract_target_price(sec_body)
        sec_eps = extract_eps_forecasts(sec_body)

        # 僅在有實質目標價、評等或預估 EPS 時納入
        if sec_tp is not None or sec_eps or sec_rating:
            found_nodes.append({
                "file_path": str(file_path),
                "filename": filename,
                "title": header_text,
                "broker": sec_broker,
                "report_date": rep_date,
                "rating": sec_rating,
                "target_price": sec_tp,
                "eps_forecasts": sec_eps,
                "source_type": "section",
            })

    # 2. Markdown 表格行解析 (同業個股報告評等彙整等)
    # 僅針對評等彙整表或晨報研究報告彙整進行逐行表格抽取
    is_rating_table_doc = any(kw in filename for kw in ("評等彙整", "同業個股", "研究報告彙整表"))
    if is_rating_table_doc:
        for line in content.splitlines():
            if not line.startswith("|") or "|" not in line[1:]:
                continue

            # 檢查該行是否包含代碼或別名
            has_code = re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", line) is not None
            has_alias = any(alias in line for alias in aliases)
            if not (has_code or has_alias):
                continue

            cols = [c.strip() for c in line.split("|") if c.strip()]
            if not cols:
                continue

            # 格式 2: |股票代號 2481|股票名稱 強茂|券商名稱 統一證券|投資評等 強力買進|目標價 205|2026 EPS(F) 5.27|2027 EPS(F) 8.29|
            if any("股票代號" in c or "目標價" in c for c in cols):
                row_broker = extract_broker("", line)
                if row_broker == "Other Broker":
                    row_broker = doc_broker
                row_rating = extract_rating(line)
                row_tp = None
                m_tp = re.search(r"目標價[^\d\n|]{0,10}([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{2,5}(?:\.[0-9]+)?)", line)
                if m_tp:
                    try:
                        row_tp = float(m_tp.group(1).replace(",", ""))
                    except ValueError:
                        pass
                row_eps = {}
                m_26 = re.search(r"2026\s*(?:EPS)?\(?F?\)?\s*([0-9]{1,3}\.[0-9]{1,2})", line, re.I)
                if m_26:
                    row_eps["2026"] = float(m_26.group(1))
                m_27 = re.search(r"2027\s*(?:EPS)?\(?F?\)?\s*([0-9]{1,3}\.[0-9]{1,2})", line, re.I)
                if m_27:
                    row_eps["2027"] = float(m_27.group(1))

                if row_tp is not None or row_eps or row_rating:
                    found_nodes.append({
                        "file_path": str(file_path),
                        "filename": filename,
                        "title": f"{filename} (彙整: {code})",
                        "broker": row_broker,
                        "report_date": rep_date,
                        "rating": row_rating,
                        "target_price": row_tp,
                        "eps_forecasts": row_eps,
                        "source_type": "table_row",
                    })
                continue

            # 格式 3: 多券商壓縮列 (例如 |2382|廣達|野村 凱基|Buy 買進|626 500|...)
            if len(cols) >= 5 and (" " in cols[2] or " " in cols[3]):
                b_tokens = cols[2].split()
                r_tokens = cols[3].split()
                tp_tokens = cols[4].split() if len(cols) > 4 else []
                e26_tokens = cols[5].split() if len(cols) > 5 else []
                e27_tokens = cols[6].split() if len(cols) > 6 else []

                for idx, b_raw in enumerate(b_tokens):
                    b_clean = extract_broker("", b_raw)
                    r_clean = extract_rating(r_tokens[idx]) if idx < len(r_tokens) else None
                    tp_clean = None
                    if idx < len(tp_tokens):
                        try:
                            val = float(tp_tokens[idx].replace(",", ""))
                            if 15.0 <= val <= 25000.0:
                                tp_clean = val
                        except ValueError:
                            pass
                    eps_clean = {}
                    if idx < len(e26_tokens):
                        try:
                            val = float(e26_tokens[idx].replace(",", ""))
                            if 0.1 <= val <= 1000.0:
                                eps_clean["2026"] = val
                        except ValueError:
                            pass
                    if idx < len(e27_tokens):
                        try:
                            val = float(e27_tokens[idx].replace(",", ""))
                            if 0.1 <= val <= 1000.0:
                                eps_clean["2027"] = val
                        except ValueError:
                            pass

                    if tp_clean is not None or eps_clean or r_clean:
                        found_nodes.append({
                            "file_path": str(file_path),
                            "filename": filename,
                            "title": f"{filename} (彙整: {code} - {b_clean})",
                            "broker": b_clean,
                            "report_date": rep_date,
                            "rating": r_clean,
                            "target_price": tp_clean,
                            "eps_forecasts": eps_clean,
                            "source_type": "table_row",
                        })
                continue

            # 格式 1: 單券商標準列 (例如 |2481|強茂|野村證券|Buy|195|||41.30%|)
            if len(cols) >= 4:
                row_broker = extract_broker("", cols[2]) if len(cols) > 2 else doc_broker
                if row_broker == "Other Broker":
                    row_broker = doc_broker
                row_rating = extract_rating(cols[3])
                row_tp = None
                if len(cols) > 4:
                    try:
                        val = float(cols[4].replace(",", "").replace("$", ""))
                        if 15.0 <= val <= 25000.0:
                            row_tp = val
                    except ValueError:
                        pass
                row_eps = {}
                if len(cols) > 5:
                    try:
                        val = float(cols[5].replace(",", ""))
                        if 0.1 <= val <= 1000.0:
                            row_eps["2026"] = val
                    except ValueError:
                        pass
                if len(cols) > 6:
                    try:
                        val = float(cols[6].replace(",", ""))
                        if 0.1 <= val <= 1000.0:
                            row_eps["2027"] = val
                    except ValueError:
                        pass

                if row_tp is not None or row_eps or row_rating:
                    found_nodes.append({
                        "file_path": str(file_path),
                        "filename": filename,
                        "title": f"{filename} (彙整: {code} - {row_broker})",
                        "broker": row_broker,
                        "report_date": rep_date,
                        "rating": row_rating,
                        "target_price": row_tp,
                        "eps_forecasts": row_eps,
                        "source_type": "table_row",
                    })

    return found_nodes


def find_reports_for_ticker(
    ticker: str,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    days: int = 90,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    """
    依個股代碼與別名搜尋近 N 天內的研報並解析。
    支援：
    1. 檔名含 代碼 / 中文名 / 英文別名 之獨立報告
    2. 多標的文件（綜合論壇、同業評等彙整表等）之次級抽取
    若同券商有多份報告，保留最新的一份。
    """
    if not reports_dir.exists():
        return []

    code = ticker.strip().upper().replace(".TW", "").replace(".TWO", "")
    aliases = get_ticker_aliases(code)
    ref_d = reference_date or date.today()
    cutoff_d = ref_d - timedelta(days=days)

    code_pattern = re.compile(r"(?<!\d)" + re.escape(code) + r"(?!\d)")
    alias_patterns = [
        re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I)
        for a in aliases
    ]

    # 多標的彙整文件關鍵字
    MULTI_TARGET_KEYWORDS = ["論壇", "評等彙整", "同業個股", "投資論壇"]

    all_parsed_reports: list[dict[str, Any]] = []

    for p in reports_dir.rglob("*.md"):
        if p.name in ("INDEX.md", "MOC.md", "convert_progress.log"):
            continue

        # 檢查檔名是否符合排除關鍵字
        is_macro_excluded = any(ex in p.name for ex in MACRO_EXCLUDE_KEYWORDS)
        is_multi_target = any(kw in p.name for kw in MULTI_TARGET_KEYWORDS)

        # 檔名命中代碼或別名
        fn_match_code = code_pattern.search(p.name) is not None
        fn_match_alias = any(pat.search(p.name) for pat in alias_patterns)
        fn_matched = fn_match_code or fn_match_alias

        if fn_matched and not is_macro_excluded:
            # 視為該標的的個股報告或專題報告
            rep = parse_report_file(p)
            if rep:
                all_parsed_reports.append(rep)
        elif is_multi_target or not is_macro_excluded:
            # 檢查是否為多標的彙整文件或論壇文件，執行次級抽取
            nodes = parse_multi_target_report(p, code, aliases)
            all_parsed_reports.extend(nodes)

    # 過濾過期報告 (超過 days 視窗)
    valid_reports: list[dict[str, Any]] = []
    for rep in all_parsed_reports:
        rep_d_str = rep.get("report_date")
        if rep_d_str:
            try:
                rd = datetime.strptime(rep_d_str, "%Y-%m-%d").date()
                if rd < cutoff_d:
                    continue
            except Exception:
                pass
        valid_reports.append(rep)

    # 排序：依報告日期降序 (若日期相同，優先選擇含目標價與預估 EPS 較完整者)
    def sort_key(r: dict[str, Any]) -> tuple[str, int, int]:
        d = r.get("report_date") or "1970-01-01"
        has_tp = 1 if r.get("target_price") is not None else 0
        eps_cnt = len(r.get("eps_forecasts") or {})
        return (d, has_tp, eps_cnt)

    valid_reports.sort(key=sort_key, reverse=True)

    # 依券商去重，保留該券商最新且最完整的一份報告
    unique_by_broker: dict[str, dict[str, Any]] = {}
    for r in valid_reports:
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
        fp = r.get("file_path")
        fn = r.get("filename")
        title = r.get("title")

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
            "file_path": fp,
            "filename": fn,
            "title": title,
        })

    # 跨券商同年度 EPS 離群過濾
    eps_2026_clean = filter_cross_broker_eps_outliers(eps_2026_list)
    eps_2027_clean = filter_cross_broker_eps_outliers(eps_2027_list)

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
    if eps_2026_clean:
        sorted_26 = sorted(eps_2026_clean)
        n = len(sorted_26)
        eps_2026_med = round(sorted_26[n // 2] if n % 2 == 1 else (sorted_26[n // 2 - 1] + sorted_26[n // 2]) / 2.0, 2)

    eps_2027_med = None
    if eps_2027_clean:
        sorted_27 = sorted(eps_2027_clean)
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
