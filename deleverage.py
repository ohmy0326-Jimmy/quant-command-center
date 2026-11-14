"""第四頁降槓進度。只試算，不下單、不推播、不寫試算表。

兩個目標是使用者自己的降槓倍數，不是 macro_cycle.REGIME_RISK。
REGIME_RISK 的偏多仍是 1.8，這裡不改那張表。
"""

from __future__ import annotations

import math

import pandas as pd

# 使用者的降槓目標。偏多上限 2.0，不是 REGIME_RISK 的偏多 1.8。
USER_DELEVERAGE_TARGETS = (
    {"id": "bullish_cap", "label": "偏多上限", "leverage": 2.0},
    {"id": "cut_period", "label": "降槓期間", "leverage": 1.5},
)

# 換成 1 倍標的。倍數與 beta_rules / stop_rules 的契約倍數一致。
# 00663L 追蹤臺灣加權指數 2 倍。1 倍對應是加權指數本身 ^TWII，不是 0050。
# 0050 是台灣 50，成分與加權不同，所以不拿來當這一檔的 1 倍。
SWAP_TO_1X = {
    "SOXL": {"underlying": "SOXX", "multiple": 3.0, "multiple_source": "契約倍數", "note": ""},
    "MUU": {"underlying": "MU", "multiple": 2.0, "multiple_source": "契約倍數", "note": ""},
    "SNXX": {"underlying": "SNDK", "multiple": 2.0, "multiple_source": "契約倍數", "note": ""},
    "LITX": {"underlying": "LITE", "multiple": 2.0, "multiple_source": "契約倍數", "note": ""},
    "AMUU": {"underlying": "AMD", "multiple": 2.0, "multiple_source": "契約倍數", "note": ""},
    "00663L": {
        "underlying": "^TWII",
        "multiple": 2.0,
        "multiple_source": "契約倍數（正2）",
        "note": "00663L 是臺灣加權正 2。換成 1 倍時對應加權指數 ^TWII，不是 0050。",
    },
}

PANEL_NOTE = (
    "偏多上限 2.0 倍與降槓期間 1.5 倍是使用者的降槓目標，不是第五頁 REGIME_RISK（偏多仍是 1.8）。"
    "槓桿＝名目曝險 ÷ 已扣質押借款的淨值。進度是目標倍數 ÷ 目前倍數，滿格代表已落到目標。"
    "需降低的名目＝目前名目 − 目標倍數 × 淨值。換成 1 倍是把該檔名目從「市值 × 倍數」降成「市值 × 1」。"
    "00663L 的 1 倍對應是加權指數 ^TWII，不是 0050。"
)


