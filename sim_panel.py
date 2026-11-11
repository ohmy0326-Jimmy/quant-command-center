"""避險試算與淨值劇本的共同結果列。不連網、不寫試算表。

缺值留 None，不當 0。台股與美股的 Beta 不混在同一格。
"""

from __future__ import annotations

from portfolio_greeks import DEFAULT_BETA_BANDS, _band


def beta_status(metrics: dict | None, regime: str | None, market: str) -> dict:
    """實際 Beta 與 PR42 的預設目標區間。沒有數字就是不完整，不是 0。"""
    key = "tw_beta" if market == "TW" else "us_beta"
    beta = None
    if isinstance(metrics, dict) and key in metrics and metrics.get(key) is not None:
        try:
            beta = float(metrics[key])
        except (TypeError, ValueError):
            beta = None
    band = _band(market, str(regime or ""), None)
    return {
        "market": market,
        "beta": beta,
        "band": band,
        "regime": regime if band is not None else None,
        "complete": beta is not None and band is not None,
        "note": "" if beta is not None and band is not None else "不完整",
        "defaults": DEFAULT_BETA_BANDS.get(market),
    }


def shared_result_rows(hedge_table, scenario_frame) -> list[dict]:
    """兩種模擬收成同一張表。沒有結果就回空清單。"""
    rows: list[dict] = []
    if hedge_table is not None and getattr(hedge_table, "empty", True) is False:
        for name, row in hedge_table.iterrows():
            rows.append({
                "面板": "台股避險試算",
                "名稱": str(name),
                "期末": _optional(row.get("期末淨值")),
                "最大回撤": _optional(row.get("最大回撤")),
                "市場": "台",
                "工具": "台指期／小台／TXO",
            })
    if scenario_frame is not None and getattr(scenario_frame, "empty", True) is False:
        for _, row in scenario_frame.iterrows():
            rows.append({
                "面板": "淨值劇本",
                "名稱": str(row.get("策略") or ""),
                "期末": _optional(row.get("期末淨值中位數")),
                "最大回撤": _optional(row.get("中位最大回撤")),
                "市場": "台",
                "工具": "劇本內的減碼與 TXO",
            })
    return rows


def _optional(value):
    if value is None:
        return None
    try:
        if value != value:
            return None
    except TypeError:
        return None
    return value
