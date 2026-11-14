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
    "倉位目標配比也是使用者自己的區間，分母同樣是已扣質押借款的淨值。"
)

# 使用者的倉位目標。不是 REGIME_RISK。百分比是淨值的百分點。
USER_EXPOSURE_BAND = {"low": 1.0, "high": 1.2, "label": "總名目曝險"}
USER_BUCKETS = (
    {"id": "core", "label": "科技核心", "low": 50.0, "high": 60.0, "role": "band", "note": "現股或一倍 ETF。槓桿 ETF 不算核心。"},
    {"id": "defense", "label": "分散/防禦", "low": 20.0, "high": 30.0, "role": "band", "note": "防禦類股、高股息與債券。"},
    {"id": "event", "label": "事件交易", "low": None, "high": 15.0, "role": "cap", "note": "這是上限，不是要補滿的區間。實際控管是單筆風險 1%、總未平倉風險不超過 6%。"},
    {"id": "cash_hedge", "label": "現金＋避險", "low": 10.0, "high": 15.0, "role": "band", "note": "現金加上避險用的期貨與賣權。"},
)
TACTICAL_CAP_PCT = 20.0
ALLOCATION_NOTE = "這些區間是使用者的倉位目標，不是牛熊分數，也不是 REGIME_RISK。"
# 可改的代號對照。沒有列在這裡、也對不上種類規則的持倉進未分類，不猜測。
CORE_SYMBOLS = {
    "2330", "TSM", "NVDA", "MU", "AMD", "SOXX", "QQQ", "AAPL", "MSFT", "AVGO",
    "AMAT", "LRCX", "ASML", "SMH", "2454", "2308", "3711", "3037",
}
DEFENSE_SYMBOLS = {"XLV", "XLP", "XLU", "0056", "TLT", "IEF", "BND", "AGG", "GLD", "SGOV", "BIL", "SHY"}
TACTICAL_MULTIPLE = {key: float(spec["multiple"]) for key, spec in SWAP_TO_1X.items()}
TACTICAL_MULTIPLE["00631L"] = 2.0
HEDGE_FUTURES = {"TX", "MTX", "FITX", "MXF", "EX", "ES", "MES", "NQ", "MNQ", "SPX", "XSP", "YM", "MYM"}


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
        "allocation_note": ALLOCATION_NOTE,
        "exposure_band": _empty_exposure_band(),
        "buckets": [_empty_bucket(spec) for spec in USER_BUCKETS],
        "unclassified": [],
        "tactical": _empty_tactical(),
        "event_risk": {"per_trade_pct": None, "open_risk_pct": None, "open_risk_cap_pct": 6.0, "note": USER_BUCKETS[2]["note"]},
        "priority_reductions": [],
        "priority_reason": "",
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


def _sum_optional(left, right):
    if left is None or right is None:
        return None
    return left + right


def _aggregate_swaps(items: list[dict]) -> list[dict]:
    """同一代號跨帳戶加總。任一戶沒有報價，整列的降低名目是 null，不加 0。"""
    grouped: dict[str, dict] = {}
    order: list[str] = []
    counts: dict[str, int] = {}
    for item in items:
        key = item["symbol"]
        counts[key] = counts.get(key, 0) + 1
        current = grouped.get(key)
        if current is None:
            grouped[key] = dict(item)
            order.append(key)
            continue
        for field in ("market_value_twd", "notional_twd", "reduction_twd"):
            current[field] = _sum_optional(current.get(field), item.get(field))
        if not current.get("name") and item.get("name"):
            current["name"] = item["name"]
        current["leverage_after"] = None
        current["cumulative_leverage"] = None
    rows = []
    for key in order:
        row = grouped[key]
        count = counts[key]
        if count > 1:
            extra = f"同一代號 {count} 個帳戶已加總。"
            note = row.get("note") or ""
            if extra not in note:
                row["note"] = (note + " " + extra).strip()
        rows.append(row)
    return rows


def _empty_exposure_band() -> dict:
    return {
        "label": USER_EXPOSURE_BAND["label"],
        "low": USER_EXPOSURE_BAND["low"],
        "high": USER_EXPOSURE_BAND["high"],
        "current": None,
        "gap_twd": None,
        "status": "未知",
        "note": "使用者目標。分母是已扣質押借款的淨值。",
    }


def _empty_bucket(spec: dict) -> dict:
    return {
        "id": spec["id"],
        "label": spec["label"],
        "role": spec["role"],
        "target_low": spec["low"],
        "target_high": spec["high"],
        "current_twd": None,
        "current_pct": None,
        "gap_twd": None,
        "status": "未知",
        "note": spec["note"],
        "symbols": [],
    }


