"""第三頁用的官方日期表。最後核對日見 VERIFIED_ON。

抓取只用來確認官方頁面仍含表上的日期。抓不到或對不到字串時，沿用這張表，
呼叫端把確定性標成「官方日期待確認」。這裡不推算、不補沒公布的下一個日期。
"""

from __future__ import annotations

import re
from datetime import date

VERIFIED_ON = "2026-11-12"

# 決策日是兩日會議的第二天。來源頁最後更新 2026-10-28。
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DECISIONS = (
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
    date(2027, 1, 27),
    date(2027, 3, 17),
    date(2027, 4, 28),
    date(2027, 6, 9),
    date(2027, 7, 28),
    date(2027, 9, 15),
    date(2027, 10, 27),
    date(2027, 12, 8),
)

# 中央銀行 114 年 12 月 18 日新聞稿，115 年預定日期。2027 年尚未公布，不補。
# https://www.cbc.gov.tw/tw/cp-357-189514-82841-1.html
CBC_MEETINGS = (
    date(2026, 3, 19),
    date(2026, 6, 18),
    date(2026, 9, 17),
    date(2026, 12, 17),
)

# BLS 頁面目前只列參考月 2026 年 10 月以後。更早的月份不回推。
# https://www.bls.gov/schedule/news_release/cpi.htm
US_CPI = (
    (date(2026, 11, 10), "2026年10月"),
    (date(2026, 12, 10), "2026年11月"),
    (date(2027, 1, 13), "2026年12月"),
    (date(2027, 2, 11), "2027年1月"),
    (date(2027, 3, 10), "2027年2月"),
    (date(2027, 4, 13), "2027年3月"),
    (date(2027, 5, 12), "2027年4月"),
    (date(2027, 6, 10), "2027年5月"),
    (date(2027, 7, 14), "2027年6月"),
    (date(2027, 8, 11), "2027年7月"),
    (date(2027, 9, 14), "2027年8月"),
    (date(2027, 10, 14), "2027年9月"),
    (date(2027, 11, 10), "2027年10月"),
    (date(2027, 12, 10), "2027年11月"),
)

# https://www.bls.gov/schedule/news_release/empsit.htm
US_PAYROLLS = (
    (date(2026, 11, 6), "2026年10月"),
    (date(2026, 12, 4), "2026年11月"),
    (date(2027, 1, 8), "2026年12月"),
    (date(2027, 2, 5), "2027年1月"),
    (date(2027, 3, 5), "2027年2月"),
    (date(2027, 4, 2), "2027年3月"),
    (date(2027, 5, 7), "2027年4月"),
    (date(2027, 6, 4), "2027年5月"),
    (date(2027, 7, 2), "2027年6月"),
    (date(2027, 8, 6), "2027年7月"),
    (date(2027, 9, 3), "2027年8月"),
    (date(2027, 10, 8), "2027年9月"),
    (date(2027, 11, 5), "2027年10月"),
    (date(2027, 12, 3), "2027年11月"),
)

# https://www.bea.gov/news/schedule/full 2026 年行程，只留總體 GDP 三次估計。
US_GDP = (
    (date(2026, 2, 20), "2025年第4季初值"),
    (date(2026, 3, 13), "2025年第4季第二次估計"),
    (date(2026, 4, 9), "2025年第4季第三次估計"),
    (date(2026, 4, 30), "2026年第1季初值"),
    (date(2026, 5, 28), "2026年第1季第二次估計"),
    (date(2026, 6, 25), "2026年第1季第三次估計"),
    (date(2026, 7, 30), "2026年第2季初值"),
    (date(2026, 8, 26), "2026年第2季第二次估計"),
    (date(2026, 9, 30), "2026年第2季第三次估計"),
    (date(2026, 10, 29), "2026年第3季初值"),
    (date(2026, 11, 25), "2026年第3季第二次估計"),
    (date(2026, 12, 23), "2026年第3季第三次估計"),
)

# 已發布新聞稿與總體資料庫「即將發布」。概估「月底前」沒有指定日的不補。
# https://nstatdb.dgbas.gov.tw/dgbasall/webMain.aspx?funid=upcoming&sys=100
# https://www.dgbas.gov.tw/News.aspx?n=3602&sms=10980
TW_GDP = (
    (date(2026, 1, 30), "2025年第4季概估"),
    (date(2026, 2, 13), "2025年第4季初步統計"),
    (date(2026, 4, 30), "2026年第1季概估"),
    (date(2026, 5, 29), "2026年第1季初步統計"),
    (date(2026, 7, 31), "2026年第2季概估"),
    (date(2026, 8, 14), "2026年第2季初步統計"),
    (date(2026, 10, 30), "2026年第3季概估"),
    (date(2026, 11, 30), "2026年第3季初步統計"),
)

