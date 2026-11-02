"""把第四頁估價結果編成唯讀 JSON。

數字全部來自 ``finalize_valuation``，這裡不再算一遍槓桿或保證金。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

SCHEMA_VERSION = "1.1.0"
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
            }
        )
    return rows


def yuanta_from_metrics(metrics: dict) -> dict:
    """元大期貨權益與維持保證金，沿用第四頁寫進 metrics 的數字。"""
    ratio = metrics.get("margin_ratio")
    return {
        "equity": float(metrics.get("futures_equity") or 0.0),
        "maintenance_margin": float(metrics.get("futures_maint") or 0.0),
        "margin_ratio_pct": None if ratio is None else float(ratio),
        "excess_margin": float(metrics.get("excess_margin") or 0.0),
        "margin_call": bool(metrics.get("margin_call")),
    }


def portfolio_payload(positions: pd.DataFrame, metrics: dict, *, usdtwd: float, as_of: datetime) -> dict:
    total_beta = float(metrics.get("total_beta", metrics.get("beta", 0.0)))
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": format_as_of(as_of),
        "currency": "TWD",
        "usdtwd": float(usdtwd),
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
        "positions": position_records(positions),
        "yuanta_futures": yuanta_from_metrics(metrics),
    }


def load_live_book(as_of: datetime | None = None) -> LiveBook:
    """讀試算表與報價。只呼叫既有的讀取函式，不寫回任何儲存。"""
    from portfolio_risk import (
        attach_quotes,
        fetch_quant_sheets,
        fetch_twii,
        fetch_usdtwd,
        finalize_valuation,
        load_service_account_info,
        market_beta_and_sector,
        normalize_positions,
        populate_market_fields_parallel,
    )

    key_data = load_service_account_info()
    accounts, _nav_history, quant_data = fetch_quant_sheets(key_data)
    usdtwd = fetch_usdtwd()
    positions = normalize_positions(quant_data)
    if not positions.empty:
        prices, betas, sectors = populate_market_fields_parallel(positions, market_beta_and_sector, fetch_twii())
        positions = attach_quotes(positions, prices, betas, sectors)
    valued, _accounts, metrics = finalize_valuation(positions, accounts, usdtwd)
    moment = as_of or taipei_now()
    return LiveBook(portfolio_payload(valued, metrics, usdtwd=usdtwd, as_of=moment), valued, metrics)
