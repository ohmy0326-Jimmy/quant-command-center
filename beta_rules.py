"""槓桿／反向 ETF 與一般持倉的有效 Beta。第四頁、第六頁、唯讀 API、美股避險共用。

已知商品用契約倍數，不使用 ETF 自己的報酬。每日重置 ETF 的自身回歸已經含槓桿，
再乘一次倍數會重複。倍數可以是負的（SOXS＝−3）。名目曝險用倍數的絕對值，
Beta 曝險用帶正負號的倍數 × 標的 Beta，只乘一次。

標的 Beta 抓不到、且不在預設表裡時，不假設 1。台股槓桿 ETF 追蹤加權指數，標的 Beta 定義為 1。
選擇權若標的是這類 ETF，沿用同一組契約倍數，不另估選擇權價格自己的 Beta。
"""
import math
import re

LEVERAGED_UNDERLYING = {
    "SOXL": ("SOXX", 3.0),
    "SOXS": ("SOXX", -3.0),
    "AMUU": ("AMD", 2.0),
    "MUU": ("MU", 2.0),
    "SNXX": ("SNDK", 2.0),
    "LITX": ("LITE", 2.0),
    "KORU": ("EWY", 3.0),
    "TQQQ": ("QQQ", 3.0),
    "SQQQ": ("QQQ", -3.0),
    "QLD": ("QQQ", 2.0),
    "QID": ("QQQ", -2.0),
    "UPRO": ("SPY", 3.0),
    "SPXU": ("SPY", -3.0),
    "SPXS": ("SPY", -3.0),
    "SSO": ("SPY", 2.0),
    "SDS": ("SPY", -2.0),
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
SOURCE_MISSING = "資料不足，Beta 未計入"

_OCC_OPTION = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def occ_root(symbol: str) -> str | None:
    """美股 OCC 代號的標的根代碼。MUU261030C00045000 → MUU。對不上回 None。"""
    match = _OCC_OPTION.match(str(symbol or "").strip().upper())
    return match.group(1) if match else None


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def underlying_of(ticker: str, kind: str, lev: float | None = None) -> tuple[str | None, float | None]:
    """已知槓桿／反向 ETF 回 (標的, 契約倍數)。倍數可以是負的。不在表內的非美股、|倍數|>1，視為追蹤加權。"""
    t_upper = str(ticker).upper()
    if t_upper in LEVERAGED_UNDERLYING:
        return LEVERAGED_UNDERLYING[t_upper]
    if "美" not in str(kind) and lev is not None and abs(lev) > 1:
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
        
    return None, f"{u or '未知標的'} {SOURCE_MISSING}"


def _beta_result(sens, gross, signed, underlying, underlying_beta, source, quality) -> dict:
    return {
        "sens": sens,
        "lev": gross,
        "signed_lev": signed,
        "underlying": underlying or "",
        "underlying_beta": underlying_beta,
        "source": source,
        "is_fallback": quality == "fallback",
        "quality": quality,
    }


def effective_beta(ticker, kind, lev, own_beta, underlying_beta=None, underlying_source="", own_beta_source="", overrides=None) -> dict:
    """有效 Beta。

    ``lev`` 是名目用的倍數絕對值。``signed_lev`` 是契約倍數（反向為負）。
    ``sens`` 是套在市值上的 Beta＝signed_lev × 標的 Beta；資料不足時是 None，不是 1。
    已知商品以對照表的契約倍數為準，試算表填 1 或填錯的正數都不會改寫它。
    """
    lev_val = _num(lev, 1.0)
    if lev_val is None:
        lev_val = 1.0
    own_val = _num(own_beta, None)
    u, map_lev = underlying_of(ticker, kind, lev_val)
    signed = float(map_lev) if map_lev is not None else float(lev_val)
    gross = abs(signed) if signed else 1.0

    if u is None:
        source = str(own_beta_source or "")
        if own_val is None or SOURCE_MISSING in source:
            return _beta_result(None, gross, signed, "", None, source or SOURCE_MISSING, "missing")
        quality = "fallback" if SOURCE_FALLBACK in source else "estimated"
        if not source:
            source = "持倉 Beta"
        return _beta_result(own_val, gross, signed, "", None, source, quality)

    ub = _num(underlying_beta, None)
    if ub is not None and ub != 0:
        src = str(underlying_source) if underlying_source else f"{u} {SOURCE_LIVE}"
        if SOURCE_DEFINITION in src:
            quality = "definition"
        elif SOURCE_FALLBACK in src:
            quality = "fallback"
        else:
            quality = "estimated"
    else:
        ub, src = fallback_underlying_beta(u, overrides)
        if ub is None:
            return _beta_result(None, gross, signed, u, None, src, "missing")
        quality = "definition" if SOURCE_DEFINITION in src else "fallback"

    return _beta_result(signed * ub, gross, signed, u, ub, f"槓桿 {signed:g}x × {src}", quality)


def beta_source_label(kind, ok: bool, definition: bool = False) -> str:
    if definition:
        return f"{SOURCE_DEFINITION} 1.0"
    if ok:
        return f"{SOURCE_LIVE}（對{'S&P 500' if '美' in str(kind) else '加權指數'}）"
    return SOURCE_MISSING


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
