"""元大期貨本日餘額自動推算。

試算表 Quant_Database：

* ``Futures_Ledger``：可新增。校正點、校正當時的持倉、出入金、手續費與期交稅預設。
* ``Trade_Log``：已經存在。A–M 是既有欄位與公式，讀寫都不要改。
  成交來自 Notion 交易日誌（Make.com 同步）。已實現損益優先讀 Q 欄
  「已實現損益」；空白或不存在時，才用平倉價、均價、口數、乘數自己算。

本日餘額 = 最近校正餘額
    + 校正後已實現損益（平倉價 − 均價）× 口數 × 乘數
    − 手續費 − 期交稅
    ± 出入金

成本用平均成本，與上面的公式一致。分頁缺少或沒有校正點時，退回
Active_Positions 元大期貨列的現金餘額（目前是 B7）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from sheet_dates import parse_sheet_date, taipei_today
from stock_futures import resolve_stock_future

FUTURES_LEDGER_SHEET = "Futures_Ledger"
TRADE_LOG_SHEET = "Trade_Log"
STALE_AFTER_DAYS = 30

# 每點新台幣。TX 200、MTX 50、FMF／TMF 10、TXO 50。
# 個股期預設 2000；代號 S+證券代號（小型）為 100。
INDEX_MULTIPLIER = {
    "TX": 200.0,
    "MTX": 50.0,
    "TMF": 10.0,
    "FMF": 10.0,
    "TXO": 50.0,
}
STOCK_FUTURES_MULTIPLIER = 2000.0
SMALL_STOCK_FUTURES_MULTIPLIER = 100.0

# 預設手續費：每口單邊，新台幣。請在 Futures_Ledger「設定」列依元大實際費率覆蓋。
DEFAULT_FEE_PER_LOT = {
    "TX": 50.0,
    "MTX": 30.0,
    "TMF": 15.0,
    "FMF": 15.0,
    "TXO": 12.0,
    "*": 20.0,
}
# 期交稅。股價類期貨＝契約金額 × 十萬分之二；選擇權＝權利金 × 千分之一。
DEFAULT_TAX_RATE = {
    "TX": 0.00002,
    "MTX": 0.00002,
    "TMF": 0.00002,
    "FMF": 0.00002,
    "TXO": 0.001,
    "*": 0.00002,
}

# 目前校正點：2026-11-03 本日餘額 137,094，剩微台 1 口、均價 46,734，原始保證金 35,050。
# 微台在持倉統計的代號是 FMF（TMF 乘數同樣是 10）。
SEED_CALIBRATION = {
    "類型": "校正",
    "日期": "2026-11-03",
    "本日餘額": 137094,
    "商品代號": "FMF",
    "買賣": "買",
    "口數": 1,
    "均價": 46734,
    "金額": None,
    "手續費預設": None,
    "稅率預設": None,
    "原始保證金": 35050,
    "備註": "剩微台 1 口，校正當下均價 46734。",
}

LEDGER_COLUMNS = [
    "類型",
    "日期",
    "本日餘額",
    "商品代號",
    "買賣",
    "口數",
    "均價",
    "金額",
    "手續費預設",
    "稅率預設",
    "原始保證金",
    "備註",
]
# Trade_Log 實際表頭 A–R。不要改這些欄，也不要另開交易明細分頁。
# N–R（類型起）可以整欄不存在或空白；A–M 的「種類」「總金額」不可改名。
EXISTING_TRADE_HEADERS = (
    "時間",
    "帳戶",
    "種類",
    "動作",
    "標的",
    "名稱",
    "數量",
    "成交價",
    "合約乘數",
    "總金額",
    "最大虧損比例",
    "覆盤筆記",
    "標的槓桿",
    "類型",
    "長短線",
    "停損價",
    "已實現損益",
    "交易後帳戶槓桿",
)
# N–R。讀取時可以不存在或空白。
APPENDED_TRADE_HEADERS = ("類型", "長短線", "停損價", "已實現損益", "交易後帳戶槓桿")
# 種類是台股期貨／台股，不是開倉平倉的「類型」。總金額是契約金額，不是出入金的「金額」。
_DO_NOT_RENAME = frozenset({"種類", "總金額"})

_HEADER_ALIASES = {
    "交易日期": "日期",
    "時間": "日期",
    "成交時間": "日期",
    "標的": "商品代號",
    "代號": "商品代號",
    "方向": "買賣",
    "動作": "買賣",
    "數量": "口數",
    "持有數量": "口數",
    "成交價": "價格",
    "期交稅": "稅",
    "開平": "開平倉",
    "餘額": "本日餘額",
    "出入金": "金額",
    "平均成本": "均價",
    "列類型": "類型",
    "帳戶名稱": "帳戶",
}
_NAME_ALIASES = {
    "微台": "FMF",
    "微型台指": "FMF",
    "微型臺指": "FMF",
    "微型臺指期": "FMF",
    "小台": "MTX",
    "小型台指": "MTX",
    "小型臺指": "MTX",
    "大台": "TX",
    "台指期": "TX",
    "臺指期": "TX",
    "台指選": "TXO",
    "臺指選": "TXO",
}
_ROOTS = (("TXO", "TXO"), ("TXF", "TX"), ("TX", "TX"), ("MXF", "MTX"), ("MTX", "MTX"), ("TMF", "TMF"), ("FMF", "FMF"))


@dataclass
class LedgerQuote:
    ok: bool
    balance: float | None
    source: str
    label: str
    calibrated_on: date | None = None
    stale: bool = False
    base: float | None = None
    realized: float = 0.0
    fees: float = 0.0
    tax: float = 0.0
    cashflow: float = 0.0
    margin_note: float | None = None
    # 每口原始保證金（新台幣），key 為 inventory_key（TX、MTX、TMF、FMF…）。
    margin_per_lot: dict[str, float] = field(default_factory=dict)
    # 每個商品的來源說明，例如「校正 35,050 ÷ 1 口」或「設定列」。
    margin_basis: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        if not self.ok or self.base is None:
            return ""
        return (
            f"校正 {self.base:,.0f}"
            f" ＋已實現 {self.realized:,.2f}"
            f" −手續費 {self.fees:,.2f}"
            f" −期交稅 {self.tax:,.2f}"
            f" ＋出入金 {self.cashflow:,.2f}"
        )


@dataclass
class _Book:
    qty: float = 0.0
    avg: float | None = None


def template_ledger_frame() -> pd.DataFrame:
    """給寫入助理的 Futures_Ledger 範本。含目前校正點與手續費、期交稅預設。"""
    rows = [dict(SEED_CALIBRATION)]
    for symbol, fee in DEFAULT_FEE_PER_LOT.items():
        note = "每口單邊手續費。稅率：期貨乘契約金額，選擇權乘權利金。"
        if symbol == "*":
            note = "個股期與未列名。代號 S+證券代號（小型）乘數 100，其餘個股期乘數 2000。"
        rows.append(
            {
                "類型": "設定",
                "日期": None,
                "本日餘額": None,
                "商品代號": symbol,
                "買賣": None,
                "口數": None,
                "均價": None,
                "金額": None,
                "手續費預設": fee,
                "稅率預設": DEFAULT_TAX_RATE[symbol],
                "原始保證金": None,
                "備註": note,
            }
        )
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def template_trade_frame() -> pd.DataFrame:
    """只列出可追加的欄。不要用這張表覆蓋既有 Trade_Log。"""
    return pd.DataFrame(columns=list(APPENDED_TRADE_HEADERS))


def symbol_root(symbol: str) -> str:
    text = str(symbol or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    if text in _NAME_ALIASES:
        return _NAME_ALIASES[text]
    # 中文別名可能沒被 upper 影響，再對原始字串查一次。
    raw = str(symbol or "").strip()
    if raw in _NAME_ALIASES:
        return _NAME_ALIASES[raw]
    sf = resolve_stock_future(raw)
    if sf is not None:
        # 同一檔個股期的各種寫法（小國巨期／S2327）併成同一個庫存鍵。
        return ("S" if sf.small else "") + sf.code + ("" if sf.small else "F")
    for prefix, root in _ROOTS:
        if text == prefix or text.startswith(prefix):
            return root
    return text


def inventory_key(symbol: str) -> str:
    root = symbol_root(symbol)
    if root in INDEX_MULTIPLIER and root != "TXO":
        return root
    return str(symbol or "").strip().upper().replace(" ", "")


def contract_multiplier(symbol: str) -> float:
    root = symbol_root(symbol)
    if root in INDEX_MULTIPLIER:
        return INDEX_MULTIPLIER[root]
    sf = resolve_stock_future(symbol)
    if sf is not None:
        return float(sf.multiplier)
    text = str(symbol or "").strip().upper().replace(" ", "")
    if len(text) >= 2 and text[0] == "S" and text[1:].isdigit():
        return SMALL_STOCK_FUTURES_MULTIPLIER
    return STOCK_FUTURES_MULTIPLIER


def _is_option(symbol: str) -> bool:
    return symbol_root(symbol) == "TXO"


def quote_yuanta_ledger(
    ledger: pd.DataFrame | None,
    trades: pd.DataFrame | None,
    positions: pd.DataFrame | None = None,
    today: date | None = None,
) -> LedgerQuote:
    """由分頁算出本日餘額。沒有校正點時 ok=False，呼叫端退回 B7。"""
    today = taipei_today(today)
    frame = _prepare(ledger)
    if frame is None or frame.empty or "類型" not in frame.columns:
        return LedgerQuote(False, None, "b7", "Futures_Ledger 缺少校正點，已退回 Active_Positions 現金餘額（B7）")

    warnings: list[str] = []
    fees = dict(DEFAULT_FEE_PER_LOT)
    taxes = dict(DEFAULT_TAX_RATE)
    calibrations: list[dict] = []
    snapshots: list[dict] = []
    flows: list[dict] = []
    explicit_margin: dict[str, float] = {}

    for _, row in frame.iterrows():
        kind = _kind(row.get("類型"))
        if kind == "設定":
            key = str(row.get("商品代號") or "").strip() or "*"
            key = "*" if key == "*" else symbol_root(key)
            fee = _optional_number(row.get("手續費預設"))
            rate = _optional_number(row.get("稅率預設"))
            per_lot_margin = _optional_number(row.get("原始保證金"))
            if per_lot_margin is not None and per_lot_margin > 0 and key != "*":
                explicit_margin[inventory_key(key)] = per_lot_margin
            if fee is not None:
                fees[key] = fee
            if rate is not None:
                taxes[key] = rate
            continue
        if kind == "校正":
            day = parse_sheet_date(row.get("日期"))
            balance = _optional_number(row.get("本日餘額"))
            if day is None or balance is None:
                warnings.append("有一列校正缺少日期或本日餘額，已略過。")
                continue
            calibrations.append({"date": day, "balance": balance, "row": row})
            if str(row.get("商品代號") or "").strip():
                snapshots.append({"date": day, "row": row})
            continue
        if kind == "持倉":
            day = parse_sheet_date(row.get("日期"))
            snapshots.append({"date": day, "row": row})
            continue
        if kind in {"出入金", "入金", "出金"}:
            day = parse_sheet_date(row.get("日期"))
            amount = _optional_number(row.get("金額"))
            if day is None or amount is None:
                warnings.append("有一列出金或入金缺少日期或金額，已略過。")
                continue
            if kind == "出金" and amount > 0:
                amount = -amount
            elif kind == "入金" and amount < 0:
                amount = -amount
            flows.append({"date": day, "amount": amount})

    if not calibrations:
        return LedgerQuote(
            False,
            None,
            "b7",
            "Futures_Ledger 缺少校正點，已退回 Active_Positions 現金餘額（B7）",
            margin_per_lot=dict(explicit_margin),
            margin_basis={key: "設定列每口原始保證金" for key in explicit_margin},
            warnings=warnings,
        )

    calibrations.sort(key=lambda item: item["date"])
    current = calibrations[-1]
    cal_day: date = current["date"]
    books = _books_at(cal_day, snapshots, positions, warnings)
    margin = _optional_number(current["row"].get("原始保證金"))
    cal_lots = {key: abs(book.qty) for key, book in books.items() if abs(book.qty) > 1e-9}
    per_lot, basis = derive_margin_per_lot(margin, cal_lots, explicit_margin)
    realized = 0.0
    fee_total = 0.0
    tax_total = 0.0
    trade_frame = _prepare(trades)
    if trade_frame is not None and not trade_frame.empty:
        parsed_rows = []
        for _, row in trade_frame.iterrows():
            if not _is_yuanta_account(row.get("帳戶")):
                continue
            if not _is_listed_derivative(row):
                continue
            day = parse_sheet_date(row.get("日期"))
            if day is None:
                continue
            parsed_rows.append((day, row))
        for day, row in sorted(parsed_rows, key=lambda item: item[0]):
            if day <= cal_day:
                continue
            pnl, fee, tax, note = _apply_trade(books, row, fees, taxes)
            if note:
                warnings.append(note)
            realized += pnl
            fee_total += fee
            tax_total += tax

    cashflow = sum(item["amount"] for item in flows if item["date"] > cal_day)
    balance = float(current["balance"]) + realized - fee_total - tax_total + cashflow
    stale = (today - cal_day).days > STALE_AFTER_DAYS
    label = f"自動推算（最後校正日 {cal_day.isoformat()}）"
    return LedgerQuote(
        True,
        balance,
        "ledger",
        label,
        calibrated_on=cal_day,
        stale=stale,
        base=float(current["balance"]),
        realized=realized,
        fees=fee_total,
        tax=tax_total,
        cashflow=cashflow,
        margin_note=margin,
        margin_per_lot=per_lot,
        margin_basis=basis,
        warnings=warnings,
    )


def derive_margin_per_lot(
    total_margin: float | None,
    lots: dict[str, float],
    explicit: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """由校正列的原始保證金（帳戶總額）推每口保證金。

    * 設定列有填「原始保證金」的商品，直接當每口保證金（優先）。
    * 校正當下只有一種期貨（例如微台 1 口）：總額 ÷ 口數。
    * 多種期貨：扣掉有設定列的商品後，只剩一種沒設定時，用剩下的總額 ÷ 口數；
      否則無法拆分，只回傳設定列的值。
    選擇權（TXO）不收原始保證金（買方），不參與拆分。
    """
    per_lot: dict[str, float] = {}
    basis: dict[str, str] = {}
    for key, value in (explicit or {}).items():
        if value is not None and value > 0:
            per_lot[key] = float(value)
            basis[key] = "設定列每口原始保證金"
    if total_margin is None or total_margin <= 0:
        return per_lot, basis
    futures_lots = {
        key: qty for key, qty in (lots or {}).items() if qty and qty > 0 and symbol_root(key) != "TXO"
    }
    if not futures_lots:
        return per_lot, basis
    unknown = [key for key in futures_lots if key not in per_lot]
    if len(unknown) != 1:
        return per_lot, basis
    target = unknown[0]
    known_total = sum(per_lot[key] * qty for key, qty in futures_lots.items() if key in per_lot)
    remainder = float(total_margin) - known_total
    qty = futures_lots[target]
    if remainder <= 0 or qty <= 0:
        return per_lot, basis
    per_lot[target] = remainder / qty
    if known_total > 0:
        basis[target] = f"校正原始保證金 {total_margin:,.0f} − 其他商品 {known_total:,.0f}，÷ {qty:g} 口"
    else:
        basis[target] = f"校正原始保證金 {total_margin:,.0f} ÷ {qty:g} 口"
    return per_lot, basis


def apply_yuanta_ledger(
    accounts: pd.DataFrame,
    ledger: pd.DataFrame | None,
    trades: pd.DataFrame | None,
    positions: pd.DataFrame | None = None,
    today: date | None = None,
) -> pd.DataFrame:
    """把推算結果寫進元大期貨列，供 finalize_valuation 當成現金餘額。

    推算失敗時不改現金，只標上退回 B7。沒有元大列就原樣退回。
    """
    if accounts is None or accounts.empty or "帳戶名稱" not in accounts.columns:
        return accounts
    out = accounts.copy()
    out["帳戶名稱"] = out["帳戶名稱"].astype(str).str.strip()
    if "現金餘額" not in out.columns:
        out["現金餘額"] = 0.0
    hits = [idx for idx in out.index if "元大期貨" in str(out.at[idx, "帳戶名稱"])]
    quote = quote_yuanta_ledger(ledger, trades, positions, today=today)
    out["_yuanta_source"] = ""
    out["_yuanta_label"] = ""
    out["_yuanta_calibrated_on"] = ""
    out["_yuanta_stale"] = False
    out["_yuanta_base"] = pd.NA
    out["_yuanta_realized"] = 0.0
    out["_yuanta_fees"] = 0.0
    out["_yuanta_tax"] = 0.0
    out["_yuanta_flow"] = 0.0
    out["_yuanta_detail"] = ""
    out["_yuanta_warning"] = ""
    out["_yuanta_margin_total"] = pd.NA
    out["_yuanta_margin_per_lot"] = ""
    if not hits:
        return out
    if len(hits) > 1:
        quote.warnings.append("找到多列元大期貨，自動推算只寫入第一列。")
    target = hits[0]
    if quote.ok and quote.balance is not None:
        out.at[target, "現金餘額"] = float(quote.balance)
        source = "ledger"
        label = quote.label
    else:
        source = "b7" if _cash_usable(out.at[target, "現金餘額"]) else "fallback"
        label = quote.label
        if source == "fallback":
            label = "Futures_Ledger 缺少校正點，且現金餘額無效，已改用備援常數"
    out.at[target, "_yuanta_source"] = source
    out.at[target, "_yuanta_label"] = label
    out.at[target, "_yuanta_calibrated_on"] = "" if quote.calibrated_on is None else quote.calibrated_on.isoformat()
    out.at[target, "_yuanta_stale"] = bool(quote.stale)
    if quote.base is not None:
        out.at[target, "_yuanta_base"] = float(quote.base)
    out.at[target, "_yuanta_realized"] = float(quote.realized)
    out.at[target, "_yuanta_fees"] = float(quote.fees)
    out.at[target, "_yuanta_tax"] = float(quote.tax)
    out.at[target, "_yuanta_flow"] = float(quote.cashflow)
    out.at[target, "_yuanta_detail"] = quote.detail
    out.at[target, "_yuanta_warning"] = "；".join(quote.warnings)
    if quote.margin_note is not None:
        out.at[target, "_yuanta_margin_total"] = float(quote.margin_note)
    out.at[target, "_yuanta_margin_per_lot"] = _encode_margin(quote.margin_per_lot, quote.margin_basis)
    return out


def _encode_margin(per_lot: dict[str, float], basis: dict[str, str]) -> str:
    if not per_lot:
        return ""
    payload = {key: {"per_lot": float(value), "basis": basis.get(key, "")} for key, value in per_lot.items()}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _decode_margin(text) -> tuple[dict[str, float], dict[str, str]]:
    raw = _cell_text(text)
    if not raw:
        return {}, {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}, {}
    per_lot: dict[str, float] = {}
    basis: dict[str, str] = {}
    if not isinstance(payload, dict):
        return {}, {}
    for key, item in payload.items():
        if not isinstance(item, dict):
            continue
        value = _optional_number(item.get("per_lot"))
        if value is None or value <= 0:
            continue
        per_lot[str(key)] = value
        basis[str(key)] = str(item.get("basis") or "")
    return per_lot, basis


def ledger_metrics_from_accounts(account_data: pd.DataFrame) -> dict:
    """從帳戶表的標記欄取出給第四頁與指標用的字典。"""
    empty = {
        "futures_cash_source": "",
        "futures_cash_label": "",
        "futures_cash_calibrated_on": "",
        "futures_cash_stale": False,
        "futures_cash_base": None,
        "futures_cash_realized": 0.0,
        "futures_cash_fees": 0.0,
        "futures_cash_tax": 0.0,
        "futures_cash_flow": 0.0,
        "futures_cash_detail": "",
        "futures_cash_warning": "",
        "futures_ledger_margin_total": None,
        "futures_ledger_margin_per_lot": {},
        "futures_ledger_margin_basis": {},
    }
    if account_data is None or account_data.empty or "_yuanta_source" not in account_data.columns:
        return empty
    tagged = account_data[account_data["_yuanta_source"].astype(str).str.strip() != ""]
    if tagged.empty:
        return empty
    row = tagged.iloc[0]
    base = row.get("_yuanta_base")
    try:
        base_num = None if pd.isna(base) else float(base)
    except (TypeError, ValueError):
        base_num = None
    stale_raw = row.get("_yuanta_stale")
    stale = bool(stale_raw) if not (isinstance(stale_raw, float) and math.isnan(stale_raw)) else False
    margin_total = _optional_number(row.get("_yuanta_margin_total"))
    margin_per_lot, margin_basis = _decode_margin(row.get("_yuanta_margin_per_lot"))
    return {
        "futures_cash_source": str(row.get("_yuanta_source") or ""),
        "futures_cash_label": str(row.get("_yuanta_label") or ""),
        "futures_cash_calibrated_on": str(row.get("_yuanta_calibrated_on") or ""),
        "futures_cash_stale": stale,
        "futures_cash_base": base_num,
        "futures_cash_realized": float(row.get("_yuanta_realized") or 0.0),
        "futures_cash_fees": float(row.get("_yuanta_fees") or 0.0),
        "futures_cash_tax": float(row.get("_yuanta_tax") or 0.0),
        "futures_cash_flow": float(row.get("_yuanta_flow") or 0.0),
        "futures_cash_detail": str(row.get("_yuanta_detail") or ""),
        "futures_cash_warning": str(row.get("_yuanta_warning") or ""),
        "futures_ledger_margin_total": margin_total,
        "futures_ledger_margin_per_lot": margin_per_lot,
        "futures_ledger_margin_basis": margin_basis,
    }


def _books_at(cal_day: date, snapshots: list[dict], positions: pd.DataFrame | None, warnings: list[str]) -> dict[str, _Book]:
    books: dict[str, _Book] = {}
    sheet_avg = _position_avg_map(positions)
    for item in snapshots:
        day = item["date"]
        if day is not None and day != cal_day:
            continue
        row = item["row"]
        symbol = str(row.get("商品代號") or "").strip()
        if not symbol:
            continue
        key = inventory_key(symbol)
        qty = _optional_number(row.get("口數"))
        avg = _optional_number(row.get("均價"))
        side = _side(row.get("買賣"))
        if qty is None:
            warnings.append(f"校正持倉 {symbol} 沒有口數，已略過。")
            continue
        signed = abs(qty) * (1.0 if side >= 0 else -1.0)
        if avg is None and key in sheet_avg:
            avg = sheet_avg[key][1]
            sheet_qty = sheet_avg[key][0]
            warnings.append(f"校正列 {symbol} 未填均價，暫用持倉統計的平均成本 {avg:g}。")
            if sheet_qty is not None and abs(abs(sheet_qty) - abs(qty)) > 1e-6:
                warnings.append(f"持倉統計 {symbol} 口數 {sheet_qty:g} 與校正口數 {qty:g} 不同，均價可能不是校正當下的成本。")
        elif avg is None:
            warnings.append(f"校正持倉 {symbol} 沒有均價，之後的平倉損益先不計入。")
        book = books.setdefault(key, _Book())
        if book.qty == 0 or book.avg is None:
            book.qty = signed
            book.avg = avg
        elif signed * book.qty > 0 and avg is not None and book.avg is not None:
            new_abs = abs(book.qty) + abs(signed)
            book.avg = (abs(book.qty) * book.avg + abs(signed) * avg) / new_abs
            book.qty += signed
        else:
            book.qty = signed
            book.avg = avg
    if not snapshots:
        for key, (qty, avg) in sheet_avg.items():
            if qty is None or avg is None:
                continue
            books[key] = _Book(qty=float(qty), avg=float(avg))
            warnings.append(f"校正點沒有寫持倉，暫用持倉統計 {key} {qty:g} 口、均價 {avg:g}。")
    return books


def _position_avg_map(positions: pd.DataFrame | None) -> dict[str, tuple[float | None, float | None]]:
    found: dict[str, tuple[float | None, float | None]] = {}
    frame = positions
    if frame is None or frame.empty:
        return found
    symbol_col = "標的" if "標的" in frame.columns else ("商品代號" if "商品代號" in frame.columns else None)
    if symbol_col is None:
        return found
    for _, row in frame.iterrows():
        if "帳戶" in frame.columns and not _is_yuanta_account(row.get("帳戶")):
            continue
        kind = str(row.get("種類") or "")
        if kind and "期" not in kind and "選" not in kind:
            continue
        symbol = str(row.get(symbol_col) or "").strip()
        if not symbol:
            continue
        key = inventory_key(symbol)
        qty = _optional_number(row.get("持有數量"))
        if qty is None:
            qty = _optional_number(row.get("口數"))
        avg = _optional_number(row.get("平均成本"))
        if avg is None:
            avg = _optional_number(row.get("均價"))
        found[key] = (qty, avg)
    return found


def _apply_trade(books: dict[str, _Book], row, fees: dict[str, float], taxes: dict[str, float]) -> tuple[float, float, float, str]:
    symbol = str(row.get("商品代號") or "").strip()
    qty = _optional_number(row.get("口數"))
    price = _optional_number(row.get("價格"))
    recorded = _optional_number(row.get("已實現損益"))
    if not symbol or qty is None or qty == 0:
        return 0.0, 0.0, 0.0, ""
    if price is None and recorded is None:
        return 0.0, 0.0, 0.0, f"{symbol} 沒有成交價，也沒有已實現損益，已略過。"
    key = inventory_key(symbol)
    root = symbol_root(symbol)
    side, action = _trade_side_action(row.get("買賣"), row.get("類型"), row.get("開平倉"))
    book = books.setdefault(key, _Book())
    listed_mult = _optional_number(row.get("合約乘數"))
    multiplier = listed_mult if listed_mult is not None and listed_mult > 0 else contract_multiplier(symbol)
    note = ""
    missing_avg = False
    if action == "close" and book.qty == 0:
        note = f"{symbol} 標記平倉但校正後沒有部位，這筆改當開倉。"
        action = "open"
    elif action == "close" and book.avg is None:
        missing_avg = True
    fill_price = price if price is not None else (book.avg or 0.0)
    computed = _fill(book, side, abs(qty), fill_price, action, multiplier)
    if recorded is not None:
        explicit_fee = _optional_number(row.get("手續費"))
        explicit_tax = _optional_number(row.get("稅"))
        return float(recorded), abs(explicit_fee or 0.0), abs(explicit_tax or 0.0), note
    if computed is None:
        computed = 0.0
        missing_avg = True
    if missing_avg:
        note = f"平倉 {symbol} 缺少校正均價，已實現損益欄也是空白，這筆損益未計入。"
    fee = _optional_number(row.get("手續費"))
    if fee is None:
        per_lot = fees.get(root, fees.get("*", DEFAULT_FEE_PER_LOT["*"]))
        fee = per_lot * abs(qty)
    tax = _optional_number(row.get("稅"))
    if tax is None:
        rate = taxes.get(root, taxes.get("*", DEFAULT_TAX_RATE["*"]))
        basis = abs(price or 0.0) * multiplier * abs(qty)
        tax = basis * rate
    return float(computed), abs(float(fee)), abs(float(tax)), note


def _is_listed_derivative(row) -> bool:
    """股票成交留在 Trade_Log，但不進元大期貨餘額。種類空白時仍照算。

    認的是「種類」（台股期貨、台股、美股），不是「類型」（開倉／平倉）。
    """
    kind = _cell_text(row.get("商品種類")) or _cell_text(row.get("種類"))
    if not kind:
        return True
    return ("期" in kind) or ("選" in kind)


def _cell_text(value) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
    except TypeError:
        if value is None:
            return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _trade_side_action(action_text, type_text, open_close_text) -> tuple[int, str]:
    """既有「動作」：買進、賣出、放空、回補。類型欄若寫開倉／平倉才覆蓋。"""
    action = str(action_text or "").strip()
    kind = str(type_text or "").strip() or str(open_close_text or "").strip()
    if action in {"買進", "買入"}:
        side, inferred = 1, "open"
    elif action in {"放空"}:
        side, inferred = -1, "open"
    elif action in {"回補"}:
        side, inferred = 1, "close"
    elif action in {"賣出", "賣"}:
        side, inferred = -1, "close"
    else:
        side = _side(action)
        inferred = _open_close(kind if kind else action)
        return side, inferred
    if kind in {"平倉", "平", "回補"}:
        return side, "close"
    if kind in {"開倉", "開", "新倉", "放空"}:
        return side, "open"
    return side, inferred


def _fill(book: _Book, side: int, qty: float, price: float, action: str, multiplier: float) -> float | None:
    if action == "auto":
        if book.qty == 0 or book.qty * side > 0:
            action = "open"
        else:
            action = "close"
    if action == "open":
        _add(book, side * qty, price)
        return 0.0
    if book.avg is None:
        if book.qty == 0:
            _add(book, side * qty, price)
            return 0.0
        _reduce_qty_only(book, side, qty, price)
        return None
    if book.qty > 0 and side < 0:
        return _close_long(book, qty, price, multiplier)
    if book.qty < 0 and side > 0:
        return _close_short(book, qty, price, multiplier)
    _add(book, side * qty, price)
    return 0.0


def _add(book: _Book, signed_qty: float, price: float) -> None:
    if book.qty == 0 or book.avg is None or signed_qty * book.qty < 0:
        book.qty = signed_qty
        book.avg = price
        return
    new_abs = abs(book.qty) + abs(signed_qty)
    book.avg = (abs(book.qty) * book.avg + abs(signed_qty) * price) / new_abs
    book.qty += signed_qty


def _close_long(book: _Book, qty: float, price: float, multiplier: float) -> float:
    close_qty = min(qty, book.qty)
    pnl = (price - float(book.avg)) * close_qty * multiplier
    book.qty -= close_qty
    remain = qty - close_qty
    if book.qty <= 1e-9:
        book.qty = 0.0
        book.avg = None
    if remain > 0:
        _add(book, -remain, price)
    return pnl


def _close_short(book: _Book, qty: float, price: float, multiplier: float) -> float:
    close_qty = min(qty, abs(book.qty))
    pnl = (float(book.avg) - price) * close_qty * multiplier
    book.qty += close_qty
    remain = qty - close_qty
    if abs(book.qty) <= 1e-9:
        book.qty = 0.0
        book.avg = None
    if remain > 0:
        _add(book, remain, price)
    return pnl


def _reduce_qty_only(book: _Book, side: int, qty: float, price: float) -> None:
    if book.qty > 0 and side < 0:
        book.qty = max(0.0, book.qty - qty)
    elif book.qty < 0 and side > 0:
        book.qty = min(0.0, book.qty + qty)
    else:
        _add(book, side * qty, price)
        return
    if abs(book.qty) <= 1e-9:
        book.qty = 0.0
        book.avg = None


def _prepare(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None:
        return None
    if not isinstance(frame, pd.DataFrame):
        return None
    if frame.empty:
        return frame
    out = frame.copy()
    renamed = {}
    seen = {str(column).strip() for column in out.columns}
    for column in out.columns:
        text = str(column).strip()
        # 種類、總金額保持原名，避免被當成「類型」或出入金「金額」。
        target = text if text in _DO_NOT_RENAME else _HEADER_ALIASES.get(text, text)
        if target != text and target in seen:
            target = text
        renamed[column] = target
        seen.add(target)
    out = out.rename(columns=renamed)
    return out


def _kind(value) -> str:
    text = str(value or "").strip()
    aliases = {
        "calibration": "校正",
        "setting": "設定",
        "cash": "出入金",
        "deposit": "入金",
        "withdraw": "出金",
        "withdrawal": "出金",
        "position": "持倉",
    }
    return aliases.get(text.lower(), text)


def _side(value) -> int:
    text = str(value or "").strip().lower()
    if text in {"賣", "s", "sell", "空", "short"}:
        return -1
    return 1


def _open_close(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"平倉", "平", "c", "close", "沖銷", "沖"}:
        return "close"
    if text in {"開倉", "開", "o", "open", "新倉"}:
        return "open"
    return "auto"


def _is_yuanta_account(value) -> bool:
    text = _cell_text(value)
    if not text or text.lower() == "none":
        return True
    return "元大" in text


def _optional_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text or text.lower() in {"nan", "none"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cash_usable(value) -> bool:
    number = _optional_number(value)
    return number is not None and number > 0
