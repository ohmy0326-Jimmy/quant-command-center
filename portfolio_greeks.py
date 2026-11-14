"""全組合 Delta／Theta，以及依多空狀態的目標 Beta。

台股與美股分開加總，不互相抵銷。
缺希臘值是 null，不當成 0。Delta 剛好是 0 仍是 0。
目標 Beta 區間是程式預設，不是使用者確認過的規則。
"""

from __future__ import annotations

import math

REGIMES = ("牛", "偏多", "中性", "偏空", "熊")

# 程式預設。台美同一組起跳數字，但分開存放，畫面可以各自改。
DEFAULT_BETA_BANDS = {
    "TW": {
        "牛": (1.10, 1.40),
        "偏多": (0.90, 1.20),
        "中性": (0.70, 1.00),
        "偏空": (0.40, 0.70),
        "熊": (0.10, 0.40),
    },
    "US": {
        "牛": (1.10, 1.40),
        "偏多": (0.90, 1.20),
        "中性": (0.70, 1.00),
        "偏空": (0.40, 0.70),
        "熊": (0.10, 0.40),
    },
}

TX_MULTIPLIER = 200.0
MTX_MULTIPLIER = 50.0
MICRO_MULTIPLIER = 10.0
TW_OPTION_MULTIPLIER = 50.0
US_OPTION_MULTIPLIER = 100.0
DEFAULT_NOTE = "程式預設，不是已確認的策略參數。"
HEDGE_FUTURES_NOTE = "僅期貨；選擇權口數因 Delta 會變動，需即時選擇權 Delta，暫不提供。"
HEDGE_POINT_VALUES = (
    ("大台", TX_MULTIPLIER),
    ("小台", MTX_MULTIPLIER),
    ("微台", MICRO_MULTIPLIER),
)


def _number(value, allow_zero: bool = True):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if not allow_zero and number == 0:
        return None
    return number


def market_of(kind: str, symbol: str) -> str:
    text = str(kind or "")
    ticker = str(symbol or "").upper()
    if "美" in text and "台" not in text:
        return "US"
    if "台" in text or ticker.endswith(".TW") or ticker.startswith("TXO"):
        return "TW"
    if ticker in {"TX", "MTX", "FMF", "TMF"}:
        return "TW"
    digits = "".join(ch for ch in ticker if ch.isdigit())
    if len(digits) == 4:
        return "TW"
    return "US"


def _is_option(kind: str, symbol: str) -> bool:
    text = f"{kind} {symbol}".lower()
    return any(token in text for token in ("選擇權", "期權", "option", "txo"))


def _band(market: str, regime: str, overrides: dict | None) -> tuple[float, float] | None:
    if regime not in REGIMES:
        return None
    custom = (overrides or {}).get(market, {}).get(regime)
    if custom is not None:
        low, high = custom
        if low is None or high is None or high < low:
            return None
        return float(low), float(high)
    pair = DEFAULT_BETA_BANDS.get(market, {}).get(regime)
    if pair is None:
        return None
    return float(pair[0]), float(pair[1])


def round_half_up(value: float) -> int:
    """正數四捨五入到整數。0.5 進位。"""
    return int(math.floor(float(value) + 0.5))


def hedge_contracts(delta_points, points_complete: bool) -> dict:
    """用台股 delta_points（每點台幣）換算放空口數。已持有的空單已在點數裡，這裡不再扣。

    點數不完整或不大於 0 時不給口數。選擇權口數不在這張表。
    """
    base = {"note": HEDGE_FUTURES_NOTE, "rows": []}
    if not points_complete or delta_points is None:
        return {**base, "available": False, "reason": "台股 Delta 點數不完整，不計算避險口數。"}
    points = _number(delta_points)
    if points is None or points <= 0:
        return {**base, "available": False, "reason": "台股 Delta 點數不大於 0，沒有需要放空的口數。"}
    rows = []
    for name, point_value in HEDGE_POINT_VALUES:
        full = points / point_value
        half = full * 0.5
        rows.append({
            "name": name,
            "point_value": point_value,
            "full": full,
            "full_lots": round_half_up(full),
            "half": half,
            "half_lots": round_half_up(half),
        })
    return {**base, "available": True, "reason": "", "rows": rows}


def _empty_market(currency: str) -> dict:
    return {
        "currency": currency,
        "delta_points": 0.0,
        "delta_notional": 0.0,
        "theta_per_day": 0.0,
        "delta_complete": True,
        "points_complete": True,
        "theta_complete": True,
        "seen": 0,
        "known_delta": 0,
        "missing": [],
    }


def _option_multiplier(row: dict, market: str) -> float | None:
    explicit = _number(row.get("合約乘數"), allow_zero=False)
    if explicit is not None:
        return explicit
    return US_OPTION_MULTIPLIER if market == "US" else TW_OPTION_MULTIPLIER


