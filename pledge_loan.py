"""股票質押借款與真正的融資分開。

Active_Positions「融資/質押借款」這一格是質押借款，淨值照扣。
質押維持率＝質押品市值 ÷ 質押借款。Sheet 沒標質押品時，用該戶台股現股市值，並標成估算。
門檻是程式預設，要對照券商契約。

真正的融資在「融資」欄。沒有這欄或餘額是 0，就是無融資。
融資仍用整戶持股市值與 130% 維持率，不套到質押借款上。
"""

from __future__ import annotations

import math

import pandas as pd

PLEDGE_THRESHOLDS = {
    "call_below_pct": 130.0,
    "yellow_high_pct": 160.0,
}
PLEDGE_NOTE = "質押維持率門檻是程式預設：低於 130% 追繳（紅）、130–160% 黃、高於 160% 綠。請對照自己的券商質押契約確認。"
MARGIN_ACCOUNT_RATIO = 1.30
_MARK_COLUMNS = ("質押品", "質押", "擔保")
_MARK_YES = {"1", "y", "yes", "true", "是", "質押", "v", "✓"}


def empty_pledge() -> dict:
    return {
        "balance": 0.0,
        "collateral_value": None,
        "collateral_basis": "none",
        "maintenance_pct": None,
        "light": None,
        "drop_to_call_pct": None,
        "estimated": False,
        "thresholds": dict(PLEDGE_THRESHOLDS),
        "note": PLEDGE_NOTE,
        "accounts": [],
    }


def empty_margin_financing() -> dict:
    return {"balance": 0.0, "label": "無融資", "status": "none", "accounts": []}


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def pledge_light(maintenance_pct: float | None) -> str | None:
    if maintenance_pct is None or not math.isfinite(maintenance_pct):
        return None
    if maintenance_pct < PLEDGE_THRESHOLDS["call_below_pct"]:
        return "red"
    if maintenance_pct <= PLEDGE_THRESHOLDS["yellow_high_pct"]:
        return "yellow"
    return "green"


def drop_to_call_pct(collateral: float | None, balance: float | None) -> float | None:
    """質押品還能跌幾個百分點才碰到 130%。已低於追繳線時是負數。"""
    if collateral is None or balance is None or balance <= 0 or collateral <= 0:
        return None
    call = PLEDGE_THRESHOLDS["call_below_pct"] / 100.0
    return (1.0 - call * balance / collateral) * 100.0


def drop_to_call_text(drop: float | None) -> str:
    if drop is None:
        return ""
    if drop < 0:
        return "已低於追繳線"
    return f"距追繳還可跌 {drop:.1f}%"


def _marked(value) -> bool:
    return str(value).strip().lower() in _MARK_YES


def _is_tw_spot(kind: str) -> bool:
    text = str(kind or "")
    if "美" in text or "期" in text or "選" in text or "可轉" in text:
        return False
    upper = text.upper()
    if "CBAS" in upper or upper == "CB":
        return False
    return "台" in text or text in {"股票", "現股"}


def _market_value(row) -> float:
    for column in ("即時市值(台幣)", "權益市值(台幣)"):
        number = _num(row.get(column))
        if number is not None:
            return abs(number)
    price = _num(row.get("即時報價"))
    qty = _num(row.get("持有數量"))
    if price is None or qty is None:
        return 0.0
    fx = _num(row.get("換算匯率"))
    if fx is None:
        fx = 1.0 if "美" not in str(row.get("種類") or "") else 0.0
    return abs(price * qty * fx)


def _account_rows(positions: pd.DataFrame | None, account: str) -> list:
    if positions is None or positions.empty or "帳戶" not in positions.columns:
        return []
    name = str(account).strip()
    matched = positions[positions["帳戶"].astype(str).str.strip() == name]
    return [row for _, row in matched.iterrows()]


def _collateral(rows: list) -> tuple[float, str]:
    mark_column = next((column for column in _MARK_COLUMNS if rows and column in rows[0].index), None)
    marked = []
    if mark_column is not None:
        marked = [row for row in rows if _marked(row.get(mark_column))]
    if marked:
        total = 0.0
        for row in marked:
            qty = _num(row.get("持有數量")) or 0.0
            if qty > 0:
                total += _market_value(row)
        return total, "sheet"
    total = 0.0
    for row in rows:
        qty = _num(row.get("持有數量")) or 0.0
        if qty > 0 and _is_tw_spot(str(row.get("種類") or "")):
            total += _market_value(row)
    return total, "estimated_tw_spot"


