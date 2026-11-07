"""第六頁的試算器：LEAPS Call、台指期，以及現股風險預算股數。只做試算，不送委託。

Black-Scholes、IV 回推、乘數、保證金與整組估價都沿用 order_assistant／portfolio_risk。
期貨口數仍由 futures_trial 計算，現股股數不走那條。
"""

from __future__ import annotations

import math
from datetime import date

from order_assistant import Snapshot, apply_order, bs_price_greeks, implied_vol_bs
from portfolio_risk import equity_ratio_pct, futures_initial_margin, futures_multiplier, is_option_row

# 現股風險預算的上限。使用者可以選更低，不可以悄悄用更高的比例。
STOCK_RISK_BUDGET_CAP_PCT = 2.0
STOCK_RISK_UNIT = "股"
STOCK_SLIPPAGE_NOTE = "跳空、滑價與手續費可能讓實際虧損大於這個停損距離。"

FX_SOURCE_LABELS = {
    "live": "即時",
    "cached": "本機最近一次成功值",
    "last_known": "淨值快照最後一筆",
    "fallback": "預設值",
    "demo": "示範持倉固定值",
    "unknown": "未標明",
}


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _stock_market(kind: str) -> str | None:
    text = str(kind or "")
    if any(token in text for token in ("期", "選", "權證")):
        return None
    if "美" in text:
        return "US"
    return "TW"


def reference_stop_price(entry: float, kind: str, ticker: str, leverage, sheet_stop_pct=None, name: str = "") -> dict:
    """用試算表停損（沒有才用商品預設）從成交價推出停損價。推不出來 price 是 None。"""
    from stop_rules import default_stop, explicit_stop

    pct = explicit_stop(sheet_stop_pct)
    source = "試算表"
    if pct is None:
        pct, source = default_stop(kind, ticker, leverage, name)
    entry_px = _finite_number(entry)
    if pct is None or entry_px is None or entry_px <= 0 or pct >= 0:
        return {"price": None, "pct": pct, "source": source}
    price = entry_px * (1.0 + pct / 100.0)
    if price <= 0 or price >= entry_px:
        return {"price": None, "pct": pct, "source": source}
    return {"price": price, "pct": pct, "source": source}


