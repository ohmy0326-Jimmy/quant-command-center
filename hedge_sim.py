"""第六頁策略模擬器裡的「避險策略模擬」計算。純邏輯，不碰 Streamlit 也不連網路。

由 /workspace/sim/hedge_sim.py 原型移植：TAIEX × β 的單一曝險帳戶，比較無避險、
觸發式期貨避險、月滾價外賣權、賣權價差四種做法；另提供目前觸發燈號與避險建議（只給建議，不下單）。
"""

from __future__ import annotations

import math
import dataclasses
import datetime
import numpy as np
import pandas as pd

TOOLS = ("none", "futures", "put", "spread")
TOOL_LABELS = {
    "none": "無避險",
    "futures": "期貨避險（觸發）",
    "put": "月滾賣權",
    "spread": "賣權價差"
}
DEFAULT_TOOL = "futures"

TRIGGERS = ("ma60", "drawdown", "score")
TRIGGER_LABELS = {
    "ma60": "收盤 < 60DMA",
    "drawdown": "自高點回撤 ≥ 8%",
    "score": "多空分數 4 週內下降 ≥ 3"
}

EVENT_WINDOWS = [
    ("2020-01-20", "2020-03-31", "2020 疫情"),
    ("2022-01-01", "2022-10-31", "2022 空頭"),
    ("2024-07-11", "2024-08-15", "2024/8"),
    ("2025-03-25", "2025-04-30", "2025/4")
]

ASSUMPTIONS = [
    "帳戶以加權指數 × β（預設 2）代表，起始淨值與槓桿用面板上的設定；融資利率每日計息。",
    "每月已實現獲利依「獲利提撥 %」進避險資金池；年度成本上限每年 1/1 依當時淨值重設。",
    "選擇權每月月底平倉再滾動，到期天數用面板設定；權利金與平倉價值都用 Black-Scholes（20 日實現波動 + 偏斜加碼）。",
    "賣權價差在觸發當天把覆蓋率補到「警戒」值，價值達成本的設定倍數時平掉一半。",
    "期貨避險：觸發成立且收盤 < 20DMA 時放空（覆蓋率用「正常」值，與原型回測一致），收盤站回 20DMA 解除；每次進出扣期貨滑價。",
]

LIMITATIONS = [
    "以 TAIEX × β 代替真實持股，個股、美股與槓桿 ETF 的耗損都沒有模擬。",
    "BS + 固定偏斜不是真實 TXO 報價，深價外與大跌時的波動率微笑會低估。",
    "不計期貨正逆價差、轉倉成本與保證金；月中不追繳。",
    "多空分數沒有歷史序列，回測不含這個觸發，它只用在目前的燈號。",
    "歷史回測不代表未來績效。",
]

@dataclasses.dataclass(frozen=True)
class HedgeParams:
    alloc_pct: float = 0.10
    annual_cap_pct: float = 0.02
    cov_normal: float = 0.5
    cov_alert: float = 1.0
    otm_pct: float = 0.05
    spread_width_pct: float = 0.07
    tenor_days: int = 50
    triggers: tuple = ("ma60", "drawdown", "score")
    drawdown_trigger: float = -0.08
    score_drop: int = 3
    score_window_days: int = 28
    skew_vol: float = 0.03
    fin_rate: float = 0.03
    futures_slip: float = 0.0005
    take_profit_multiple: float = 3.0
    rf: float = 0.015
    beta: float = 2.0
    leverage: float = 1.5
    nav0: float = 3.45e6

    def __post_init__(self):
        if not (0 <= self.alloc_pct <= 1):
            raise ValueError("alloc_pct 必須在 0 到 1 之間")
        if not (0 <= self.annual_cap_pct <= 0.2):
            raise ValueError("annual_cap_pct 必須在 0 到 0.2 之間")
        if not (0 <= self.cov_normal <= 2):
            raise ValueError("cov_normal 必須在 0 到 2 之間")
        if not (0 <= self.cov_alert <= 2):
            raise ValueError("cov_alert 必須在 0 到 2 之間")
        if self.cov_alert < self.cov_normal:
            raise ValueError("cov_alert 不能小於 cov_normal")
        if not (0 <= self.otm_pct <= 0.3):
            raise ValueError("otm_pct 必須在 0 到 0.3 之間")
        if not (0 < self.spread_width_pct <= 0.3):
            raise ValueError("spread_width_pct 必須大於 0 且小於等於 0.3")
        if not (7 <= self.tenor_days <= 180):
            raise ValueError("tenor_days 必須在 7 到 180 之間")
        for t in self.triggers:
            if t not in TRIGGERS:
                raise ValueError(f"未知的觸發條件: {t}")
        if self.leverage < 1:
            raise ValueError("leverage 不能小於 1")