def _empty_tactical() -> dict:
    return {
        "label": "戰術曝險",
        "cap_pct": TACTICAL_CAP_PCT,
        "notional_twd": None,
        "notional_pct": None,
        "exceeded": None,
        "warning": "",
        "positions": [],
        "note": "槓桿 ETF 用名目（市值 × 倍數）計入總曝險，標成戰術曝險，不算科技核心。上限是淨值的 20%。",
    }


def _is_put(row) -> bool:
    text = f"{row.get('種類') or ''} {row.get('名稱') or ''} {row.get('標的') or ''}".lower()
    return "put" in text or "賣權" in text


def _is_hedge_future(row) -> bool:
    kind = str(row.get("種類") or "")
    key = _ticker_key(row.get("標的"))
    if "期貨" not in kind and "期貨" not in key:
        return False
    return key in HEDGE_FUTURES or key.startswith("TX") or key.startswith("MTX")


def _bucket_of(row) -> str:
    key = _ticker_key(row.get("標的"))
    kind = str(row.get("種類") or "")
    name = str(row.get("名稱") or "")
    multiple = _finite(row.get("標的槓桿"))
    if key in TACTICAL_MULTIPLE:
        return "tactical"
    if multiple is not None and abs(multiple) >= 1.5 and "ETF" in kind:
        return "tactical"
    if key in CORE_SYMBOLS:
        return "core"
    if key in DEFENSE_SYMBOLS or "債" in name or "債" in kind:
        return "defense"
    if _is_put(row) or _is_hedge_future(row):
        return "cash_hedge"
    if "選擇權" in kind or "期權" in kind or "option" in kind.lower() or "期貨" in kind:
        return "event"
    return "unclassified"


def _allocation_amount(row, bucket: str) -> tuple[float | None, float | None]:
    """回傳 (計入桶子的台幣, 戰術名目)。缺報價是 None，不是 0。"""
    market_value = _finite(row.get("即時市值(台幣)"))
    if bucket == "tactical":
        key = _ticker_key(row.get("標的"))
        multiple = TACTICAL_MULTIPLE.get(key)
        if multiple is None:
            multiple = _finite(row.get("標的槓桿"))
        if market_value is None or multiple is None:
            return None, None
        return None, abs(market_value) * abs(multiple)
    if bucket == "cash_hedge" and ("期貨" in str(row.get("種類") or "")):
        margin = _finite(row.get("維持保證金(原幣)"))
        fx = _finite(row.get("換算匯率")) or 1.0
        if margin is None:
            return None, None
        return abs(margin) * abs(fx), None
    if market_value is None:
        return None, None
    return abs(market_value), None


def _band_gap(value: float | None, low, high, role: str) -> tuple[float | None, str]:
    if value is None:
        return None, "未知"
    if role == "cap":
        if high is None:
            return None, "未知"
        excess = value - high
        if excess > 1e-6:
            return excess, "超過上限"
        return 0.0, "未超過上限"
    if low is None or high is None:
        return None, "未知"
    if value < low - 1e-9:
        return low - value, "低於區間"
    if value > high + 1e-9:
        return value - high, "高於區間"
    return 0.0, "在區間內"


def _event_risk(positions: pd.DataFrame | None, nav: float | None) -> dict:
    note = "實際控管是單筆風險 1%、總未平倉風險不超過 6%。沒有單筆風險數字時是未知，不是 0。"
    per_trade = None
    open_risk = None
    frame = positions if isinstance(positions, pd.DataFrame) else None
    if frame is not None and not frame.empty:
        for column in ("單筆風險%", "單筆風險", "風險佔淨值"):
            if column in frame.columns:
                numbers = [_finite(value) for value in frame[column]]
                numbers = [item for item in numbers if item is not None]
                if numbers:
                    per_trade = max(numbers)
                break
        for column in ("未平倉風險%", "總未平倉風險"):
            if column in frame.columns:
                numbers = [_finite(value) for value in frame[column]]
                numbers = [item for item in numbers if item is not None]
                if numbers:
                    open_risk = sum(numbers)
                break
    return {"per_trade_pct": per_trade, "open_risk_pct": open_risk, "open_risk_cap_pct": 6.0, "per_trade_cap_pct": 1.0, "note": note}