def _future_multiplier(symbol: str, row: dict) -> float | None:
    explicit = _number(row.get("合約乘數"), allow_zero=False)
    if explicit is not None:
        return explicit
    from portfolio_risk import futures_multiplier

    value = futures_multiplier(symbol)
    return None if value is None or value <= 0 else float(value)


def position_greeks(row: dict) -> dict:
    """一筆部位的原生貨幣 Delta 名目與每日 Theta。算不出來是 None。"""
    kind = str(row.get("種類") or "")
    symbol = str(row.get("標的") or row.get("symbol") or "")
    market = market_of(kind, symbol)
    qty = _number(row.get("持有數量") if "持有數量" in row else row.get("qty"))
    option = _is_option(kind, symbol)
    future = ("期" in kind) and not option
    needs_theta = option
    theta = None
    notional = None
    if option:
        delta = _number(row.get("Delta") if "Delta" in row else row.get("delta"))
        underlying = _number(row.get("標的現價"), allow_zero=False) or _number(row.get("即時報價"), allow_zero=False)
        multiplier = _option_multiplier(row, market)
        if delta is None or qty is None or underlying is None or multiplier is None:
            notional = None
        else:
            notional = delta * qty * multiplier * underlying
        theta_quote = _number(row.get("Theta") if "Theta" in row else row.get("theta"))
        if theta_quote is None or qty is None or multiplier is None:
            theta = None
        else:
            theta = theta_quote * qty * multiplier
    elif future:
        price = _number(row.get("即時報價") if "即時報價" in row else row.get("price"), allow_zero=False)
        multiplier = _future_multiplier(symbol, row)
        if qty is None or price is None or multiplier is None:
            notional = None
        else:
            notional = qty * price * multiplier
        theta = 0.0
        needs_theta = False
    else:
        price = _number(row.get("即時報價") if "即時報價" in row else row.get("price"), allow_zero=False)
        leverage = _number(row.get("採用槓桿"))
        if leverage is None:
            leverage = _number(row.get("標的槓桿")) or 1.0
        if qty is None or price is None:
            notional = None
        else:
            notional = qty * price * leverage
        theta = 0.0
        needs_theta = False
    return {
        "symbol": symbol,
        "market": market,
        "notional": notional,
        "theta": theta,
        "needs_theta": needs_theta,
    }


def aggregate_greeks(rows, tw_index=None, us_index=None) -> dict:
    """台股用 TWD 與台指點數，美股用 USD 與標普點數。沒有指數價時點數是 null。"""
    books = {"tw": _empty_market("TWD"), "us": _empty_market("USD")}
    indexes = {"tw": _number(tw_index, allow_zero=False), "us": _number(us_index, allow_zero=False)}
    iterable = []
    if rows is None:
        iterable = []
    elif hasattr(rows, "iterrows"):
        iterable = [record for _, record in rows.iterrows()]
    else:
        iterable = list(rows)
    if not iterable:
        for book in books.values():
            book["delta_points"] = None
            book["points_complete"] = False
        return _pack_greeks(books)
    for row in iterable:
        item = row if isinstance(row, dict) else position_greeks(row)
        if "notional" not in item:
            item = position_greeks(item)
        book = books["us" if item["market"] == "US" else "tw"]
        book["seen"] += 1
        if item["notional"] is None:
            book["delta_complete"] = False
            book["points_complete"] = False
            if item["symbol"]:
                book["missing"].append(item["symbol"])
        else:
            book["delta_notional"] += item["notional"]
            book["known_delta"] += 1
        if item["needs_theta"] and item["theta"] is None:
            book["theta_complete"] = False
            if item["symbol"] and item["symbol"] not in book["missing"]:
                book["missing"].append(item["symbol"])
        elif item["theta"] is not None and book["theta_per_day"] is not None:
            book["theta_per_day"] += item["theta"]
    for key, book in books.items():
        if not book["delta_complete"] or book["known_delta"] == 0:
            if book["known_delta"] == 0:
                book["delta_notional"] = None
            book["points_complete"] = False
        if not book["points_complete"] or indexes[key] is None or book["delta_notional"] is None:
            book["delta_points"] = None
            if indexes[key] is None:
                book["points_complete"] = False
        else:
            book["delta_points"] = book["delta_notional"] / indexes[key]
        if not book["theta_complete"]:
            book["theta_per_day"] = None
    return _pack_greeks(books)


def _pack_greeks(books: dict) -> dict:
    found = {"tw": _freeze(books["tw"]), "us": _freeze(books["us"])}
    tw = found["tw"]
    tw["hedge_contracts"] = hedge_contracts(tw.get("delta_points"), bool(tw.get("points_complete")))
    return found