def stock_risk_quantity(
    *,
    nav_twd: float,
    entry_price: float,
    stop_price: float,
    kind: str,
    budget_pct: float = STOCK_RISK_BUDGET_CAP_PCT,
    fx_rate: float | None = None,
    fx_source: str = "unknown",
    fx_warning: str | None = None,
) -> dict:
    """買進現股的風險預算上限股數。

    股數 ＝ 風險預算（台幣）÷ 每股停損（台幣），無條件捨去到整股。
    台股價格已是新台幣。美股價差先乘美元／台幣。這不是現金買得起的數量。
    """
    market = _stock_market(kind)
    source = str(fx_source or "unknown")
    result = {
        "ok": False,
        "reason": "",
        "market": market,
        "market_label": "美股" if market == "US" else "台股",
        "unit": STOCK_RISK_UNIT,
        "budget_pct_requested": None,
        "budget_pct_used": None,
        "budget_capped": False,
        "risk_budget_twd": None,
        "loss_per_share_native": None,
        "loss_per_share_twd": None,
        "fx_rate": None,
        "fx_source": source,
        "fx_source_label": FX_SOURCE_LABELS.get(source, source),
        "fx_warning": fx_warning or "",
        "shares": None,
        "lines": [],
    }
    if market is None:
        result["reason"] = "這個試算只給現股與股票型 ETF，單位是股。期貨口數請用期貨下單試算。"
        result["lines"] = [result["reason"]]
        return result

    requested = _finite_number(budget_pct)
    if requested is None or requested <= 0:
        result["reason"] = "風險預算要大於 0%。"
        result["lines"] = [result["reason"]]
        return result
    result["budget_pct_requested"] = requested
    used = requested
    if requested > STOCK_RISK_BUDGET_CAP_PCT + 1e-9:
        used = STOCK_RISK_BUDGET_CAP_PCT
        result["budget_capped"] = True
    result["budget_pct_used"] = used

    nav = _finite_number(nav_twd)
    if nav is None or nav <= 0:
        result["reason"] = "淨值要是正數，才算得出風險預算。"
        result["lines"] = [result["reason"]]
        return result
    entry = _finite_number(entry_price)
    if entry is None or entry <= 0:
        result["reason"] = "成交價要大於 0。"
        result["lines"] = [result["reason"]]
        return result
    stop = _finite_number(stop_price)
    if stop is None or stop <= 0 or stop >= entry:
        result["reason"] = "買進現股的停損價要大於 0，而且要低於成交價。停損距離不是正數時不給股數。"
        result["lines"] = [result["reason"]]
        return result

    loss_native = entry - stop
    if market == "US":
        fx = _finite_number(fx_rate)
        if fx is None or fx <= 0:
            result["reason"] = "沒有可用的美元／台幣匯率，不算股數。"
            result["lines"] = [result["reason"]]
            return result
        result["fx_rate"] = fx
        loss_twd = loss_native * fx
    else:
        result["fx_rate"] = 1.0
        loss_twd = loss_native
    if loss_twd <= 0:
        result["reason"] = "每股停損換算成台幣後不是正數，不給股數。"
        result["lines"] = [result["reason"]]
        return result

    if market != "US":
        result["fx_warning"] = ""
    budget = nav * used / 100.0
    shares = int(math.floor(budget / loss_twd))
    if shares < 0:
        shares = 0
    result.update(
        ok=True,
        reason="",
        risk_budget_twd=budget,
        loss_per_share_native=loss_native,
        loss_per_share_twd=loss_twd,
        shares=shares,
    )
    result["lines"] = _stock_risk_lines(result)
    return result


def _stock_risk_lines(result: dict) -> list[str]:
    shares = int(result["shares"])
    unit = result["unit"]
    lines = [
        f"風險預算允許的最大數量：{shares:,} {unit}（{result['market_label']}，無條件捨去到整股）。",
        (
            f"風險預算 NT$ {result['risk_budget_twd']:,.0f}"
            f"（淨值的 {result['budget_pct_used']:.2f}%）"
            f" ÷ 每股停損 NT$ {result['loss_per_share_twd']:,.2f}。"
        ),
    ]
    if result["market"] == "US":
        lines.append(
            f"美股價差 US$ {result['loss_per_share_native']:,.4f} × 匯率 {result['fx_rate']:.4f}。"
            f"匯率來源：{result['fx_source_label']}。"
        )
        lines.append("美股單位是股，無條件捨去，不拆碎股。")
    else:
        lines.append("台股價格已是新台幣。單位是股，不是口，也不是張。")
    if shares == 0:
        lines.append("一股的停損就超過風險預算，無條件捨去後是 0 股。")
    if result["budget_capped"]:
        lines.append(
            f"輸入的 {result['budget_pct_requested']:.2f}% 高過 {STOCK_RISK_BUDGET_CAP_PCT:.0f}%，"
            f"已改以 {STOCK_RISK_BUDGET_CAP_PCT:.0f}% 計算，沒有放大預算。"
        )
    if result.get("fx_warning"):
        lines.append(str(result["fx_warning"]))
    elif result["market"] == "US" and result["fx_source"] != "live":
        lines.append(f"美元／台幣不是即時匯率（{result['fx_source_label']}）。")
    lines.append(STOCK_SLIPPAGE_NOTE)
    lines.append("這是風險預算下的股數上限，沒有拿來跟帳戶現金比較。現金、下單後槓桿與 Beta 另外看試算，不會用這個股數去擋單。")
    return lines


def years_to_expiry(expiry: date, today: date) -> float:
    days = (expiry - today).days
    return max(0.0, days / 365.0)

