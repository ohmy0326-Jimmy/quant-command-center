"""淨值情境模擬（策略模擬器）。

純函式、不碰 Streamlit、不讀金鑰。輸入是第四頁 `finalize_valuation`
（與 `/v1/portfolio` 同一套）已經算好的持倉與指標。

現金用「融資殘值」＝淨值 − 股票／ETF／選擇權市值，不用 API 的現金水位。
期貨只以名目計曝險，損益進出殘值；槓桿 ETF 的淨值用市值、曝險再乘倍數；
選擇權用 Delta。詳見 `cash_basis_text`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from portfolio_risk import futures_multiplier, is_option_row, is_taiex_future

SEED = 20261103
N_PATHS = 2000
N_DAYS = 126
TRADING_DAYS = 252

FEE = 0.01 / TRADING_DAYS
COST = 0.003
BORROW = 0.05 / TRADING_DAYS
CASH_RATE = 0.01 / TRADING_DAYS
RISK_FREE = 0.01
MEM_BETA = 1.3
MEM_IDIO = 0.25
PREHISTORY = 60
COOLDOWN = 5
MC_DROP = 0.90
MC_LEVERAGE = 2.0

DEFAULT_VOL_TW = 0.22
DEFAULT_VOL_SOX = 0.45
DEFAULT_RHO = 0.75

GROUPS = ("futures", "lev3", "lev2", "stock")
GROUP_LABELS = {
    "futures": "期貨",
    "lev3": "3x",
    "lev2": "2x",
    "stock": "個股",
}

TRIGGER_MA20 = "ma20_two_days"
TRIGGER_MA60 = "ma60_first_profit"
TRIGGER_CROSS = "ma20_cross_ma60"
TRIGGERS = (TRIGGER_MA20, TRIGGER_MA60, TRIGGER_CROSS)
TRIGGER_LABELS = {
    TRIGGER_MA20: "20MA 連 2 日",
    TRIGGER_MA60: "站上 60MA 且首批獲利",
    TRIGGER_CROSS: "20MA 上穿 60MA",
}

# 固定袖套。同一袖套內的槓桿取曝險加權平均（部位同質時就是該倍數）。
KEYS = (
    "fut_tw",
    "fut_other",
    "tw_3x",
    "us_3x",
    "tw_2x",
    "us_2x",
    "us_2x_mem",
    "tw_stock",
    "us_stock",
)
GROUP_OF = {
    "fut_tw": "futures",
    "fut_other": "futures",
    "tw_3x": "lev3",
    "us_3x": "lev3",
    "tw_2x": "lev2",
    "us_2x": "lev2",
    "us_2x_mem": "lev2",
    "tw_stock": "stock",
    "us_stock": "stock",
}
FACTOR_OF = {
    "fut_tw": "tw",
    "fut_other": "tw",
    "tw_3x": "tw",
    "us_3x": "sox",
    "tw_2x": "tw",
    "us_2x": "sox",
    "us_2x_mem": "mem",
    "tw_stock": "tw",
    "us_stock": "sox",
}
MODE_OF = {
    "fut_tw": "futures",
    "fut_other": "futures",
    "tw_3x": "etf",
    "us_3x": "etf",
    "tw_2x": "etf",
    "us_2x": "etf",
    "us_2x_mem": "etf",
    "tw_stock": "stock",
    "us_stock": "stock",
}
SLEEVE_LABELS = {
    "fut_tw": "台指期名目",
    "fut_other": "個股期名目",
    "tw_3x": "台股 3x 市值",
    "us_3x": "美股／費半 3x 市值",
    "tw_2x": "台股 2x 市值",
    "us_2x": "美股／費半 2x 市值",
    "us_2x_mem": "記憶體 2x 市值",
    "tw_stock": "台股個股市值",
    "us_stock": "美股個股市值",
    "opt_tw": "台指選擇權市值",
    "opt_us": "美股選擇權市值",
}
_ASSET_KEYS = tuple(k for k in KEYS if MODE_OF[k] != "futures")
_MEM_TICKERS = {"MUU", "SNXX", "AMUU"}

_FONT_READY = False


@dataclass(frozen=True)
class Scenario:
    name: str
    tw_target: float
    sox_target: float
    vol_mult: float
    weight: float = 1.0

    def __post_init__(self):
        if self.vol_mult <= 0:
            raise ValueError("波動倍數要大於 0")
        if self.tw_target <= -1 or self.sox_target <= -1:
            raise ValueError("六個月目標不能小於等於 -100%")


DEFAULT_SCENARIOS = (
    Scenario("反彈 Rebound", 0.12, 0.25, 1.0, 0.30),
    Scenario("盤整震盪 Range/chop", 0.0, 0.0, 1.2, 0.45),
    Scenario("續跌 Decline", -0.15, -0.30, 1.25, 0.25),
)


@dataclass(frozen=True)
class Strategy:
    """一組可比較的交易規則。`hold=True` 是續抱，其餘參數不生效。"""

    name: str
    hold: bool = False
    target_leverage: float = 1.8
    reduce_priority: tuple[str, ...] = GROUPS
    triggers: tuple[str, ...] = TRIGGERS
    n_batches: int = 3
    stop_stock: float = -0.08
    stop_2x: float = -0.08
    stop_3x: float = -0.08
    hedge_enabled: bool = False
    hedge_ratio: float = 0.0
    hedge_long_strike: float = 0.95
    hedge_short_strike: float = 0.85
    brake_enabled: bool = True
    brake_pause: float = -0.08
    brake_cut: float = -0.12
    brake_release: float = -0.08

    def __post_init__(self):
        if not self.name or not str(self.name).strip():
            raise ValueError("策略需要名稱")
        if self.hold:
            return
        if not 1.0 - 1e-9 <= float(self.target_leverage) <= 2.0 + 1e-9:
            raise ValueError("降槓目標要在 1.0x 到 2.0x")
        if tuple(self.reduce_priority) != tuple(dict.fromkeys(self.reduce_priority)):
            raise ValueError("減碼順序不能重複")
        if set(self.reduce_priority) != set(GROUPS):
            raise ValueError("減碼順序要包含期貨、3x、2x、個股各一次")
        if not self.triggers:
            raise ValueError("至少選一個回補條件")
        for trigger in self.triggers:
            if trigger not in TRIGGERS:
                raise ValueError(f"未知的回補條件：{trigger}")
        if not 1 <= int(self.n_batches) <= 6:
            raise ValueError("回補批數要在 1 到 6")
        for label, stop in (("個股", self.stop_stock), ("2x", self.stop_2x), ("3x", self.stop_3x)):
            if not -0.8 <= float(stop) < 0:
                raise ValueError(f"{label}停損要介於 -80% 與 0 之間")
        if self.hedge_enabled:
            if not 0 <= float(self.hedge_ratio) <= 1:
                raise ValueError("避險比例要介於 0% 與 100%")
            if not 0 < float(self.hedge_short_strike) < float(self.hedge_long_strike) < 1.5:
                raise ValueError("賣權價差要買較高履約、賣更低履約")
        if self.brake_enabled:
            if not (self.brake_cut < self.brake_pause < 0 and self.brake_cut < self.brake_release < 0):
                raise ValueError("煞車門檻要是負的，而且降到 1x 的回撤要比停新倉更深")


@dataclass(frozen=True)
class MarketCalib:
    vol_tw: float
    vol_sox: float
    rho: float
    pre_tw: np.ndarray
    pre_sx: np.ndarray
    source: str
    note: str


@dataclass
class Book:
    nav: float
    cash_residual: float
    reported_cash: float
    reported_cash_pct: float
    debt: float
    futures_equity: float
    excess_margin: float
    non_futures_cash: float
    yuanta_option_mv: float
    amounts: dict
    levs: dict
    betas: dict
    opt_tw_mv: float = 0.0
    opt_tw_delta: float = 0.0
    opt_us_mv: float = 0.0
    opt_us_delta: float = 0.0
    tw_hedge_base: float = 0.0
    gross_exposure: float = 0.0
    leverage: float = 0.0
    cash_identity_gap: float = 0.0
    notes: tuple[str, ...] = ()


@dataclass
class StrategyStats:
    median_end: float
    p10_end: float
    median_mdd: float
    p_dd20: float
    p_mc: float
    median_end_leverage: float


@dataclass
class SimResult:
    scenario_names: list[str]
    strategy_names: list[str]
    weights: list[float]
    stats: dict
    weighted: dict
    quantiles: dict
    nav0: float
    opening_leverage: dict
    opening_nav: dict
    market_note: str
    n_paths: int
    n_days: int
    seed: int


def strategy_hold(name: str = "A 續抱") -> Strategy:
    return Strategy(name=name, hold=True)


def strategy_replenish(name: str = "B 停損回補", target_leverage: float = 1.8) -> Strategy:
    target = float(np.clip(target_leverage, 1.0, 2.0))
    return Strategy(name=name, hold=False, target_leverage=target)


def normalize_weights(weights) -> np.ndarray:
    arr = np.asarray(list(weights), dtype=float)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("權重不能是空的")
    if np.any(~np.isfinite(arr)) or np.any(arr < 0):
        raise ValueError("權重要是非負有限數字")
    total = float(arr.sum())
    if total <= 0:
        raise ValueError("權重合計要大於 0")
    return arr / total


def default_market() -> MarketCalib:
    flat = np.ones(PREHISTORY)
    return MarketCalib(
        vol_tw=DEFAULT_VOL_TW,
        vol_sox=DEFAULT_VOL_SOX,
        rho=DEFAULT_RHO,
        pre_tw=flat.copy(),
        pre_sx=flat.copy(),
        source="default",
        note="yfinance 失敗，波動改用原型預設：台指 22%、費半 45%、相關 0.75。20／60 日均線用平坦初值。",
    )


def _fit_history(series: pd.Series) -> np.ndarray:
    values = np.asarray(series.dropna().to_numpy(dtype=float))
    values = values[np.isfinite(values) & (values > 0)]
    if values.size == 0:
        return np.ones(PREHISTORY)
    if values.size >= PREHISTORY:
        values = values[-PREHISTORY:]
    else:
        values = np.pad(values, (PREHISTORY - values.size, 0), mode="edge")
    if values[-1] == 0:
        return np.ones(PREHISTORY)
    return values / values[-1]


def _download_closes(symbol: str) -> pd.Series:
    import yfinance as yf

    frame = yf.download(symbol, period="6mo", interval="1d", auto_adjust=True, progress=False, threads=False)
    if frame is None or frame.empty:
        raise RuntimeError(f"{symbol} 沒有資料")
    close = frame["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    if close.empty:
        raise RuntimeError(f"{symbol} 收盤是空的")
    close.name = symbol
    return close


def calibrate_market(fetch=None) -> MarketCalib:
    """用 ^TWII、^SOX 近 60 個交易日估波動、相關，並把均線初值歸一到 1。失敗就用原型預設。"""
    fetch = fetch or _download_closes
    try:
        tw = fetch("^TWII")
        sx = fetch("^SOX")
        joined = pd.concat([tw.rename("tw"), sx.rename("sx")], axis=1, join="inner").dropna()
        joined = joined.tail(PREHISTORY + 5)
        if len(joined) < 40:
            raise RuntimeError("重疊交易日不足")
        rets = np.log(joined).diff().dropna()
        vol_tw = float(rets["tw"].std(ddof=1) * np.sqrt(TRADING_DAYS))
        vol_sx = float(rets["sx"].std(ddof=1) * np.sqrt(TRADING_DAYS))
        rho = float(rets["tw"].corr(rets["sx"]))
        if not all(np.isfinite(v) for v in (vol_tw, vol_sx, rho)):
            raise RuntimeError("波動不是有限數字")
        if vol_tw <= 0 or vol_sx <= 0 or vol_tw > 3 or vol_sx > 3:
            raise RuntimeError("波動超出可接受範圍")
        rho = float(np.clip(rho, -0.99, 0.99))
        pre_tw = _fit_history(joined["tw"])
        pre_sx = _fit_history(joined["sx"])
        note = (
            f"波動取自 yfinance ^TWII／^SOX 近 {len(rets)} 個交易日："
            f"台指 {vol_tw:.1%}、費半 {vol_sx:.1%}、相關 {rho:.2f}。"
            "均線用這段價格，最後一天歸一成 1。"
        )
        return MarketCalib(vol_tw, vol_sx, rho, pre_tw, pre_sx, "yfinance", note)
    except Exception:
        return default_market()


def _empty_amounts() -> dict:
    return {key: 0.0 for key in KEYS}


def _default_levs() -> dict:
    return {
        "fut_tw": 1.0,
        "fut_other": 1.0,
        "tw_3x": 3.0,
        "us_3x": 3.0,
        "tw_2x": 2.0,
        "us_2x": 2.0,
        "us_2x_mem": 2.0,
        "tw_stock": 1.0,
        "us_stock": 1.0,
    }


def _default_betas() -> dict:
    return {key: 1.0 for key in KEYS}


def _unit(key: str, lev: float) -> float:
    return 1.0 if MODE_OF[key] == "futures" else float(lev)


def _gross(amounts: dict, levs: dict, opt_tw_delta: float, opt_us_delta: float) -> float:
    total = abs(float(opt_tw_delta)) + abs(float(opt_us_delta))
    for key, amount in amounts.items():
        total += abs(float(amount)) * _unit(key, levs.get(key, 1.0))
    return float(total)


def _asset_mv(amounts: dict, opt_tw_mv: float, opt_us_mv: float) -> float:
    total = float(opt_tw_mv) + float(opt_us_mv)
    for key in _ASSET_KEYS:
        total += float(amounts.get(key, 0.0))
    return total


def _tw_hedge_base(amounts: dict, levs: dict, betas: dict, opt_tw_delta: float) -> float:
    base = 0.0
    for key in ("fut_tw", "fut_other"):
        if amounts.get(key, 0.0) > 0:
            base += float(amounts[key])
    for key in ("tw_2x", "tw_3x"):
        if amounts.get(key, 0.0) > 0:
            base += float(amounts[key]) * _unit(key, levs.get(key, 1.0))
    stock = float(amounts.get("tw_stock", 0.0))
    if stock > 0:
        base += stock * max(float(betas.get("tw_stock", 1.0)), 0.0)
    if opt_tw_delta > 0:
        base += float(opt_tw_delta)
    return float(base)


def finalize_book(book: Book) -> Book:
    book.gross_exposure = _gross(book.amounts, book.levs, book.opt_tw_delta, book.opt_us_delta)
    assets = _asset_mv(book.amounts, book.opt_tw_mv, book.opt_us_mv)
    # 呼叫端若已給定 nav（估價結果），殘值負責把恆等式補齊。
    if book.nav > 0 and abs((book.cash_residual + assets) - book.nav) > 1.0:
        book.cash_residual = float(book.nav) - assets
    else:
        book.nav = float(book.cash_residual) + assets
    book.leverage = (book.gross_exposure / book.nav) if book.nav > 0 else 0.0
    book.tw_hedge_base = _tw_hedge_base(book.amounts, book.levs, book.betas, book.opt_tw_delta)
    expected = book.non_futures_cash - book.debt + book.futures_equity - book.yuanta_option_mv
    book.cash_identity_gap = float(book.cash_residual - expected)
    return book


def make_book(
    *,
    nav: float | None = None,
    cash_residual: float = 0.0,
    amounts: dict | None = None,
    levs: dict | None = None,
    betas: dict | None = None,
    opt_tw_mv: float = 0.0,
    opt_tw_delta: float = 0.0,
    opt_us_mv: float = 0.0,
    opt_us_delta: float = 0.0,
    reported_cash: float = 0.0,
    debt: float = 0.0,
    futures_equity: float = 0.0,
    excess_margin: float = 0.0,
    non_futures_cash: float = 0.0,
    yuanta_option_mv: float = 0.0,
    notes: tuple[str, ...] = (),
) -> Book:
    merged_amounts = _empty_amounts()
    if amounts:
        merged_amounts.update({key: float(amounts.get(key, 0.0)) for key in KEYS})
    merged_levs = _default_levs()
    if levs:
        merged_levs.update({key: float(levs[key]) for key in levs if key in merged_levs})
    merged_betas = _default_betas()
    if betas:
        merged_betas.update({key: float(betas[key]) for key in betas if key in merged_betas})
    book = Book(
        nav=float(nav) if nav is not None else 0.0,
        cash_residual=float(cash_residual),
        reported_cash=float(reported_cash),
        reported_cash_pct=0.0,
        debt=float(debt),
        futures_equity=float(futures_equity),
        excess_margin=float(excess_margin),
        non_futures_cash=float(non_futures_cash),
        yuanta_option_mv=float(yuanta_option_mv),
        amounts=merged_amounts,
        levs=merged_levs,
        betas=merged_betas,
        opt_tw_mv=float(opt_tw_mv),
        opt_tw_delta=float(opt_tw_delta),
        opt_us_mv=float(opt_us_mv),
        opt_us_delta=float(opt_us_delta),
        notes=tuple(notes),
    )
    if nav is None:
        book.nav = 0.0
        book.cash_residual = float(cash_residual)
    finalize_book(book)
    if book.nav > 0:
        book.reported_cash_pct = book.reported_cash / book.nav * 100.0
    return book


def cash_basis_text(book: Book) -> str:
    lines = [
        (
            f"報表現金 NT$ {book.reported_cash:,.0f}（佔淨值 {book.reported_cash_pct:.1f}%）"
            "＝非期貨帳戶現金餘額＋期貨可動用保證金（權益數高於原始保證金才算，不足則以 0 計）。"
            f"融資／質押借款 NT$ {book.debt:,.0f} 已經從淨值扣除，不會再從這筆現金扣一次。"
        ),
        (
            f"元大期貨權益數 NT$ {book.futures_equity:,.0f}（本日餘額，空白時用備援 168,308，加上未平倉損益與選擇權市值）"
            "整筆都在淨值裡。被原始保證金鎖住的部分不算報表現金。"
        ),
        (
            f"模擬用的融資殘值是 NT$ {book.cash_residual:,.0f}＝淨值 − 股票、ETF 與選擇權市值。"
            "期貨名目不是淨值裡的資產，只有損益進出殘值。殘值為負表示淨融資，套融資利率；為正套現金利率。"
            "槓桿 ETF 進淨值的是市值，有效曝險再乘標的槓桿。"
        ),
        (
            f"這次有效曝險 NT$ {book.gross_exposure:,.0f}，合併槓桿 {book.leverage:.2f}x。"
            "所以報表現金可以是正的，同時融資殘值是負的：兩邊不是同一個欄位。"
        ),
    ]
    if abs(book.cash_identity_gap) >= 1:
        lines.append(
            f"殘值與「非期貨現金 − 融資 ＋ 期貨權益 − 元大選擇權市值」差 NT$ {book.cash_identity_gap:,.0f}。"
            "差額多半來自動用保證金以外、仍算進淨值的期貨市值，或選擇權市值在淨值裡取了絕對值。"
        )
    return "\n\n".join(lines)


def _row_float(row, col: str, default: float = 0.0) -> float:
    if col not in row or pd.isna(row[col]):
        return default
    try:
        return float(row[col])
    except (TypeError, ValueError):
        return default


def _option_multiplier(row, kind: str) -> float:
    mult = _row_float(row, "合約乘數", 0.0)
    if mult > 0:
        return mult
    return 100.0 if "美" in kind else 50.0


def _is_memory(ticker: str, name: str, sector: str) -> bool:
    base = ticker.upper().split(".")[0]
    if base in _MEM_TICKERS:
        return True
    blob = f"{name} {sector}"
    return ("記憶體" in blob) or ("memory" in blob.lower())


def _sleeve_key(kind: str, ticker: str, name: str, sector: str, lev: float) -> str:
    us = "美" in kind
    if lev >= 2.5:
        if us and _is_memory(ticker, name, sector):
            return "us_3x"
        return "us_3x" if us else "tw_3x"
    if lev >= 1.5:
        if us and _is_memory(ticker, name, sector):
            return "us_2x_mem"
        return "us_2x" if us else "tw_2x"
    return "us_stock" if us else "tw_stock"


def build_book(valued: pd.DataFrame, metrics: dict) -> Book:
    """把第四頁估價結果收成模擬袖套。曝險口徑沿用 `finalize_valuation`。"""
    metrics = metrics or {}
    nav = float(metrics.get("nav") or 0.0)
    amounts = _empty_amounts()
    lev_weight = {key: 0.0 for key in KEYS}
    lev_sum = {key: 0.0 for key in KEYS}
    beta_weight = {key: 0.0 for key in KEYS}
    beta_sum = {key: 0.0 for key in KEYS}
    opt_tw_mv = opt_us_mv = opt_tw_delta = opt_us_delta = 0.0
    yuanta_option_mv = 0.0
    asset_mv = 0.0
    notes: list[str] = []
    mixed_lev: dict[str, set] = {key: set() for key in KEYS}

    frame = valued if valued is not None else pd.DataFrame()
    if frame is not None and not frame.empty:
        for _, row in frame.iterrows():
            ticker = str(row.get("標的", "")).strip()
            kind = str(row.get("種類", ""))
            name = str(row.get("名稱", "") or "")
            sector = str(row.get("產業別", "") or "")
            account = str(row.get("帳戶", "") or "")
            qty = _row_float(row, "持有數量")
            price = _row_float(row, "即時報價")
            fx = _row_float(row, "換算匯率", 1.0) or 1.0
            lev = _row_float(row, "標的槓桿", 1.0) or 1.0
            beta = _row_float(row, "Beta值", 1.0)
            beta_exp = _row_float(row, "Beta曝險(台幣)")
            display_mv = _row_float(row, "即時市值(台幣)")
            opt = is_option_row(row)
            fut = ("期" in kind) and not opt
            yuanta = "元大期貨" in account
            mult = _row_float(row, "合約乘數", 0.0)
            if mult <= 0:
                mult = futures_multiplier(ticker) if fut or is_taiex_future(ticker) else _option_multiplier(row, kind)

            if opt:
                if yuanta:
                    contrib = price * qty * _option_multiplier(row, kind) * fx
                    yuanta_option_mv += contrib
                else:
                    contrib = display_mv
                asset_mv += contrib
                if "美" in kind:
                    opt_us_mv += contrib
                    opt_us_delta += beta_exp
                else:
                    opt_tw_mv += contrib
                    opt_tw_delta += beta_exp
                if abs(beta_exp) < 1 and abs(qty) > 0:
                    notes.append(f"{ticker} 沒有可用的 Delta，這筆選擇權曝險以 0 計。")
                continue

            if fut:
                signed_notional = qty * price * (mult if mult > 0 else futures_multiplier(ticker)) * fx
                key = "fut_tw" if is_taiex_future(ticker) else "fut_other"
                amounts[key] += signed_notional
                weight = abs(signed_notional)
                beta_weight[key] += (beta if key == "fut_other" else 1.0) * weight
                beta_sum[key] += weight
                # 元大期貨的名目不進淨值；非元大帳戶的保證金市值已在淨值裡，留在殘值，不再當資產。
                continue

            key = _sleeve_key(kind, ticker, name, sector, lev)
            signed_mv = price * qty * fx
            contrib = display_mv if display_mv else signed_mv
            if qty < 0 and contrib > 0:
                signed_mv = -abs(contrib)
            elif abs(abs(signed_mv) - abs(contrib)) <= 1.0:
                signed_mv = signed_mv if abs(signed_mv - contrib) <= 1.0 else contrib
            else:
                signed_mv = contrib if qty >= 0 else -abs(contrib)
            amounts[key] += signed_mv
            asset_mv += contrib if qty >= 0 else signed_mv
            weight = abs(signed_mv)
            use_lev = lev if MODE_OF[key] == "etf" else 1.0
            lev_weight[key] += use_lev * weight
            lev_sum[key] += weight
            mixed_lev[key].add(round(float(lev), 2))
            beta_weight[key] += beta * weight
            beta_sum[key] += weight

    levs = _default_levs()
    betas = _default_betas()
    for key in KEYS:
        if lev_sum[key] > 0:
            levs[key] = lev_weight[key] / lev_sum[key]
        if beta_sum[key] > 0:
            betas[key] = beta_weight[key] / beta_sum[key]
        if len(mixed_lev[key]) > 1:
            notes.append(f"{SLEEVE_LABELS[key]}有一種以上的標的槓桿，模擬用曝險加權平均 {levs[key]:.2f}x。")

    excess = float(metrics.get("excess_margin") or 0.0)
    reported = float(metrics.get("cash") or 0.0)
    book = make_book(
        nav=nav,
        cash_residual=nav - asset_mv,
        amounts=amounts,
        levs=levs,
        betas=betas,
        opt_tw_mv=opt_tw_mv,
        opt_tw_delta=opt_tw_delta,
        opt_us_mv=opt_us_mv,
        opt_us_delta=opt_us_delta,
        reported_cash=reported,
        debt=float(metrics.get("debt") or 0.0),
        futures_equity=float(metrics.get("futures_equity") or 0.0),
        excess_margin=excess,
        non_futures_cash=reported - max(excess, 0.0),
        yuanta_option_mv=yuanta_option_mv,
        notes=tuple(dict.fromkeys(notes)),
    )
    return book


def _norm_cdf(x):
    x = np.asarray(x, dtype=float)
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = np.where(x >= 0.0, 1.0, -1.0)
    ax = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + p * ax)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * np.exp(-(ax * ax))
    return 0.5 * (1.0 + sign * y)


def _bs_put(spot, strike: float, tau, rate: float, vol: float):
    spot = np.asarray(spot, dtype=float)
    tau = np.maximum(np.asarray(tau, dtype=float), 0.0)
    vol = max(float(vol), 1e-4)
    rate = float(rate)
    intrinsic = np.maximum(strike - spot, 0.0)
    sqrt_t = np.sqrt(np.maximum(tau, 1e-12))
    d1 = (np.log(np.maximum(spot, 1e-12) / strike) + (rate + 0.5 * vol * vol) * tau) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    price = strike * np.exp(-rate * tau) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return np.where(tau <= 1e-8, intrinsic, np.maximum(price, 0.0))


def _bs_put_delta(spot, strike: float, tau, rate: float, vol: float):
    spot = np.asarray(spot, dtype=float)
    tau = np.maximum(np.asarray(tau, dtype=float), 0.0)
    vol = max(float(vol), 1e-4)
    sqrt_t = np.sqrt(np.maximum(tau, 1e-12))
    d1 = (np.log(np.maximum(spot, 1e-12) / strike) + (rate + 0.5 * vol * vol) * tau) / (vol * sqrt_t)
    delta = _norm_cdf(d1) - 1.0
    intrinsic = np.where(spot < strike, -1.0, np.where(spot > strike, 0.0, -0.5))
    return np.where(tau <= 1e-8, intrinsic, delta)


def put_spread_value(spot, long_strike: float, short_strike: float, tau, vol: float, rate: float = RISK_FREE):
    """買較高履約、賣更低履約的賣權價差，價格用現貨的比例（1＝起始指數）。"""
    return _bs_put(spot, long_strike, tau, rate, vol) - _bs_put(spot, short_strike, tau, rate, vol)


def put_spread_delta(spot, long_strike: float, short_strike: float, tau, vol: float, rate: float = RISK_FREE):
    return _bs_put_delta(spot, long_strike, tau, rate, vol) - _bs_put_delta(spot, short_strike, tau, rate, vol)


def apply_delever(book: Book, strategy: Strategy) -> tuple[Book, dict]:
    """開盤先降到目標槓桿。回傳新帳與各袖套要回補的數量（帶方向）。"""
    budgets = {key: 0.0 for key in KEYS}
    if strategy.hold:
        return book, budgets
    amounts = {key: float(book.amounts.get(key, 0.0)) for key in KEYS}
    cash = float(book.cash_residual)
    gross0 = _gross(amounts, book.levs, book.opt_tw_delta, book.opt_us_delta)
    gap = max(0.0, gross0 - float(strategy.target_leverage) * float(book.nav))
    for group in strategy.reduce_priority:
        if gap <= 1.0:
            break
        keys = [key for key in KEYS if GROUP_OF[key] == group]
        reducible = sum(abs(amounts[key]) * _unit(key, book.levs[key]) for key in keys)
        if reducible <= 1e-8:
            continue
        cut_exp = min(gap, reducible)
        for key in keys:
            exp_k = abs(amounts[key]) * _unit(key, book.levs[key])
            if exp_k <= 0:
                continue
            reduce_amt = min(abs(amounts[key]), (cut_exp * (exp_k / reducible)) / _unit(key, book.levs[key]))
            sign = 1.0 if amounts[key] >= 0 else -1.0
            amounts[key] -= sign * reduce_amt
            budgets[key] += sign * reduce_amt
            if MODE_OF[key] == "futures":
                cash -= COST * reduce_amt
            elif sign > 0:
                cash += reduce_amt * (1.0 - COST)
            else:
                cash -= reduce_amt * (1.0 + COST)
        gap -= cut_exp
    traded = make_book(
        nav=None,
        cash_residual=cash,
        amounts=amounts,
        levs=book.levs,
        betas=book.betas,
        opt_tw_mv=book.opt_tw_mv,
        opt_tw_delta=book.opt_tw_delta,
        opt_us_mv=book.opt_us_mv,
        opt_us_delta=book.opt_us_delta,
        reported_cash=book.reported_cash,
        debt=book.debt,
        futures_equity=book.futures_equity,
        excess_margin=book.excess_margin,
        non_futures_cash=book.non_futures_cash,
        yuanta_option_mv=book.yuanta_option_mv,
        notes=book.notes,
    )
    # make_book 在 nav=None 時用殘值重算淨值，交易成本會讓淨值略降。
    return traded, budgets


def _stop_line(strategy: Strategy, key: str) -> float:
    group = GROUP_OF[key]
    if group == "lev3":
        return float(strategy.stop_3x)
    if group == "lev2":
        return float(strategy.stop_2x)
    return float(strategy.stop_stock)


def _factor_array(key: str, r_tw, r_sox, r_mem, beta: float):
    factor = FACTOR_OF[key]
    if factor == "tw":
        base = r_tw
    elif factor == "mem":
        base = r_mem
    else:
        base = r_sox
    if MODE_OF[key] == "stock" or key == "fut_other":
        return beta * base
    return base


def _grow(mode: str, amount, ret, lev: float):
    if mode == "futures":
        pnl = amount * ret
        return amount * (1.0 + ret), pnl
    if mode == "etf":
        return amount * (1.0 + lev * ret - FEE), np.zeros_like(amount)
    return amount * (1.0 + ret), np.zeros_like(amount)


def _cash_for_close(mode: str, removed):
    """`removed` 是從部位拿掉的數量（多單為正）。"""
    if mode == "futures":
        return -COST * np.abs(removed)
    return np.where(removed >= 0.0, removed * (1.0 - COST), removed * (1.0 + COST))


def _cash_for_open(mode: str, added):
    if mode == "futures":
        return -COST * np.abs(added)
    return -np.where(added >= 0.0, added * (1.0 + COST), added * (1.0 - COST))


class _PathState:
    def __init__(self, n: int, book: Book, budgets: dict, n_batches: int, pre_tw, pre_sx):
        self.n = n
        self.cash = np.full(n, book.cash_residual)
        self.core = {key: np.full(n, float(book.amounts.get(key, 0.0))) for key in KEYS}
        self.batch = np.zeros((n, len(KEYS), n_batches))
        self.entry = np.zeros((n, len(KEYS), n_batches))
        self.cool = np.zeros((n, len(KEYS), n_batches))
        self.budgets = budgets
        self.opt_tw_mv = np.full(n, book.opt_tw_mv)
        self.opt_tw_delta = np.full(n, book.opt_tw_delta)
        self.opt_us_mv = np.full(n, book.opt_us_mv)
        self.opt_us_delta = np.full(n, book.opt_us_delta)
        self.tw = np.ones(n)
        self.sx = np.ones(n)
        self.hist_tw = np.tile(_as_prehistory(pre_tw), (n, 1))
        self.hist_sx = np.tile(_as_prehistory(pre_sx), (n, 1))
        self.up_tw = np.zeros(n)
        self.up_sx = np.zeros(n)
        self.hedge_notional = 0.0
        self.hard = np.zeros(n, dtype=bool)

    def shift_history(self, which: str, price):
        hist = self.hist_tw if which == "tw" else self.hist_sx
        hist[:, :-1] = hist[:, 1:]
        hist[:, -1] = price

    def gross(self) -> np.ndarray:
        total = np.abs(self.opt_tw_delta) + np.abs(self.opt_us_delta)
        for idx, key in enumerate(KEYS):
            unit = _unit(key, 1.0)
            # lev from outside; filled by caller via unit_of
            total = total + np.abs(self.core[key]) * self._units[key]
            total = total + np.abs(self.batch[:, idx, :]).sum(axis=1) * self._units[key]
        return total

    def net(self, hedge_delta_notional) -> np.ndarray:
        total = self.opt_tw_delta + self.opt_us_delta + hedge_delta_notional
        for idx, key in enumerate(KEYS):
            total = total + self.core[key] * self._units[key]
            total = total + self.batch[:, idx, :].sum(axis=1) * self._units[key]
        return total

    def asset_value(self) -> np.ndarray:
        total = self.opt_tw_mv + self.opt_us_mv
        for idx, key in enumerate(KEYS):
            if MODE_OF[key] == "futures":
                continue
            total = total + self.core[key] + self.batch[:, idx, :].sum(axis=1)
        return total


def _attach_units(state: _PathState, levs: dict):
    state._units = {key: _unit(key, levs[key]) for key in KEYS}


def _nav_of(state: _PathState, hedge_mtm) -> np.ndarray:
    return state.cash + state.asset_value() + hedge_mtm


def _as_prehistory(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float).reshape(-1)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return np.ones(PREHISTORY)
    if arr.size >= PREHISTORY:
        arr = arr[-PREHISTORY:]
    else:
        arr = np.pad(arr, (PREHISTORY - arr.size, 0), mode="edge")
    if arr[-1] == 0:
        return np.ones(PREHISTORY)
    return arr / arr[-1]


def _ma(hist, window: int) -> np.ndarray:
    return hist[:, -window:].mean(axis=1)


def _run_paths(book: Book, strategy: Strategy, rt, rs, rm, market: MarketCalib) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    traded, budgets = apply_delever(book, strategy)
    n, n_days = rt.shape
    state = _PathState(n, traded, budgets, strategy.n_batches if not strategy.hold else 1, market.pre_tw, market.pre_sx)
    _attach_units(state, traded.levs)
    hedge_on = (not strategy.hold) and strategy.hedge_enabled and strategy.hedge_ratio > 0 and book.tw_hedge_base > 0
    k_long = float(strategy.hedge_long_strike)
    k_short = float(strategy.hedge_short_strike)
    if hedge_on:
        state.hedge_notional = float(strategy.hedge_ratio) * float(book.tw_hedge_base)
        premium = float(put_spread_value(1.0, k_long, k_short, n_days / TRADING_DAYS, market.vol_tw))
        state.cash -= state.hedge_notional * premium * (1.0 + COST)

    def hedge_quote(price, remaining_days):
        if not hedge_on:
            return np.zeros(n), np.zeros(n)
        tau = max(remaining_days / TRADING_DAYS, 0.0)
        value = put_spread_value(price, k_long, k_short, tau, market.vol_tw) * state.hedge_notional
        delta = put_spread_delta(price, k_long, k_short, tau, market.vol_tw) * state.hedge_notional
        return value, delta

    mtm0, _delta0 = hedge_quote(np.ones(n), n_days)
    nav = np.maximum(_nav_of(state, mtm0), 1e-6)
    nav_path = np.zeros((n, n_days + 1))
    nav_path[:, 0] = nav
    peak = nav.copy()
    mc = np.zeros(n, dtype=bool)
    opening_nav = float(np.median(nav))
    opening_lev = float(np.median(state.gross() / nav))

    for t in range(n_days):
        r_tw = rt[:, t]
        r_sox = rs[:, t]
        r_mem = rm[:, t]
        state.tw *= 1.0 + r_tw
        state.sx *= 1.0 + r_sox
        state.shift_history("tw", state.tw)
        state.shift_history("sx", state.sx)

        fut_pnl = np.zeros(n)
        for idx, key in enumerate(KEYS):
            ret = _factor_array(key, r_tw, r_sox, r_mem, traded.betas[key])
            new_core, pnl = _grow(MODE_OF[key], state.core[key], ret, traded.levs[key])
            state.core[key] = new_core
            fut_pnl = fut_pnl + pnl
            new_batch, batch_pnl = _grow(MODE_OF[key], state.batch[:, idx, :], ret[:, None], traded.levs[key])
            state.batch[:, idx, :] = new_batch
            fut_pnl = fut_pnl + np.sum(batch_pnl, axis=1)
        state.cash += fut_pnl
        state.cash *= np.where(state.cash < 0.0, 1.0 + BORROW, 1.0 + CASH_RATE)
        state.opt_tw_mv = state.opt_tw_mv + state.opt_tw_delta * r_tw
        state.opt_tw_delta = state.opt_tw_delta * (1.0 + r_tw)
        state.opt_us_mv = state.opt_us_mv + state.opt_us_delta * r_sox
        state.opt_us_delta = state.opt_us_delta * (1.0 + r_sox)

        mtm, hedge_delta = hedge_quote(state.tw, n_days - t - 1)
        if strategy.hold:
            nav = np.maximum(_nav_of(state, mtm), 1e-6)
            nav_path[:, t + 1] = nav
            mc |= (state.tw < MC_DROP) & (state.net(hedge_delta) / nav > MC_LEVERAGE)
            continue

        state.cool = np.maximum(state.cool - 1.0, 0.0)
        for idx, key in enumerate(KEYS):
            line = _stop_line(strategy, key)
            qty = state.batch[:, idx, :]
            ent = state.entry[:, idx, :]
            held = np.abs(qty) > 1e-8
            pnl_pct = np.zeros_like(qty)
            np.divide(qty - ent, np.maximum(np.abs(ent), 1e-12), out=pnl_pct, where=held)
            hit = held & (pnl_pct <= line)
            if not np.any(hit):
                continue
            removed = np.where(hit, qty, 0.0)
            state.cash += _cash_for_close(MODE_OF[key], removed).sum(axis=1)
            state.batch[:, idx, :] = np.where(hit, 0.0, qty)
            state.entry[:, idx, :] = np.where(hit, 0.0, ent)
            state.cool[:, idx, :] = np.where(hit, COOLDOWN, state.cool[:, idx, :])

        mtm, hedge_delta = hedge_quote(state.tw, n_days - t - 1)
        nav = np.maximum(_nav_of(state, mtm), 1e-6)
        peak = np.maximum(peak, nav)
        dd = nav / peak - 1.0

        if strategy.brake_enabled:
            trig = (dd <= strategy.brake_cut) & ~state.hard
            if np.any(trig):
                for idx, key in enumerate(KEYS):
                    qty = state.batch[:, idx, :]
                    removed = np.where(trig[:, None], qty, 0.0)
                    state.cash += _cash_for_close(MODE_OF[key], removed).sum(axis=1)
                    keep = ~trig[:, None]
                    state.batch[:, idx, :] = np.where(keep, qty, 0.0)
                    state.entry[:, idx, :] = np.where(keep, state.entry[:, idx, :], 0.0)
                nav = np.maximum(_nav_of(state, mtm), 1e-6)
                gross = np.maximum(state.gross(), 1e-9)
                scale = np.ones(n)
                scale[trig] = np.clip(nav[trig] / gross[trig], 0.0, 1.0)
                for key in KEYS:
                    old = state.core[key]
                    state.core[key] = old * scale
                    removed = old - state.core[key]
                    state.cash += _cash_for_close(MODE_OF[key], removed)
                state.hard |= trig
            state.hard &= ~(dd > strategy.brake_release)

        nav = np.maximum(_nav_of(state, mtm), 1e-6)
        gross = state.gross()
        room = np.maximum(strategy.target_leverage * nav - gross, 0.0)
        ok = np.ones(n, dtype=bool)
        if strategy.brake_enabled:
            ok = (dd > strategy.brake_pause) & ~state.hard

        ma20_tw, ma60_tw = _ma(state.hist_tw, 20), _ma(state.hist_tw, 60)
        ma20_sx, ma60_sx = _ma(state.hist_sx, 20), _ma(state.hist_sx, 60)
        state.up_tw = np.where(state.tw > ma20_tw, state.up_tw + 1.0, 0.0)
        state.up_sx = np.where(state.sx > ma20_sx, state.up_sx + 1.0, 0.0)
        # 今天才建的批次不能當下一批的「已持有」，避免同一天把三批一次補完。
        held_before = np.abs(state.batch) > 1e-6

        for k in range(strategy.n_batches):
            trigger = strategy.triggers[k % len(strategy.triggers)]
            for idx, key in enumerate(KEYS):
                budget = state.budgets.get(key, 0.0)
                if abs(budget) <= 1e-8:
                    continue
                factor = FACTOR_OF[key]
                price = state.tw if factor == "tw" else state.sx
                ma20 = ma20_tw if factor == "tw" else ma20_sx
                ma60 = ma60_tw if factor == "tw" else ma60_sx
                up = state.up_tw if factor == "tw" else state.up_sx
                if trigger == TRIGGER_MA20:
                    cond = up >= 2.0
                elif trigger == TRIGGER_MA60:
                    cond = price > ma60
                else:
                    cond = ma20 > ma60
                if k > 0:
                    prev = np.ones(n, dtype=bool)
                    for j in range(k):
                        prev &= held_before[:, idx, j]
                    cond = cond & prev
                    if trigger == TRIGGER_MA60:
                        q0 = state.batch[:, idx, 0]
                        e0 = state.entry[:, idx, 0]
                        held0 = held_before[:, idx, 0]
                        pnl0 = np.zeros(n)
                        np.divide(q0 - e0, np.maximum(np.abs(e0), 1e-12), out=pnl0, where=held0)
                        cond = cond & held0 & (pnl0 > 0.0)
                held = np.abs(state.batch[:, idx, k]) > 1e-6
                want = cond & ok & ~held & (state.cool[:, idx, k] <= 0.0)
                if not np.any(want):
                    continue
                unit = state._units[key]
                slice_abs = abs(budget) / strategy.n_batches
                add_abs = np.minimum(slice_abs, room / max(unit, 1e-12))
                add_abs = np.where(want & (add_abs > 0.01 * slice_abs), add_abs, 0.0)
                if not np.any(add_abs > 0):
                    continue
                sign = 1.0 if budget > 0 else -1.0
                added = sign * add_abs
                state.cash += _cash_for_open(MODE_OF[key], added)
                state.batch[:, idx, k] += added
                opened = add_abs > 0
                state.entry[:, idx, k] = np.where(opened, state.batch[:, idx, k], state.entry[:, idx, k])
                room = np.maximum(room - add_abs * unit, 0.0)

        mtm, hedge_delta = hedge_quote(state.tw, n_days - t - 1)
        nav = np.maximum(_nav_of(state, mtm), 1e-6)
        nav_path[:, t + 1] = nav
        mc |= (state.tw < MC_DROP) & (state.net(hedge_delta) / nav > MC_LEVERAGE)

    end_gross = state.gross()
    end_lev = end_gross / np.maximum(nav_path[:, -1], 1e-6)
    return nav_path, mc, end_lev, opening_nav, opening_lev


def _stats_from_paths(nav: np.ndarray, mc: np.ndarray, end_lev: np.ndarray) -> tuple[StrategyStats, np.ndarray, np.ndarray, np.ndarray]:
    peak = np.maximum.accumulate(nav, axis=1)
    mdd = (nav / peak - 1.0).min(axis=1)
    end = nav[:, -1]
    quant = np.percentile(nav, [10, 50, 90], axis=0)
    stats = StrategyStats(
        median_end=float(np.median(end)),
        p10_end=float(np.percentile(end, 10)),
        median_mdd=float(np.median(mdd)),
        p_dd20=float(np.mean(mdd <= -0.20)),
        p_mc=float(np.mean(mc)),
        median_end_leverage=float(np.median(end_lev)),
    )
    return stats, quant, end, mdd


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    cumulative /= cumulative[-1]
    idx = int(np.searchsorted(cumulative, q, side="left"))
    idx = min(idx, values.size - 1)
    return float(values[order][idx])


def _weighted_stats(ends, mdds, mcs, levs, weights) -> StrategyStats:
    end = np.concatenate(ends)
    mdd = np.concatenate(mdds)
    mc = np.concatenate(mcs)
    lev = np.concatenate(levs)
    weight = np.concatenate(weights)
    weight = weight / weight.sum()
    return StrategyStats(
        median_end=_weighted_quantile(end, weight, 0.5),
        p10_end=_weighted_quantile(end, weight, 0.1),
        median_mdd=_weighted_quantile(mdd, weight, 0.5),
        p_dd20=float(np.sum(weight * (mdd <= -0.20))),
        p_mc=float(np.sum(weight * mc)),
        median_end_leverage=_weighted_quantile(lev, weight, 0.5),
    )


def generate_shocks(scenarios, market: MarketCalib, n_paths: int, n_days: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    shocks = {}
    rho = float(np.clip(market.rho, -0.99, 0.99))
    for scenario in scenarios:
        s_tw = market.vol_tw * scenario.vol_mult / np.sqrt(TRADING_DAYS)
        s_sx = market.vol_sox * scenario.vol_mult / np.sqrt(TRADING_DAYS)
        z1 = rng.standard_normal((n_paths, n_days))
        z2 = rho * z1 + np.sqrt(1.0 - rho * rho) * rng.standard_normal((n_paths, n_days))
        z3 = rng.standard_normal((n_paths, n_days))
        drift_tw = np.log1p(scenario.tw_target) / n_days
        drift_sx = np.log1p(scenario.sox_target) / n_days
        r_tw = np.expm1(drift_tw + s_tw * z1)
        r_sx = np.expm1(drift_sx + s_sx * z2)
        r_mem = MEM_BETA * r_sx + MEM_IDIO * scenario.vol_mult / np.sqrt(TRADING_DAYS) * z3
        shocks[scenario.name] = (r_tw, r_sx, r_mem)
    return shocks


def simulate(
    book: Book,
    strategies: list[Strategy],
    *,
    market: MarketCalib | None = None,
    scenarios=None,
    weights=None,
    n_paths: int = N_PATHS,
    n_days: int = N_DAYS,
    seed: int = SEED,
    shocks: dict | None = None,
) -> SimResult:
    if book.nav <= 0:
        raise ValueError("淨值要大於 0 才能模擬")
    if not strategies:
        raise ValueError("至少要一組策略")
    if len(strategies) > 4:
        raise ValueError("最多比較 4 組策略")
    names = [s.name for s in strategies]
    if len(set(names)) != len(names):
        raise ValueError("策略名稱不能重複")
    scenario_list = list(scenarios or DEFAULT_SCENARIOS)
    if weights is None:
        weight_arr = normalize_weights([s.weight for s in scenario_list])
    else:
        if len(weights) != len(scenario_list):
            raise ValueError("權重數量要和情境一樣")
        weight_arr = normalize_weights(weights)
    market = market or default_market()
    if shocks is None:
        shocks = generate_shocks(scenario_list, market, int(n_paths), int(n_days), int(seed))
    else:
        sample = next(iter(shocks.values()))
        n_paths, n_days = int(sample[0].shape[0]), int(sample[0].shape[1])

    stats: dict[tuple[str, str], StrategyStats] = {}
    quantiles: dict[tuple[str, str], np.ndarray] = {}
    opening_leverage: dict[str, float] = {}
    opening_nav: dict[str, float] = {}
    bucket_end = {s.name: [] for s in strategies}
    bucket_mdd = {s.name: [] for s in strategies}
    bucket_mc = {s.name: [] for s in strategies}
    bucket_lev = {s.name: [] for s in strategies}
    bucket_w = {s.name: [] for s in strategies}

    for scenario, weight in zip(scenario_list, weight_arr):
        if scenario.name not in shocks:
            raise ValueError(f"缺少情境路徑：{scenario.name}")
        rt, rs, rm = shocks[scenario.name]
        for strategy in strategies:
            nav, mc, end_lev, open_nav, open_lev = _run_paths(book, strategy, rt, rs, rm, market)
            opening_nav[strategy.name] = open_nav
            opening_leverage[strategy.name] = open_lev
            st, quant, end, mdd = _stats_from_paths(nav, mc, end_lev)
            stats[(scenario.name, strategy.name)] = st
            quantiles[(scenario.name, strategy.name)] = quant
            bucket_end[strategy.name].append(end)
            bucket_mdd[strategy.name].append(mdd)
            bucket_mc[strategy.name].append(mc.astype(float))
            bucket_lev[strategy.name].append(end_lev)
            bucket_w[strategy.name].append(np.full(end.shape[0], float(weight)))

    weighted = {
        name: _weighted_stats(bucket_end[name], bucket_mdd[name], bucket_mc[name], bucket_lev[name], bucket_w[name])
        for name in names
    }
    return SimResult(
        scenario_names=[s.name for s in scenario_list],
        strategy_names=names,
        weights=[float(w) for w in weight_arr],
        stats=stats,
        weighted=weighted,
        quantiles=quantiles,
        nav0=float(book.nav),
        opening_leverage=opening_leverage,
        opening_nav=opening_nav,
        market_note=market.note,
        n_paths=int(n_paths),
        n_days=int(n_days),
        seed=int(seed),
    )


def result_frame(result: SimResult) -> pd.DataFrame:
    rows = []
    for scenario, weight in zip(result.scenario_names, result.weights):
        for name in result.strategy_names:
            stats = result.stats[(scenario, name)]
            rows.append(_row(scenario, weight, name, stats))
    for name in result.strategy_names:
        rows.append(_row("加權", None, name, result.weighted[name]))
    return pd.DataFrame(rows)


def _row(scenario: str, weight, name: str, stats: StrategyStats) -> dict:
    return {
        "情境": scenario if weight is None else f"{scenario}（{weight:.0%}）",
        "策略": name,
        "期末淨值中位數": stats.median_end,
        "最差一成(P10)": stats.p10_end,
        "中位最大回撤": stats.median_mdd,
        "觸及-20%機率": stats.p_dd20,
        "追繳風險機率": stats.p_mc,
    }


def _setup_cjk_font():
    global _FONT_READY
    if _FONT_READY:
        return
    import matplotlib
    from matplotlib import font_manager as fm
    from matplotlib import pyplot as plt

    if matplotlib.get_backend().lower() != "agg":
        try:
            matplotlib.use("Agg")
        except Exception:
            pass
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ):
        if os.path.exists(path):
            fm.fontManager.addfont(path)
            plt.rcParams["font.family"] = fm.FontProperties(fname=path).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False
    _FONT_READY = True


_COLORS = ("#d62728", "#1f77b4", "#2ca02c", "#9467bd")


def scenario_title(scenario: Scenario) -> str:
    return f"{scenario.name}\n台指{scenario.tw_target:+.0%} / SOX{scenario.sox_target:+.0%}（6個月，波動×{scenario.vol_mult:g}）"


def make_figure(result: SimResult, scenarios=None):
    """三個情境面板。圖例放在圖下方，不壓標題。"""
    _setup_cjk_font()
    from matplotlib import pyplot as plt

    scenarios = list(scenarios or DEFAULT_SCENARIOS)
    by_name = {s.name: s for s in scenarios}
    fig, axes = plt.subplots(1, len(result.scenario_names), figsize=(6.2 * len(result.scenario_names), 6.4), sharey=True)
    if len(result.scenario_names) == 1:
        axes = [axes]
    x = np.arange(result.n_days + 1)
    for ax, name in zip(axes, result.scenario_names):
        for idx, strategy_name in enumerate(result.strategy_names):
            color = _COLORS[idx % len(_COLORS)]
            band = result.quantiles[(name, strategy_name)] / 1_000_000.0
            ax.fill_between(x, band[0], band[2], color=color, alpha=0.16, linewidth=0)
            ax.plot(x, band[1], color=color, lw=2.0, label=f"{strategy_name} 中位數")
            ax.plot([], [], color=color, alpha=0.35, lw=8, label=f"{strategy_name} 10–90%")
        ax.axhline(result.nav0 / 1_000_000.0, color="gray", lw=0.8, ls=":")
        scenario = by_name.get(name)
        ax.set_title(scenario_title(scenario) if scenario else name, fontsize=11)
        ax.set_xlabel("交易日")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("淨值（NT$ 百萬）")
    fig.suptitle(
        f"淨值蒙地卡羅模擬（每情境{result.n_paths}條路徑，{result.n_days}交易日）— 示意模型，非預測",
        fontsize=13,
    )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=min(4, max(len(labels), 1)), frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.08, 1, 0.92))
    return fig


def sleeve_rows(book: Book) -> list[dict]:
    rows = []
    for key in KEYS:
        amount = float(book.amounts.get(key, 0.0))
        if abs(amount) < 1:
            continue
        unit = _unit(key, book.levs.get(key, 1.0))
        rows.append(
            {
                "袖套": SLEEVE_LABELS[key],
                "市值或名目": amount,
                "有效曝險": abs(amount) * unit,
                "倍數": book.levs.get(key, 1.0),
            }
        )
    if abs(book.opt_tw_delta) >= 1 or abs(book.opt_tw_mv) >= 1:
        rows.append({"袖套": SLEEVE_LABELS["opt_tw"], "市值或名目": book.opt_tw_mv, "有效曝險": book.opt_tw_delta, "倍數": 1.0})
    if abs(book.opt_us_delta) >= 1 or abs(book.opt_us_mv) >= 1:
        rows.append({"袖套": SLEEVE_LABELS["opt_us"], "市值或名目": book.opt_us_mv, "有效曝險": book.opt_us_delta, "倍數": 1.0})
    rows.append({"袖套": "融資殘值（模擬現金）", "市值或名目": book.cash_residual, "有效曝險": 0.0, "倍數": 0.0})
    return rows