def _account_level(balance: float, collateral: float, basis: str) -> dict:
    pct = (collateral / balance * 100.0) if balance > 0 else None
    return {
        "balance": balance,
        "collateral_value": collateral,
        "collateral_basis": basis,
        "maintenance_pct": pct,
        "light": pledge_light(pct),
        "drop_to_call_pct": drop_to_call_pct(collateral, balance),
    }


def pledge_view(accounts: pd.DataFrame | None, positions: pd.DataFrame | None) -> dict:
    """有餘額的質押戶。維持率用質押品；沒標明就用該戶台股現股，basis 為 estimated_tw_spot。"""
    view = empty_pledge()
    if accounts is None or accounts.empty or "帳戶名稱" not in accounts.columns:
        return view
    if "融資/質押借款" not in accounts.columns:
        return view
    rows = []
    for _, account in accounts.iterrows():
        balance = _num(account.get("融資/質押借款"))
        if balance is None or balance <= 0:
            continue
        name = str(account.get("帳戶名稱") or "").strip()
        collateral, basis = _collateral(_account_rows(positions, name))
        level = _account_level(balance, collateral, basis)
        level["account"] = name
        rows.append(level)
    if not rows:
        return view
    balance = sum(item["balance"] for item in rows)
    collateral = sum(item["collateral_value"] for item in rows)
    estimated = any(item["collateral_basis"] == "estimated_tw_spot" for item in rows)
    pct = collateral / balance * 100.0 if balance > 0 else None
    view.update(
        {
            "balance": balance,
            "collateral_value": collateral,
            "collateral_basis": "estimated_tw_spot" if estimated else "sheet",
            "maintenance_pct": pct,
            "light": pledge_light(pct),
            "drop_to_call_pct": drop_to_call_pct(collateral, balance),
            "estimated": estimated,
            "accounts": rows,
        }
    )
    return view


def pledge_caption(view: dict | None) -> str:
    view = view or empty_pledge()
    if float(view.get("balance") or 0.0) <= 0:
        return "沒有質押借款。" + view.get("note", "")
    pct = view.get("maintenance_pct")
    pct_text = "—" if pct is None else f"{pct:.1f}%"
    estimated = "（估算）" if view.get("estimated") else ""
    light = {"red": "紅", "yellow": "黃", "green": "綠"}.get(view.get("light") or "", "")
    light_text = f"{light}燈。" if light else ""
    return f"質押維持率 {pct_text}{estimated}。{light_text}{drop_to_call_text(view.get('drop_to_call_pct'))}。{view.get('note', '')}"


def margin_financing_view(accounts: pd.DataFrame | None, positions: pd.DataFrame | None = None, maintenance_ratio: float = MARGIN_ACCOUNT_RATIO) -> dict:
    """只看「融資」欄。質押借款不算融資。沒有餘額就是無融資。"""
    view = empty_margin_financing()
    if accounts is None or accounts.empty or "帳戶名稱" not in accounts.columns or "融資" not in accounts.columns:
        return view
    rows = []
    for _, account in accounts.iterrows():
        name = str(account.get("帳戶名稱") or "").strip()
        if "元大期貨" in name:
            continue
        balance = _num(account.get("融資"))
        if balance is None or balance <= 0:
            continue
        market = _num(account.get("即時持股市值"))
        if market is None:
            market = sum(_market_value(row) for row in _account_rows(positions, name))
        ratio_now = (market / balance) if market and market > 0 else 0.0
        drop = (1.0 - maintenance_ratio * balance / market) * 100.0 if market and market > 0 else -100.0
        rows.append(
            {
                "account": name,
                "balance": balance,
                "market_value": float(market or 0.0),
                "maintenance_pct": ratio_now * 100.0,
                "drop_to_call_pct": drop,
                "already_call": bool(ratio_now < maintenance_ratio),
            }
        )
    if not rows:
        return view
    view["balance"] = sum(item["balance"] for item in rows)
    view["label"] = "融資"
    view["status"] = "active"
    view["accounts"] = rows
    return view


def margin_account_call_levels(snapshot, maintenance_ratio: float = MARGIN_ACCOUNT_RATIO) -> list[dict]:
    """真正的融資整戶維持率。質押借款不進這裡。"""
    accounts = getattr(snapshot, "accounts_valued", None)
    positions = getattr(snapshot, "valued", None)
    view = margin_financing_view(accounts, positions, maintenance_ratio)
    levels = []
    for item in view["accounts"]:
        levels.append(
            {
                "account": item["account"],
                "market_value": item["market_value"],
                "debt": item["balance"],
                "maintenance_now_pct": item["maintenance_pct"],
                "trigger_drop_pct": item["drop_to_call_pct"],
                "already_call": item["already_call"],
            }
        )
    return levels