def iv_signal(iv: float | None, ref_vol: float | None = None) -> dict:
    if iv is None:
        return {"color": "none", "label": "無法回推 IV", "detail": ""}
    
    if ref_vol is not None and ref_vol > 0:
        ratio = iv / ref_vol
        if ratio < 0.9:
            color, label = "green", "偏低（相對參考波動率便宜）"
        elif ratio <= 1.2:
            color, label = "yellow", "中間"
        else:
            color, label = "red", "偏高（權利金偏貴）"
        detail = f"IV {iv*100:.1f}% ÷ 參考 {ref_vol*100:.1f}% = {ratio:.2f} 倍"
    else:
        if iv < 0.30:
            color, label = "green", "偏低"
        elif iv <= 0.60:
            color, label = "yellow", "中間"
        else:
            color, label = "red", "偏高"
        detail = "未填參考波動率，用絕對水準判斷（30%／60%）"
        
    return {"color": color, "label": label, "detail": detail}

def leaps_call_metrics(spot, strike, expiry: date, premium, rate=0.04, today: date | None = None, contracts=1, target_exposure=None, ref_vol=None, contract_size=100) -> dict:
    if today is None:
        today = date.today()
    t = years_to_expiry(expiry, today)
    
    intrinsic = max(spot - strike, 0.0)
    extrinsic = max(premium - intrinsic, 0.0)
    extrinsic_pct = (extrinsic / premium * 100.0) if premium > 0 else 0.0
    extrinsic_annual_pct = (extrinsic / spot / t * 100.0) if (spot > 0 and t > 0) else 0.0
    breakeven = strike + premium
    
    res = {
        "ok": True,
        "error": "",
        "t_years": t,
        "iv": None,
        "delta": 0.0,
        "model_price": 0.0,
        "theta_year": 0.0,
        "intrinsic": intrinsic,
        "extrinsic": extrinsic,
        "extrinsic_pct": extrinsic_pct,
        "extrinsic_annual_pct": extrinsic_annual_pct,
        "breakeven": breakeven,
        "leverage_ratio": 0.0,
        "equivalent_shares": 0.0,
        "delta_exposure": 0.0,
        "cost_total": premium * contract_size * contracts,
        "theta_month_total": 0.0,
        "shares_per_contract": 0.0,
        "suggested_contracts": None,
        "suggested_cost": None,
        "suggested_equivalent_shares": None,
        "signal": {"color": "none", "label": "無法回推 IV", "detail": ""},
        "warnings": []
    }
    
    if spot <= 0 or strike <= 0 or premium <= 0 or t <= 0:
        res["ok"] = False
        res["error"] = "現價、履約價、權利金與到期時間必須大於 0"
        return res
        
    iv = implied_vol_bs(premium, spot, strike, t, rate, kind="call")
    if iv is None:
        res["ok"] = False
        res["error"] = "權利金低於內含價值或高於現價，無法回推隱含波動率"
        return res
        
    res["iv"] = iv
    res["signal"] = iv_signal(iv, ref_vol)
    
    price_model, delta, theta_year = bs_price_greeks(spot, strike, t, rate, iv, "call")
    res["model_price"] = price_model
    res["delta"] = delta
    res["theta_year"] = theta_year
    
    res["leverage_ratio"] = (delta * spot / premium) if premium > 0 else 0.0
    res["shares_per_contract"] = delta * contract_size
    res["equivalent_shares"] = delta * contract_size * contracts
    res["delta_exposure"] = res["equivalent_shares"] * spot
    res["theta_month_total"] = theta_year * 30.0 / 365.0 * contract_size * contracts
    
    if target_exposure is not None and target_exposure > 0:
        shares_per_contract = res["shares_per_contract"]
        if shares_per_contract > 0 and spot > 0:
            suggested_contracts = math.ceil(target_exposure / (shares_per_contract * spot))
            suggested_contracts = max(1, suggested_contracts)
            res["suggested_contracts"] = suggested_contracts
            res["suggested_cost"] = suggested_contracts * premium * contract_size
            res["suggested_equivalent_shares"] = shares_per_contract * suggested_contracts
    
    if delta < 0.70:
        res["warnings"].append("Delta 低於 0.70，比較像槓桿投機而不是股票替代")
    if t < 0.75:
        res["warnings"].append("到期不到 9 個月，不算 LEAPS，時間價值會加速流失")
    if extrinsic_pct > 30.0:
        res["warnings"].append("時間價值占權利金超過 30%")
        
    return res


