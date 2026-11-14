"""把第四頁估價結果編成唯讀 JSON。

數字全部來自 ``finalize_valuation``，這裡不再算一遍槓桿或保證金。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from cb_holdings import product_kind

SCHEMA_VERSION = "1.10.0"
TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass
class LiveBook:
    payload: dict
    positions: pd.DataFrame
    metrics: dict


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def format_as_of(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TAIPEI)
    else:
        moment = moment.astimezone(TAIPEI)
    return moment.isoformat(timespec="seconds")


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _name(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _col(row, frame: pd.DataFrame, *names: str) -> float | None:
    for name in names:
        if name in frame.columns:
            value = _finite(row[name])
            if value is not None:
                return value
    return None


def position_records(positions: pd.DataFrame) -> list[dict]:
    """每列對應第四頁持倉表。市值與損益換算成台幣。"""
    if positions is None or positions.empty:
        return []
    rows = []
    for _, row in positions.iterrows():
        fx = _finite(row["換算匯率"])
        pnl_native = _finite(row["淨未實現損益(原幣)"])
        pnl = None if fx is None or pnl_native is None else pnl_native * fx
        rows.append(
            {
                "symbol": str(row["標的"]),
                "name": _name(row["名稱"]) if "名稱" in positions.columns else "",
                "qty": _finite(row["持有數量"]),
                "price": _finite(row["即時報價"]),
                "market_value": _finite(row["即時市值(台幣)"]),
                "pnl": pnl,
                "equity_value": _col(row, positions, "權益市值(台幣)", "即時市值(台幣)"),
                "leverage": _col(row, positions, "採用槓桿", "標的槓桿"),
                "notional_exposure": _col(row, positions, "實質曝險(台幣)"),
                "underlying": _name(row["標的代號"]) if "標的代號" in positions.columns else "",
                "underlying_beta": _col(row, positions, "採用標的Beta"),
                "beta": _col(row, positions, "曝險Beta"),
                "effective_beta": _col(row, positions, "有效Beta"),
                "beta_exposure": _col(row, positions, "Beta曝險(台幣)"),
                "beta_source": _name(row["Beta來源"]) if "Beta來源" in positions.columns else "",
                "beta_quality": _beta_quality(_name(row["Beta來源"]) if "Beta來源" in positions.columns else ""),
                "product": product_kind(row.get("種類")) or "",
                "delta": _col(row, positions, "Delta"),
                "delta_source": _name(row["Delta來源"]) if "Delta來源" in positions.columns else "",
                "conversion_value": _col(row, positions, "轉換價值(台幣)"),
                "conversion_value_points": _col(row, positions, "轉換價值(百元)"),
                "parity_premium_pct": _col(row, positions, "轉換溢價(%)"),
                "exposure_known": _exposure_known(row, positions),
                "delta_status": _delta_status(row, positions),
            }
        )
    return rows


def _beta_quality(source: str) -> str:
    text = source or ""
    if "未計入" in text or "缺漏" in text:
        return "missing"
    if "預設值" in text or "手動" in text:
        return "fallback"
    if "定義" in text:
        return "definition"
    if text:
        return "estimated"
    return "missing"


def _exposure_known(row, frame: pd.DataFrame) -> bool:
    if "曝險已知" not in frame.columns:
        return True
    flag = row["曝險已知"]
    if isinstance(flag, str):
        return flag.strip().lower() not in {"false", "0", "no"}
    try:
        if pd.isna(flag):
            return True
    except TypeError:
        pass
    return bool(flag)


def _delta_status(row, frame: pd.DataFrame) -> str:
    if "Delta狀態" not in frame.columns:
        return "not_applicable"
    text = _name(row["Delta狀態"])
    if text in {"known", "missing", "not_applicable"}:
        return text
    return "not_applicable"


def yuanta_from_metrics(metrics: dict) -> dict:
    """元大期貨權益與維持保證金，沿用第四頁寫進 metrics 的數字。"""
    ratio = metrics.get("margin_ratio")
    pts = metrics.get("margin_call_points")
    if pts is not None and pd.isna(pts):
        pts = None
    risk_ind = metrics.get("risk_indicator")
    return {
        "equity": float(metrics.get("futures_equity") or 0.0),
        "cash_ledger": float(metrics.get("futures_cash_ledger") or 0.0),
        "initial_margin": float(metrics.get("futures_initial") or 0.0),
        "maintenance_margin": float(metrics.get("futures_maint") or 0.0),
        "risk_indicator_pct": float(risk_ind) if risk_ind is not None else None,
        "margin_ratio_pct": None if ratio is None else float(ratio),
        "excess_margin": float(metrics.get("excess_margin") or 0.0),
        "margin_call_distance": float(metrics.get("margin_call_distance") or 0.0),
        "margin_call_points": float(pts) if pts is not None else None,
        "margin_call": bool(metrics.get("margin_call")),
    }


def beta_leverage_block(metrics: dict) -> dict:
    """1.3.0 新增：台股／美股／合計各三個數字，名目曝險口徑一致（tw + us = total）。"""

    def leg(prefix: str, nav_key: str, lev_key: str, avg_key: str, acct_key: str) -> dict:
        nav = float(metrics.get(nav_key, 0.0) or 0.0)
        if prefix and metrics.get("beta_basis") == "total_nav":
            nav = float(metrics.get("nav", 0.0) or 0.0)
        return {
            "notional_exposure": float(metrics.get(f"{prefix}notional_exposure", 0.0) or 0.0),
            "beta_exposure": float(metrics.get(f"{prefix}beta_exposure", 0.0) or 0.0),
            "nav": nav,
            "leverage": float(metrics.get(lev_key, 0.0) or 0.0),
            "avg_beta": float(metrics.get(avg_key, 0.0) or 0.0),
            "account_beta": float(metrics.get(acct_key, 0.0) or 0.0),
        }

    return {
        "tw": leg("tw_", "tw_nav", "tw_leverage", "tw_avg_beta", "tw_account_beta"),
        "us": leg("us_", "us_nav", "us_leverage", "us_avg_beta", "us_account_beta"),
        "total": leg("", "nav", "total_leverage", "avg_beta", "account_beta"),
    }


def portfolio_payload(
    positions: pd.DataFrame,
    metrics: dict,
    *,
    usdtwd: float,
    as_of: datetime,
    usdtwd_source: str = "live",
    warnings: list[str] | None = None,
    regime: str | None = None,
    credit_transmission: dict | None = None,
) -> dict:
    total_beta = float(metrics.get("total_beta", metrics.get("beta", 0.0)))
    complete = bool(metrics.get("exposure_complete", True))
    scope = str(metrics.get("exposure_scope") or ("complete" if complete else "partial"))
    if scope not in {"complete", "partial"}:
        scope = "complete" if complete else "partial"
    warning_lines = [str(item) for item in (warnings or []) if str(item)]
    if not complete:
        from portfolio_risk import exposure_warning_text

        text = exposure_warning_text(metrics)
        if text not in warning_lines:
            warning_lines.append(text)
    from portfolio_risk import beta_quality_warning

    quality = beta_quality_warning(metrics)
    if quality and quality not in warning_lines:
        warning_lines.append(quality)
    if credit_transmission is None:
        from credit_transmission import absent_board, api_view

        credit_transmission = api_view(absent_board())
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": format_as_of(as_of),
        "currency": "TWD",
        "usdtwd": float(usdtwd),
        "usdtwd_source": str(usdtwd_source),
        "warnings": warning_lines,
        "exposure_complete": complete,
        "exposure_scope": scope,
        "unknown_delta_symbols": [str(item) for item in (metrics.get("unknown_delta_symbols") or [])],
        "beta_complete": bool(metrics.get("beta_complete", True)),
        "unknown_beta_symbols": [str(item) for item in (metrics.get("unknown_beta_symbols") or [])],
        "sleeve_nav_complete": bool(metrics.get("sleeve_nav_complete", True)),
        "unallocated_nav": float(metrics.get("unallocated_nav", 0.0) or 0.0),
        "beta_basis": "total_nav" if metrics.get("beta_basis") == "total_nav" else "sleeve_nav",
        "beta_label": "本地基準敏感度合計。台股對加權指數，美股對 S&P 500，不是單一指數 Beta。",
        "net_value": float(metrics.get("nav", 0.0)),
        "exposure": float(metrics.get("exposure", 0.0)),
        "leverage": float(metrics.get("leverage", 0.0)),
        "cash_pct": float(metrics.get("cash_pct", 0.0)),
        "beta": total_beta,
        "tw_beta": float(metrics.get("tw_beta", 0.0)),
        "us_beta": float(metrics.get("us_beta", 0.0)),
        "total_beta": total_beta,
        "tw_nav": float(metrics.get("tw_nav", 0.0)),
        "us_nav": float(metrics.get("us_nav", 0.0)),
        "tw_exposure": float(metrics.get("tw_exposure", 0.0)),
        "us_exposure": float(metrics.get("us_exposure", 0.0)),
        "beta_leverage": beta_leverage_block(metrics),
        "greeks": greeks_block(positions, metrics),
        "beta_targets": beta_target_block(metrics, regime),
        "credit_transmission": credit_transmission,
        "positions": position_records(positions),
        "yuanta_futures": yuanta_from_metrics(metrics),
    }


def greeks_block(positions: pd.DataFrame, metrics: dict) -> dict:
    from portfolio_greeks import aggregate_greeks

    stored = metrics.get("portfolio_greeks")
    if isinstance(stored, dict) and "tw" in stored and "us" in stored:
        from portfolio_greeks import apply_indexes

        return apply_indexes(stored, metrics.get("tw_index"), metrics.get("us_index"))
    return aggregate_greeks(positions, metrics.get("tw_index"), metrics.get("us_index"))


def beta_target_block(metrics: dict, regime: str | None) -> dict:
    from portfolio_greeks import beta_targets

    return beta_targets(
        regime,
        metrics.get("tw_beta"),
        metrics.get("us_beta"),
        metrics.get("tw_nav"),
        metrics.get("us_nav"),
        tw_index=metrics.get("tw_index"),
        us_index=metrics.get("us_index"),
        usdtwd=metrics.get("usdtwd"),
        beta_complete=bool(metrics.get("exposure_complete", True)),
    )


def load_live_book(as_of: datetime | None = None) -> LiveBook:
    """讀試算表與報價。只呼叫既有的讀取函式，不寫回任何儲存。"""
    from portfolio_risk import (
        attach_quotes,
        fetch_quant_sheets,
        fetch_twii,
        finalize_valuation,
        load_service_account_info,
        market_beta_and_sector,
        normalize_positions,
        populate_market_fields_parallel,
        resolve_usdtwd,
    )
    from nav_snapshot import last_known_usdtwd

    key_data = load_service_account_info()
    accounts, nav_history, quant_data = fetch_quant_sheets(key_data)
    fx = resolve_usdtwd(last_known=last_known_usdtwd(nav_history))
    usdtwd = fx.rate
    warnings = [fx.warning()] if fx.is_fallback else []
    positions = normalize_positions(quant_data)
    tw_index = None
    if not positions.empty:
        extras = {}
        tw_index = fetch_twii()
        prices, betas, sectors = populate_market_fields_parallel(positions, market_beta_and_sector, tw_index, extras=extras)
        positions = attach_quotes(positions, prices, betas, sectors, extras=extras)
    valued, _accounts, metrics = finalize_valuation(positions, accounts, usdtwd)
    if tw_index is not None:
        metrics["tw_index"] = tw_index
    metrics["usdtwd"] = usdtwd
    from portfolio_greeks import trusted_regime
    from macro_cycle_live import macro_view_for_orders

    moment = as_of or taipei_now()
    from credit_transmission import api_view, load_board

    payload = portfolio_payload(
        valued,
        metrics,
        usdtwd=usdtwd,
        as_of=moment,
        usdtwd_source=fx.source,
        warnings=warnings,
        regime=trusted_regime(macro_view_for_orders()),
        credit_transmission=api_view(load_board(moment.date())),
    )
    return LiveBook(payload, valued, metrics)
