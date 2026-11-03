"""下單洞察與分析模組 (order_insights.py)

提供下單前後的帳戶選擇、Beta 拆解、相對強度 (RS Rating)、
追繳點位試算、選擇權避險建議與風控優先順序清單。

RS Rating 計算說明：
Raw Score = 0.4 * R3 + 0.2 * R6 + 0.2 * R9 + 0.2 * R12
其中 Rn 代表近 n 個月（以 63/126/189/252 個交易日估算）的報酬率 (close_now / close_n_days_ago - 1)。
若歷史資料介於 63 至 251 個交易日，則依可用的期間重新歸一化權重計算 Raw Score；若少於 63 個交易日則回傳 None。
Rating 為該標的 Raw Score 在對照宇宙（Universe，不含標的自身）中的百分位數 (Percentile Rank)，
計算公式為：
  pct = (strictly_less_count + 0.5 * equal_count) / n_universe
  rating = int(min(99, max(1, round(pct * 99))))
範圍映射至整數 1 至 99。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from order_assistant import (
    TXO_MULTIPLIER,
    Rules,
    Snapshot,
    bs_price_greeks,
    loss_breaches,
    positions_as_dicts,
    taiex_shock_state,
)

# ---------------------------------------------------------------------------
# 1. 帳戶管理與選擇
# ---------------------------------------------------------------------------

ACCOUNT_CHOICES = ["第一證券", "IB", "嘉信理財", "投資先生", "口袋證券", "元大期貨"]


def account_options(accounts: pd.DataFrame) -> list[str]:
    """回傳帳戶選單列表。

    優先列出預設的 ACCOUNT_CHOICES，若傳入的 accounts 資料表中含有其他帳戶名稱，
    則依序去重後附加於後方。
    """
    options = list(ACCOUNT_CHOICES)
    if accounts is not None and not accounts.empty and "帳戶名稱" in accounts.columns:
        for name in accounts["帳戶名稱"].dropna():
            s_name = str(name).strip()
            if s_name and s_name not in options:
                options.append(s_name)
    return options


def default_account(snapshot: Snapshot, kind: str, held_account: str | None) -> str:
    """根據商品種類與當前持倉推薦預設帳戶。

    規則：
    1. 若已持有且 `held_account` 非空，優先使用 `held_account`。
    2. 期貨/選擇權商品（種類包含「期」或「選」）→ 「元大期貨」。
    3. 美股商品（種類包含「美」）→ 優先搜尋投組中含 IB 或 嘉信 的帳戶，若無則回傳 「IB」。
    4. 其餘（台股/現貨）→ 優先使用 第一證券 / 口袋證券 / 投資先生 中已存在的帳戶，否則回傳 「第一證券」。
    """
    if held_account and str(held_account).strip():
        return str(held_account).strip()

    k = str(kind)
    if "期" in k or "選" in k:
        return "元大期貨"

    existing_names: list[str] = []
    if snapshot is not None:
        acc_df = getattr(snapshot, "accounts_valued", None)
        if acc_df is None or acc_df.empty:
            acc_df = getattr(snapshot, "accounts", None)
        if acc_df is not None and not acc_df.empty and "帳戶名稱" in acc_df.columns:
            existing_names = [str(n).strip() for n in acc_df["帳戶名稱"].dropna()]

    if "美" in k:
        for name in existing_names:
            if "IB" in name.upper() or "嘉信" in name:
                return name
        return "IB"

    for pref in ("第一證券", "口袋證券", "投資先生"):
        if pref in existing_names:
            return pref
    return "第一證券"


def account_kind_warnings(account: str, kind: str) -> list[str]:
    """檢查帳戶與商品種類的搭配，回傳提示或警語。"""
    warnings: list[str] = []
    acc = str(account).strip()
    k = str(kind)
    is_fut_opt = ("期" in k) or ("選" in k)

    if is_fut_opt and "元大期貨" not in acc:
        warnings.append(f"期貨或選擇權下單帳戶建議為元大期貨（目前選擇：{acc}）")
    elif not is_fut_opt and "元大期貨" in acc:
        warnings.append(f"現貨股票/ETF下單帳戶不建議為元大期貨（目前選擇：{acc}）")

    if "美" in k and acc in ("第一證券", "口袋證券", "投資先生"):
        warnings.append(f"美股商品於「{acc}」下單將以複委託方式執行")

    return warnings


def _clean_float(val: Any, default: float = 0.0) -> float:
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def account_view(snapshot: Snapshot, name: str) -> dict:
    """查詢特定帳戶的當前概況。"""
    target_name = str(name).strip()
    acc_df = getattr(snapshot, "accounts_valued", None)
    if acc_df is not None and not acc_df.empty and "帳戶名稱" in acc_df.columns:
        mask = acc_df["帳戶名稱"].astype(str).str.strip() == target_name
        matched = acc_df[mask]
        if not matched.empty:
            row = matched.iloc[0]
            cash = _clean_float(row.get("現金餘額", 0.0))
            debt = _clean_float(row.get("融資/質押借款", 0.0))
            mv = _clean_float(row.get("即時持股市值", 0.0))
            nav = _clean_float(row.get("帳戶淨值", 0.0))
            exp = _clean_float(row.get("名目曝險", 0.0))
            lev = (exp / nav) if nav > 0 else 0.0
            avail_raw = row.get("可動用保證金") if "可動用保證金" in row else None
            avail = None if avail_raw is None or pd.isna(avail_raw) else _clean_float(avail_raw)
            return {
                "name": target_name,
                "exists": True,
                "cash": cash,
                "debt": debt,
                "market_value": mv,
                "nav": nav,
                "exposure": exp,
                "leverage": lev,
                "available_margin": avail,
            }

    return {
        "name": target_name,
        "exists": False,
        "cash": 0.0,
        "debt": 0.0,
        "market_value": 0.0,
        "nav": 0.0,
        "exposure": 0.0,
        "leverage": 0.0,
        "available_margin": None,
    }


# ---------------------------------------------------------------------------
# 2. Beta 拆解與槓桿說明
# ---------------------------------------------------------------------------


def beta_split_change(before_metrics: dict, after_metrics: dict) -> list[dict]:
    """計算下單前後台股、美股與總體的 Beta 及曝險變化。"""
    tw_b_before = float(before_metrics.get("tw_beta", 0.0))
    tw_b_after = float(after_metrics.get("tw_beta", 0.0))
    us_b_before = float(before_metrics.get("us_beta", 0.0))
    us_b_after = float(after_metrics.get("us_beta", 0.0))

    tot_b_before = float(before_metrics.get("total_beta", before_metrics.get("beta", 0.0)))
    tot_b_after = float(after_metrics.get("total_beta", after_metrics.get("beta", 0.0)))

    tw_exp_before = float(before_metrics.get("tw_exposure", 0.0))
    tw_exp_after = float(after_metrics.get("tw_exposure", 0.0))
    us_exp_before = float(before_metrics.get("us_exposure", 0.0))
    us_exp_after = float(after_metrics.get("us_exposure", 0.0))

    tot_exp_before = tw_exp_before + us_exp_before
    tot_exp_after = tw_exp_after + us_exp_after

    return [
        {
            "label": "台股",
            "before": tw_b_before,
            "after": tw_b_after,
            "delta": tw_b_after - tw_b_before,
            "exposure_before": tw_exp_before,
            "exposure_after": tw_exp_after,
        },
        {
            "label": "美股",
            "before": us_b_before,
            "after": us_b_after,
            "delta": us_b_after - us_b_before,
            "exposure_before": us_exp_before,
            "exposure_after": us_exp_after,
        },
        {
            "label": "總",
            "before": tot_b_before,
            "after": tot_b_after,
            "delta": tot_b_after - tot_b_before,
            "exposure_before": tot_exp_before,
            "exposure_after": tot_exp_after,
        },
    ]


def leveraged_beta_note(lev: float, beta: float) -> str | None:
    """槓桿 ETF (lev > 1) 設定提醒，說明系統採用槓桿倍數計算曝險。"""
    if lev > 1.0 and beta > 1.0 + 1e-9 and abs(beta - lev) > 0.05:
        return (
            f"標的槓桿為 {lev:g}x，系統計算曝險時僅採計槓桿倍數，"
            f"忽略手動輸入的 Beta 值 {beta:g}（避免槓桿與 Beta 重複計算敏感度）。"
        )
    return None


# ---------------------------------------------------------------------------
# 3. RS Rating (IBD 風格相對強度)
# ---------------------------------------------------------------------------

TW_RS_UNIVERSE = (
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW", "2881.TW", "2882.TW",
    "2891.TW", "3711.TW", "2412.TW", "3008.TW", "2303.TW", "2886.TW", "2884.TW",
    "2885.TW", "5880.TW", "2002.TW", "2892.TW", "1301.TW", "1303.TW", "2880.TW",
    "2357.TW", "3231.TW", "2379.TW", "3034.TW", "2345.TW", "6669.TW", "2327.TW",
    "2883.TW", "5876.TW", "2890.TW", "2887.TW", "1216.TW", "1101.TW", "2395.TW",
    "3045.TW", "2408.TW", "3037.TW", "2301.TW", "6505.TW", "4938.TW", "2801.TW",
    "1402.TW", "2603.TW", "2609.TW", "2615.TW", "2356.TW", "4904.TW", "2207.TW",
    "9910.TW",
)

US_RS_UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B",
    "JPM", "UNH", "V", "XOM", "MA", "HD", "PG", "COST", "JNJ", "ABBV", "BAC",
    "CRM", "WMT", "CVX", "MRK", "NFLX", "AMD", "KO", "PEP", "ORCL", "LLY",
    "MCD", "CSCO", "ACN", "ABT", "DHR", "INTC", "TXN", "QCOM", "AMAT", "GE",
    "PFE", "PM", "CAT", "DIS", "IBM", "UBER", "NOW", "AMGN", "GS", "ISRG",
    "LOW", "BKNG", "SPGI", "RTX", "AXP", "HON", "T", "MS", "PGR", "SYK",
    "NKE", "VRTX", "BLK", "ETN", "LRCX", "REGN", "ADP", "MMC", "C", "SLB",
    "MDLZ", "CI", "MU", "FI", "CB", "PLTR", "PANW", "DE", "EOG", "ADI",
    "BDX", "BSX", "KLAC", "SCHW", "SNPS", "TJX", "WM", "CDNS", "HCA", "SBUX",
    "SHW", "ITW", "MDT", "ORLY", "TGT", "CSX", "MCK",
)

RS_BENCHMARK_LABEL = {
    "TW": "台股 50 大權值股（0050 成分股代理，對照加權指數）",
    "US": "S&P 500 前百大市值股（S&P 500 代理）",
}

RS_WARN_THRESHOLD = 70


def default_put_delta(
    spot: float, otm_pct: float = 0.05, days: int = 30, iv: float = 0.20, rate: float = 0.015
) -> float:
    """計算指定 OTM 比例之 TXO Put Delta（負值）。若 spot <= 0 則回傳 0.0。"""
    if spot <= 0:
        return 0.0
    strike = round(spot * (1.0 - otm_pct) / 50.0) * 50.0
    _price, delta, _theta = bs_price_greeks(spot, strike, days / 365.0, rate, iv, "put")
    return float(delta)


def rs_raw_score(close: pd.Series) -> float | None:
    """計算單一標的 Raw RS Score (0.4*R3 + 0.2*R6 + 0.2*R9 + 0.2*R12)。"""
    if close is None:
        return None
    s = close.dropna()
    if s.empty:
        return None
    s = s.sort_index()
    n = len(s)
    if n < 64:
        return None

    close_now = float(s.iloc[-1])
    if close_now <= 0:
        return None

    periods = [(64, 0.4), (127, 0.2), (190, 0.2), (253, 0.2)]
    valid_weights = []
    returns = []
    for idx_offset, w in periods:
        if n >= idx_offset:
            prev_p = float(s.iloc[-idx_offset])
            if prev_p > 0:
                r = close_now / prev_p - 1.0
                returns.append(r)
                valid_weights.append(w)

    if not returns:
        return None

    sum_w = sum(valid_weights)
    raw = sum(r * (w / sum_w) for r, w in zip(returns, valid_weights))
    return float(raw)


def rs_rating_from_raws(target_raw: float | None, universe_raws: list[float]) -> int | None:
    """將 target_raw 對照 universe_raws 計算百分位評分 (1..99)。"""
    if target_raw is None:
        return None

    valid_univ = [
        float(x)
        for x in universe_raws
        if x is not None and not (isinstance(x, float) and math.isnan(x))
    ]

    if target_raw in valid_univ:
        valid_univ.remove(target_raw)

    n_universe = len(valid_univ)
    if n_universe < 10:
        return None

    less_cnt = sum(1 for x in valid_univ if x < target_raw)
    eq_cnt = sum(1 for x in valid_univ if x == target_raw)
    pct = (less_cnt + 0.5 * eq_cnt) / n_universe
    rating = int(min(99, max(1, round(pct * 99))))
    return rating


def rs_symbol(ticker: str, kind: str) -> str:
    """將標的代碼轉換為 Yahoo Finance 對應代碼。期貨/選擇權回傳空字串。"""
    k = str(kind)
    t = str(ticker).strip().upper()
    if "期" in k or "選" in k or t in ("TX", "MTX", "FMF", "TMF"):
        return ""
    if "美" in k:
        return t
    if not t:
        return ""
    if t.endswith(".TW") or t.endswith(".TWO"):
        return t
    if t.isalnum():
        return f"{t}.TW"
    return t


def rs_universe_for(kind: str) -> tuple:
    """回傳商品對應的對照宇宙代碼清單 (美股 / 台股)。"""
    if "美" in str(kind):
        return US_RS_UNIVERSE
    return TW_RS_UNIVERSE


def compute_rs(
    target_close: pd.Series, universe_closes: dict[str, pd.Series], target_symbol: str | None = None
) -> dict:
    """計算標的 RS Rating、Raw Score、Universe 規模與各期報酬率。"""
    target_raw = rs_raw_score(target_close)
    target_sym_clean = target_symbol.strip().upper() if target_symbol else None
    univ_raws = []
    for k, s in universe_closes.items():
        if target_sym_clean and str(k).strip().upper() == target_sym_clean:
            continue
        r = rs_raw_score(s)
        if r is not None:
            univ_raws.append(r)

    rating = rs_rating_from_raws(target_raw, univ_raws)

    r3, r6, r9, r12 = None, None, None, None
    if target_close is not None:
        s = target_close.dropna()
        if not s.empty:
            s = s.sort_index()
            n = len(s)
            close_now = float(s.iloc[-1])
            if close_now > 0:
                if n >= 64 and float(s.iloc[-64]) > 0:
                    r3 = float(close_now / s.iloc[-64] - 1.0)
                if n >= 127 and float(s.iloc[-127]) > 0:
                    r6 = float(close_now / s.iloc[-127] - 1.0)
                if n >= 190 and float(s.iloc[-190]) > 0:
                    r9 = float(close_now / s.iloc[-190] - 1.0)
                if n >= 253 and float(s.iloc[-253]) > 0:
                    r12 = float(close_now / s.iloc[-253] - 1.0)

    return {
        "rating": rating,
        "raw": target_raw,
        "universe_size": len(univ_raws),
        "returns": {"3m": r3, "6m": r6, "9m": r9, "12m": r12},
    }


def rs_for_ticker(ticker: str, kind: str, fetch=None) -> dict:
    """針對指定標的計算 RS Rating 及相關資料。"""
    benchmark = RS_BENCHMARK_LABEL["US" if "美" in str(kind) else "TW"]
    symbol = rs_symbol(ticker, kind)
    if not symbol:
        return {
            "symbol": "",
            "rating": None,
            "raw": None,
            "universe_size": 0,
            "returns": {"3m": None, "6m": None, "9m": None, "12m": None},
            "benchmark": "",
            "error": "期貨／選擇權不計算 RS",
        }

    fetch_fn = fetch if fetch is not None else fetch_close_history
    universe = rs_universe_for(kind)
    req_symbols = list(dict.fromkeys([symbol] + list(universe)))
    closes = fetch_fn(req_symbols) or {}

    target_series = closes.get(symbol)
    if (target_series is None or target_series.dropna().empty) and symbol.endswith(".TW"):
        alt_symbol = symbol[:-3] + ".TWO"
        closes_alt = fetch_fn([alt_symbol]) or {}
        if alt_symbol in closes_alt and not closes_alt[alt_symbol].dropna().empty:
            symbol = alt_symbol
            target_series = closes_alt[alt_symbol]
            closes[alt_symbol] = target_series

    if target_series is None or target_series.dropna().empty:
        return {
            "symbol": symbol,
            "rating": None,
            "raw": None,
            "universe_size": 0,
            "returns": {"3m": None, "6m": None, "9m": None, "12m": None},
            "benchmark": benchmark,
            "error": f"抓不到 {symbol} 的歷史股價",
        }

    res = compute_rs(target_series, closes, target_symbol=symbol)
    err = None
    if res.get("rating") is None:
        err = "對照股價不足，無法排名"

    return {
        "symbol": symbol,
        "rating": res["rating"],
        "raw": res["raw"],
        "universe_size": res["universe_size"],
        "returns": res["returns"],
        "benchmark": benchmark,
        "error": err,
    }


def fetch_close_history(symbols: list[str], period: str = "13mo") -> dict[str, pd.Series]:
    """使用 yfinance 批量抓取收盤價歷史紀錄（內部延遲載入 yfinance）。"""
    try:
        import yfinance as yf

        clean_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not clean_symbols:
            return {}

        data = yf.download(
            clean_symbols,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="column",
        )
        if data is None or data.empty:
            return {}

        out: dict[str, pd.Series] = {}
        if len(clean_symbols) == 1:
            sym = clean_symbols[0]
            if "Close" in data.columns:
                out[sym] = data["Close"].dropna()
            elif isinstance(data, pd.Series):
                out[sym] = data.dropna()
        else:
            if "Close" in data.columns:
                close_df = data["Close"]
                for sym in clean_symbols:
                    if sym in close_df.columns:
                        out[sym] = close_df[sym].dropna()
        return out
    except Exception:
        return {}


def rs_warning(rating: int | float | None) -> str | None:
    """若 RS Rating 存在且低於門檻 (70)，回傳提醒訊息。"""
    if rating is not None and rating < RS_WARN_THRESHOLD:
        return f"相對強度 (RS Rating) 為 {int(rating)}，低於參考門檻 {RS_WARN_THRESHOLD}，屬於弱勢標的。"
    return None


# ---------------------------------------------------------------------------
# 4. 追繳點位與融資維持率
# ---------------------------------------------------------------------------

MARGIN_MAINTENANCE_RATIO = 1.30


def futures_margin_call_level(
    snapshot: Snapshot, max_move: float = 0.6, tol: float = 1e-4
) -> dict | None:
    """計算臺指期觸發追繳的大盤指數點位與變動百分比。

    若無期貨維持保證金需求，回傳 None。
    """
    shock0 = taiex_shock_state(snapshot, 0.0)
    if float(shock0.get("futures_maint", 0.0)) <= 0:
        return None

    twii = float(snapshot.twii) if snapshot.twii else 0.0
    if shock0.get("margin_call", False):
        return {
            "already": True,
            "index_level": twii,
            "move_pct": 0.0,
            "points": 0.0,
            "direction": "已追繳",
        }

    def _find_boundary(low_m: float, high_m: float) -> float:
        for _ in range(50):
            mid = (low_m + high_m) / 2.0
            if taiex_shock_state(snapshot, mid).get("margin_call", False):
                high_m = mid
            else:
                low_m = mid
            if abs(high_m - low_m) < tol:
                break
        return high_m

    move_down = None
    if taiex_shock_state(snapshot, -max_move).get("margin_call", False):
        move_down = _find_boundary(0.0, -max_move)

    move_up = None
    if taiex_shock_state(snapshot, max_move).get("margin_call", False):
        move_up = _find_boundary(0.0, max_move)

    if move_down is None and move_up is None:
        return {
            "already": False,
            "index_level": None,
            "move_pct": None,
            "points": None,
            "direction": f"±{int(max_move * 100)}% 內不會追繳",
        }

    if move_down is not None and move_up is None:
        chosen = move_down
        direction = "下跌"
    elif move_up is not None and move_down is None:
        chosen = move_up
        direction = "上漲"
    else:
        if abs(move_down) <= abs(move_up):
            chosen = move_down
            direction = "下跌"
        else:
            chosen = move_up
            direction = "上漲"

    idx_lvl = twii * (1.0 + chosen) if twii else 0.0
    pts = twii * chosen if twii else 0.0

    return {
        "already": False,
        "index_level": idx_lvl,
        "move_pct": chosen * 100.0,
        "points": pts,
        "direction": direction,
    }


def margin_account_call_levels(
    snapshot: Snapshot, maintenance_ratio: float = 1.30
) -> list[dict]:
    """計算非期貨帳戶（融資/質押）觸發整戶維持率追繳的跌幅幅度。"""
    acc_df = getattr(snapshot, "accounts_valued", None)
    if acc_df is None or acc_df.empty:
        return []

    results = []
    for _, row in acc_df.iterrows():
        acc_name = str(row.get("帳戶名稱", "")).strip()
        if "元大期貨" in acc_name:
            continue
        debt = float(row.get("融資/質押借款", 0.0))
        if math.isnan(debt):
            debt = 0.0
        mv = float(row.get("即時持股市值", 0.0))
        if math.isnan(mv):
            mv = 0.0
        if debt > 0 and mv > 0:
            ratio_now = mv / debt
            maint_now_pct = ratio_now * 100.0
            trigger_drop_pct = (1.0 - maintenance_ratio * debt / mv) * 100.0
            already_call = bool(ratio_now < maintenance_ratio)
            results.append(
                {
                    "account": acc_name,
                    "market_value": mv,
                    "debt": debt,
                    "maintenance_now_pct": maint_now_pct,
                    "trigger_drop_pct": trigger_drop_pct,
                    "already_call": already_call,
                }
            )

    return results


# ---------------------------------------------------------------------------
# 5. 選擇權避險試算 (TXO Put)
# ---------------------------------------------------------------------------


def hedge_suggestion(
    snapshot: Snapshot, put_delta: float | None = None, target_ratio: float = 1.0
) -> dict:
    """計算台指選擇權 Put 避險所需的建議口數。"""
    tw_long = 0.0
    tw_hedge = 0.0

    val_df = getattr(snapshot, "valued", None)
    if val_df is not None and not val_df.empty:
        for _, row in val_df.iterrows():
            kind = str(row.get("種類", ""))
            if "美" not in kind:
                b_exp = float(row.get("Beta曝險(台幣)", 0.0))
                if b_exp > 0:
                    tw_long += b_exp
                elif b_exp < 0:
                    tw_hedge += -b_exp

    net_exp = float(snapshot.metrics.get("tw_exposure", 0.0))
    current_hedge_ratio = (tw_hedge / tw_long) if tw_long > 0 else 0.0
    index = float(snapshot.twii) if snapshot.twii else 0.0

    lots_full = (
        max(net_exp, 0.0) * float(target_ratio) / (index * TXO_MULTIPLIER)
        if index > 0
        else 0.0
    )

    lots_delta = None
    if put_delta is not None and 0 < abs(float(put_delta)) <= 1.0:
        lots_delta = lots_full / abs(float(put_delta))

    suggested_raw = lots_delta if lots_delta is not None else lots_full
    suggested_lots = int(math.ceil(suggested_raw))

    return {
        "tw_long": tw_long,
        "tw_hedge": tw_hedge,
        "net_exposure": net_exp,
        "current_hedge_ratio": current_hedge_ratio,
        "index": index,
        "lots_full": lots_full,
        "lots_delta": lots_delta,
        "suggested_lots": suggested_lots,
        "put_delta": put_delta,
        "formula": "口數 = 台股淨曝險 ÷ (台指 × 50)，再除以 |Delta|",
    }


# ---------------------------------------------------------------------------
# 6. 風控優先順序與處置清單
# ---------------------------------------------------------------------------


def triggered_stop_losses(snapshot: Snapshot) -> list[dict]:
    """篩選已觸發個別停損門檻的部位。"""
    triggered = []
    positions = positions_as_dicts(getattr(snapshot, "valued", None))
    for pos in positions:
        m_loss = pos["max_loss_pct"]
        if -100.0 < m_loss < 0.0 and pos["roi"] <= m_loss:
            triggered.append(
                {
                    "ticker": pos["ticker"],
                    "name": pos["name"],
                    "account": pos["account"],
                    "roi": pos["roi"],
                    "max_loss_pct": m_loss,
                    "mv_twd": pos["mv_twd"],
                    "pnl_twd": pos["pnl_twd"],
                }
            )
    return triggered


def priority_list(
    snapshot: Snapshot, rules: Rules, after: Snapshot | None = None
) -> list[dict]:
    """生成投組風控處置的優先順序清單 (0: 追繳風險, 1: 觸發停損, 2: 超過虧損上限)。"""
    items: list[dict] = []
    target_snap = after if after is not None else snapshot

    # 優先級 0: 追繳風險 (期貨與融資)
    if target_snap.metrics.get("margin_call", False):
        detail = (
            "下單後會追繳"
            if (after is not None and after.metrics.get("margin_call", False))
            else "目前已追繳"
        )
        val = abs(float(target_snap.metrics.get("margin_call_distance", 0.0)))
        items.append(
            {
                "priority": 0,
                "category": "追繳風險",
                "ticker": "期貨保證金",
                "account": "元大期貨",
                "detail": detail,
                "value": val,
            }
        )
    elif after is not None and not snapshot.metrics.get("margin_call", False):
        shock = taiex_shock_state(after, -0.10)
        if shock.get("margin_call", False):
            val = abs(float(after.metrics.get("margin_call_distance", 0.0)))
            items.append(
                {
                    "priority": 0,
                    "category": "追繳風險",
                    "ticker": "期貨保證金",
                    "account": "元大期貨",
                    "detail": "台指 -10% 會追繳",
                    "value": val,
                }
            )

    for margin_acc in margin_account_call_levels(target_snap):
        if margin_acc["already_call"] or margin_acc["trigger_drop_pct"] < 10.0:
            det = (
                f"融資整戶維持率 {margin_acc['maintenance_now_pct']:.1f}%"
                + (
                    "（已遭追繳）"
                    if margin_acc["already_call"]
                    else f"（跌幅 {margin_acc['trigger_drop_pct']:.1f}% 觸發追繳）"
                )
            )
            items.append(
                {
                    "priority": 0,
                    "category": "融資追繳風險",
                    "ticker": "現貨融資",
                    "account": margin_acc["account"],
                    "detail": det,
                    "value": margin_acc["debt"],
                }
            )

    # 優先級 1: 觸發停損
    stop_rows = triggered_stop_losses(snapshot)
    cat1_tickers = {r["ticker"] for r in stop_rows}
    for r in stop_rows:
        det = f"報酬率 {r['roi']:.1f}% 已低於停損門檻 {r['max_loss_pct']:.1f}%"
        items.append(
            {
                "priority": 1,
                "category": "觸發停損",
                "ticker": r["ticker"],
                "account": r["account"],
                "detail": det,
                "value": r["mv_twd"],
            }
        )

    # 優先級 2: 超過虧損上限
    breaches = loss_breaches(snapshot, rules)
    account_of = {pos["ticker"]: pos["account"] for pos in positions_as_dicts(getattr(snapshot, "valued", None))}
    for breach in breaches:
        if breach["ticker"] not in cat1_tickers:
            items.append(
                {
                    "priority": 2,
                    "category": "超過虧損上限",
                    "ticker": breach["ticker"],
                    "account": breach.get("account") or account_of.get(breach["ticker"], ""),
                    "detail": breach["reason"],
                    "value": breach["trim_value"],
                }
            )

    items.sort(key=lambda x: (x["priority"], -abs(x["value"])))
    return items
