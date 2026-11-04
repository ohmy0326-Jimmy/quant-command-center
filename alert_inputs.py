"""即時警報的讀數：全部沿用既有計算（第四頁估價、第一頁宏觀風險、第五頁多空、第六頁避險燈）。

每一項各自 try；讀不到回 None，該項維持上一次狀態，不會誤報。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


def _safe(name: str, fn):
    try:
        return fn()
    except Exception as exc:  # 單項失敗不影響其他項
        logger.warning("alert reading failed key=%s type=%s", name, type(exc).__name__)
        return None


def _portfolio(spreadsheet):
    from nav_snapshot import _live_market
    from order_assistant import Snapshot
    from portfolio_risk import attach_quotes, fetch_twii, fetch_usdtwd, normalize_positions, read_quant_sheets

    accounts, nav_data, quant = read_quant_sheets(spreadsheet)
    positions = normalize_positions(quant)
    prices, betas, sectors, extras = _live_market(positions)
    if not positions.empty:
        positions = attach_quotes(positions, prices, betas, sectors, extras=extras)
    snapshot = Snapshot(positions, accounts, fetch_usdtwd(), fetch_twii())
    return snapshot, nav_data


def margin_reading(snapshot) -> dict | None:
    from order_insights import futures_margin_call_level

    level = futures_margin_call_level(snapshot)
    if level is None:  # 沒有期貨部位
        return {"already": False, "move_pct": None, "index_level": None, "direction": "無期貨部位"}
    return level


def drawdown_reading(metrics: dict, nav_data) -> dict | None:
    # PR #27 的出入金調整指數尚未合併到 main，這裡沿用第四頁同一條淨值曲線。
    from risk_performance import drawdown_stats
    from sheet_dates import nav_growth_series

    stats = drawdown_stats(nav_growth_series(nav_data, float(metrics.get("nav", 0.0) or 0.0)))
    if stats["current_dd_pct"] is None:
        return None
    return {"dd_pct": stats["current_dd_pct"]}


def stop_loss_reading(snapshot) -> dict:
    from order_insights import triggered_stop_losses

    return {"positions": triggered_stop_losses(snapshot)}


def macro_reading(now: datetime) -> dict | None:
    import macro_risk

    today = now.astimezone(macro_risk.TPE).date()
    loaders = {
        "vix": lambda: macro_risk.fetch_history("^VIX", "1mo"),
        "tnx": lambda: macro_risk.fetch_history("^TNX", "1mo"),
        "foreign": lambda: macro_risk.fetch_foreign_flows(today),
        "usdtwd": lambda: macro_risk.fetch_history("USDTWD=X", "3mo"),
        "taiex": lambda: macro_risk.fetch_history("^TWII", "3mo"),
    }
    _items, summary = macro_risk.evaluate(loaders, now)
    if summary["level"] == "grey":  # 資料不足 → 不判斷
        return None
    return summary


def macro_cycle_view(spreadsheet) -> dict:
    from macro_cycle import load_macro_cycle
    from macro_cycle_live import fetch_auto_results, read_macro_tabs
    from macro_cycle_sheet import compose_macro_view

    return compose_macro_view(load_macro_cycle(), read_macro_tabs(spreadsheet), fetch_auto_results())


def regime_reading(view: dict) -> dict | None:
    if not view or view.get("fallback") or not view.get("regime"):
        return None
    return {"regime": view["regime"], "total": view.get("total")}


def quadrant_reading(view: dict) -> dict | None:
    industries = (view or {}).get("industries") or []
    items = {str(i.get("name")): str(i.get("cycle")) for i in industries if i.get("name") and i.get("cycle")}
    return {"items": items} if items else None


def hedge_reading(view: dict | None) -> dict | None:
    import macro_risk
    from hedge_sim import HedgeParams, trigger_status

    px = macro_risk.fetch_history("^TWII", "2y")["Close"].dropna()
    if len(px) < 60:
        return None
    view = view or {}
    return trigger_status(px, HedgeParams(), score_history=view.get("history"), score_total=view.get("total"), score_as_of=view.get("score_updated_at"))


def gather_readings(spreadsheet, now: datetime) -> dict:
    readings: dict = {}
    loaded = _safe("portfolio", lambda: _portfolio(spreadsheet))
    if loaded is not None:
        snapshot, nav_data = loaded
        metrics = snapshot.metrics
        readings["margin"] = _safe("margin", lambda: margin_reading(snapshot))
        readings["stop_loss"] = _safe("stop_loss", lambda: stop_loss_reading(snapshot))
        readings["drawdown"] = _safe("drawdown", lambda: drawdown_reading(metrics, nav_data))
        if float(metrics.get("nav", 0.0) or 0.0) > 0:
            readings["leverage"] = {"value": float(metrics.get("total_leverage", metrics.get("leverage", 0.0)) or 0.0)}
            readings["cash"] = {"value": float(metrics.get("cash_pct", 0.0) or 0.0)}
    readings["macro"] = _safe("macro", lambda: macro_reading(now))
    view = _safe("macro_cycle", lambda: macro_cycle_view(spreadsheet))
    if view is not None:
        readings["regime"] = regime_reading(view)
        readings["quadrant"] = quadrant_reading(view)
    readings["hedge"] = _safe("hedge", lambda: hedge_reading(view))
    return readings


def telegram_config() -> tuple[str, str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def run_alert_check(now: datetime | None = None) -> dict:
    """/v1/alert-check 用：讀數 → 比對 Alert_State → 推播 → 寫回狀態。"""
    from alert_engine import SheetAlertStore, run_check
    from portfolio_briefing import taipei_now
    from portfolio_risk import load_service_account_info, open_quant_spreadsheet
    from telegram_html import send_message

    token, chat_id = telegram_config()
    if not token or not chat_id:
        raise TelegramNotConfigured()
    moment = now or taipei_now()
    spreadsheet = open_quant_spreadsheet(load_service_account_info())
    store = SheetAlertStore(spreadsheet)
    previous = store.load()
    readings = gather_readings(spreadsheet, moment)

    def send(alert, silent: bool) -> bool:
        response = send_message(token, chat_id, alert.html(moment), disable_notification=silent)
        if response.status_code != 200:
            logger.warning("telegram send failed key=%s status=%s", alert.key, response.status_code)
        return response.status_code == 200

    result = run_check(readings, previous, send, moment)
    store.save(result.states)
    return result.summary()


class TelegramNotConfigured(Exception):
    pass