def bs_put(S: float, K: float, T: float, v: float, rf: float = 0.015) -> float:
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (rf + v * v / 2.0) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    def norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return K * math.exp(-rf * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def realized_vol(px: pd.Series, window: int = 20) -> pd.Series:
    return px.pct_change().fillna(0).rolling(window).std().mul(math.sqrt(252)).bfill().clip(0.08, 1.0)

def prepare(px: pd.Series, params: HedgeParams, score_alert: pd.Series | None = None) -> pd.DataFrame:
    if len(px) < 80:
        raise ValueError("價格資料不足 80 筆")
    df = pd.DataFrame({"S": px})
    df["r"] = px.pct_change().fillna(0)
    df["vol"] = realized_vol(px, 20)
    df["ma60"] = px.rolling(60).mean()
    df["ma20"] = px.rolling(20).mean()
    df["dd"] = px / px.cummax() - 1.0
    
    trig_conds = []
    if "ma60" in params.triggers:
        trig_conds.append(px < df["ma60"])
    if "drawdown" in params.triggers:
        trig_conds.append(df["dd"] <= params.drawdown_trigger)
    if "score" in params.triggers and score_alert is not None:
        sa = score_alert.reindex(px.index).ffill().fillna(False).astype(bool)
        trig_conds.append(sa)
        
    if trig_conds:
        df["trig"] = pd.concat(trig_conds, axis=1).any(axis=1).fillna(False).astype(bool)
    else:
        df["trig"] = False
        
    df["month"] = df.index.to_period("M")
    return df

def _pv(p: dict, S: float, dt: pd.Timestamp, v: float, params: HedgeParams) -> float:
    T = max((p["exp"] - dt).days, 0) / 365.0
    val = bs_put(S, p["K1"], T, v + params.skew_vol, params.rf)
    if p["K2"]:
        val -= bs_put(S, p["K2"], T, v + params.skew_vol, params.rf)
    return val * p["units"]

def _open_pos(nav: float, S: float, v: float, dt: pd.Timestamp, cov: float, mode: str, params: HedgeParams) -> tuple[float, dict]:
    units = cov * params.beta * nav / S
    K1 = S * (1.0 - params.otm_pct)
    K2 = S * (1.0 - (params.otm_pct + params.spread_width_pct)) if mode == "spread" else None
    exp = dt + pd.Timedelta(days=round(params.tenor_days))
    u = dict(K1=K1, K2=K2, exp=exp, units=units, cost=0.0, tp=False)
    return _pv(u, S, dt, v, params), u

def run(df: pd.DataFrame, mode: str, params: HedgeParams) -> tuple[pd.Series, float]:
    if mode not in TOOLS:
        raise ValueError(f"未知的模式: {mode}")

    fin_d = (params.leverage - 1.0) * params.fin_rate / 252.0
    nav = params.nav0
    navs = np.empty(len(df), dtype=float)
    pool = 0.0
    spent_yr = 0.0
    cap_yr = params.annual_cap_pct * params.nav0
    
    S_arr = df["S"].values
    r_arr = df["r"].values
    v_arr = df["vol"].values
    trig_arr = df["trig"].values
    ma20_arr = df["ma20"].values
    month_arr = df["month"].values
    
    dates = df.index
    yr = dates[0].year
    month_start_nav = params.nav0
    hedged = False
    prev_trig = False
    cost = 0.0
    pos = []

    for i in range(len(df)):
        dt = dates[i]
        S = S_arr[i]
        v = v_arr[i]
        r = r_arr[i]
        trig = trig_arr[i]
        ma20 = ma20_arr[i]
        
        beta = params.beta
        if mode == "futures" and hedged:
            beta = params.beta * (1.0 - params.cov_normal)
            
        if i > 0:
            S_prev, dt_prev, v_prev = S_arr[i-1], dates[i-1], v_arr[i-1]
        else:
            S_prev, dt_prev, v_prev = S, dt, v
            
        val_before = sum(_pv(p, S_prev, dt_prev, v_prev, params) for p in pos)
        nav *= 1.0 + beta * r - fin_d
        val_after = sum(_pv(p, S, dt, v, params) for p in pos)
        nav += val_after - val_before
        
        if mode == "futures":
            if not hedged and trig and S < ma20:
                hedged = True
                c = params.futures_slip * nav * params.beta * params.cov_normal
                nav -= c
                cost += c
            elif hedged and S > ma20:
                hedged = False
                c = params.futures_slip * nav * params.beta * params.cov_normal
                nav -= c
                cost += c
                
        if dt.year != yr:
            yr = dt.year
            spent_yr = 0.0
            cap_yr = params.annual_cap_pct * nav
            
        new_month = (i + 1 == len(df)) or (month_arr[i + 1] != month_arr[i])
        
        if mode in ("put", "spread"):
            if mode == "spread":
                for p in pos:
                    if not p["tp"] and _pv(p, S, dt, v, params) >= params.take_profit_multiple * p["cost"]:
                        p["units"] /= 2.0
                        p["cost"] /= 2.0
                        p["tp"] = True
                        
            if mode == "spread" and trig and not prev_trig and pos:
                c, u = _open_pos(nav, S, v, dt, params.cov_alert - params.cov_normal, mode, params)
                spend = min(pool, cap_yr - spent_yr, c)
                if spend > 0:
                    f = spend / c
                    pos.append(dict(K1=u["K1"], K2=u["K2"], exp=u["exp"], units=u["units"]*f, cost=spend, tp=False))
                    pool -= spend
                    spent_yr += spend
                    nav -= spend
                    cost += spend
                    
            if new_month:
                profit = nav - month_start_nav
                if profit > 0:
                    pool += params.alloc_pct * profit
                pos.clear()
                
                cov_to_use = params.cov_alert if (mode == "spread" and trig) else params.cov_normal
                c, u = _open_pos(nav, S, v, dt, cov_to_use, mode, params)
                spend = min(pool, max(cap_yr - spent_yr, 0.0), c)
                if spend > 0:
                    f = spend / c
                    pos.append(dict(K1=u["K1"], K2=u["K2"], exp=u["exp"], units=u["units"]*f, cost=spend, tp=False))
                    pool -= spend
                    spent_yr += spend
                    nav -= spend
                    cost += spend
                    
        if new_month:
            month_start_nav = nav
            
        prev_trig = trig
        navs[i] = nav
        
    return pd.Series(navs, index=df.index), cost

def metrics(nav: pd.Series, cost: float, nav0: float) -> dict:
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    dd = nav / nav.cummax() - 1.0
    try:
        m = nav.resample("ME").last()
    except ValueError:
        m = nav.resample("M").last()
        
    mret = m.pct_change().fillna(m.iloc[0] / nav0 - 1.0)
    breach = (dd.groupby(dd.index.to_period("M")).min() <= -0.20).sum()
    
    return {
        "期末淨值": nav.iloc[-1],
        "CAGR": (nav.iloc[-1] / nav0) ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0,
        "最大回撤": dd.min(),
        "最差月份": mret.min(),
        "累計避險成本": cost,
        "回撤超過20%月數": int(breach)
    }

def simulate(px: pd.Series, params: HedgeParams, tools=TOOLS, score_alert=None) -> dict:
    df = prepare(px, params, score_alert)
    nav_dict = {}
    cost_dict = {}
    metrics_list = []
    
    for tool in tools:
        nav, cost = run(df, tool, params)
        nav_dict[tool] = nav
        cost_dict[tool] = cost
        m = metrics(nav, cost, params.nav0)
        m["_tool"] = TOOL_LABELS[tool]
        metrics_list.append(m)
        
    tab = pd.DataFrame(metrics_list).set_index("_tool")
    tab.index.name = None
    
    return {
        "df": df,
        "nav": nav_dict,
        "cost": cost_dict,
        "table": tab
    }

def drawdown(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1.0

def _iso(text) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def score_dropped(history: list[dict], current_total: int | None, as_of: str | None = None, drop: int = 3, window_days: int = 28) -> bool | None:
    """多空分數在 window_days 內自高點下降 ≥ drop 分。

    只看早於 as_of 的週分數（與 macro_cycle.week_change 一致）；as_of 沒給就用最新一週。
    沒有本週分數，或窗口內沒有更早的分數，回 None（燈號顯示無資料）。
    """
    if current_total is None:
        return None
    rows = [(_iso(r.get("week")), r.get("total")) for r in (history or [])]
    rows = [(d, int(t)) for d, t in rows if d is not None and t is not None]
    ref = _iso(as_of) if as_of else None
    if ref is None:
        if not rows:
            return None
        ref = max(d for d, _ in rows)
        prior = [t for d, t in rows if d < ref and (ref - d).days <= window_days]
        prior += [t for d, t in rows if d == ref]
    else:
        prior = [t for d, t in rows if d < ref and (ref - d).days <= window_days]
    if not prior:
        return None
    return (max(prior + [int(current_total)]) - int(current_total)) >= drop


def trigger_status(px: pd.Series, params: HedgeParams, score_history=None, score_total=None, score_as_of=None) -> dict:
    if px is None or len(px.dropna()) == 0:
        raise ValueError("沒有指數收盤價")
    px = px.dropna().astype(float)
    close = float(px.iloc[-1])
    max252 = float(px.iloc[-252:].max())
    dd = (close / max252 - 1.0) if (len(px) >= 20 and max252 > 0) else None
    vol20 = float(realized_vol(px, 20).iloc[-1]) if len(px) >= 21 else None
    
    ma20 = float(px.iloc[-20:].mean()) if len(px) >= 20 else None
    ma60 = float(px.iloc[-60:].mean()) if len(px) >= 60 else None
    lights = {
        "ma60": bool(close < ma60) if ma60 is not None else None,
        "drawdown": bool(dd <= params.drawdown_trigger) if dd is not None else None,
        "score": score_dropped(score_history or [], score_total, score_as_of, params.score_drop, params.score_window_days)
    }
    
    alert = any(lights.get(k) is True for k in params.triggers)
    release = bool(close > ma20) if ma20 is not None else None
    
    return {
        "close": close,
        "ma20": ma20,
        "ma60": ma60,
        "drawdown": dd,
        "vol20": vol20,
        "lights": lights,
        "selected": params.triggers,
        "alert": alert,
        "release": release
    }

def nearest_otm_strike(spot: float, otm_pct: float, step: int = 50) -> float:
    return float(math.floor(spot * (1.0 - otm_pct) / step) * step)

def recommend_hedge(*, nav: float, beta_exposure: float, index_level: float, vol20: float, alert: bool, tool: str, params: HedgeParams, pool_balance: float, spent_ytd: float, quote_fn=None, strike_step: int = 50) -> dict:
    if nav <= 0 or index_level <= 0:
        raise ValueError("NAV 與 index_level 必須大於 0")
        
    account_beta = beta_exposure / nav
    
    if tool == "none":
        coverage = 0.0
    elif tool == "futures":
        coverage = params.cov_normal if alert else 0.0
    elif tool in ("put", "spread"):
        coverage = params.cov_alert if alert else params.cov_normal
    else:
        raise ValueError(f"未知的避險工具: {tool}")
        
    hedge_notional = beta_exposure * coverage
    annual_cap = params.annual_cap_pct * nav
    remaining_cap = max(annual_cap - spent_ytd, 0.0)
    available = max(min(pool_balance, remaining_cap), 0.0)
    
    res = {
        "tool": tool,
        "alert": alert,
        "coverage": coverage,
        "account_beta": account_beta,
        "hedge_notional": hedge_notional,
        "annual_cap": annual_cap,
        "remaining_cap": remaining_cap,
        "pool_balance": pool_balance,
        "spent_ytd": spent_ytd,
        "available": available,
        "notes": []
    }
    
    if tool == "none":
        res.update({
            "lots_info": {}, "legs": [], "net_premium_points": 0.0,
            "total_cost": 0.0, "budget_ok": True, "affordable_lots": 0
        })
        res["notes"].append("無避險工具，不需操作。")
        return res
        
    if tool == "futures":
        mtx_lots = round(hedge_notional / (index_level * 50))
        tx_lots = round(hedge_notional / (index_level * 200))
        res["lots_info"] = {
            "mtx": mtx_lots,
            "tx": tx_lots,
            "mtx_residual_beta": (beta_exposure - mtx_lots * index_level * 50) / nav,
            "tx_residual_beta": (beta_exposure - tx_lots * index_level * 200) / nav,
            "est_slip_cost": hedge_notional * params.futures_slip
        }
        res.update({
            "legs": [], "net_premium_points": 0.0,
            "total_cost": res["lots_info"]["est_slip_cost"],
            "budget_ok": True, "affordable_lots": mtx_lots
        })
        if not alert:
            res["notes"].append("目前未觸發避險，建議空手。")
        else:
            res["notes"].append(f"建議放空小台指 {mtx_lots} 口或大台指 {tx_lots} 口。")
        return res
        
    lots = round(hedge_notional / (index_level * 50))
    K1 = nearest_otm_strike(index_level, params.otm_pct, strike_step)
    T = params.tenor_days / 365.0
    v = float(vol20 if vol20 else 0.2) + params.skew_vol
    
    legs = []
    def add_leg(K, is_long):
        model_p = bs_put(index_level, K, T, v, params.rf)
        q = None
        if quote_fn is not None:
            try:
                q = quote_fn(K)
                q = float(q) if q is not None else None
            except Exception:
                q = None
        if q is not None and (not math.isfinite(q) or q <= 0):
            q = None
        used = q if q is not None else model_p
        legs.append({
            "strike": K,
            "is_long": is_long,
            "model_premium": model_p,
            "quote": q,
            "used_premium": used,
            "source": "報價" if q is not None else "BS 估計"
        })
        
    add_leg(K1, True)
    if tool == "spread":
        K2 = nearest_otm_strike(index_level, params.otm_pct + params.spread_width_pct, strike_step)
        add_leg(K2, False)
        
    net_points = legs[0]["used_premium"]
    if tool == "spread":
        net_points -= legs[1]["used_premium"]
        
    total_cost = net_points * 50 * lots
    budget_ok = total_cost <= available
    affordable_lots = math.floor(available / (net_points * 50)) if net_points > 0 else lots
    
    res["lots_info"] = {"lots": lots}
    res["legs"] = legs
    res["net_premium_points"] = net_points
    res["total_cost"] = total_cost
    res["budget_ok"] = budget_ok
    res["affordable_lots"] = affordable_lots
    
    if not budget_ok:
        res["notes"].append(f"預算不足！需要 {total_cost:,.0f}，但可用僅 {available:,.0f}。")
        res["notes"].append(f"在預算內最多可買 {affordable_lots} 口。")
    else:
        res["notes"].append(f"預算充足，可買 {lots} 口。")
        
    return res

def format_quote(value) -> str:
    try:
        value = None if value is None else float(value)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return "無報價"
    return f"{value:,.1f}"