def _build_allocation(positions: pd.DataFrame | None, metrics: dict, nav: float | None, exposure: float | None, leverage: float | None) -> dict:
    buckets = {spec["id"]: _empty_bucket(spec) for spec in USER_BUCKETS}
    for bucket in buckets.values():
        bucket["current_twd"] = 0.0
        bucket["symbols"] = []
    unclassified = []
    tactical_positions = []
    tactical_total = 0.0
    tactical_unknown = False
    frame = positions if isinstance(positions, pd.DataFrame) else None
    if frame is not None and not frame.empty and "標的" in frame.columns:
        for _, row in frame.iterrows():
            qty = _finite(row.get("持有數量"))
            if qty is None or qty == 0:
                continue
            bucket_id = _bucket_of(row)
            symbol = _ticker_key(row.get("標的"))
            amount, notional = _allocation_amount(row, bucket_id)
            if bucket_id == "tactical":
                tactical_positions.append({
                    "symbol": symbol,
                    "name": str(row.get("名稱") or ""),
                    "multiple": TACTICAL_MULTIPLE.get(symbol) or _finite(row.get("標的槓桿")),
                    "market_value_twd": _finite(row.get("即時市值(台幣)")),
                    "notional_twd": notional,
                    "tag": "戰術曝險",
                })
                if notional is None:
                    tactical_unknown = True
                else:
                    tactical_total += notional
                continue
            if bucket_id == "unclassified":
                unclassified.append({"symbol": symbol, "name": str(row.get("名稱") or ""), "market_value_twd": amount})
                continue
            bucket = buckets[bucket_id]
            if symbol and symbol not in bucket["symbols"]:
                bucket["symbols"].append(symbol)
            if amount is None:
                bucket["current_twd"] = None
            elif bucket["current_twd"] is not None:
                bucket["current_twd"] += amount
    cash = _finite(metrics.get("cash"))
    cash_bucket = buckets["cash_hedge"]
    if cash is None:
        cash_bucket["current_twd"] = None
    elif cash_bucket["current_twd"] is not None:
        cash_bucket["current_twd"] += max(cash, 0.0)
    built = []
    for spec in USER_BUCKETS:
        bucket = buckets[spec["id"]]
        current = bucket["current_twd"]
        if nav is None or nav <= 0 or current is None:
            bucket["current_twd"] = None if current is None else current
            bucket["current_pct"] = None
            bucket["gap_twd"] = None
            bucket["status"] = "未知"
        else:
            pct = current / nav * 100.0
            bucket["current_pct"] = pct
            if spec["role"] == "cap":
                gap, status = _band_gap(pct, spec["low"], spec["high"], "cap")
                bucket["gap_twd"] = None if gap is None else gap / 100.0 * nav
                bucket["status"] = status
            else:
                gap, status = _band_gap(pct, spec["low"], spec["high"], "band")
                bucket["gap_twd"] = None if gap is None else gap / 100.0 * nav
                bucket["status"] = status
        built.append(bucket)
    band = _empty_exposure_band()
    band["current"] = leverage
    if leverage is None or nav is None or exposure is None:
        band["gap_twd"] = None
        band["status"] = "未知"
    else:
        gap, status = _band_gap(leverage, USER_EXPOSURE_BAND["low"], USER_EXPOSURE_BAND["high"], "band")
        band["gap_twd"] = None if gap is None else gap * nav
        band["status"] = status
    merged_tactical: dict[str, dict] = {}
    order: list[str] = []
    for item in tactical_positions:
        key = item["symbol"]
        current = merged_tactical.get(key)
        if current is None:
            merged_tactical[key] = dict(item)
            order.append(key)
            continue
        for field in ("market_value_twd", "notional_twd"):
            current[field] = _sum_optional(current.get(field), item.get(field))
    tactical_positions = [merged_tactical[key] for key in order]
    tactical = _empty_tactical()
    tactical["positions"] = tactical_positions
    if tactical_unknown or (not tactical_positions and tactical_total == 0 and frame is not None):
        # 沒有戰術持倉時是 0，不是未知。有持倉但缺報價才是未知。
        pass
    if any(item["notional_twd"] is None for item in tactical_positions):
        tactical["notional_twd"] = None
        tactical["notional_pct"] = None
        tactical["exceeded"] = None
        tactical["warning"] = "有槓桿 ETF 沒有報價或倍數，戰術名目未知，沒有當成 0。"
    else:
        tactical["notional_twd"] = tactical_total
        if nav is None or nav <= 0:
            tactical["notional_pct"] = None
            tactical["exceeded"] = None
        else:
            pct = tactical_total / nav * 100.0
            tactical["notional_pct"] = pct
            tactical["exceeded"] = pct > TACTICAL_CAP_PCT + 1e-9
            if tactical["exceeded"]:
                tactical["warning"] = f"戰術曝險 {pct:.1f}% 超過淨值的 {TACTICAL_CAP_PCT:.0f}%。"
    return {
        "exposure_band": band,
        "buckets": built,
        "unclassified": unclassified,
        "tactical": tactical,
        "event_risk": _event_risk(frame, nav),
    }


