"""第三頁用的官方日期表。最後核對日見 VERIFIED_ON。

抓取只用來確認官方頁面仍含表上的日期。抓不到或對不到字串時，沿用這張表，
呼叫端把確定性標成「官方日期待確認」。這裡不推算、不補沒公布的下一個日期。
"""

from __future__ import annotations

from datetime import date

VERIFIED_ON = "2026-11-11"

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
        "needles": ("December 8-9",),
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
        "needles": ("11-11-2026", "12-01-2026"),
    },
}
PENDING = "官方日期待確認"


def confirm_official_dates(get=None) -> set[str]:
    """官方頁面仍含表上的對照字串才算確認。失敗的來源不在回傳集合裡。"""
    getter = get or _http_get
    confirmed = set()
    for key, spec in SOURCES.items():
        try:
            response = getter(spec["url"], timeout=8)
            status = getattr(response, "status_code", 0)
            if status != 200:
                continue
            body = response.text if isinstance(getattr(response, "text", None), str) else ""
        except Exception:
            continue
        if all(needle in body for needle in spec["needles"]):
            confirmed.add(key)
    return confirmed


def _http_get(url: str, timeout: int = 8):
    import requests

    return requests.get(url, timeout=timeout, headers={"User-Agent": "quant-command-center/1.0"})
