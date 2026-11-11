"""第五頁四個手動指標的代理分數，以及覆蓋率。

分數是 -2 到 +2。這個模組不連網。缺資料回傳 None，不當 0。
倉庫 JSON 的示例分數不在這裡採用。
"""

from __future__ import annotations

from datetime import date

# 費城聯準會製造業調查（擴散指數，季調）。使用者點名的代號，FRED 有這三條。
PHILLY_HEADLINE = "GACDFSA066MSFRBPHI"
PHILLY_NEW_ORDERS = "NOCDFSA066MSFRBPHI"
PHILLY_INVENTORIES = "IVCDFSA066MSFRBPHI"

# 紐約聯準會帝國製造業調查。使用者寫的 GACDINA066MSFRBNY、NOCDINA、IVCDINA 在 FRED 不存在。
# 實際代號是 DISA，不是 INA。
EMPIRE_HEADLINE = "GACDISA066MSFRBNY"
EMPIRE_NEW_ORDERS = "NOCDISA066MSFRBNY"
EMPIRE_INVENTORIES = "IVCDISA066MSFRBNY"
EMPIRE_IDS_NOT_ON_FRED = (
    "GACDINA066MSFRBNY",
    "NOCDINA066MSFRBNY",
    "IVCDINA066MSFRBNY",
)

# 國發會景氣指標開放資料。圖表 API（POST /n/json/lightscore）會被 Cloudflare 擋住，改走這份。
NDC_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/6099"
NDC_SCORE_FILE = "景氣指標與燈號.csv"
NDC_SCORE_COLUMN = "景氣對策信號綜合分數"
# 官方紅燈是 38～45。檔案裡的燈號寫「紅」。
NDC_RED_MIN = 38

# 財政部關務署海關進出口貿易統計（data.gov.tw 資料集 6053）。年增率由出口總值自己算。
CUSTOMS_EXPORT_URL = "https://opendata.customs.gov.tw/data/6053/csv.csv"
CUSTOMS_EXPORT_COLUMN = "出口總值(新臺幣千元)"

# 評價：標普 500 本益比的倒數是盈餘殖利率，減 FRED DFII10。沒有單獨的 ERP 序列。
SPX_TICKER = "^GSPC"
REAL_YIELD_SERIES = "DFII10"

# 產業相對強弱。沒有穩定產業 ETF 的不硬套，留給手動覆寫。
# 台股對加權指數，其餘對 SPY。代號是價格推估用的，不是基本面分類。
INDUSTRY_ETFS: dict[str, tuple[str, str]] = {
    "半導體設備": ("SMH", "SPY"),
    "AI 算力": ("SOXX", "SPY"),
    "軟體雲端": ("IGV", "SPY"),
    "生技醫療": ("XBI", "SPY"),
    "綠能儲能": ("ICLN", "SPY"),
    "重電電網": ("GRID", "SPY"),
    "汽車零組件": ("CARZ", "SPY"),
    "先進製程": ("00891.TW", "^TWII"),
    "IC 設計": ("00892.TW", "^TWII"),
    "金融": ("0055.TW", "^TWII"),
}
UNMAPPED_INDUSTRIES = (
    "光通訊/CPO",
    "消費電子",
    "記憶體",
    "伺服器組裝",
    "封測",
    "半導體材料",
    "PCB/載板",
    "被動元件",
    "面板",
    "航運",
)
PRICE_CYCLE_LABEL = "價格推估，非基本面"
RS_LAG_3M = 63
RS_LAG_6M = 126

PROXY_IDS = ("pmi", "earnings_revisions", "valuation", "tw_activity")
VALUATION_WEIGHT = 0.5
OVERRIDE_DAYS = 45
COVERAGE_INCOMPLETE = 0.70
COVERAGE_FORCE_NEUTRAL = 0.50
TOTAL_SCORE_WEIGHT = 8.5  # 八項權重 1，評價 0.5


def indicator_weight(indicator_id: str) -> float:
    if indicator_id == "valuation":
        return VALUATION_WEIGHT
    return 1.0


def clamp_score(value: int) -> int:
    return max(-2, min(2, int(value)))


def round_half_away(value: float) -> int:
    if value >= 0:
        return int(value + 0.5)
    return int(value - 0.5)


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def manual_override_fresh(source: str | None, updated_at, today, days: int = OVERRIDE_DAYS) -> bool:
    """source 必須是手動，而且 updated_at 落在今天往回 days 天以內（含當天與第 days 天）。"""
    if str(source or "").strip().lower() not in {"manual", "手動"}:
        return False
    updated = _as_date(updated_at)
    current = _as_date(today)
    if updated is None or current is None:
        return False
    age = (current - updated).days
    return 0 <= age <= days