def _freeze(book: dict) -> dict:
    no_rows = book["seen"] == 0
    notional = 0.0 if no_rows else book["delta_notional"]
    if not no_rows and (not book["delta_complete"] or book["known_delta"] == 0):
        notional = None if book["known_delta"] == 0 else book["delta_notional"]
    points = 0.0 if no_rows else book["delta_points"]
    if no_rows:
        points_complete = True
    elif not book["points_complete"] or points is None:
        points = None
        points_complete = False
    else:
        points_complete = True
    theta = 0.0 if no_rows else book["theta_per_day"]
    if not no_rows and not book["theta_complete"]:
        theta = None
    return {
        "currency": book["currency"],
        "delta_points": points,
        "delta_notional": notional,
        "theta_per_day": theta,
        "delta_complete": bool(no_rows or (book["delta_complete"] and book["known_delta"] > 0)),
        "points_complete": bool(points_complete),
        "theta_complete": bool(no_rows or book["theta_complete"]),
        "missing": list(dict.fromkeys(book["missing"])),
    }


def beta_targets(regime: str | None, tw_beta, us_beta, tw_nav, us_nav, *, tw_index=None, us_index=None, usdtwd=None, beta_complete: bool = True, overrides: dict | None = None) -> dict:
    """區間外才估算同市場避險。狀態不在五種裡就不設目標。"""
    name = str(regime or "").strip()
    known = name in REGIMES
    return {
        "default": True,
        "note": DEFAULT_NOTE,
        "regime": name if known else None,
        "tw": _market_target("TW", name if known else None, tw_beta, tw_nav, tw_index, None, beta_complete, overrides),
        "us": _market_target("US", name if known else None, us_beta, us_nav, us_index, usdtwd, beta_complete, overrides),
    }


def _market_target(market, regime, current, nav, index_level, usdtwd, beta_complete, overrides):
    band = _band(market, regime, overrides) if regime else None
    if band is None:
        return None
    current_value = _number(current)
    nav_value = _number(nav, allow_zero=False)
    if not beta_complete or current_value is None or nav_value is None:
        return {
            "low": band[0],
            "high": band[1],
            "current": current_value,
            "complete": False,
            "inside": None,
            "gap": None,
            "hedge_notional": None,
            "instruments": "台指期／小台／TXO" if market == "TW" else "SPX／XSP／SPY",
            "tx_lots": None,
            "mtx_lots": None,
            "index_points": None,
        }
    low, high = band
    if current_value > high:
        gap = current_value - high
    elif current_value < low:
        gap = current_value - low
    else:
        gap = 0.0
    native_nav = nav_value
    index = _number(index_level, allow_zero=False)
    fx = _number(usdtwd, allow_zero=False)
    if market == "US":
        if fx is None:
            hedge = None
            points = None
        else:
            hedge = gap * nav_value / fx
            points = None if index is None or hedge is None else hedge / index
            native_nav = nav_value / fx
    else:
        hedge = gap * nav_value
        points = None if index is None else hedge / index
    tx_lots = None if market != "TW" or index is None or hedge is None else hedge / (index * TX_MULTIPLIER)
    mtx_lots = None if market != "TW" or index is None or hedge is None else hedge / (index * MTX_MULTIPLIER)
    return {
        "low": low,
        "high": high,
        "current": current_value,
        "complete": True,
        "inside": gap == 0.0,
        "gap": gap,
        "hedge_notional": hedge,
        "nav": native_nav,
        "instruments": "台指期／小台／TXO" if market == "TW" else "SPX／XSP／SPY",
        "tx_lots": tx_lots,
        "mtx_lots": mtx_lots,
        "index_points": points,
    }


def apply_indexes(greeks: dict, tw_index=None, us_index=None) -> dict:
    """名目已經完整時，才用指數價換算點數。"""
    found = {"tw": dict(greeks.get("tw") or {}), "us": dict(greeks.get("us") or {})}
    for key, index in (("tw", tw_index), ("us", us_index)):
        book = found[key]
        level = _number(index, allow_zero=False)
        if book.get("delta_complete") and book.get("delta_notional") is not None and level is not None:
            book["delta_points"] = book["delta_notional"] / level
            book["points_complete"] = True
        elif book.get("delta_notional") == 0 and book.get("delta_complete") and not book.get("missing"):
            book["delta_points"] = 0.0
            book["points_complete"] = True
    tw = found["tw"]
    tw["hedge_contracts"] = hedge_contracts(tw.get("delta_points"), bool(tw.get("points_complete")))
    return found


def trusted_regime(view: dict | None) -> str | None:
    """只有試算表上的五種狀態才拿來設目標。倉庫示例不當成正式狀態。"""
    if not view or view.get("data_origin") != "sheet":
        return None
    regime = str(view.get("regime") or "").strip()
    return regime if regime in REGIMES else None