FUTURES_PRODUCTS = {"TX": "大台", "MTX": "小台", "FMF": "微台（TMF）"}

def normalize_futures_product(code: str) -> str:
    code = code.strip().upper()
    mapping = {
        "TMF": "FMF",
        "大台": "TX",
        "小台": "MTX",
        "微台": "FMF",
        "微台（TMF）": "FMF"
    }
    code = mapping.get(code, code)
    if code not in FUTURES_PRODUCTS:
        raise ValueError(f"未知的期貨商品代號: {code}")
    return code

def default_margin_per_lot(product: str, ledger_margin: float | None = None, ledger_product: str | None = None) -> float:
    try:
        norm_prod = normalize_futures_product(product)
    except ValueError:
        norm_prod = product
        
    if ledger_margin is not None and ledger_margin > 0 and ledger_product is not None:
        try:
            if normalize_futures_product(ledger_product) == norm_prod:
                return float(ledger_margin)
        except ValueError:
            pass
            
    margin = futures_initial_margin(norm_prod)
    return float(margin) if margin is not None else 0.0

MARGIN_SOURCE_LEDGER = "Futures_Ledger"
MARGIN_SOURCE_DEFAULT = "預設值"

# 試算器的商品代號 → Futures_Ledger 可能使用的代號（微台在持倉統計是 FMF，元大有時寫 TMF）。
_LEDGER_KEYS = {"TX": ("TX",), "MTX": ("MTX",), "FMF": ("FMF", "TMF")}


def ledger_margin_per_lot(product: str, metrics: dict | None) -> dict:
    """每口原始保證金：先找 Futures_Ledger 推出的值，找不到就用系統預設。

    回傳 {"value", "source"（Futures_Ledger／預設值）, "basis"（推算方式）}。
    metrics 通常是 snapshot.metrics；鍵 futures_ledger_margin_per_lot 由 futures_ledger 寫入。
    """
    try:
        norm_prod = normalize_futures_product(product)
    except ValueError:
        norm_prod = str(product or "").strip().upper()
    per_lot = {}
    basis = {}
    if isinstance(metrics, dict):
        raw = metrics.get("futures_ledger_margin_per_lot")
        per_lot = raw if isinstance(raw, dict) else {}
        raw_basis = metrics.get("futures_ledger_margin_basis")
        basis = raw_basis if isinstance(raw_basis, dict) else {}
    for key in _LEDGER_KEYS.get(norm_prod, (norm_prod,)):
        value = per_lot.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return {"value": number, "source": MARGIN_SOURCE_LEDGER, "basis": str(basis.get(key) or "")}
    return {
        "value": float(default_margin_per_lot(norm_prod)),
        "source": MARGIN_SOURCE_DEFAULT,
        "basis": "系統設定的原始保證金（TX 701,000／MTX 175,250／FMF 35,050）",
    }


def _margin_map(metrics: dict | None) -> dict[str, float]:
    """把 Futures_Ledger 的每口保證金整理成試算器代號（TX／MTX／FMF）。"""
    out: dict[str, float] = {}
    for product in FUTURES_PRODUCTS:
        info = ledger_margin_per_lot(product, metrics)
        if info["source"] == MARGIN_SOURCE_LEDGER:
            out[product] = float(info["value"])
    return out


