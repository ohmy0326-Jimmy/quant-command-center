"""美股避險模組：計算 S&P 500 曝險、選擇權避險策略與壓力測試。"""

from __future__ import annotations

import math
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

import pandas as pd

from order_assistant import (
    Snapshot,
    bs_price_greeks,
    implied_vol_bs,
    iv_percentile,
    iv_traffic_light,
    option_expiry_pnl,
    structure_greeks,
    monthly_theta_cash,
    hedge_profit_plan,
    split_betas,
)


US_HEDGE_INSTRUMENTS = {
    "SPY": {"label": "SPY（S&P 500 ETF）", "multiplier": 100.0},
    "XSP": {"label": "XSP（迷你 S&P 500 指數，SPX ÷ 10）", "multiplier": 100.0},
    "SPX": {"label": "SPX（S&P 500 指數）", "multiplier": 100.0},
}
DEFAULT_US_INSTRUMENT = "SPY"

from beta_rules import LEVERAGED_UNDERLYING, FALLBACK_UNDERLYING_BETA, effective_beta, fetch_underlying_beta, fallback_underlying_beta  # noqa: F401  (保留舊匯入路徑)

_CACHE: dict[str, tuple[float, dict]] = {}
_SUCCESS_TTL = 600
_FAILURE_TTL = 120