# MSCI 2026-08-12 公布的後八次檢討。https://app2.msci.com/eqb/pressreleases/archive/ir_dates.csv
MSCI_REVIEWS = (
    {"announce": date(2026, 11, 11), "effective": date(2026, 12, 1), "name": "2026年11月"},
    {"announce": date(2027, 2, 9), "effective": date(2027, 3, 1), "name": "2027年2月"},
    {"announce": date(2027, 5, 10), "effective": date(2027, 5, 28), "name": "2027年5月"},
    {"announce": date(2027, 8, 12), "effective": date(2027, 9, 1), "name": "2027年8月"},
    {"announce": date(2027, 11, 11), "effective": date(2027, 12, 1), "name": "2027年11月"},
)

SOURCES = {
    "fomc": {
        "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        # 月份與日期分在兩個標籤，頁面上沒有連續的 "December 8-9"。改逐日解析。
        "needles": (),
    },
    "cbc": {
        "url": "https://www.cbc.gov.tw/tw/cp-357-189514-82841-1.html",
        "needles": ("12月17日",),
    },
    "us_cpi": {
        "url": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "needles": ("Dec. 10, 2026",),
    },
    "us_payrolls": {
        "url": "https://www.bls.gov/schedule/news_release/empsit.htm",
        "needles": ("Dec. 04, 2026",),
    },
    "us_gdp": {
        "url": "https://www.bea.gov/news/schedule/full",
        "needles": ("November 25", "3rd Quarter 2026"),
    },
    "tw_gdp": {
        "url": "https://nstatdb.dgbas.gov.tw/dgbasall/webMain.aspx?funid=upcoming&sys=100",
        "needles": ("115-11-30",),
    },
    "msci": {
        "url": "https://app2.msci.com/eqb/pressreleases/archive/ir_dates.csv",
        # 檔案是 MM-DD-YYYY，而且「接下來八次」會把已經公布的那次移出。改逐日對。
        "needles": (),
    },
}

_FOMC_MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}
_FOMC_ROW = re.compile(
    r"<strong>(January|February|March|April|May|June|July|August|September|October|November|December)</strong>\s*</div>\s*"
    r'<div class="fomc-meeting__date[^"]*">\s*([^<]+?)\s*</div>',
    re.IGNORECASE,
)
_MSCI_DATE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")


def parse_fomc_decision_dates(html: str) -> set[date]:
    """決策日是會議區間的最後一天。對不到的字（例如 notation vote）略過。"""
    text = str(html or "")
    parts = re.split(r'<a id="\d+">(\d{4}) FOMC Meetings</a>', text)
    found: set[date] = set()
    for index in range(1, len(parts), 2):
        try:
            year = int(parts[index])
        except ValueError:
            continue
        body = re.split(r'<a id="\d+">\d{4} FOMC Meetings</a>', parts[index + 1])[0]
        for month_name, raw in _FOMC_ROW.findall(body):
            token = raw.strip().rstrip("*").strip()
            if not re.fullmatch(r"\d{1,2}(?:-\d{1,2})?", token):
                continue
            month = _FOMC_MONTHS.get(month_name.title())
            if month is None:
                continue
            try:
                found.add(date(year, month, int(token.split("-")[-1])))
            except ValueError:
                continue
    return found


def parse_msci_dates(text: str) -> set[date]:
    """MSCI 檢討 CSV 的日期是月-日-年。"""
    found: set[date] = set()
    for month, day, year in _MSCI_DATE.findall(str(text or "")):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return found


PENDING = "官方日期待確認"


def confirm_official_dates(get=None) -> set[str]:
    """官方頁對得到才算確認。FOMC 與 MSCI 是逐日鍵，其餘來源仍是整表鍵。

    抓不到、或表上的日期不在頁面裡，那個來源／那一天不會在回傳集合。不新增表上沒有的日期。
    """
    getter = get or _http_get
    confirmed: set[str] = set()
    for key, spec in SOURCES.items():
        body = _read_source(getter, spec["url"])
        if body is None:
            continue
        if key == "fomc":
            live = parse_fomc_decision_dates(body)
            for day in FOMC_DECISIONS:
                if day in live:
                    confirmed.add(f"fomc:{day.isoformat()}")
            continue
        if key == "msci":
            live = parse_msci_dates(body)
            for item in MSCI_REVIEWS:
                for day in (item["announce"], item["effective"]):
                    if day in live:
                        confirmed.add(f"msci:{day.isoformat()}")
            continue
        if spec["needles"] and all(needle in body for needle in spec["needles"]):
            confirmed.add(key)
    return confirmed


def _read_source(getter, url: str) -> str | None:
    try:
        response = getter(url, timeout=8)
        status = getattr(response, "status_code", 0)
        if status != 200:
            return None
        body = response.text if isinstance(getattr(response, "text", None), str) else ""
    except Exception:
        return None
    return body


def _http_get(url: str, timeout: int = 8):
    import requests

    return requests.get(url, timeout=timeout, headers={"User-Agent": "quant-command-center/1.0"})