def annotate_priority(block: dict | None, brake_light: str | None = None, bear_overall: str | None = None) -> dict:
    """煞車燈或熊市預警亮時，把戰術曝險列成優先減碼。未知燈不觸發。"""
    block = dict(block or empty_deleverage())
    brake = str(brake_light or "")
    bear = str(bear_overall or "")
    lit_brake = brake in {"🟡", "🔴", "yellow", "red"}
    lit_bear = bear in {"yellow", "red"}
    if not lit_brake and not lit_bear:
        block["priority_reductions"] = []
        block["priority_reason"] = ""
        return block
    reasons = []
    if lit_brake:
        reasons.append("回撤煞車燈亮")
    if lit_bear:
        reasons.append("熊市預警燈亮")
    rows = []
    for item in (block.get("tactical") or {}).get("positions") or []:
        rows.append({
            "symbol": item.get("symbol"),
            "name": item.get("name") or "",
            "notional_twd": item.get("notional_twd"),
            "tag": "戰術曝險",
            "reason": "、".join(reasons) + "，優先減這檔槓桿 ETF。",
        })
    rows.sort(key=lambda item: (item["notional_twd"] is None, -(item["notional_twd"] or 0.0)))
    block["priority_reductions"] = rows
    block["priority_reason"] = "、".join(reasons)
    return block


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
    swaps = _aggregate_swaps(swaps)
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
    view.update(_build_allocation(frame, metrics, nav, exposure if complete else None, leverage))
    return annotate_priority(view, metrics.get("brake_light"), metrics.get("bear_overall"))


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
    st.markdown("**倉位目標配比**")
    st.caption(block.get("allocation_note") or ALLOCATION_NOTE)
    band = block.get("exposure_band") or {}
    current = band.get("current")
    current_text = "未知" if current is None else f"{current:.2f} 倍"
    st.caption(
        f"{band.get('label') or '總名目曝險'} 目標 {band.get('low')}–{band.get('high')} 倍，"
        f"目前 {current_text}，缺口 {_money(band.get('gap_twd'))}（{band.get('status') or '未知'}）。"
    )
    for bucket in block.get("buckets") or []:
        pct = bucket.get("current_pct")
        pct_text = "未知" if pct is None else f"{pct:.1f}%"
        if bucket.get("role") == "cap":
            target = f"上限 {bucket.get('target_high')}%"
        else:
            target = f"{bucket.get('target_low')}–{bucket.get('target_high')}%"
        st.caption(f"{bucket['label']} 目標 {target}，目前 {pct_text}，缺口 {_money(bucket.get('gap_twd'))}（{bucket.get('status') or '未知'}）。{bucket.get('note') or ''}")
    event_risk = block.get("event_risk") or {}
    per_trade = event_risk.get("per_trade_pct")
    open_risk = event_risk.get("open_risk_pct")
    st.caption(
        "單筆風險 "
        + ("未知" if per_trade is None else f"{per_trade:.2f}%")
        + "（上限 1%）。總未平倉風險 "
        + ("未知" if open_risk is None else f"{open_risk:.2f}%")
        + "（上限 6%）。"
    )
    tactical = block.get("tactical") or {}
    if tactical.get("exceeded"):
        st.warning(tactical.get("warning") or "戰術曝險超過淨值的 20%。")
    elif tactical.get("warning"):
        st.caption(tactical["warning"])
    else:
        pct = tactical.get("notional_pct")
        pct_text = "未知" if pct is None else f"{pct:.1f}%"
        st.caption(f"戰術曝險 {pct_text}，上限 {TACTICAL_CAP_PCT:.0f}%。槓桿 ETF 不算科技核心。")
    unclassified = block.get("unclassified") or []
    if unclassified:
        names = "、".join(item.get("symbol") or "—" for item in unclassified)
        st.caption("未分類（沒有對上對照表，不猜測）：" + names)
    priority = block.get("priority_reductions") or []
    if priority:
        st.error((block.get("priority_reason") or "優先減碼") + "：" + "、".join(item.get("symbol") or "—" for item in priority))
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