def initial_margin_with_ledger(valued, per_lot: dict[str, float] | None, fallback: float = 0.0) -> float:
    """元大期貨部位的原始保證金合計。有 Futures_Ledger／畫面上的每口值就用它，否則沿用估價表的預設占用。

    valued 是 finalize_valuation 的持倉表（snapshot.valued）。沒有期貨列時回 fallback。
    """
    if valued is None or getattr(valued, "empty", True) or "種類" not in valued.columns:
        return float(fallback or 0.0)
    total = 0.0
    saw = False
    for _, row in valued.iterrows():
        if "期" not in str(row.get("種類")) or is_option_row(row):
            continue
        saw = True
        try:
            qty = abs(float(row.get("持有數量") or 0.0))
        except (TypeError, ValueError):
            qty = 0.0
        value = None
        try:
            prod = normalize_futures_product(str(row.get("標的") or ""))
            value = (per_lot or {}).get(prod)
        except ValueError:
            prod = None
        if value is not None and value > 0:
            total += float(value) * qty
        else:
            try:
                total += float(row.get("實質佔用本金(原幣)") or 0.0)
            except (TypeError, ValueError):
                pass
    return total if saw else float(fallback or 0.0)


def futures_trial(product, lots, price, stop_price=None, side="buy", margin_per_lot=None, max_loss=None) -> dict:
    res = {
        "ok": True,
        "error": "",
        "product": product,
        "product_name": "",
        "side": side,
        "lots": lots,
        "price": price,
        "multiplier": 0.0,
        "margin_per_lot": 0.0,
        "margin_needed": 0.0,
        "notional": 0.0,
        "stop_price": stop_price,
        "loss_points": None,
        "loss_per_lot": None,
        "stop_loss_total": None,
        "max_loss": max_loss,
        "max_lots_by_loss": None,
        "warnings": []
    }
    
    try:
        norm_prod = normalize_futures_product(product)
        res["product"] = norm_prod
        res["product_name"] = FUTURES_PRODUCTS[norm_prod]
    except ValueError:
        res["ok"] = False
        res["error"] = f"未知的期貨商品: {product}"
        return res
        
    try:
        lots_value = float(lots)
    except (TypeError, ValueError):
        lots_value = -1.0
    if lots_value < 0 or not lots_value.is_integer():
        res["ok"] = False
        res["error"] = "口數必須是大於等於 0 的整數"
        return res
    lots = int(lots_value)
    res["lots"] = lots
    price = float(price or 0.0)
    res["price"] = price

    if price <= 0:
        res["ok"] = False
        res["error"] = "價格必須大於 0"
        return res
        
    mult = futures_multiplier(norm_prod)
    res["multiplier"] = mult
    
    if margin_per_lot is None:
        margin_per_lot = default_margin_per_lot(norm_prod)
    res["margin_per_lot"] = margin_per_lot
    res["margin_needed"] = margin_per_lot * lots
    res["notional"] = lots * price * mult
    
    is_long = side in ("buy", "買進")
    is_short = side in ("sell", "賣出")
    
    if stop_price is not None:
        if is_long and stop_price >= price:
            res["warnings"].append("停損價在錯的一邊")
        elif is_short and stop_price <= price:
            res["warnings"].append("停損價在錯的一邊")
        else:
            loss_points = abs(price - stop_price)
            loss_per_lot = loss_points * mult
            res["loss_points"] = loss_points
            res["loss_per_lot"] = loss_per_lot
            res["stop_loss_total"] = loss_per_lot * lots
            
            if max_loss is not None and max_loss > 0 and loss_per_lot > 0:
                max_lots = math.floor(max_loss / loss_per_lot)
                res["max_lots_by_loss"] = max_lots
                if lots > max_lots:
                    res["warnings"].append(f"口數超過停損上限，最多 {max_lots} 口")
    elif max_loss is not None and max_loss > 0:
        res["max_lots_by_loss"] = None
        
    return res