def empty_deleverage() -> dict:
    return {
        "label": "使用者降槓目標",
        "note": PANEL_NOTE,
        "net_value": None,
        "notional_exposure": None,
        "leverage": None,
        "exposure_complete": False,
        "targets": [_target_unknown(item) for item in USER_DELEVERAGE_TARGETS],
        "swaps": [],
    }


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _ticker_key(symbol) -> str:
    text = str(symbol or "").strip().upper()
    for suffix in (".TW", ".TWO"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _target_unknown(spec: dict, reason: str = "名目或淨值不完整，沒有把缺的部位當成 0。") -> dict:
    return {
        "id": spec["id"],
        "label": spec["label"],
        "target_leverage": float(spec["leverage"]),
        "reduction_twd": None,
        "progress": None,
        "reached": None,
        "reason": reason,
    }


def _targets(nav: float | None, exposure: float | None, leverage: float | None, complete: bool) -> list[dict]:
    rows = []
    for spec in USER_DELEVERAGE_TARGETS:
        target = float(spec["leverage"])
        if not complete or nav is None or exposure is None or leverage is None or nav <= 0:
            rows.append(_target_unknown(spec))
            continue
        reduction = exposure - target * nav
        if reduction < 0:
            reduction = 0.0
        progress = 1.0 if leverage <= target else target / leverage
        rows.append({
            "id": spec["id"],
            "label": spec["label"],
            "target_leverage": target,
            "reduction_twd": reduction,
            "progress": progress,
            "reached": bool(leverage <= target + 1e-9),
            "reason": "",
        })
    return rows


def _swap_row(row) -> dict | None:
    key = _ticker_key(row.get("標的"))
    spec = SWAP_TO_1X.get(key)
    qty = _finite(row.get("持有數量"))
    if qty is None or qty == 0 or spec is None:
        return None
    price = _finite(row.get("即時報價"))
    market_value = _finite(row.get("即時市值(台幣)"))
    if market_value is None and price is not None:
        fx = _finite(row.get("換算匯率"))
        kind = str(row.get("種類") or "")
        if "美" in kind and fx is None:
            market_value = None
        else:
            market_value = abs(price * qty * (fx if fx is not None else 1.0))
    multiple = float(spec["multiple"])
    base = {
        "symbol": key,
        "name": str(row.get("名稱") or ""),
        "underlying": spec["underlying"],
        "multiple": multiple,
        "multiple_source": spec["multiple_source"],
        "note": spec["note"],
        "market_value_twd": None,
        "notional_twd": None,
        "reduction_twd": None,
        "leverage_after": None,
        "cumulative_leverage": None,
    }
    if price is None or market_value is None:
        base["note"] = "沒有報價，這檔要降的名目未知，不是 0。" + (base["note"] and " " + base["note"])
        return base
    notional = abs(market_value) * abs(multiple)
    reduction = abs(market_value) * max(abs(multiple) - 1.0, 0.0)
    base["market_value_twd"] = abs(market_value)
    base["notional_twd"] = notional
    base["reduction_twd"] = reduction
    return base


def build_deleverage(positions: pd.DataFrame | None, metrics: dict | None) -> dict:
    """用已扣質押借款的淨值與名目曝險。缺報價的槓桿 ETF 名目是 null，不是 0。"""
    metrics = metrics or {}
    view = empty_deleverage()
    nav = _finite(metrics.get("nav"))
    exposure = _finite(metrics.get("exposure"))
    complete = bool(metrics.get("exposure_complete", True)) and nav is not None and exposure is not None and nav > 0
    leverage = (exposure / nav) if complete else None
    view["net_value"] = nav
    view["notional_exposure"] = exposure if complete else None
    view["leverage"] = leverage
    view["exposure_complete"] = complete
    view["targets"] = _targets(nav, exposure, leverage, complete)
    swaps = []
    frame = positions if isinstance(positions, pd.DataFrame) else None
    if frame is not None and not frame.empty and "標的" in frame.columns:
        for _, row in frame.iterrows():
            item = _swap_row(row)
            if item is not None:
                swaps.append(item)
    swaps.sort(key=lambda item: (item["reduction_twd"] is None, -(item["reduction_twd"] or 0.0)))
    running = exposure if complete else None
    for item in swaps:
        if running is None or nav is None or nav <= 0 or item["reduction_twd"] is None:
            continue
        running -= item["reduction_twd"]
        after = running / nav
        item["leverage_after"] = after
        item["cumulative_leverage"] = after
    view["swaps"] = swaps
    return view


def render_deleverage_panel(st, block: dict | None) -> None:
    """放在第四頁槓桿表旁邊。只顯示。"""
    block = block or empty_deleverage()
    st.markdown("**降槓進度**")
    st.caption(block.get("note") or PANEL_NOTE)
    leverage = block.get("leverage")
    if leverage is None:
        st.warning("目前槓桿未知。名目或淨值不完整，沒有用 0。")
    else:
        st.caption(f"目前槓桿 {leverage:.2f} 倍（名目 { _money(block.get('notional_exposure')) } ÷ 淨值 { _money(block.get('net_value')) }）。")
    for target in block.get("targets") or []:
        label = f"{target['label']} {target['target_leverage']:.1f} 倍"
        progress = target.get("progress")
        if progress is None:
            st.caption(f"{label}：{target.get('reason') or '未知'}")
            continue
        st.progress(min(1.0, max(0.0, float(progress))), text=label)
        reduction = target.get("reduction_twd")
        if reduction is None:
            st.caption(f"{label}：需降低的名目未知。")
        elif reduction <= 0:
            st.caption(f"{label}：已在目標以內，不用再降名目。")
        else:
            st.caption(f"{label}：需再降低名目 NT$ {reduction:,.0f}。")
    swaps = block.get("swaps") or []
    if not swaps:
        st.caption("目前沒有對照表裡的槓桿 ETF 持倉。")
        return
    frame = pd.DataFrame([
        {
            "標的": item["symbol"],
            "換成": item.get("underlying") or "—",
            "倍數": item.get("multiple"),
            "倍數來源": item.get("multiple_source") or "",
            "降低名目": item.get("reduction_twd"),
            "換完槓桿": item.get("cumulative_leverage"),
            "備註": item.get("note") or "",
        }
        for item in swaps
    ])
    st.dataframe(frame, hide_index=True, width="stretch")
    st.caption("由降低名目大的先換。換完槓桿是累計換成 1 倍之後的組合槓桿。沒有報價的列是未知，不是 0。")


def _money(value) -> str:
    number = _finite(value)
    if number is None:
        return "未知"
    return f"NT$ {number:,.0f}"