def _num(value, default: float | None = 0.0) -> float | None:
    """轉成有限浮點數；None、NaN、文字都回 default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clear_cache() -> None:
    """清除模組快取。"""
    _CACHE.clear()


def instrument_prices(spx: float | None, spy: float | None = None) -> dict:
    """計算 SPX, XSP, SPY 價格與比例。"""
    spx = _num(spx, None)
    spy = _num(spy, None)
    spx_valid = spx is not None and spx > 0
    spy_valid = spy is not None and spy > 0

    if not spx_valid and not spy_valid:
        return {
            "SPX": None,
            "XSP": None,
            "SPY": None,
            "spy_estimated": True,
            "spx_estimated": True,
            "spx_per_spy": None,
        }
    elif not spx_valid and spy_valid:
        return {
            "SPX": spy * 10.0,
            "XSP": spy,
            "SPY": spy,
            "spy_estimated": False,
            "spx_estimated": True,
            "spx_per_spy": 10.0,
        }
    elif spx_valid and not spy_valid:
        return {
            "SPX": spx,
            "XSP": spx / 10.0,
            "SPY": spx / 10.0,
            "spy_estimated": True,
            "spx_estimated": False,
            "spx_per_spy": 10.0,
        }
    else:
        return {
            "SPX": spx,
            "XSP": spx / 10.0,
            "SPY": spy,
            "spy_estimated": False,
            "spx_estimated": False,
            "spx_per_spy": spx / spy,
        }


def underlying_beta(ticker: str, beta_fn: Callable | None = None, overrides: dict | None = None) -> tuple[float, str]:
    """取得槓桿 ETF 的底層標的 Beta。"""
    res = fetch_underlying_beta(ticker, beta_fn)
    if res and not math.isnan(res.get("標的Beta", math.nan)):
        return res["標的Beta"], res["標的Beta來源"]
    
    u = LEVERAGED_UNDERLYING[ticker][0] if ticker in LEVERAGED_UNDERLYING else ticker
    return fallback_underlying_beta(u, overrides)

def us_spx_exposure(snapshot: Snapshot, beta_fn: Callable | None = None, overrides: dict | None = None) -> dict:
    """計算美股部位對 S&P 500 的實質曝險。"""
    rows = []
    exposure_twd = 0.0
    notional_twd = 0.0
    fallback_rows = []
    incomplete_rows = []

    if snapshot.valued is not None and not snapshot.valued.empty:
        for _, row in snapshot.valued.iterrows():
            kind = str(row.get("種類", ""))
            if "美" not in kind:
                continue

            ticker = str(row.get("標的", "")).upper()
            name = str(row.get("名稱", ""))
            mv_twd = _num(row.get("即時市值(台幣)"), 0.0)
            lev = _num(row.get("採用槓桿"), None)
            if lev is None:
                lev = _num(row.get("標的槓桿"), 1.0)
            notional = _num(row.get("實質曝險(台幣)"), None)
            exposure = _num(row.get("Beta曝險(台幣)"), None)
            incomplete = notional is None or exposure is None
            if not incomplete:
                notional_twd += notional
                exposure_twd += exposure
            
            eff_beta = _num(row.get("有效Beta"), None)
            u = str(row.get("標的代號", ""))
            ub = _num(row.get("採用標的Beta"), None)
            src = str(row.get("Beta來源", ""))
            is_fallback = "預設值" in src
            method = src or "持倉 Beta（對 S&P 500）"

            if is_fallback:
                fallback_rows.append(ticker)
            if incomplete:
                incomplete_rows.append(ticker)

            rows.append({
                "ticker": ticker,
                "name": name,
                "kind": kind,
                "mv_twd": mv_twd,
                "lev": lev,
                "underlying": u,
                "underlying_beta": ub,
                "beta_source": src,
                "eff_beta": eff_beta,
                "exposure_twd": exposure,
                "notional_twd": notional,
                "method": method,
                "incomplete": incomplete,
            })

    usdtwd = float(snapshot.usdtwd)
    exposure_usd = exposure_twd / usdtwd if usdtwd > 0 else 0.0
    us_nav = float(snapshot.metrics.get("us_nav", 0.0))
    # 混合帳戶沒有可分的現金時，第四頁的美股敏感度改除以總淨值。這裡用同一個分母。
    if snapshot.metrics.get("beta_basis") == "total_nav":
        denom = float(snapshot.metrics.get("nav") or 0.0)
    else:
        denom = us_nav

    return {
        "rows": rows,
        "exposure_twd": exposure_twd,
        "exposure_usd": exposure_usd,
        "page4_us_exposure": float(snapshot.metrics.get("us_exposure", 0.0)),
        "us_nav": us_nav,
        "us_beta_spx": (exposure_twd / denom) if denom > 0 else 0.0,
        "notional_twd": notional_twd,
        "us_leverage": (notional_twd / denom) if denom > 0 else 0.0,
        "us_avg_beta": (exposure_twd / notional_twd) if notional_twd > 0 else 0.0,
        "fallback_rows": fallback_rows,
        "incomplete_rows": incomplete_rows,
        "beta_complete": bool(snapshot.metrics.get("beta_complete", not incomplete_rows)),
        "exposure_complete": bool(snapshot.metrics.get("exposure_complete", not incomplete_rows)),
    }


def hedge_contracts(exposure_usd: float, price: float, hedge_ratio: float = 1.0, multiplier: float = 100.0) -> dict:
    """計算避險所需口數（四捨五入）。"""
    exposure_usd = max(_num(exposure_usd, 0.0), 0.0)
    price = _num(price, 0.0)
    multiplier = _num(multiplier, 0.0)
    hedge_ratio = max(_num(hedge_ratio, 0.0), 0.0)

    if price <= 0 or multiplier <= 0:
        raw = 0.0
        contracts = 0
    else:
        raw = (exposure_usd * hedge_ratio) / (price * multiplier)
        if raw <= 0 or not math.isfinite(raw):
            contracts = 0
        else:
            cleaned = Decimal(str(round(raw, 10)))
            contracts = int(cleaned.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return {
        "raw": raw,
        "contracts": contracts,
        "notional_per_contract": price * multiplier,
        "hedged_notional_usd": contracts * price * multiplier,
        "formula": "口數 = 美股 S&P 曝險（美元）× 避險比率 ÷（價格 × 100），四捨五入",
    }


def resolve_us_leg(kind: str, strike: float, signed_lots: float, premium: float | None, spot: float, t_years: float, rate: float, iv_fallback: float, vix_percent: float | None = None) -> dict:
    """解析選擇權單支腳的隱含波動率或理論價。"""
    kind = str(kind).lower()
    strike = float(strike)
    signed_lots = float(signed_lots)

    premium = _num(premium, None)
    if premium is not None and premium > 0:
        iv = implied_vol_bs(premium, spot, strike, t_years, rate, kind)
        if iv is None:
            iv = iv_fallback
            source = "manual_bad_iv"
            note = "權利金無法回推隱含波動率（低於內含價值或過高），Greeks 改用畫面上的波動率。"
        else:
            source = "manual"
            note = f"權利金 {premium:g} 回推隱含波動率 {iv*100:.2f}%"
        no_quote = False
    else:
        vix_percent = _num(vix_percent, None)
        if vix_percent is not None and vix_percent > 0:
            if kind == "put" and strike < spot:
                sigma = (vix_percent + 3.0) / 100.0
            else:
                sigma = vix_percent / 100.0
        else:
            sigma = iv_fallback

        premium, _, _ = bs_price_greeks(spot, strike, t_years, rate, sigma, kind)
        iv = sigma
        source = "theory"
        no_quote = True
        note = f"無報價：用理論價 {premium:.2f}（波動率 {sigma*100:.1f}%）"

    return {
        "kind": kind,
        "strike": strike,
        "signed_lots": signed_lots,
        "premium": premium,
        "iv": iv,
        "source": source,
        "no_quote": no_quote,
        "note": note,
    }


def build_us_strategies(spot: float, contracts: int, k_put: float, k_short: float, k_call: float, premiums: dict, t_years: float, rate: float, iv_fallback: float, vix_percent: float | None = None) -> dict:
    """建立買進賣權、賣權價差、領口策略。"""
    put_leg = resolve_us_leg("put", k_put, contracts, premiums.get("put"), spot, t_years, rate, iv_fallback, vix_percent)
    short_put_leg = resolve_us_leg("put", k_short, -contracts, premiums.get("short_put"), spot, t_years, rate, iv_fallback, vix_percent)
    call_leg = resolve_us_leg("call", k_call, -contracts, premiums.get("call"), spot, t_years, rate, iv_fallback, vix_percent)

    strategies = {"買進賣權": [put_leg]}
    warnings = []

    if k_short >= k_put:
        warnings.append("賣權價差的空頭履約價需要低於買進履約價，這欄先不列入比較。")
    else:
        strategies["賣權價差"] = [put_leg, short_put_leg]

    if k_call <= k_put:
        warnings.append("領口的賣出買權履約價需要高於買進賣權履約價，這欄先不列入比較。")
    else:
        strategies["領口"] = [put_leg, call_leg]

    return {"strategies": strategies, "warnings": warnings}


def us_strategy_summary(snapshot: Snapshot, name: str, legs: list[dict], spot: float, t_years: float, rate: float, us_exposure_twd: float, multiplier: float = 100.0) -> dict:
    """計算單一避險策略的摘要指標與 Greeks。"""
    greeks = structure_greeks(spot, legs, t_years, rate, multiplier)
    fx = float(snapshot.usdtwd)
    
    debit_usd = greeks["net_debit"]
    debit_twd = debit_usd * fx
    long_premium_usd = greeks["long_premium"]
    long_premium_twd = long_premium_usd * fx
    delta_per_point = greeks["delta_per_point"]
    option_exposure_twd = delta_per_point * spot * fx
    theta_month_usd = monthly_theta_cash(spot, legs, t_years, rate, multiplier)
    theta_month_twd = theta_month_usd * fx

    base = dict(snapshot.metrics)
    base["us_exposure"] = us_exposure_twd
    before = split_betas(base)
    after = split_betas(base, us_exposure_delta=option_exposure_twd, us_cash_delta=-debit_twd)

    nav = float(snapshot.metrics.get("nav", 0.0))
    if nav <= 0:
        nav = 1.0

    no_quote = any(leg.get("no_quote", False) for leg in legs)

    return {
        "name": name,
        "legs": legs,
        "multiplier": float(multiplier),
        "net_debit_usd": debit_usd,
        "net_debit": debit_twd,
        "premium_pct_nav": debit_twd / nav * 100.0,
        "long_premium_usd": long_premium_usd,
        "long_premium": long_premium_twd,
        "delta_per_point": delta_per_point,
        "option_exposure": option_exposure_twd,
        "theta_month_usd": theta_month_usd,
        "theta_month": theta_month_twd,
        "us_beta_before": before["us_beta"],
        "us_beta_after": after["us_beta"],
        "tw_beta_before": before["tw_beta"],
        "tw_beta_after": after["tw_beta"],
        "total_beta_before": before["total_beta"],
        "total_beta_after": after["total_beta"],
        "no_quote": no_quote,
    }


def us_payoff_curve(snapshot: Snapshot, summaries: list[dict], spot: float, us_exposure_twd: float, multiplier: float = 100.0) -> pd.DataFrame:
    """產生到期損益曲線 DataFrame。"""
    rows = []
    fx = float(snapshot.usdtwd)
    for step in range(-30, 16):
        move = step / 100.0
        idx = spot * (1.0 + move)
        port = us_exposure_twd * move
        row = {"漲跌幅%": step, "指數": idx, "投組": port}
        for summary in summaries:
            name = summary["name"]
            legs = summary["legs"]
            mult = summary["multiplier"]
            opt_pnl = option_expiry_pnl(idx, legs, mult) * fx
            row[name] = port + opt_pnl
        rows.append(row)
    return pd.DataFrame(rows)


def us_hedge_stress_table(snapshot: Snapshot, summary: dict, spot: float, us_exposure_twd: float, moves: tuple = (-0.10, -0.20, -0.30), multiple: float = 3.0, realize_fraction: float = 0.5) -> list[dict]:
    """產生壓力情境下的避險損益與停利計畫表。"""
    nav0 = float(snapshot.metrics.get("nav", 0.0))
    fx = float(snapshot.usdtwd)
    legs = summary["legs"]
    mult = summary["multiplier"]
    net_debit = summary["net_debit"]
    long_premium = summary["long_premium"]

    rows = []
    for move in moves:
        port_pnl = us_exposure_twd * move
        idx = spot * (1.0 + move)
        hedge_pnl = option_expiry_pnl(idx, legs, mult) * fx
        hedge_value = max(hedge_pnl + net_debit, 0.0)
        nav = nav0 + port_pnl + hedge_pnl
        drawdown_pct = ((nav - nav0) / nav0 * 100.0) if nav0 > 0 else 0.0
        cost = net_debit if net_debit > 0 else long_premium
        plan = hedge_profit_plan(cost, hedge_value, multiple, realize_fraction)

        rows.append({
            "move": move,
            "name": f"S&P 500 {move*100:.0f}%",
            "port_pnl": port_pnl,
            "hedge_pnl": hedge_pnl,
            "hedge_value": hedge_value,
            "nav": nav,
            "drawdown_pct": drawdown_pct,
            "profit_plan": plan,
        })
    return rows


def iv_signal_vs_vix(iv_percent: float, vix_history: list[float]) -> dict:
    """根據 VIX 歷史資料判斷 IV 燈號。"""
    pct = iv_percentile(iv_percent, vix_history)
    light = iv_traffic_light(pct)
    return {
        "percentile": pct,
        "color": light["color"],
        "label": light["label"],
    }


def fetch_us_vix(*, get: Callable | None = None, use_cache: bool | None = None) -> dict:
    """抓取美股 VIX 歷史資料。"""
    if use_cache is None:
        use_cache = get is None
    if use_cache:
        cached = _CACHE.get("vix")
        if cached:
            stamped, payload = cached
            ttl = _SUCCESS_TTL if payload.get("ok") else _FAILURE_TTL
            if time.monotonic() - stamped <= ttl:
                return payload

    if get is None:
        def default_get():
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="1y")
            if hist.empty:
                return []
            closes = hist["Close"].dropna()
            return [(str(d.date()), float(c)) for d, c in zip(closes.index, closes)]
        get = default_get

    try:
        data = [(str(day)[:10], _num(val, None)) for day, val in (get() or [])]
        data = [(day, val) for day, val in data if val is not None and val > 0]
        if not data:
            raise ValueError("No data")
        last_date, last_val = data[-1]
        history = [v for _, v in data]
        res = {
            "ok": True,
            "value": last_val,
            "as_of": last_date,
            "source": "Yahoo Finance（CBOE VIX 日收盤）",
            "history": history,
            "history_days": len(history),
            "history_label": f"近一年美股 VIX 日收盤（{len(history)} 個交易日）",
        }
    except Exception:
        res = {
            "ok": False,
            "value": None,
            "as_of": "",
            "source": "無報價（美股 VIX 抓取失敗）",
            "history": [],
            "history_days": 0,
            "history_label": "近一年美股 VIX 日收盤（沒有資料）",
        }

    if use_cache:
        _CACHE["vix"] = (time.monotonic(), res)
    return res


def fetch_us_index_prices(*, get: Callable | None = None, use_cache: bool | None = None) -> dict:
    """抓取 SPX 與 SPY 最新價格。"""
    if use_cache is None:
        use_cache = get is None
    if use_cache:
        cached = _CACHE.get("index")
        if cached:
            stamped, payload = cached
            ttl = _SUCCESS_TTL if payload.get("ok") else _FAILURE_TTL
            if time.monotonic() - stamped <= ttl:
                return payload

    if get is None:
        def default_get(symbol):
            import yfinance as yf
            hist = yf.Ticker(symbol).history(period="5d")
            if hist.empty:
                return None
            return float(hist["Close"].iloc[-1])
        get = default_get

    def _one(symbol: str) -> float | None:
        try:
            value = _num(get(symbol), None)
        except Exception:
            return None
        return value if value is not None and value > 0 else None

    spx = _one("^GSPC")
    spy = _one("SPY")
    ok = spx is not None or spy is not None
    res = {
        "ok": ok,
        "SPX": spx,
        "SPY": spy,
        "source": "Yahoo Finance（收盤或延遲報價）" if ok else "無報價（S&P 500／SPY 抓取失敗）",
    }

    if use_cache:
        _CACHE["index"] = (time.monotonic(), res)
    return res