def futures_order_impact(snapshot: Snapshot, trial: dict, account: str | None = None) -> dict:
    """下單前後的槓桿、Beta 與權益比率。

    權益比率＝權益數 ÷ 原始保證金。權益數用 finalize_valuation 的元大期貨帳戶淨值
    （本日餘額來自 Futures_Ledger 推算，加上未平倉損益與選擇權市值）；原始保證金用
    Futures_Ledger 推出的每口保證金，本次試算商品用畫面上的每口值，其餘沿用預設。
    """
    product = trial.get("product", "")
    side = trial.get("side", "buy")
    lots = trial.get("lots", 0)
    price = trial.get("price", 0.0)
    product_name = trial.get("product_name", "")
    
    order = {
        "ticker": product,
        "side": side,
        "qty": lots,
        "price": price,
        "kind": "期貨",
        "beta": 1.0,
        "sector": "臺指期貨",
        "name": product_name,
        "account": account
    }
    
    after_snapshot, warnings = apply_order(snapshot, order)
    
    before = snapshot.metrics
    after = after_snapshot.metrics

    per_lot = _margin_map(before)
    trial_margin = trial.get("margin_per_lot")
    try:
        trial_margin = float(trial_margin) if trial_margin is not None else None
    except (TypeError, ValueError):
        trial_margin = None
    if product in FUTURES_PRODUCTS and trial_margin is not None and trial_margin > 0:
        per_lot[product] = trial_margin

    initial_before = initial_margin_with_ledger(
        getattr(snapshot, "valued", None), per_lot, fallback=float(before.get("futures_initial") or 0.0)
    )
    initial_after = initial_margin_with_ledger(
        getattr(after_snapshot, "valued", None), per_lot, fallback=float(after.get("futures_initial") or 0.0)
    )
    equity_before = before.get("futures_equity")
    equity_after = after.get("futures_equity")

    eq_ratio_before = equity_ratio_pct(equity_before, initial_before)
    eq_ratio_after = equity_ratio_pct(equity_after, initial_after)
    
    # 可動用保證金＝權益數 − 原始保證金；原始保證金改用上面的 Futures_Ledger 口徑。
    excess_margin_before = float(before.get("excess_margin") or 0.0)
    excess_margin_before += float(before.get("futures_initial") or 0.0) - initial_before
    margin_needed = float(trial.get("margin_needed") or 0.0)
    
    opening = str(side).lower() in ("buy", "買進")
    # 賣出（平多單）會釋出保證金，不需要再檢查可動用保證金。
    margin_sufficient = bool(excess_margin_before >= margin_needed) if opening else True
    margin_shortfall = max(0.0, margin_needed - excess_margin_before) if opening else 0.0

    cash_source = str(before.get("futures_cash_source") or "")
    equity_source = "Futures_Ledger 本日餘額" if cash_source == "ledger" else ("Active_Positions 現金餘額（B7）" if cash_source == "b7" else "備援常數")
    
    return {
        "after": after_snapshot,
        "warnings": warnings,
        "leverage_before": float(before.get("leverage") or 0.0),
        "leverage_after": float(after.get("leverage") or 0.0),
        "total_beta_before": float(before.get("total_beta") or 0.0),
        "total_beta_after": float(after.get("total_beta") or 0.0),
        "tw_beta_before": float(before.get("tw_beta") or 0.0),
        "tw_beta_after": float(after.get("tw_beta") or 0.0),
        "nav_before": float(before.get("nav") or 0.0),
        "nav_after": float(after.get("nav") or 0.0),
        "exposure_before": float(before.get("exposure") or 0.0),
        "exposure_after": float(after.get("exposure") or 0.0),
        "equity_before": float(equity_before or 0.0),
        "equity_after": float(equity_after or 0.0),
        "equity_source": equity_source,
        "initial_margin_before": initial_before,
        "initial_margin_after": initial_after,
        "equity_ratio_before": eq_ratio_before,
        "equity_ratio_after": eq_ratio_after,
        "excess_margin_before": excess_margin_before,
        "margin_sufficient": margin_sufficient,
        "margin_shortfall": margin_shortfall
    }