def score_pmi_level(mean: float, direction: str) -> int:
    """三個月均值與方向。mean 在 -5～+5 而且方向不明才是 0，那一條優先。

    大於 +10 且上升是 +2；大於 +10 但沒有上升是 +1。
    0 的邊界：大於 0 到 +10 是 +1，小於 0 到 -10 是 -1，剛好 0 且方向明確也是 0。
    """
    if -5 <= mean <= 5 and direction == "unclear":
        return 0
    if mean > 10 and direction == "rising":
        return 2
    if mean > 10:
        return 1
    if 0 < mean <= 10:
        return 1
    if mean == 0:
        return 0
    if -10 <= mean < 0:
        return -1
    if mean < -10:
        return -2
    return 0


def pmi_confirmation(base: int, spread_delta: float | None, headline_delta: float | None) -> int:
    """新訂單減庫存比總指數先轉，加或減 1，再夾回 -2～+2。沒有差值就不調。"""
    if spread_delta is None or headline_delta is None:
        return clamp_score(base)
    adjust = 0
    if spread_delta > 0 and headline_delta <= 0:
        adjust = 1
    elif spread_delta < 0 and headline_delta >= 0:
        adjust = -1
    return clamp_score(base + adjust)


def _month_key(value) -> str:
    text = str(value)[:10]
    return text[:7]


