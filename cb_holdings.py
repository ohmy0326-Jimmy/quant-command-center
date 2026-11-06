"""可轉債（CB）與可轉債資產交換（CBAS）的市值、轉換價值與 Delta 曝險。

不連網、不下單。櫃買中心報價是百元報價：每張面額預設 100,000 元，
1 點報價 = 面額 / 100（預設 1,000 元）。

市值整筆進淨值，跟現股一樣是已付現的部位，不是期貨保證金。
名目曝險 = Delta × 轉換價值（張數帶正負），再乘標的 Beta 得到 Beta 曝險。
不加標的槓桿，也不把 CB 與 CBAS 互相抵銷，不做跨市場對沖。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

DEFAULT_FACE = 100_000.0
# 報價大於這個數，視為每張台幣市價，除以（面額/100）換回百元報價。
# 百元報價通常在數十到數百；每張市價是萬元以上。
QUOTE_AS_CASH_IF_ABOVE = 3_000.0

DELTA_SHEET = "試算表"
DELTA_FLOOR = "估算(債底)"
DELTA_PARITY = "估算(轉換價值/市價)"
DELTA_BS = "估算(Black-Scholes)"


def product_kind(kind) -> str | None:
    """種類 → ``cb`` / ``cbas`` / None。CBAS 要先判，因為字串裡也有「可轉債」。"""
    text = str(kind or "").strip().lower().replace(" ", "").replace("　", "")
    if not text or text == "nan":
        return None
    if "cbas" in text or "資產交換" in text:
        return "cbas"
    if text in {"cb", "convertible", "convertiblebond"} or "可轉債" in text or "轉換公司債" in text or "可轉換" in text:
        return "cb"
    return None


def is_convertible_row(row) -> bool:
    return product_kind(row.get("種類") if hasattr(row, "get") else None) is not None


def read_number(row, *names: str) -> float | None:
    for name in names:
        try:
            value = row.get(name) if hasattr(row, "get") else row[name]
        except Exception:
            continue
        number = _finite(value)
        if number is not None:
            return number
    return None


def _finite(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def face_value(row) -> float:
    face = read_number(row, "面額", "每張面額")
    if face is None or face <= 0:
        return DEFAULT_FACE
    return face


def point_value(face: float) -> float:
    return float(face) / 100.0


def as_quote_points(quote: float | None, face: float) -> float | None:
    """百元報價。大於門檻的數字當成每張台幣市價。"""
    if quote is None:
        return None
    scale = point_value(face)
    if scale > 0 and abs(quote) > QUOTE_AS_CASH_IF_ABOVE:
        return quote / scale
    return quote


def bond_quote_points(row, face: float) -> tuple[float | None, str]:
    """市價優先序：百元報價 → 即時報價 → 平均成本。"""
    for column, label in (
        ("百元報價", "持倉統計百元報價"),
        ("債券報價", "持倉統計債券報價"),
        ("即時報價", "即時報價"),
        ("平均成本", "平均成本"),
    ):
        raw = read_number(row, column)
        if raw is None:
            continue
        points = as_quote_points(raw, face)
        if points is not None:
            return points, label
    return None, ""


def yahoo_equity_ticker(ticker: str) -> str:
    text = str(ticker or "").strip().upper()
    if not text or text == "NAN":
        return ""
    if text.endswith(".TW") or text.endswith(".TWO"):
        return text
    if re.fullmatch(r"\d{4}", text):
        return text + ".TW"
    return text


def convertible_quote_context(frame) -> tuple:
    """報價快取用的 (標的, 標的股票)。沒有可轉債／CBAS 時是空 tuple。"""
    if frame is None or getattr(frame, "empty", True) or "標的" not in getattr(frame, "columns", []):
        return ()
    rows = []
    for _, row in frame.iterrows():
        if product_kind(row.get("種類")) is None:
            continue
        symbol = str(row.get("標的") or "").strip().upper()
        if symbol:
            rows.append((symbol, underlying_ticker_from_row(row)))
    return tuple(dict.fromkeys(rows))


def underlying_ticker_from_row(row) -> str:
    """標的股票。有填標的代號就用；沒填時，四碼股票代號才從「標的」推（五碼債碼不推）。"""
    for column in ("標的代號", "標的股票", "轉換標的"):
        raw = row.get(column) if hasattr(row, "get") else None
        text = str(raw or "").strip()
        if text and text.lower() != "nan":
            return yahoo_equity_ticker(text)
    symbol = str((row.get("標的") if hasattr(row, "get") else "") or "").strip().upper()
    base = symbol.split(".")[0]
    if re.fullmatch(r"\d{4}", base):
        return yahoo_equity_ticker(base)
    return ""


def conversion_value_points(stock_price: float | None, conversion_price: float | None) -> float | None:
    """轉換價值（百元報價）＝ 股價 / 轉換價格 × 100。跟可轉債報價同一單位。"""
    if stock_price is None or conversion_price is None:
        return None
    if stock_price <= 0 or conversion_price <= 0:
        return None
    return stock_price / conversion_price * 100.0


def conversion_value_amount(stock_price: float | None, conversion_price: float | None, face: float, qty: float) -> float | None:
    """整筆轉換價值（報價幣別）＝ 股價 × (面額 / 轉換價格) × 張數。張數為負時為負。"""
    if stock_price is None or conversion_price is None or conversion_price <= 0 or face <= 0:
        return None
    if stock_price <= 0:
        return None
    return stock_price * (face / conversion_price) * qty


def parity_delta(conversion_points: float | None, quote_points: float | None) -> float | None:
    """可轉債：轉換價值 / 市價，限制在 0～1。沒有正的市價就沒有 Delta。"""
    if conversion_points is None or quote_points is None or quote_points <= 0 or conversion_points < 0:
        return None
    return min(1.0, max(0.0, conversion_points / quote_points))


def bond_floor_delta(conversion_points: float | None, quote_points: float | None, floor_points: float | None) -> float | None:
    """有純債價值（百元）時：權利金裡有多少是轉換內含價值，限制在 0～1。

    內含 = max(轉換價值 − 純債價值, 0)，權利金 = 市價 − 純債價值。
    市價沒有高於債底（權利金 ≤ 0）時 Delta 為 0，不把折價當成股票。
    """
    if conversion_points is None or quote_points is None or floor_points is None:
        return None
    premium = quote_points - floor_points
    intrinsic = conversion_points - floor_points
    if premium <= 1e-9:
        return 0.0
    if intrinsic <= 0:
        return 0.0
    return min(1.0, max(0.0, intrinsic / premium))


def _cell(row, column: str):
    try:
        return row.get(column) if hasattr(row, "get") else row[column]
    except Exception:
        return None


def _years_to_expiry(row, today: date) -> float | None:
    raw = None
    for column in ("到期日", "到期日期", "到期", "Expiry", "Expiration"):
        candidate = _cell(row, column)
        if candidate is None:
            continue
        if isinstance(candidate, float) and not math.isfinite(candidate):
            continue
        text = str(candidate).strip()
        if not text or text.lower() in {"nan", "nat", "none"}:
            continue
        raw = candidate
        break
    if raw is None:
        return None
    expiry = _parse_date(raw)
    if expiry is None or expiry <= today:
        return None
    return (expiry - today).days / 365.0


def _parse_date(raw) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        number = float(raw)
        if 30_000 < number < 100_000:
            return (datetime(1899, 12, 30) + timedelta(days=number)).date()
    except (TypeError, ValueError):
        pass
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt != "%Y%m%d" else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def _volatility(row) -> float | None:
    raw = read_number(row, "波動率", "隱含波動率")
    if raw is None or raw <= 0:
        return None
    if raw > 3.0:
        raw = raw / 100.0
    if raw <= 0:
        return None
    return raw


def black_scholes_call_delta(spot: float, strike: float, t_years: float, sigma: float, rate: float = 0.0) -> float | None:
    """買權 Delta。沿用下單輔助的 Black-Scholes，不另寫一套公式。"""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return None
    from order_assistant import bs_price_greeks

    _price, delta, _theta = bs_price_greeks(spot, strike, t_years, rate, sigma, "call")
    if delta is None or not math.isfinite(float(delta)):
        return None
    return min(1.0, max(0.0, float(delta)))


def sheet_delta(row) -> float | None:
    """試算表手填的 Delta。估算字樣代表上一輪算出來的，要重算。"""
    source = str((row.get("Delta來源") if hasattr(row, "get") else "") or "")
    if source.startswith("估算"):
        return None
    for column in ("Delta", "delta"):
        number = read_number(row, column)
        if number is not None:
            return number
    return None


def resolve_delta(row, *, product: str, quote_points, conversion_points, floor_points, spot, conversion_price, today: date) -> tuple[float | None, str]:
    supplied = sheet_delta(row)
    if supplied is not None:
        return supplied, DELTA_SHEET
    if product == "cb":
        if floor_points is not None:
            estimated = bond_floor_delta(conversion_points, quote_points, floor_points)
            if estimated is not None:
                return estimated, DELTA_FLOOR
        estimated = parity_delta(conversion_points, quote_points)
        if estimated is not None:
            return estimated, DELTA_PARITY
        return None, ""
    sigma = _volatility(row)
    t_years = _years_to_expiry(row, today)
    rate = read_number(row, "利率")
    if rate is None:
        rate = 0.0
    if sigma is None or t_years is None or spot is None or conversion_price is None:
        return None, ""
    estimated = black_scholes_call_delta(spot, conversion_price, t_years, sigma, rate)
    if estimated is None:
        return None, ""
    return estimated, DELTA_BS


def underlying_beta(row) -> tuple[float, str]:
    for column in ("標的Beta", "採用標的Beta"):
        number = read_number(row, column)
        if number is not None and number > 0:
            return number, "標的Beta"
    own = read_number(row, "Beta值")
    source = str((row.get("Beta來源") if hasattr(row, "get") else "") or "")
    if own is not None and own > 0 and "暫用" not in source and "預設值" not in source:
        return own, source or "Beta值"
    return 1.0, "暫用 1"


@dataclass(frozen=True)
class ConvertibleMark:
    product: str
    quote_points: float | None
    quote_source: str
    cost_points: float | None
    face: float
    qty: float
    market_value: float
    cost_value: float
    pnl: float
    stock_price: float | None
    conversion_price: float | None
    conversion_points: float | None
    conversion_value: float | None
    delta: float | None
    delta_source: str
    premium_pct: float | None
    equity_exposure: float | None
    beta: float
    beta_note: str
    underlying: str

    @property
    def beta_source(self) -> str:
        name = "可轉債" if self.product == "cb" else "CBAS"
        text = f"{name} Delta 曝險"
        if self.beta_note == "暫用 1":
            text += "（標的 Beta 暫用 1）"
        return text


def mark_convertible(row, *, today: date | None = None) -> ConvertibleMark | None:
    """一列持倉的市值與曝險。不是可轉債／CBAS 就回 None。損益不含證交稅。"""
    product = product_kind(row.get("種類") if hasattr(row, "get") else None)
    if product is None:
        return None
    today = today or date.today()
    face = face_value(row)
    scale = point_value(face)
    qty = read_number(row, "持有數量") or 0.0
    quote_points, quote_source = bond_quote_points(row, face)
    cost_raw = read_number(row, "平均成本")
    cost_points = as_quote_points(cost_raw, face) if cost_raw is not None else None
    market_value = 0.0 if quote_points is None else quote_points * scale * qty
    cost_value = 0.0 if cost_points is None else cost_points * scale * qty
    pnl = market_value - cost_value

    conversion_price = read_number(row, "轉換價格", "轉股價格")
    stock_price = read_number(row, "標的現價", "標的股價")
    conv_points = conversion_value_points(stock_price, conversion_price)
    conv_amount = conversion_value_amount(stock_price, conversion_price, face, qty)
    floor_raw = read_number(row, "純債價值", "債底")
    floor_points = as_quote_points(floor_raw, face) if floor_raw is not None else None
    delta, delta_source = resolve_delta(
        row,
        product=product,
        quote_points=quote_points,
        conversion_points=conv_points,
        floor_points=floor_points if product == "cb" else None,
        spot=stock_price,
        conversion_price=conversion_price,
        today=today,
    )
    premium = None
    if product == "cb" and quote_points is not None and quote_points > 0 and conv_points is not None and conv_points > 0:
        premium = (quote_points / conv_points - 1.0) * 100.0
    equity = None if delta is None or conv_amount is None else delta * conv_amount
    beta, beta_note = underlying_beta(row)
    return ConvertibleMark(
        product=product,
        quote_points=quote_points,
        quote_source=quote_source,
        cost_points=cost_points,
        face=face,
        qty=qty,
        market_value=market_value,
        cost_value=cost_value,
        pnl=pnl,
        stock_price=stock_price,
        conversion_price=conversion_price,
        conversion_points=conv_points,
        conversion_value=conv_amount,
        delta=delta,
        delta_source=delta_source,
        premium_pct=premium,
        equity_exposure=equity,
        beta=beta,
        beta_note=beta_note,
        underlying=underlying_ticker_from_row(row),
    )
