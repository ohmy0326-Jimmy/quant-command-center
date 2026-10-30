"""下單前的投組試算：風控規則、減碼、單筆衝擊、選擇權與壓力情境。

淨值、槓桿、Beta、保證金都呼叫 portfolio_risk.finalize_valuation，
與第四頁共用同一套算法。這裡不會送出任何委託。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from portfolio_risk import (
    FUTURES_MARGIN,
    STOCK_TAX_RATE,
    YUANTA_FUTURES_BASE_EQUITY,
    _futures_margin_row,
    finalize_valuation,
    futures_multiplier,
    is_taiex_future,
)

TXO_MULTIPLIER = 50.0  # 台指選擇權每點新台幣


@dataclass
class Rules:
    target_leverage: float = 1.8
    stage2_leverage: float = 1.5
    min_cash_pct: float = 15.0
    max_loss_pct_nav: float = 2.0
    hedge_budget_pct_nav: float = 1.5
    beta_low: float = 1.5
    beta_high: float = 2.0
    max_drawdown_pct: float = -20.0
    hedge_realize_multiple: float = 3.0
    hedge_realize_fraction: float = 0.5
    sox_shock: float = -0.30
    fx_shock: float = 0.05


@dataclass
class Snapshot:
    """已帶報價的持倉＋帳戶。建立時就用第四頁的公式估價。"""

    positions: pd.DataFrame
    accounts: pd.DataFrame
    usdtwd: float
    twii: float
    valued: pd.DataFrame = field(init=False)
    accounts_valued: pd.DataFrame = field(init=False)
    metrics: dict = field(init=False)

    def __post_init__(self):
        self.positions = _coerce_positions(self.positions)
        self.accounts = _coerce_accounts(self.accounts)
        self.usdtwd = float(self.usdtwd)
        self.twii = float(self.twii)
        self.valued, self.accounts_valued, self.metrics = finalize_valuation(self.positions, self.accounts, self.usdtwd)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price_greeks(spot: float, strike: float, t_years: float, rate: float, sigma: float, kind: str = "put"):
    """Black-Scholes 價格、Delta、Theta（每一年，dV/dt，時間消逝為負）。

    價格與現貨同一單位（指數點數或股價）。不依賴 scipy。
    """
    kind = "call" if str(kind).lower().startswith("c") else "put"
    spot = float(spot)
    strike = float(strike)
    rate = float(rate)
    sigma = max(float(sigma), 0.0)
    t_years = max(float(t_years), 0.0)

    if spot <= 0 or strike <= 0:
        return 0.0, 0.0, 0.0

    if t_years <= 1e-8 or sigma <= 1e-8:
        if kind == "put":
            price = max(strike - spot, 0.0)
            delta = -1.0 if spot < strike else (0.0 if spot > strike else -0.5)
        else:
            price = max(spot - strike, 0.0)
            delta = 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
        return price, delta, 0.0

    vol_sqrt = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / vol_sqrt
    d2 = d1 - vol_sqrt
    disc = math.exp(-rate * t_years)
    pdf = _npdf(d1)
    if kind == "call":
        price = spot * _ncdf(d1) - strike * disc * _ncdf(d2)
        delta = _ncdf(d1)
        theta = (-spot * pdf * sigma / (2.0 * math.sqrt(t_years))) - rate * strike * disc * _ncdf(d2)
    else:
        price = strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)
        delta = _ncdf(d1) - 1.0
        theta = (-spot * pdf * sigma / (2.0 * math.sqrt(t_years))) + rate * strike * disc * _ncdf(-d2)
    return float(price), float(delta), float(theta)


def intrinsic_value(kind: str, strike: float, spot: float) -> float:
    if str(kind).lower().startswith("c"):
        return max(float(spot) - float(strike), 0.0)
    return max(float(strike) - float(spot), 0.0)


def option_expiry_pnl(spot: float, legs: list[dict], multiplier: float) -> float:
    """到期損益（含權利金）。legs: kind, strike, signed_lots（買進為正）, premium（每點，正數）。"""
    pnl = 0.0
    for leg in legs:
        signed = float(leg["signed_lots"])
        premium = float(leg["premium"])
        points = intrinsic_value(leg["kind"], leg["strike"], spot)
        pnl += signed * (points - premium) * float(multiplier)
    return pnl


def monthly_theta_cash(spot: float, legs: list[dict], t_years: float, rate: float, multiplier: float) -> float:
    """約 30 個日曆日的時間價值，新台幣（或與乘數相同幣別）。負值代表長部位耗損。"""
    total = 0.0
    for leg in legs:
        sigma = float(leg.get("iv", 0.2))
        _, _, theta_year = bs_price_greeks(spot, leg["strike"], t_years, rate, sigma, leg["kind"])
        total += float(leg["signed_lots"]) * theta_year * (30.0 / 365.0) * float(multiplier)
    return total


def structure_greeks(spot: float, legs: list[dict], t_years: float, rate: float, multiplier: float) -> dict:
    """每點 Delta（與乘數同幣別）與淨權利金支出（買進為正）。"""
    delta_cash = 0.0
    net_debit = 0.0
    long_premium = 0.0
    for leg in legs:
        sigma = float(leg.get("iv", 0.2))
        premium = float(leg["premium"])
        if premium <= 0:
            premium, _, _ = bs_price_greeks(spot, leg["strike"], t_years, rate, sigma, leg["kind"])
            leg = dict(leg)
            leg["premium"] = premium
        _, delta, _ = bs_price_greeks(spot, leg["strike"], t_years, rate, sigma, leg["kind"])
        signed = float(leg["signed_lots"])
        delta_cash += signed * delta * float(multiplier)
        net_debit += signed * float(leg["premium"]) * float(multiplier)
        if signed > 0:
            long_premium += signed * float(leg["premium"]) * float(multiplier)
    return {"delta_per_point": delta_cash, "net_debit": net_debit, "long_premium": long_premium}


def hedge_profit_plan(cost: float, hedge_value: float, multiple: float = 3.0, realize_fraction: float = 0.5) -> dict:
    """避險市值達到成本的 multiple 倍時，實現 realize_fraction，其餘續抱。"""
    cost = max(float(cost), 0.0)
    hedge_value = max(float(hedge_value), 0.0)
    multiple = float(multiple)
    realize_fraction = min(max(float(realize_fraction), 0.0), 1.0)
    triggered = cost > 0 and hedge_value + 1e-9 >= multiple * cost
    realize_cash = hedge_value * realize_fraction if triggered else 0.0
    return {
        "triggered": triggered,
        "cost": cost,
        "hedge_value": hedge_value,
        "multiple": multiple,
        "realize_cash": realize_cash,
        "keep_value": hedge_value - realize_cash,
        "keep_fraction": (1.0 - realize_fraction) if triggered else 1.0,
        "value_to_cost": (hedge_value / cost) if cost > 0 else None,
    }


def parse_tickers(text: str) -> list[str]:
    if not text:
        return []
    parts = text.replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
    out = []
    for part in parts:
        token = part.strip().upper()
        if token and token not in out:
            out.append(token)
    return out


def positions_as_dicts(valued: pd.DataFrame) -> list[dict]:
    if valued is None or valued.empty:
        return []
    rows = []
    for _, row in valued.iterrows():
        fx = float(row.get("換算匯率", 1.0))
        rows.append(
            {
                "ticker": str(row["標的"]).upper(),
                "name": str(row.get("名稱", "") or ""),
                "account": str(row.get("帳戶", "")),
                "kind": str(row.get("種類", "")),
                "qty": float(row["持有數量"]),
                "price": float(row["即時報價"]),
                "cost": float(row["平均成本"]),
                "lev": float(row["標的槓桿"]),
                "beta": float(row["Beta值"]),
                "sector": str(row.get("產業別", "其他")),
                "mv_twd": float(row["即時市值(台幣)"]),
                "exposure": float(row["實質曝險(台幣)"]),
                "pnl_native": float(row["淨未實現損益(原幣)"]),
                "pnl_twd": float(row["淨未實現損益(原幣)"]) * fx,
                "fx": fx,
                "roi": float(row["報酬率 (%)"]),
                "max_loss_pct": float(row["最大虧損比例"]),
                "occupied_native": float(row["實質佔用本金(原幣)"]),
                "notional_native": float(row["名目合約總值(原幣)"]),
            }
        )
    return rows


def _is_futures_kind(kind: str) -> bool:
    return "期" in str(kind)


def _fx_for_kind(kind: str, usdtwd: float) -> float:
    return float(usdtwd) if "美" in str(kind) else 1.0


def _coerce_positions(positions: pd.DataFrame) -> pd.DataFrame:
    frame = positions.copy()
    for col in ("持有數量", "平均成本", "投入總金額", "最大虧損比例", "標的槓桿", "即時報價", "Beta值"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
    return frame


def _coerce_accounts(accounts: pd.DataFrame) -> pd.DataFrame:
    frame = accounts.copy()
    for col in ("現金餘額", "融資/質押借款"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0).astype(float)
    return frame


def economic_exposure(snapshot: Snapshot) -> float:
    """期貨用契約名目（指數×乘數×口數），其餘沿用第四頁的實質曝險。"""
    total = 0.0
    for pos in positions_as_dicts(snapshot.valued):
        if _is_futures_kind(pos["kind"]) and is_taiex_future(pos["ticker"]):
            total += abs(pos["qty"]) * pos["price"] * futures_multiplier(pos["ticker"]) * pos["fx"]
        else:
            total += pos["exposure"]
    return total


def economic_leverage(snapshot: Snapshot) -> float:
    nav = float(snapshot.metrics["nav"])
    if nav <= 0:
        return 0.0
    return economic_exposure(snapshot) / nav


def taiex_dollar_delta(snapshot: Snapshot) -> float:
    """投組對台指每點的新台幣 Delta（未含選擇權）。

    一般台股：市值 × Beta／指數。槓桿 ETF：市值 × 標的槓桿／指數。
    臺指期：口數 × 乘數。
    """
    twii = float(snapshot.twii) if snapshot.twii else 0.0
    if twii <= 0:
        return 0.0
    delta = 0.0
    for pos in positions_as_dicts(snapshot.valued):
        if _is_futures_kind(pos["kind"]) and is_taiex_future(pos["ticker"]):
            delta += pos["qty"] * futures_multiplier(pos["ticker"]) * pos["fx"]
        elif "美" in pos["kind"]:
            continue
        else:
            sens = pos["lev"] if pos["lev"] > 1 else pos["beta"]
            delta += pos["mv_twd"] * sens / twii
    return delta


def beta_equivalent(dollar_delta: float, twii: float, nav: float) -> float:
    """1% 台指變動對淨值的百分比敏感度。"""
    if nav <= 0 or twii <= 0:
        return 0.0
    return float(dollar_delta) * float(twii) / float(nav)


_SEMI_TICKERS = {
    "SOXL", "SOXX", "USD", "TSM", "NVDA", "AMD", "AVGO", "SMH", "MU", "ARM",
    "2330", "2454", "2303", "3711", "3034", "2379", "3008", "6669",
}
_SEMI_HINTS = ("晶圓", "半導體", "記憶體", "ai晶片", "芯片", "semi")


def is_semi_asset(sector: str, ticker: str) -> bool:
    text = str(sector).lower()
    if any(hint in text for hint in _SEMI_HINTS):
        return True
    return str(ticker).upper() in _SEMI_TICKERS


def loss_breaches(snapshot: Snapshot, rules: Rules) -> list[dict]:
    """單一部位虧損超過淨值上限時，算出要減碼多少才回到上限內。"""
    nav = float(snapshot.metrics["nav"])
    if nav <= 0:
        return []
    budget = nav * float(rules.max_loss_pct_nav) / 100.0
    breaches = []
    for pos in positions_as_dicts(snapshot.valued):
        current_loss = max(0.0, -pos["pnl_twd"])
        stop_risk = 0.0
        if -100.0 < pos["max_loss_pct"] < 0 and pos["occupied_native"] > 0:
            stop_risk = abs(pos["occupied_native"] * pos["fx"] * pos["max_loss_pct"] / 100.0)
        frac_loss = (1.0 - budget / current_loss) if current_loss > budget > 0 else 0.0
        frac_stop = (1.0 - budget / stop_risk) if stop_risk > budget > 0 else 0.0
        frac = max(0.0, min(1.0, max(frac_loss, frac_stop)))
        if frac <= 1e-6:
            continue
        reasons = []
        if frac_loss > 0:
            reasons.append("目前浮虧已超過單一部位上限")
        if frac_stop > 0:
            reasons.append("若觸及最大虧損比例，金額損失超過單一部位上限")
        breaches.append(
            {
                "ticker": pos["ticker"],
                "name": pos["name"],
                "qty": pos["qty"],
                "mv_twd": pos["mv_twd"],
                "current_loss": current_loss,
                "stop_risk": stop_risk,
                "budget": budget,
                "trim_fraction": frac,
                "trim_qty": abs(pos["qty"]) * frac,
                "trim_value": pos["mv_twd"] * frac,
                "reason": "；".join(reasons),
            }
        )
    breaches.sort(key=lambda item: item["trim_value"], reverse=True)
    return breaches


def health_flags(snapshot: Snapshot, rules: Rules, stress_rows: list[dict] | None = None) -> list[dict]:
    metrics = snapshot.metrics
    flags = []

    def add(name: str, ok: bool, detail: str):
        flags.append({"name": name, "ok": bool(ok), "status": "符合" if ok else "超出", "detail": detail})

    add(
        "合併槓桿",
        metrics["leverage"] <= rules.target_leverage + 1e-9,
        f"{metrics['leverage']:.2f}x，上限 {rules.target_leverage:.2f}x（第二階段 {rules.stage2_leverage:.2f}x）",
    )
    add(
        "現金水位",
        metrics["cash_pct"] + 1e-9 >= rules.min_cash_pct,
        f"{metrics['cash_pct']:.1f}%，下限 {rules.min_cash_pct:.1f}%",
    )
    beta_ok = rules.beta_low - 1e-9 <= metrics["beta"] <= rules.beta_high + 1e-9
    add("投組 Beta", beta_ok, f"{metrics['beta']:.2f}，目標 {rules.beta_low:.2f}–{rules.beta_high:.2f}")
    if metrics["margin_ratio"] is None:
        add("保證金比率", True, "目前沒有期貨維持保證金")
    else:
        add(
            "保證金比率",
            not metrics["margin_call"],
            f"{metrics['margin_ratio']:.1f}%（低於 100% 視為追繳；可動用保證金 NT$ {metrics['excess_margin']:,.0f}）",
        )
    breaches = loss_breaches(snapshot, rules)
    add(
        "單一部位虧損",
        len(breaches) == 0,
        "沒有部位超過上限" if not breaches else f"{len(breaches)} 檔超過淨值 {rules.max_loss_pct_nav:.1f}%",
    )
    if stress_rows:
        worst = min(stress_rows, key=lambda row: row["drawdown_pct"])
        add(
            "壓力回撤",
            worst["drawdown_pct"] >= rules.max_drawdown_pct - 1e-9,
            f"最深為 {worst['name']} {worst['drawdown_pct']:.1f}%，容忍 {rules.max_drawdown_pct:.0f}%",
        )
        called = [row["name"] for row in stress_rows if row["margin_call"]]
        add("壓力後追繳", len(called) == 0, "各情境都未追繳" if not called else "、".join(called) + " 會追繳")
    return flags


def _route_account(accounts: pd.DataFrame, kind: str) -> str:
    if accounts is None or accounts.empty or "帳戶名稱" not in accounts.columns:
        return "元大期貨" if _is_futures_kind(kind) else "現貨主帳戶"
    names = [str(name).strip() for name in accounts["帳戶名稱"].tolist()]
    if _is_futures_kind(kind):
        for name in names:
            if "期" in name:
                return name
        return "元大期貨"
    for name in names:
        if "期" not in name:
            return name
    return names[0]


def _ensure_account(accounts: pd.DataFrame, name: str) -> pd.DataFrame:
    accounts = accounts.copy()
    if accounts.empty or "帳戶名稱" not in accounts.columns:
        return pd.DataFrame([{"帳戶名稱": name, "現金餘額": 0.0, "融資/質押借款": 0.0}])
    if name not in accounts["帳戶名稱"].astype(str).str.strip().tolist():
        extra = {"帳戶名稱": name, "現金餘額": 0.0, "融資/質押借款": 0.0}
        accounts = pd.concat([accounts, pd.DataFrame([extra])], ignore_index=True)
    if "現金餘額" not in accounts.columns:
        accounts["現金餘額"] = 0.0
    if "融資/質押借款" not in accounts.columns:
        accounts["融資/質押借款"] = 0.0
    return accounts


def _blank_position(columns) -> dict:
    row = {col: 0.0 for col in columns}
    for col in ("帳戶", "種類", "標的", "名稱", "產業別"):
        if col in row:
            row[col] = ""
    return row


def apply_order(snapshot: Snapshot, order: dict) -> tuple[Snapshot, list[str]]:
    """在副本上套用一筆買賣，再重跑第四頁的估價。不會改到原始持倉。"""
    warnings: list[str] = []
    ticker = str(order.get("ticker", "")).strip().upper()
    if not ticker:
        return snapshot, ["沒有代號，無法試算"]
    side = str(order.get("side", "buy")).lower()
    if side in ("買", "買進", "buy", "b"):
        side = "buy"
    elif side in ("賣", "賣出", "sell", "s"):
        side = "sell"
    else:
        return snapshot, ["買賣方向無法辨識"]

    qty = abs(float(order.get("qty", 0) or 0))
    price = float(order.get("price", 0) or 0)
    if qty <= 0:
        return snapshot, []
    if price <= 0:
        return snapshot, ["價格需大於 0"]

    kind = str(order.get("kind", "台股"))
    if is_taiex_future(ticker) and "期" not in kind:
        kind = "期貨"
    positions = _coerce_positions(snapshot.positions)
    accounts = _ensure_account(_coerce_accounts(snapshot.accounts), _route_account(snapshot.accounts, kind))
    account = str(order.get("account") or "").strip() or _route_account(accounts, kind)
    accounts = _ensure_account(accounts, account)

    if positions.empty or "標的" not in positions.columns:
        positions = pd.DataFrame(columns=["帳戶", "種類", "標的", "名稱", "持有數量", "平均成本", "投入總金額", "最大虧損比例", "標的槓桿", "即時報價", "Beta值", "產業別"])

    mask = positions["標的"].astype(str).str.upper().eq(ticker) & positions["帳戶"].astype(str).str.strip().eq(account) if not positions.empty else pd.Series(dtype=bool)
    existing = positions[mask] if not positions.empty else positions
    if not existing.empty:
        kind = str(existing.iloc[0]["種類"])
    fx = _fx_for_kind(kind, snapshot.usdtwd)

    if existing.empty:
        if side == "sell":
            return snapshot, [f"{ticker} 不在 {account}，沒有部位可賣"]
        lev = order.get("leverage")
        if _is_futures_kind(kind) and (lev is None or float(lev) <= 0):
            initial = FUTURES_MARGIN.get(ticker, (None, None))[0]
            notional = price * futures_multiplier(ticker) * qty
            lev = (notional / initial) if initial else 1.0
        lev = float(lev if lev not in (None, "") else 1.0)
        row = _blank_position(positions.columns)
        row.update(
            {
                "帳戶": account,
                "種類": kind,
                "標的": ticker,
                "名稱": str(order.get("name") or ticker),
                "持有數量": qty,
                "平均成本": price,
                "投入總金額": 0.0 if _is_futures_kind(kind) else price * qty,
                "最大虧損比例": float(order.get("max_loss_pct", -100) or -100),
                "標的槓桿": lev,
                "即時報價": price,
                "Beta值": float(order.get("beta", 1.0) or 1.0),
                "產業別": str(order.get("sector") or "其他"),
            }
        )
        positions = pd.concat([positions, pd.DataFrame([row])], ignore_index=True)
        traded_qty = qty
    else:
        idx = existing.index[0]
        old_qty = float(positions.at[idx, "持有數量"])
        old_cost = float(positions.at[idx, "平均成本"])
        old_notional = float(positions.at[idx, "投入總金額"])
        if side == "buy":
            new_qty = old_qty + qty
            if old_qty > 0:
                new_cost = (old_cost * old_qty + price * qty) / new_qty
                new_notional = old_notional + price * qty
            else:
                new_cost = price if new_qty <= 0 else (price * qty) / new_qty
                new_notional = price * new_qty
            traded_qty = qty
        else:
            if old_qty <= 0:
                return snapshot, [f"{ticker} 目前不是多單，這次試算不放空"]
            sold = min(qty, old_qty)
            if sold + 1e-9 < qty:
                warnings.append(f"賣出數量超過持有，已改為賣出 {sold:g}")
            new_qty = old_qty - sold
            new_cost = old_cost
            new_notional = old_notional * (new_qty / old_qty) if old_qty else 0.0
            traded_qty = sold
        if abs(new_qty) < 1e-10:
            positions = positions.drop(index=idx).reset_index(drop=True)
        else:
            positions.at[idx, "持有數量"] = new_qty
            positions.at[idx, "平均成本"] = new_cost
            positions.at[idx, "投入總金額"] = new_notional

    if not _is_futures_kind(kind):
        cash_delta = price * traded_qty * fx
        acc_idx = accounts.index[accounts["帳戶名稱"].astype(str).str.strip().eq(account)]
        if len(acc_idx) == 0:
            warnings.append("找不到入帳帳戶")
        else:
            i = acc_idx[0]
            cash = float(accounts.at[i, "現金餘額"])
            debt = float(accounts.at[i, "融資/質押借款"])
            if side == "buy":
                if cash + 1e-6 >= cash_delta:
                    accounts.at[i, "現金餘額"] = cash - cash_delta
                else:
                    shortfall = cash_delta - max(cash, 0.0)
                    accounts.at[i, "現金餘額"] = 0.0
                    accounts.at[i, "融資/質押借款"] = debt + shortfall
                    warnings.append(f"現金不足，不足的 NT$ {shortfall:,.0f} 以融資估算（只試算，不會向券商送單）")
            else:
                accounts.at[i, "現金餘額"] = cash + cash_delta

    updated = Snapshot(positions, accounts, snapshot.usdtwd, snapshot.twii)
    return updated, warnings


def _candidates(snapshot: Snapshot, phase: str, protected: set[str]) -> list[dict]:
    rows = []
    for pos in positions_as_dicts(snapshot.valued):
        if pos["ticker"] in protected or pos["qty"] <= 0:
            continue
        futures = _is_futures_kind(pos["kind"])
        if phase == "leveraged":
            if futures or pos["lev"] <= 1:
                continue
        elif phase == "laggard":
            if futures or pos["lev"] > 1 or pos["roi"] >= 0:
                continue
        elif phase == "other":
            if futures:
                continue
        elif phase == "futures":
            if not futures:
                continue
        else:
            continue
        rows.append(pos)
    if phase == "leveraged":
        rows.sort(key=lambda pos: (-pos["lev"], pos["roi"]))
    elif phase == "laggard":
        rows.sort(key=lambda pos: pos["roi"])
    elif phase == "futures":
        rows.sort(key=lambda pos: -pos["exposure"])
    else:
        rows.sort(key=lambda pos: -pos["exposure"])
    return rows


def _shares_to_sell(snapshot: Snapshot, pos: dict, target_leverage: float, min_cash_pct: float) -> float:
    nav = float(snapshot.metrics["nav"])
    if nav <= 0 or pos["qty"] <= 0 or pos["mv_twd"] <= 0:
        return 0.0
    exp_gap = max(0.0, float(snapshot.metrics["exposure"]) - target_leverage * nav)
    cash_gap = max(0.0, nav * min_cash_pct / 100.0 - float(snapshot.metrics["cash"]))
    if exp_gap < 1 and cash_gap < 1:
        return 0.0
    if _is_futures_kind(pos["kind"]):
        return min(pos["qty"], 1.0)
    fracs = []
    if pos["exposure"] > 0 and exp_gap > 0:
        fracs.append(exp_gap / pos["exposure"])
    if pos["mv_twd"] > 0 and cash_gap > 0:
        fracs.append(cash_gap / pos["mv_twd"])
    if not fracs:
        return 0.0
    frac = min(1.0, max(fracs))
    raw = pos["qty"] * frac
    if raw <= 1e-8:
        return 0.0
    if abs(pos["qty"] - round(pos["qty"])) < 1e-6:
        return float(min(int(round(pos["qty"])), max(1, int(math.ceil(raw - 1e-9)))))
    return float(min(pos["qty"], raw))


def _action_row(pos: dict, action: str, qty: float, value: float, reason: str, after: Snapshot) -> dict:
    return {
        "ticker": pos["ticker"] if pos else "",
        "name": pos.get("name", "") if pos else "",
        "current_value": pos.get("mv_twd", 0.0) if pos else 0.0,
        "action": action,
        "qty": qty,
        "value": value,
        "reason": reason,
        "leverage_after": after.metrics["leverage"],
        "cash_pct_after": after.metrics["cash_pct"],
        "beta_after": after.metrics["beta"],
        "kind": pos.get("kind", "") if pos else "",
        "price": pos.get("price", 0.0) if pos else 0.0,
    }


def _within_target(snapshot: Snapshot, target_leverage: float, min_cash_pct: float) -> bool:
    nav = float(snapshot.metrics["nav"])
    if nav <= 0:
        return True
    leverage_ok = snapshot.metrics["leverage"] <= target_leverage + 1e-6
    cash_ok = snapshot.metrics["cash_pct"] + 1e-6 >= min_cash_pct
    return leverage_ok and cash_ok


def _lookup(snapshot: Snapshot, ticker: str) -> dict | None:
    for pos in positions_as_dicts(snapshot.valued):
        if pos["ticker"] == ticker:
            return pos
    return None


def _sector_of(snapshot: Snapshot, ticker: str, hints: dict | None = None) -> str:
    found = _lookup(snapshot, ticker)
    if found:
        return found["sector"]
    extra = {"0050": "台灣50", "006208": "台灣50", "00663L": "台股槓桿ETF"}
    if hints and ticker in hints:
        return hints[ticker]
    if ticker in extra:
        return extra[ticker]
    from portfolio_risk import DETAILED_SECTOR_MAP

    return DETAILED_SECTOR_MAP.get(ticker, "其他")


def propose_rebalance(
    snapshot: Snapshot,
    rules: Rules,
    core_tickers: list[str] | None = None,
    rotate_fraction: float = 0.3,
    strict_sector: bool = False,
    target_leverage: float | None = None,
) -> dict:
    """先減槓桿型（同樣一塊錢砍掉最多曝險），再處理弱勢股，最後才動到期貨。

    換股用賣出弱勢股的資金買進核心，兩邊都是 1 倍時曝險大致不變。
    """
    cores = [ticker for ticker in (core_tickers or []) if ticker]
    protected = set(cores)
    target = float(rules.target_leverage if target_leverage is None else target_leverage)
    working = snapshot
    actions: list[dict] = []
    notes: list[str] = []
    blacklist: set[str] = set()

    phase_reason = {
        "leveraged": "槓桿型優先減碼（每元市值降最多曝險），用來壓槓桿並補現金",
        "laggard": "弱勢股減碼，補足槓桿或現金還沒達標的部分",
        "other": "非核心部位減碼，補足仍未達標的槓桿或現金",
        "futures": "股票減碼後仍未達標，再減期貨口數",
    }

    for phase in ("leveraged", "laggard", "other", "futures"):
        steps = 0
        while steps < 40 and not _within_target(working, target, rules.min_cash_pct):
            steps += 1
            choices = [pos for pos in _candidates(working, phase, protected) if pos["ticker"] not in blacklist]
            if not choices:
                break
            pos = choices[0]
            qty = _shares_to_sell(working, pos, target, rules.min_cash_pct)
            if qty <= 0:
                blacklist.add(pos["ticker"])
                continue
            before_exp = working.metrics["exposure"]
            order = {
                "ticker": pos["ticker"],
                "side": "sell",
                "qty": qty,
                "price": pos["price"],
                "kind": pos["kind"],
                "account": pos["account"],
                "beta": pos["beta"],
                "leverage": pos["lev"],
                "sector": pos["sector"],
                "name": pos["name"],
            }
            working, order_warnings = apply_order(working, order)
            notes.extend(order_warnings)
            if abs(working.metrics["exposure"] - before_exp) < 1 and phase != "futures":
                blacklist.add(pos["ticker"])
                continue
            value = qty / pos["qty"] * pos["mv_twd"] if pos["qty"] else 0.0
            actions.append(_action_row(pos, "賣出", qty, value, phase_reason[phase], working))

    if not _within_target(working, target, rules.min_cash_pct):
        notes.append("非核心部位已盡量減碼，仍未同時達到槓桿與現金目標。核心持股未動。")

    rotate_fraction = min(max(float(rotate_fraction), 0.0), 1.0)
    if rotate_fraction > 0 and cores:
        laggards = [
            pos
            for pos in positions_as_dicts(working.valued)
            if pos["qty"] > 0 and pos["roi"] < 0 and pos["lev"] <= 1 and not _is_futures_kind(pos["kind"]) and pos["ticker"] not in protected
        ]
        laggards.sort(key=lambda pos: pos["roi"])
        for pos in laggards:
            raw_qty = pos["qty"] * rotate_fraction
            if abs(pos["qty"] - round(pos["qty"])) < 1e-6:
                sell_qty = float(int(math.floor(raw_qty + 1e-9)))
            else:
                sell_qty = raw_qty
            if sell_qty <= 0:
                continue
            same = [core for core in cores if _sector_of(working, core) == pos["sector"]]
            if same:
                targets = same[:1]
            elif strict_sector:
                actions.append(
                    _action_row(pos, "暫不換股", 0.0, 0.0, f"弱勢但沒有同產業核心（{pos['sector']}），嚴格模式下先留著", working)
                )
                continue
            else:
                targets = list(cores)
            proceeds = sell_qty / pos["qty"] * pos["mv_twd"]
            working, sell_warnings = apply_order(
                working,
                {
                    "ticker": pos["ticker"],
                    "side": "sell",
                    "qty": sell_qty,
                    "price": pos["price"],
                    "kind": pos["kind"],
                    "account": pos["account"],
                    "name": pos["name"],
                },
            )
            notes.extend(sell_warnings)
            actions.append(_action_row(pos, "賣出", sell_qty, proceeds, f"弱勢股換股（報酬率 {pos['roi']:.1f}%）", working))
            share = proceeds / len(targets)
            for core in targets:
                held = _lookup(working, core)
                core_price = held["price"] if held else float(order_price_hint(core, snapshot))
                if core_price <= 0:
                    notes.append(f"{core} 沒有參考價，這筆換股資金留在現金")
                    continue
                core_kind = held["kind"] if held else ("美股" if core.isalpha() and len(core) <= 5 and not core[:1].isdigit() else "台股")
                if str(core)[:1].isdigit():
                    core_kind = "台股"
                fx = _fx_for_kind(core_kind, working.usdtwd)
                buy_qty = share / (core_price * fx) if core_price * fx > 0 else 0.0
                if abs(buy_qty - round(buy_qty)) > 1e-6:
                    buy_qty = math.floor(buy_qty)
                else:
                    buy_qty = float(int(math.floor(buy_qty + 1e-9)))
                if buy_qty <= 0:
                    notes.append(f"{core} 這筆金額不足 1 股，資金留在現金")
                    continue
                core_sector = _sector_of(working, core)
                reason = f"換入核心 {core}"
                if core_sector != pos["sector"]:
                    reason += f"（產業由 {pos['sector']} 轉為 {core_sector}，用核心維持風險預算）"
                else:
                    reason += f"（同產業 {core_sector}）"
                before_buy = _lookup(working, core)
                working, buy_warnings = apply_order(
                    working,
                    {
                        "ticker": core,
                        "side": "buy",
                        "qty": buy_qty,
                        "price": core_price,
                        "kind": core_kind,
                        "account": pos["account"],
                        "beta": before_buy["beta"] if before_buy else 1.0,
                        "leverage": before_buy["lev"] if before_buy else 1.0,
                        "sector": core_sector,
                        "name": before_buy["name"] if before_buy else core,
                    },
                )
                notes.extend(buy_warnings)
                bought = _lookup(working, core) or {"ticker": core, "name": core, "mv_twd": share, "kind": core_kind, "price": core_price}
                actions.append(_action_row(bought, "買進", buy_qty, buy_qty * core_price * fx, reason, working))

    return {
        "actions": actions,
        "before": dict(snapshot.metrics),
        "after": dict(working.metrics),
        "after_snapshot": working,
        "notes": notes,
        "target_leverage": target,
    }


def order_price_hint(ticker: str, snapshot: Snapshot) -> float:
    found = _lookup(snapshot, str(ticker).upper())
    if found:
        return found["price"]
    return 0.0


def _clip_return(value: float) -> float:
    return max(float(value), -0.99)


def shock_positions(snapshot: Snapshot, taiex: float = 0.0, sox: float = 0.0) -> pd.DataFrame:
    """依情境改寫價格。台指打在台股與臺指期；費半打在半導體相關部位。"""
    positions = _coerce_positions(snapshot.positions)
    if positions.empty:
        return positions
    for idx, row in positions.iterrows():
        price = float(row["即時報價"])
        ticker = str(row["標的"]).upper()
        kind = str(row["種類"])
        lev = float(row["標的槓桿"])
        beta = float(row["Beta值"])
        sector = str(row.get("產業別", ""))
        if _is_futures_kind(kind) and is_taiex_future(ticker):
            positions.at[idx, "即時報價"] = max(price * (1.0 + taiex), 0.01)
            continue
        if "美" in kind:
            if sox != 0 and is_semi_asset(sector, ticker):
                mult = lev if lev > 1 else 1.0
                positions.at[idx, "即時報價"] = max(price * (1.0 + _clip_return(sox * mult)), 0.01)
            continue
        driver = taiex
        if abs(taiex) < 1e-12 and abs(sox) > 0 and is_semi_asset(sector, ticker):
            driver = sox
        if abs(driver) < 1e-12:
            continue
        mult = lev if lev > 1 else (beta if abs(taiex) > 0 else 1.0)
        positions.at[idx, "即時報價"] = max(price * (1.0 + _clip_return(driver * mult)), 0.01)
    return positions


def run_stress(snapshot: Snapshot, name: str, taiex: float = 0.0, sox: float = 0.0, fx: float = 0.0) -> dict:
    shocked = shock_positions(snapshot, taiex=taiex, sox=sox)
    usdtwd = float(snapshot.usdtwd) * (1.0 + float(fx))
    stressed = Snapshot(shocked, snapshot.accounts, usdtwd, snapshot.twii * (1.0 + taiex) if snapshot.twii else snapshot.twii)
    nav0 = float(snapshot.metrics["nav"])
    nav1 = float(stressed.metrics["nav"])
    drawdown = ((nav1 - nav0) / nav0 * 100.0) if nav0 else 0.0
    return {
        "name": name,
        "nav": nav1,
        "drawdown_pct": drawdown,
        "leverage": stressed.metrics["leverage"],
        "cash_pct": stressed.metrics["cash_pct"],
        "margin_ratio": stressed.metrics["margin_ratio"],
        "margin_call": bool(stressed.metrics["margin_call"]),
        "futures_equity": stressed.metrics["futures_equity"],
        "futures_maint": stressed.metrics["futures_maint"],
        "snapshot": stressed,
    }


def standard_stress(snapshot: Snapshot, rules: Rules) -> list[dict]:
    return [
        run_stress(snapshot, "台指 -10%", taiex=-0.10),
        run_stress(snapshot, "台指 -20%", taiex=-0.20),
        run_stress(snapshot, f"費半 {rules.sox_shock * 100:.0f}%", sox=rules.sox_shock),
        run_stress(snapshot, f"新台幣貶值 {rules.fx_shock * 100:.0f}%", fx=rules.fx_shock),
    ]


def _taiex_book(snapshot: Snapshot):
    """把持倉攤成純數字，給損益圖一次準備、沿用第四頁的淨值公式。"""
    rows = []
    positions = snapshot.positions
    if positions is not None and not positions.empty:
        usdtwd = float(snapshot.usdtwd)
        for _, row in positions.iterrows():
            kind = str(row.get("種類", ""))
            rows.append(
                {
                    "account": str(row.get("帳戶", "")).strip(),
                    "kind": kind,
                    "ticker": str(row.get("標的", "")).upper(),
                    "price": float(row["即時報價"]),
                    "qty": float(row["持有數量"]),
                    "cost": float(row["平均成本"]),
                    "notional": float(row.get("投入總金額", 0) or 0),
                    "lev": float(row.get("標的槓桿", 1) or 1),
                    "beta": float(row.get("Beta值", 1) or 1),
                    "is_fut": "期" in kind,
                    "is_us": "美" in kind,
                    "fx": usdtwd if "美" in kind else 1.0,
                }
            )
    accounts = []
    frame = snapshot.accounts
    if frame is not None and not frame.empty and "帳戶名稱" in frame.columns:
        for _, row in frame.iterrows():
            accounts.append(
                {
                    "name": str(row["帳戶名稱"]).strip(),
                    "cash": float(row.get("現金餘額", 0) or 0),
                    "debt": float(row.get("融資/質押借款", 0) or 0),
                }
            )
    return rows, accounts


def _price_after_taiex(item: dict, move: float) -> float:
    price = item["price"]
    if item["is_fut"] and is_taiex_future(item["ticker"]):
        return max(price * (1.0 + move), 0.01)
    if item["is_us"] or abs(move) < 1e-12:
        return price
    mult = item["lev"] if item["lev"] > 1 else item["beta"]
    return max(price * (1.0 + _clip_return(move * mult)), 0.01)


def _row_pnl_and_mv(item: dict, price: float) -> tuple[float, float]:
    if item["is_fut"]:
        occupied, _maint = _futures_margin_row(item["ticker"], abs(item["qty"]), abs(item["notional"]))
        pnl = (price - item["cost"]) * item["qty"] * futures_multiplier(item["ticker"])
        display_mv = occupied + pnl
    else:
        notional = item["notional"]
        cost = item["cost"]
        latest = notional * (price / cost) if cost != 0 else notional
        pnl = (latest - notional) - (abs(latest) * STOCK_TAX_RATE)
        display_mv = price * item["qty"]
    return pnl, abs(display_mv) * item["fx"]


def taiex_shock_state(snapshot: Snapshot, move: float, book=None) -> dict:
    """台指漲跌後的淨值與期貨權益。數字對齊 run_stress，但不重建 DataFrame。"""
    rows, accounts = book if book is not None else _taiex_book(snapshot)
    grouped: dict[str, list[tuple[float, float, float, float]]] = {}
    for item in rows:
        pnl, mv_twd = _row_pnl_and_mv(item, _price_after_taiex(item, move))
        _occupied, maint = (0.0, 0.0)
        if item["is_fut"]:
            _occupied, maint = _futures_margin_row(item["ticker"], abs(item["qty"]), abs(item["notional"]))
        grouped.setdefault(item["account"], []).append((pnl, mv_twd, item["fx"], maint))
    nav = 0.0
    futures_equity = 0.0
    futures_maint = 0.0
    saw_futures = False
    for account in accounts:
        name = account["name"]
        subset = grouped.get(name, [])
        if "元大期貨" in name:
            saw_futures = True
            if subset:
                contrib = sum(pnl for pnl, _mv, _fx, _maint in subset) * subset[0][2]
                futures_maint += sum(maint for _pnl, _mv, _fx, maint in subset)
            else:
                contrib = 0.0
            equity = YUANTA_FUTURES_BASE_EQUITY + contrib
            futures_equity += equity
            nav += equity
        else:
            market = sum(mv for _pnl, mv, _fx, _maint in subset)
            nav += account["cash"] + market - account["debt"]
    ratio = (futures_equity / futures_maint * 100.0) if saw_futures and futures_maint > 0 else None
    base = float(snapshot.metrics["nav"])
    drawdown = ((nav - base) / base * 100.0) if base else 0.0
    return {
        "nav": nav,
        "drawdown_pct": drawdown,
        "futures_equity": futures_equity,
        "futures_maint": futures_maint,
        "margin_ratio": ratio,
        "margin_call": bool(saw_futures and futures_maint > 0 and futures_equity < futures_maint),
    }


def nav_after_taiex_move(snapshot: Snapshot, move: float, book=None) -> float:
    """台指漲跌後的淨值。公式與 finalize_valuation 相同，但不重建整張表。"""
    return taiex_shock_state(snapshot, move, book)["nav"]


def portfolio_pnl_at_taiex_move(snapshot: Snapshot, move: float) -> float:
    """台指漲跌 move（-0.1 = -10%）時，台股與臺指期的損益。美股在這張圖上不變。"""
    return nav_after_taiex_move(snapshot, move) - float(snapshot.metrics["nav"])


def build_payoff_curve(snapshot: Snapshot, strategies: dict[str, list[dict]], multiplier: float = TXO_MULTIPLIER) -> pd.DataFrame:
    """指數從 -30% 到 +15%，投組損益加上各避險到期損益。"""
    spot0 = float(snapshot.twii) if snapshot.twii else 0.0
    book = _taiex_book(snapshot)
    base_nav = float(snapshot.metrics["nav"])
    rows = []
    for step in range(-30, 16):
        move = step / 100.0
        spot = spot0 * (1.0 + move) if spot0 else 0.0
        port = nav_after_taiex_move(snapshot, move, book) - base_nav
        row = {"漲跌幅%": step, "指數": spot, "投組": port}
        for name, legs in strategies.items():
            hedge = option_expiry_pnl(spot, legs, multiplier) if spot0 else 0.0
            row[name] = port + hedge
        rows.append(row)
    return pd.DataFrame(rows)


def resolve_leg_premium(spot: float, leg: dict, t_years: float, rate: float) -> dict:
    leg = dict(leg)
    if float(leg.get("premium", 0) or 0) <= 0:
        price, _, _ = bs_price_greeks(spot, leg["strike"], t_years, rate, leg.get("iv", 0.2), leg["kind"])
        leg["premium"] = price
    return leg


def strategy_summary(
    snapshot: Snapshot,
    name: str,
    legs: list[dict],
    t_years: float,
    rate: float,
    multiplier: float = TXO_MULTIPLIER,
) -> dict:
    spot = float(snapshot.twii)
    legs = [resolve_leg_premium(spot, leg, t_years, rate) for leg in legs]
    greeks = structure_greeks(spot, legs, t_years, rate, multiplier)
    theta = monthly_theta_cash(spot, legs, t_years, rate, multiplier)
    nav = float(snapshot.metrics["nav"]) or 1.0
    debit = float(greeks["net_debit"])
    base_delta = taiex_dollar_delta(snapshot)
    hedged_delta = base_delta + greeks["delta_per_point"]
    return {
        "name": name,
        "legs": legs,
        "net_debit": debit,
        "premium_pct_nav": debit / nav * 100.0,
        "long_premium": greeks["long_premium"],
        "delta_per_point": greeks["delta_per_point"],
        "beta_before": beta_equivalent(base_delta, spot, nav),
        "beta_after": beta_equivalent(hedged_delta, spot, nav),
        "theta_month": theta,
        "multiplier": multiplier,
    }


def hedge_stress_table(snapshot: Snapshot, summary: dict, moves=(-0.10, -0.20, -0.30)) -> list[dict]:
    """壓力點用到期內含價值估計避險，權利金在一開始就從淨值扣除。"""
    rows = []
    spot0 = float(snapshot.twii)
    debit = float(summary["net_debit"])
    book = _taiex_book(snapshot)
    for move in moves:
        stress = taiex_shock_state(snapshot, move, book)
        spot = spot0 * (1.0 + move)
        hedge_pnl = option_expiry_pnl(spot, summary["legs"], summary["multiplier"])
        intrinsic_cash = hedge_pnl + debit  # 加回權利金，得到避險合約的到期內含價值
        # hedge_pnl 已是內含價值減權利金，震後淨值再加上它就是避險後淨值。
        nav = float(stress["nav"]) + hedge_pnl
        equity = float(stress["futures_equity"]) + hedge_pnl
        maint = float(stress["futures_maint"])
        ratio = (equity / maint * 100.0) if maint > 0 else None
        call = bool(maint > 0 and equity < maint)
        cost_basis = debit if debit > 0 else float(summary["long_premium"])
        plan = hedge_profit_plan(cost_basis, max(intrinsic_cash, 0.0))
        rows.append(
            {
                "move": move,
                "name": f"台指 {move * 100:.0f}%",
                "nav": nav,
                "drawdown_pct": ((nav - snapshot.metrics["nav"]) / snapshot.metrics["nav"] * 100.0) if snapshot.metrics["nav"] else 0.0,
                "hedge_pnl": hedge_pnl,
                "hedge_value": max(intrinsic_cash, 0.0),
                "margin_ratio": ratio,
                "margin_call": call,
                "profit_plan": plan,
            }
        )
    return rows


def suggest_addback_text(rules: Rules, cores: str) -> str:
    core_text = cores.strip() or "（尚未指定）"
    realize_pct = rules.hedge_realize_fraction * 100.0
    return (
        "條件式回補計畫（只寫在這裡，不會自動下單）\n"
        f"1. 槓桿回落：合併槓桿 ≤ {rules.target_leverage:.2f}x"
        f"（第二階段 {rules.stage2_leverage:.2f}x）且現金水位 ≥ {rules.min_cash_pct:.0f}% 才考慮回補。\n"
        f"2. 回補標的只限核心：{core_text}。單筆以停損估算的損失不得超過淨值 {rules.max_loss_pct_nav:.1f}%。\n"
        f"3. 標的槓桿大於 1 的商品預設不回補。若要加回，槓桿需比目標再低 0.30x，"
        f"且投組 Beta 不高於 {rules.beta_high:.2f}。\n"
        f"4. 若台指 -20% 或費半 {rules.sox_shock * 100:.0f}% 的回撤深於 {rules.max_drawdown_pct:.0f}%，"
        "停止回補，先補保證金。\n"
        f"5. 選擇權權利金累計達到淨值 {rules.hedge_budget_pct_nav:.1f}% 的年度預算後不再加碼。"
        f"避險市值 ≥ 成本 {rules.hedge_realize_multiple:.1f} 倍時，實現 {realize_pct:.0f}% 入現金，其餘續抱。\n"
        "6. 回補時優先補回剛減碼的產業，避免只加單一股票。"
    )


def format_qty_text(ticker: str, kind: str, qty: float) -> str:
    if _is_futures_kind(kind) or is_taiex_future(ticker):
        return f"{qty:,.0f} 口"
    if "美" in str(kind):
        return f"{qty:,.2f} 股"
    lots = qty / 1000.0
    return f"{qty:,.0f} 股（{lots:.2f} 張）"


def build_checklist_text(
    snapshot: Snapshot,
    rules: Rules,
    flags: list[dict],
    plan: dict | None = None,
    order_note: str | None = None,
    hedge_note: str | None = None,
    addback_text: str | None = None,
) -> str:
    """純文字檢查清單，給下載與 Telegram。不含任何金鑰。"""
    metrics = snapshot.metrics
    lines = [
        "下單輔助檢查清單（規劃用，不是委託）",
        f"淨值 NT$ {metrics['nav']:,.0f}",
        f"實質曝險 NT$ {metrics['exposure']:,.0f}",
        f"合併槓桿 {metrics['leverage']:.2f}x（上限 {rules.target_leverage:.2f}x，第二階段 {rules.stage2_leverage:.2f}x）",
        f"現金水位 {metrics['cash_pct']:.1f}%（下限 {rules.min_cash_pct:.1f}%）",
        f"Beta {metrics['beta']:.2f}（目標 {rules.beta_low:.2f}–{rules.beta_high:.2f}）",
    ]
    if metrics["margin_ratio"] is not None:
        lines.append(
            f"保證金比率 {metrics['margin_ratio']:.1f}%，可動用保證金 NT$ {metrics['excess_margin']:,.0f}"
            + ("，目前已低於維持保證金" if metrics["margin_call"] else "")
        )
    lines.append("")
    lines.append("規則檢查：")
    for flag in flags:
        mark = "OK" if flag["ok"] else "注意"
        lines.append(f"- [{mark}] {flag['name']}：{flag['detail']}")
    if plan and plan.get("actions"):
        lines.append("")
        lines.append(f"調倉建議（目標槓桿 {plan.get('target_leverage', rules.target_leverage):.2f}x）：")
        for action in plan["actions"]:
            if action["action"] == "暫不換股":
                lines.append(f"- {action['ticker']} 暫不換股：{action['reason']}")
                continue
            lines.append(
                f"- {action['action']} {action['ticker']} {format_qty_text(action['ticker'], action.get('kind', ''), action['qty'])}"
                f"，約 NT$ {action['value']:,.0f}。{action['reason']}"
            )
        after = plan["after"]
        lines.append(
            f"建議執行後：槓桿 {after['leverage']:.2f}x，現金 {after['cash_pct']:.1f}%，Beta {after['beta']:.2f}"
        )
    if order_note:
        lines.append("")
        lines.append("單筆試算：")
        lines.append(order_note)
    if hedge_note:
        lines.append("")
        lines.append("選擇權避險：")
        lines.append(hedge_note)
    if addback_text:
        lines.append("")
        lines.append(addback_text.strip())
    lines.append("")
    lines.append("此清單不會送到券商。")
    return "\n".join(lines)


def build_sample_frames() -> tuple[pd.DataFrame, pd.DataFrame, float, float]:
    """離線示範持倉。數字讓槓桿略高於 1.8x、現金低於 15%，方便看減碼建議。"""
    usdtwd = 32.0
    twii = 23000.0
    positions = pd.DataFrame(
        [
            _sample_row("現貨主帳戶", "台股", "2330", "台積電", 3000, 900, 1080, 1, 1.15, "晶圓代工", -8),
            _sample_row("現貨主帳戶", "台股", "0050", "元大台灣50", 4000, 150, 180, 1, 1.0, "台灣50", -10),
            _sample_row("現貨主帳戶", "台股", "2454", "聯發科", 2000, 1500, 1100, 1, 1.25, "AI晶片", -12),
            _sample_row("現貨主帳戶", "台股", "00663L", "台灣50正2", 20000, 95, 100, 2, 1.9, "台股槓桿ETF", -15),
            _sample_row("現貨主帳戶", "美股", "SOXL", "半導體三倍做多", 4500, 45, 40, 3, 3.1, "半導體槓桿ETF", -20),
            _sample_row("現貨主帳戶", "美股", "NVDA", "輝達", 20, 120, 145, 1, 1.7, "AI晶片", -10),
            _sample_row("元大期貨", "期貨", "TX", "臺指期", 1, 18000, 23000, 1, 1.0, "臺指期貨", -100),
        ]
    )
    accounts = pd.DataFrame(
        [
            {"帳戶名稱": "現貨主帳戶", "現金餘額": 1_500_000, "融資/質押借款": 800_000},
            {"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0},
        ]
    )
    return positions, accounts, usdtwd, twii


def _sample_row(account, kind, ticker, name, qty, cost, price, lev, beta, sector, max_loss):
    notional = 0.0 if "期" in kind else float(cost) * float(qty)
    return {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": name,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": notional,
        "最大虧損比例": max_loss,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": sector,
    }


def build_sample_snapshot() -> Snapshot:
    positions, accounts, usdtwd, twii = build_sample_frames()
    return Snapshot(positions, accounts, usdtwd, twii)


def order_rule_warnings(before: Snapshot, after: Snapshot, rules: Rules, stress_rows: list[dict]) -> list[str]:
    warnings = []
    if after.metrics["leverage"] > rules.target_leverage + 1e-9:
        warnings.append(f"下單後槓桿 {after.metrics['leverage']:.2f}x，高於上限 {rules.target_leverage:.2f}x")
    if after.metrics["cash_pct"] + 1e-9 < rules.min_cash_pct:
        warnings.append(f"下單後現金 {after.metrics['cash_pct']:.1f}%，低於下限 {rules.min_cash_pct:.1f}%")
    if not (rules.beta_low - 1e-9 <= after.metrics["beta"] <= rules.beta_high + 1e-9):
        warnings.append(f"下單後 Beta {after.metrics['beta']:.2f}，不在 {rules.beta_low:.2f}–{rules.beta_high:.2f}")
    if after.metrics["margin_call"]:
        warnings.append("下單後期貨權益低於維持保證金，會落入追繳")
    breaches = loss_breaches(after, rules)
    if breaches:
        names = "、".join(item["ticker"] for item in breaches[:5])
        warnings.append(f"下單後仍有部位超過單一虧損上限：{names}")
    for row in stress_rows:
        if row["drawdown_pct"] < rules.max_drawdown_pct:
            warnings.append(f"{row['name']} 回撤 {row['drawdown_pct']:.1f}%，深於容忍 {rules.max_drawdown_pct:.0f}%")
        if row["margin_call"]:
            warnings.append(f"{row['name']} 之後保證金會追繳")
    if not warnings:
        warnings.append("這筆試算沒有踩到目前的風控規則")
    return warnings
