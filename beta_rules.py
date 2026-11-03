"""槓桿 ETF 與一般持倉的有效 Beta：全系統（第四頁、第六頁、唯讀 API、美股避險）共用同一條規則。

槓桿 ETF = 槓桿 × 標的一年 Beta（美股對 S&P 500；台股槓桿 ETF 追蹤加權指數，定義為 1）。
抓不到即時 Beta 時用 FALLBACK_UNDERLYING_BETA 的預設值，並在來源欄標示「預設值」。
"""
import math

LEVERAGED_UNDERLYING = {
    "SOXL": ("SOXX", 3.0),
    "AMUU": ("AMD", 2.0),
    "MUU": ("MU", 2.0),
    "SNXX": ("SNDK", 2.0),
    "LITX": ("LITE", 2.0),
    "KORU": ("EWY", 3.0),
    "TQQQ": ("QQQ", 3.0),
    "QLD": ("QQQ", 2.0),
    "UPRO": ("SPY", 3.0),
    "SSO": ("SPY", 2.0),
    "TECL": ("XLK", 3.0),
    "NVDL": ("NVDA", 2.0),
    "USD": ("SMH", 2.0),
}

FALLBACK_UNDERLYING_BETA = {
    "SOXX": 1.6,
    "AMD": 1.9,
    "MU": 1.8,
    "SNDK": 2.0,
    "LITE": 1.8,
    "EWY": 1.1,
    "QQQ": 1.15,
    "SPY": 1.0,
    "XLK": 1.25,
    "NVDA": 1.9,
    "SMH": 1.6,
}

TW_INDEX_UNDERLYING = "^TWII"

SOURCE_LIVE = "一年回歸"
SOURCE_FALLBACK = "預設值"
SOURCE_DEFINITION = "定義"


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def underlying_of(ticker: str, kind: str, lev: float | None = None) -> tuple[str | None, float | None]:
    t_upper = str(ticker).upper()
    if t_upper in LEVERAGED_UNDERLYING:
        return LEVERAGED_UNDERLYING[t_upper]
    elif "美" not in str(kind) and lev is not None and lev > 1:
        return (TW_INDEX_UNDERLYING, None)
    return (None, None)


def fetch_underlying_beta(ticker: str, beta_fn=None) -> dict:
    t_upper = str(ticker).upper()
    if t_upper not in LEVERAGED_UNDERLYING:
        return {}
    u = LEVERAGED_UNDERLYING[t_upper][0]
    if beta_fn is None:
        return {}
    
    ok = False
    beta = math.nan
    if hasattr(beta_fn, "info"):
        try:
            b, s, ok_flag = beta_fn.info(u, False)
            beta = _num(b, math.nan)
            ok = ok_flag and math.isfinite(beta) and beta > 0
        except Exception:
            ok = False
    else:
        try:
            b, s = beta_fn(u, False)
            beta = _num(b, math.nan)
            ok = math.isfinite(beta) and beta > 0
        except Exception:
            ok = False
            
    if ok:
        return {
            "標的代號": u,
            "標的Beta": beta,
            "標的Beta來源": f"{u} {SOURCE_LIVE}（對 S&P 500）"
        }
    else:
        return {
            "標的代號": u,
            "標的Beta": math.nan,
            "標的Beta來源": ""
        }


def fallback_underlying_beta(underlying: str | None, overrides: dict | None = None) -> tuple[float, str]:
    overrides = overrides or {}
    u = str(underlying).upper() if underlying else ""
    
    manual = _num(overrides.get(u), None)
    if manual is not None and manual > 0:
        return manual, f"{u} {SOURCE_FALLBACK}（手動）"
        
    if u == TW_INDEX_UNDERLYING:
        return 1.0, f"加權指數 {SOURCE_DEFINITION} = 1"
        
    if u in FALLBACK_UNDERLYING_BETA:
        return FALLBACK_UNDERLYING_BETA[u], f"{u} {SOURCE_FALLBACK}"
        
    return 1.0, f"{u or '未知標的'} {SOURCE_FALLBACK} 1.0"


def effective_beta(ticker, kind, lev, own_beta, underlying_beta=None, underlying_source="", own_beta_source="", overrides=None) -> dict:
    lev_val = _num(lev, 1.0)
    own_val = _num(own_beta, None)
    
    u, map_lev = underlying_of(ticker, kind, lev_val)
    if u is not None and lev_val <= 1 and map_lev:
        lev_val = map_lev
        
    if u is None or lev_val <= 1:
        sens = own_val if own_val is not None else 1.0
        source = str(own_beta_source) if own_beta_source else ("持倉 Beta" if own_val is not None else f"{SOURCE_FALLBACK} 1.0")
        is_fallback = (own_val is None) or (SOURCE_FALLBACK in source)
        return {
            "sens": sens,
            "lev": lev_val,
            "underlying": "",
            "underlying_beta": None,
            "source": source,
            "is_fallback": is_fallback
        }
        
    ub = _num(underlying_beta, None)
    if ub is not None and ub > 0:
        src = str(underlying_source) if underlying_source else f"{u} {SOURCE_LIVE}"
        fb = False
    else:
        ub, src = fallback_underlying_beta(u, overrides)
        fb = SOURCE_FALLBACK in src
        
    sens = lev_val * ub
    source = f"槓桿 {lev_val:g}x × {src}"
    
    return {
        "sens": sens,
        "lev": lev_val,
        "underlying": u,
        "underlying_beta": ub,
        "source": source,
        "is_fallback": fb
    }


def beta_source_label(kind, ok: bool, definition: bool = False) -> str:
    if definition:
        return f"{SOURCE_DEFINITION} 1.0"
    if ok:
        return f"{SOURCE_LIVE}（對{'S&P 500' if '美' in kind else '加權指數'}）"
    return f"{SOURCE_FALLBACK} 1.0（抓不到報價）"


BETA_FALLBACK_STATE_KEY = "beta_fallback_overrides"


def fallback_table_rows(overrides: dict | None = None) -> list[dict]:
    overrides = overrides or {}
    rows = []
    for u, default_val in FALLBACK_UNDERLYING_BETA.items():
        etfs = [etf for etf, (und, _) in LEVERAGED_UNDERLYING.items() if und == u]
        manual = _num(overrides.get(u), None)
        has_manual = manual is not None and manual > 0 and abs(manual - default_val) > 1e-9
        val = manual if (manual is not None and manual > 0) else default_val
        rows.append({
            "標的": u,
            "預設 Beta": float(val),
            "對應槓桿 ETF": "、".join(etfs),
            "已手動修改": bool(has_manual),
        })
    return rows


def overrides_from_rows(rows) -> dict:
    result = {}
    if rows is None:
        return result
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    for r in rows:
        u = r.get("標的") if isinstance(r, dict) else None
        if not u:
            continue
        v = _num(r.get("預設 Beta"), None)
        if v is None or v <= 0:
            continue
        default_val = FALLBACK_UNDERLYING_BETA.get(u)
        if default_val is not None and abs(v - default_val) > 1e-9:
            result[str(u)] = float(v)
    return result