def average_two_surveys(left: list[tuple[str, float]], right: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """同一個月兩邊都有才平均。缺一邊的月份不拿單邊充數。"""
    left_map = {_month_key(day): float(value) for day, value in left or []}
    right_map = {_month_key(day): float(value) for day, value in right or []}
    months = sorted(set(left_map) & set(right_map))
    return [(month, (left_map[month] + right_map[month]) / 2.0) for month in months]


def _window_mean(points: list[tuple[str, float]], start: int, end: int) -> float | None:
    window = points[start:end]
    if not window:
        return None
    return sum(value for _month, value in window) / len(window)


def _three_month_delta(points: list[tuple[str, float]]) -> float | None:
    if len(points) < 6:
        return None
    recent = _window_mean(points, -3, None)
    prior = _window_mean(points, -6, -3)
    if recent is None or prior is None:
        return None
    return recent - prior


def _direction_from_delta(delta: float | None) -> str:
    if delta is None or delta == 0:
        return "unclear"
    return "rising" if delta > 0 else "falling"


def score_pmi_series(
    philly: list[tuple[str, float]] | None,
    empire: list[tuple[str, float]] | None,
    philly_orders: list[tuple[str, float]] | None = None,
    philly_inventories: list[tuple[str, float]] | None = None,
    empire_orders: list[tuple[str, float]] | None = None,
    empire_inventories: list[tuple[str, float]] | None = None,
) -> dict:
    """費城與紐約總指數先按月平均，再用三個月均值與方向。確認項可缺。"""
    headline = average_two_surveys(philly or [], empire or [])
    if len(headline) < 3:
        return {"ok": False, "score": None, "reason": "費城或紐約製造業調查不足三個月"}
    mean = _window_mean(headline, -3, None)
    delta = _three_month_delta(headline)
    direction = _direction_from_delta(delta)
    base = score_pmi_level(float(mean), direction)
    spread = _survey_spread(philly_orders, philly_inventories, empire_orders, empire_inventories)
    spread_delta = _three_month_delta(spread) if spread else None
    score = pmi_confirmation(base, spread_delta, delta)
    note = "代理：費城 GACDFSA066MSFRBPHI 與紐約 GACDISA066MSFRBNY 的三個月均值。"
    if spread_delta is None:
        note += "新訂單減庫存不足，沒有加減 1。"
    else:
        note += "新訂單減庫存若先轉，分數加減 1。"
    return {
        "ok": True,
        "score": score,
        "value": f"三月均值 {mean:+.1f}（{direction}）",
        "trend": direction,
        "data_date": headline[-1][0],
        "note": note,
        "layer": "代理",
    }


def _survey_spread(philly_orders, philly_inventories, empire_orders, empire_inventories):
    philly = _orders_minus_inventories(philly_orders, philly_inventories)
    empire = _orders_minus_inventories(empire_orders, empire_inventories)
    if philly and empire:
        return average_two_surveys(philly, empire)
    return philly or empire


def _orders_minus_inventories(orders, inventories) -> list[tuple[str, float]]:
    order_map = {_month_key(day): float(value) for day, value in orders or []}
    inventory_map = {_month_key(day): float(value) for day, value in inventories or []}
    months = sorted(set(order_map) & set(inventory_map))
    return [(month, order_map[month] - inventory_map[month]) for month in months]


def score_tw_activity(score: float | None, export_yoy: float | None, history: list[float] | None = None) -> dict:
    """國發會綜合分數加上出口年增率。兩邊缺一就不是 0，是未知。

    紅燈連兩個月而且分數下滑時，結果不高於 +1。紅燈用官方 38 分。
    """
    if score is None or export_yoy is None:
        return {"ok": False, "score": None, "reason": "景氣對策信號或出口年增率缺資料"}
    score = float(score)
    export_yoy = float(export_yoy)
    if 9 <= score <= 16:
        base = -2
    elif 17 <= score <= 22 or export_yoy < 0:
        base = -1
    elif 32 <= score <= 45 and export_yoy > 15:
        base = 2
    elif 23 <= score <= 31 and export_yoy > 0:
        base = 1
    else:
        return {
            "ok": False,
            "score": None,
            "reason": f"分數 {score:g}、出口年增率 {export_yoy:g}% 沒有對上規則",
        }
    series = [float(item) for item in (history or [])]
    capped = False
    if len(series) >= 2 and series[-1] >= NDC_RED_MIN and series[-2] >= NDC_RED_MIN and series[-1] < series[-2]:
        if base > 1:
            base = 1
            capped = True
    note = "代理：國發會景氣對策信號與財政部關務署出口總值年增率。"
    if capped:
        note += "連續兩個月紅燈且分數下滑，上限壓在 +1。"
    return {
        "ok": True,
        "score": base,
        "value": f"對策信號 {score:g}，出口年增率 {export_yoy:+.1f}%",
        "trend": "紅燈降溫" if capped else "",
        "note": note,
        "layer": "代理",
    }


def score_erp(erp_pct: float, forward: bool) -> int:
    """追溯門檻：>4 +2、3～4 +1、2～3 0、1～2 -1、<1 -2。遠期整組下移 0.5 個百分點。

    上緣給較高的那一檔：追溯的 4% 是 +1（要大於 4 才 +2），3% 是 +1。
    """
    shift = 0.5 if forward else 0.0
    plus_two = 4 - shift
    plus_one = 3 - shift
    zero = 2 - shift
    minus_one = 1 - shift
    if erp_pct > plus_two:
        return 2
    if erp_pct >= plus_one:
        return 1
    if erp_pct >= zero:
        return 0
    if erp_pct >= minus_one:
        return -1
    return -2


def score_valuation(earnings_yield_pct: float | None, real_yield_pct: float | None, forward: bool) -> dict:
    if earnings_yield_pct is None or real_yield_pct is None:
        return {"ok": False, "score": None, "reason": "盈餘殖利率或 DFII10 缺資料"}
    erp = float(earnings_yield_pct) - float(real_yield_pct)
    kind = "遠期" if forward else "追溯"
    return {
        "ok": True,
        "score": score_erp(erp, forward),
        "value": f"{kind} ERP {erp:.2f}%（盈餘殖利率 {earnings_yield_pct:.2f}% − 實質利率 {real_yield_pct:.2f}%）",
        "trend": kind,
        "note": f"代理：標普 500 {kind}本益比換算的盈餘殖利率減 FRED DFII10。權重 0.5。",
        "layer": "代理",
        "weight": VALUATION_WEIGHT,
        "yield_kind": kind,
    }


def score_earnings_revisions() -> dict:
    return {"ok": False, "score": None, "reason": "企業獲利修正沒有自動代理", "layer": "未知"}


def cycle_from_relative(rs_3m: float | None, rs_6m: float | None) -> tuple[str | None, str | None]:
    """3 個月與 6 個月相對強弱。0 不歸邊。

    兩段都正、但 3 個月沒有高於 6 個月時仍標成長，方向改持平或下行。
    這是四條明示規則以外、兩段都還是正報酬的情況。
    """
    if rs_3m is None or rs_6m is None:
        return None, None
    rs_3m = float(rs_3m)
    rs_6m = float(rs_6m)
    if rs_3m == 0 or rs_6m == 0:
        return None, None
    if rs_3m > rs_6m:
        direction = "上行"
    elif rs_3m < rs_6m:
        direction = "下行"
    else:
        direction = "持平"
    if rs_3m > 0 and rs_6m > 0:
        return "成長", direction
    if rs_3m > 0 and rs_6m < 0:
        return "復甦", direction
    if rs_3m < 0 and rs_6m > 0:
        return "趨緩", direction
    if rs_3m < 0 and rs_6m < 0:
        return "衰退", direction
    return None, None


def relative_return(closes: list[float] | None, lag: int) -> float | None:
    if not closes or len(closes) <= lag:
        return None
    base = float(closes[-1 - lag])
    if base == 0:
        return None
    return float(closes[-1]) / base - 1.0


def relative_strength(sector: list[float] | None, benchmark: list[float] | None) -> tuple[float | None, float | None]:
    rs_3m = _spread_return(sector, benchmark, RS_LAG_3M)
    rs_6m = _spread_return(sector, benchmark, RS_LAG_6M)
    return rs_3m, rs_6m


def _spread_return(sector, benchmark, lag: int) -> float | None:
    left = relative_return(sector, lag)
    right = relative_return(benchmark, lag)
    if left is None or right is None:
        return None
    return left - right


def aggregate_scores(indicators: list[dict]) -> dict:
    """總分 = 已有的加權分數 / 已有權重 × 全部權重。缺項不當 0。

    覆蓋率低於 70% 標不完整，槓桿不高於中性、現金不低於中性。
    低於 50% 直接用中性配置。中性的數字來自 macro_cycle.REGIME_RISK，不在這裡另寫一套。
    """
    from macro_cycle import regime_for_score, risk_params_for_regime

    available: list[tuple[int, float]] = []
    for item in indicators or []:
        if item.get("example") or not item.get("scored"):
            continue
        if item.get("score") is None:
            continue
        available.append((int(item["score"]), indicator_weight(str(item.get("id") or ""))))
    available_weight = sum(weight for _score, weight in available)
    coverage = 0.0 if TOTAL_SCORE_WEIGHT == 0 else available_weight / TOTAL_SCORE_WEIGHT
    neutral = risk_params_for_regime("中性")
    if available_weight <= 0:
        return _coverage_result(None, None, "中性", neutral, coverage, True, "沒有可計分的指標，沿用中性。")
    weighted = sum(score * weight for score, weight in available)
    raw = weighted / available_weight * TOTAL_SCORE_WEIGHT
    total = max(-18, min(18, round_half_away(raw)))
    raw_regime = regime_for_score(total)
    if coverage < COVERAGE_FORCE_NEUTRAL:
        return _coverage_result(
            total,
            raw_regime,
            "中性",
            neutral,
            coverage,
            True,
            "覆蓋率低於 50%，改用中性配置（槓桿 1.5 倍、現金 15%）。",
        )
    risk = risk_params_for_regime(raw_regime)
    if coverage < COVERAGE_INCOMPLETE:
        risk = {
            "regime": raw_regime,
            "target_leverage": min(float(risk["target_leverage"]), float(neutral["target_leverage"])),
            "min_cash_pct": max(float(risk["min_cash_pct"]), float(neutral["min_cash_pct"])),
        }
        return _coverage_result(
            total,
            raw_regime,
            raw_regime,
            risk,
            coverage,
            True,
            "覆蓋率低於 70%，標成不完整。槓桿不高於中性，現金不低於中性。",
        )
    return _coverage_result(total, raw_regime, raw_regime, risk, coverage, False, "")


def _coverage_result(total, raw_regime, regime, risk, coverage, incomplete: bool, note: str) -> dict:
    return {
        "total": total,
        "raw_regime": raw_regime,
        "regime": regime,
        "risk": {
            "regime": regime,
            "target_leverage": float(risk["target_leverage"]),
            "min_cash_pct": float(risk["min_cash_pct"]),
        },
        "coverage": coverage,
        "coverage_status": "不完整" if incomplete else "完整",
        "coverage_note": note,
    }


def parse_fred_points(text: str) -> list[tuple[str, float]]:
    points = []
    for line in str(text or "").splitlines()[1:]:
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 2 or parts[1] in {"", "."}:
            continue
        try:
            points.append((parts[0][:10], float(parts[1])))
        except ValueError:
            continue
    return points


def parse_ndc_scores(text: str) -> list[dict]:
    import csv
    import io

    rows = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for row in reader:
        raw = str(row.get(NDC_SCORE_COLUMN) or "").strip()
        month = _yyyymm(row.get("Date"))
        if month is None or raw in {"", "-"}:
            continue
        try:
            score = float(raw)
        except ValueError:
            continue
        rows.append({"month": month, "score": score, "light": str(row.get("景氣對策信號") or "").strip()})
    rows.sort(key=lambda item: item["month"])
    return rows


def parse_export_yoy(text: str) -> dict:
    import csv
    import io

    totals: dict[tuple[int, int], float] = {}
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for row in reader:
        try:
            year = int(str(row.get("年度") or "").strip())
            month = int(str(row.get("月份") or "").strip())
            value = float(str(row.get(CUSTOMS_EXPORT_COLUMN) or "").replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if year < 1911:
            year += 1911
        totals[(year, month)] = value
    if not totals:
        return {"ok": False}
    year, month = max(totals)
    previous = totals.get((year - 1, month))
    latest = totals[(year, month)]
    if previous in (None, 0):
        return {"ok": False, "month": f"{year:04d}-{month:02d}"}
    return {
        "ok": True,
        "month": f"{year:04d}-{month:02d}",
        "yoy": (latest - previous) / previous * 100.0,
        "latest": latest,
        "previous": previous,
    }


def _yyyymm(value) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 6:
        return None
    year, month = int(digits[:4]), int(digits[4:6])
    if year < 1900 or not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def proxies_from_payloads(payload: dict, today: str) -> dict:
    """把已經下載的數列轉成四個代理與產業相對強弱。下載失敗是未知，不是 0。"""
    payload = payload or {}
    pmi_raw = payload.get("pmi") or {}
    pmi = score_pmi_series(
        pmi_raw.get("philly"),
        pmi_raw.get("empire"),
        pmi_raw.get("philly_orders"),
        pmi_raw.get("philly_inventories"),
        pmi_raw.get("empire_orders"),
        pmi_raw.get("empire_inventories"),
    )
    tw_scores = list(payload.get("ndc_scores") or [])
    export = payload.get("export") or {}
    history = [float(item["score"]) for item in tw_scores if item.get("score") is not None]
    latest_score = history[-1] if history else None
    yoy = export.get("yoy") if export.get("ok") else None
    tw = score_tw_activity(latest_score, yoy, history)
    if tw.get("ok"):
        dates = [item.get("month") for item in tw_scores if item.get("month")]
        if export.get("month"):
            dates.append(export["month"])
        tw["data_date"] = max(dates) if dates else today
    pe = payload.get("pe") or {}
    forward_pe = _positive(pe.get("forward"))
    trailing_pe = _positive(pe.get("trailing"))
    dfii_points = _dfii_points(payload.get("dfii10"))
    real_yield = dfii_points[-1][1] if dfii_points else None
    if forward_pe is not None:
        valuation = score_valuation(100.0 / forward_pe, real_yield, True)
    else:
        valuation = score_valuation(None if trailing_pe is None else 100.0 / trailing_pe, real_yield, False)
    if valuation.get("ok") and dfii_points:
        valuation["data_date"] = dfii_points[-1][0]
    closes = payload.get("closes") or {}
    industry_rs = {}
    for name, (symbol, benchmark) in INDUSTRY_ETFS.items():
        rs_3m, rs_6m = relative_strength(closes.get(symbol), closes.get(benchmark))
        cycle, direction = cycle_from_relative(rs_3m, rs_6m)
        industry_rs[name] = {
            "ok": bool(cycle and direction),
            "cycle": cycle,
            "direction": direction,
            "rs_3m": rs_3m,
            "rs_6m": rs_6m,
            "symbol": symbol,
            "benchmark": benchmark,
        }
    return {
        "pmi": pmi,
        "tw_activity": tw,
        "valuation": valuation,
        "earnings_revisions": score_earnings_revisions(),
        "industry_rs": industry_rs,
    }


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _dfii_points(value) -> list[tuple[str, float]]:
    if not value:
        return []
    first = value[0]
    if isinstance(first, tuple):
        return [(str(day), float(level)) for day, level in value]
    return [("", float(level)) for level in value]


def coverage_from_ids(present_ids: list[str]) -> float:
    weight = sum(indicator_weight(item_id) for item_id in present_ids)
    if TOTAL_SCORE_WEIGHT == 0:
        return 0.0
    return weight / TOTAL_SCORE_WEIGHT


def weighted_total(scores: dict[str, int]) -> int | None:
    if not scores:
        return None
    weight = sum(indicator_weight(item_id) for item_id in scores)
    if weight <= 0:
        return None
    raw = sum(int(scores[item_id]) * indicator_weight(item_id) for item_id in scores) / weight * TOTAL_SCORE_WEIGHT
    return max(-18, min(18, round_half_away(raw)))
