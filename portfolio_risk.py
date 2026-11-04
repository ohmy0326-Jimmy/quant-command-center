"""第四頁與下單輔助共用的部位評價。

槓桿、Beta、保證金與淨值的算法與「部位監控與量化風控」相同，
避免兩個頁面各算各的。本模組不做下單、不讀取終端機輸出的金鑰。
"""

from __future__ import annotations

import math
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import taifex_quotes
from beta_rules import beta_source_label, effective_beta, fetch_underlying_beta

# 元大期貨備援本日餘額（未含未平倉損益之現金帳頁面），最後確認日期 2026-11-02（來自元大 App）。
YUANTA_FUTURES_BASE_EQUITY = 168308.0


def equity_ratio_pct(equity, initial_margin) -> float | None:
    """權益比率＝權益數 ÷ 原始保證金 × 100。第四頁與第六頁共用。沒有原始保證金時為 None。"""
    try:
        equity_value = float(equity)
        initial = float(initial_margin)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(equity_value) or not math.isfinite(initial) or initial <= 0:
        return None
    return equity_value / initial * 100.0


def yuanta_cash_ledger(value) -> float:
    """元大期貨本日餘額 (cash ledger, 不含未平倉損益)。

    若傳入有效正數則直接使用；否則（0、空值、負數、NaN）回退至 YUANTA_FUTURES_BASE_EQUITY。
    """
    if value is None:
        return YUANTA_FUTURES_BASE_EQUITY
    try:
        if pd.isna(value):
            return YUANTA_FUTURES_BASE_EQUITY
        val = float(value)
        if math.isnan(val) or math.isinf(val) or val <= 0:
            return YUANTA_FUTURES_BASE_EQUITY
        return val
    except (ValueError, TypeError):
        return YUANTA_FUTURES_BASE_EQUITY


# (原始保證金, 維持保證金)，單位：TWD／口。數字來自第四頁。
FUTURES_MARGIN = {
    "TMF": (35050.0, 26900.0),
    "FMF": (35050.0, 26900.0),
    "MTX": (175250.0, 134500.0),
    "TX": (701000.0, 538000.0),
}

# 每點合約乘數（TWD）。未列名的期貨沿用第四頁的 2000。
FUTURES_MULTIPLIER = {
    "FMF": 10.0,
    "TMF": 10.0,
    "MTX": 50.0,
    "TX": 200.0,
}
DEFAULT_FUTURES_MULTIPLIER = 2000.0

# 第四頁對股票未實現損益扣除的證交稅率（以市值估算，不從帳戶淨值再扣一次）。
STOCK_TAX_RATE = 0.003

TAIEX_FUTURES = ("TX", "MTX", "FMF", "TMF")

DETAILED_SECTOR_MAP = {
    "MUU": "記憶體",
    "SNXX": "記憶體",
    "AMUU": "AI晶片",
    "KORU": "韓國指數",
    "金居": "銅箔材料",
    "2330": "晶圓代工",
    "TSM": "晶圓代工",
    "NVDA": "AI晶片",
    "AMD": "AI晶片",
    "AVGO": "網通與半導體",
    "MSFT": "軟體與雲端",
    "AAPL": "消費電子",
    "GOOGL": "數位廣告與雲端",
    "META": "社群與AI",
    "SOXL": "半導體槓桿ETF",
    "00631L": "台股槓桿ETF",
    "LITX": "光通產業",
    "TX": "臺指期貨",
    "MTX": "小型臺指",
    "FMF": "微型臺指期",
    "TMF": "微型臺指期",
}


def futures_multiplier(ticker: str) -> float:
    return float(FUTURES_MULTIPLIER.get(str(ticker).upper(), DEFAULT_FUTURES_MULTIPLIER))


def futures_initial_margin(ticker: str) -> float | None:
    spec = FUTURES_MARGIN.get(str(ticker).upper())
    return None if spec is None else float(spec[0])


def futures_maintenance_margin(ticker: str) -> float | None:
    spec = FUTURES_MARGIN.get(str(ticker).upper())
    return None if spec is None else float(spec[1])


def is_taiex_future(ticker: str) -> bool:
    return str(ticker).upper() in TAIEX_FUTURES


def load_service_account_info(secrets_fallback=None) -> dict:
    """與第四頁相同的金鑰讀取順序：環境變數 → Secret Manager → Streamlit secrets。

    secrets_fallback 由呼叫端提供（通常讀 st.secrets），本函式不打印金鑰。
    """
    import json

    raw_val = os.environ.get("GCP_KEY_JSON", "") or os.environ.get("GOOGLE_SERVICE_KEY_JSON", "")
    raw_val = raw_val.strip()

    if not raw_val:
        try:
            from google.cloud import secretmanager

            client_sm = secretmanager.SecretManagerServiceClient()
            project_id = "quant-dashboard-512010"
            secret_name = f"projects/{project_id}/secrets/google-service-key/versions/latest"
            response = client_sm.access_secret_version(request={"name": secret_name})
            raw_val = response.payload.data.decode("UTF-8").strip()
        except Exception:
            pass

    if not raw_val:
        if secrets_fallback is None:
            raise RuntimeError("找不到 Google 服務帳戶金鑰")
        return secrets_fallback()

    if raw_val.startswith("\ufeff"):
        raw_val = raw_val[1:]
    if (raw_val.startswith("'") and raw_val.endswith("'")) or (raw_val.startswith('"') and raw_val.endswith('"')):
        raw_val = raw_val[1:-1].strip()
    return json.loads(raw_val)


def fetch_quant_sheets(key_data: dict):
    """讀取 Quant_Database。回傳順序與第四頁相同：(帳戶, 淨值歷史, 持倉)。"""
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open("Quant_Database")
    from futures_ledger import FUTURES_LEDGER_SHEET, TRADE_LOG_SHEET, apply_yuanta_ledger
    from sheet_dates import normalize_nav_frame

    accounts = pd.DataFrame(spreadsheet.worksheet("Active_Positions").get_all_records())
    nav = normalize_nav_frame(pd.DataFrame(spreadsheet.worksheet("Daily_NAV").get_all_records()))
    positions = pd.DataFrame(spreadsheet.worksheet("持倉統計").get_all_records())
    accounts = apply_yuanta_ledger(
        accounts,
        _optional_sheet(spreadsheet, FUTURES_LEDGER_SHEET),
        _optional_sheet(spreadsheet, TRADE_LOG_SHEET),
        positions,
    )
    return accounts, nav, positions


def _optional_sheet(spreadsheet, title: str) -> pd.DataFrame | None:
    """分頁不存在或讀取失敗時回 None，讓本日餘額退回 B7，不讓整頁失敗。"""
    try:
        worksheet = spreadsheet.worksheet(title)
        return pd.DataFrame(worksheet.get_all_records())
    except Exception:
        return None


def fetch_usdtwd(default: float = 32.5) -> float:
    try:
        return float(yf.Ticker("USDTWD=X").history(period="1d")["Close"].iloc[-1])
    except Exception:
        return float(default)


def fetch_twii(default: float = 23000.0) -> float:
    try:
        return float(yf.Ticker("^TWII").history(period="1d")["Close"].iloc[-1])
    except Exception:
        return float(default)


def market_beta_info(ticker, is_tw: bool) -> tuple[float, str, bool]:
    """一年期回歸 Beta。共變異數與變異數的 ddof 與第四頁相同，不另做修正。"""
    t_upper = str(ticker).upper()
    if t_upper in DETAILED_SECTOR_MAP:
        refined_sector = DETAILED_SECTOR_MAP[t_upper]
    else:
        try:
            info = yf.Ticker(ticker).info
            refined_sector = info.get("industry", "") or info.get("sector", "") or "其他"
        except Exception:
            refined_sector = "其他與衍生品"

    if t_upper in TAIEX_FUTURES:
        return 1.0, str(refined_sector), True

    try:
        benchmark = "^TWII" if is_tw else "^GSPC"
        df_asset = yf.Ticker(ticker).history(period="1y")["Close"]
        df_bench = yf.Ticker(benchmark).history(period="1y")["Close"]
        combined = pd.concat([df_asset, df_bench], axis=1).dropna()
        if len(combined) > 30:
            returns = combined.pct_change().dropna()
            cov = np.cov(returns.iloc[:, 0], returns.iloc[:, 1])[0][1]
            var_bench = np.var(returns.iloc[:, 1])
            beta = cov / var_bench if var_bench != 0 else 1.0
            return float(round(beta, 2)), str(refined_sector), True
        else:
            return 1.0, str(refined_sector), False
    except Exception:
        return 1.0, str(refined_sector), False

def market_beta_and_sector(ticker, is_tw: bool) -> tuple[float, str]:
    return market_beta_info(ticker, is_tw)[:2]

market_beta_and_sector.info = market_beta_info


def is_option_row(row) -> bool:
    kind = str(row.get("種類", "")).lower()
    if any(k in kind for k in ("選擇權", "期權", "option")):
        return True
    name = str(row.get("名稱", "")).lower()
    if any(k in name for k in ("call", "put", "買權", "賣權")):
        return True
    return False


def parse_expiry(row) -> date | None:
    expiry_cols = ("到期日", "到期日期", "到期", "Expiry", "Expiration", "expiry", "expiration")
    for col in expiry_cols:
        if col in row and pd.notna(row[col]):
            val = row[col]
            if str(val).strip():
                try:
                    num = float(val)
                    if 30000 < num < 100000:
                        return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=num)).date()
                except (ValueError, TypeError):
                    pass
                dt = pd.to_datetime(val, errors="coerce")
                if pd.notna(dt):
                    return dt.date()

    if is_option_row(row):
        for text_val in (str(row.get("名稱", "")), str(row.get("標的", ""))):
            if not text_val:
                continue
            m = re.search(r"[A-Z]{1,6}(\d{2})(\d{2})(\d{2})[CP]\d{8}", text_val, re.IGNORECASE)
            if m:
                try:
                    yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    year = 2000 + yy if yy < 80 else 1900 + yy
                    return date(year, mm, dd)
                except ValueError:
                    pass
            m = re.search(r"(?<!\d)(20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])(?!\d)", text_val)
            if m:
                try:
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    pass
            m = re.search(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", text_val)
            if m:
                try:
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    pass
            m = re.search(r"(?<!\d)(0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])[-/](20\d{2})(?!\d)", text_val)
            if m:
                try:
                    return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                except ValueError:
                    pass
    return None


def normalize_positions(quant_data: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """清洗持倉表。欄位定義與第四頁進入報價迴圈之前相同。"""
    if quant_data is None or quant_data.empty or "標的" not in quant_data.columns:
        return pd.DataFrame()

    df = quant_data.copy()
    raw_target = df["標的"].astype(str).str.strip().str.upper()
    df["標的"] = raw_target
    df = df[df["標的"] != ""].copy()
    if df.empty:
        return pd.DataFrame()

    if today is None:
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()

    expired_removed = []
    keep_mask = []
    for _idx, row in df.iterrows():
        exp = parse_expiry(row)
        if exp is not None and exp < today:
            expired_removed.append(str(row["標的"]).strip().upper())
            keep_mask.append(False)
        else:
            keep_mask.append(True)

    df = df[keep_mask].copy()
    df.attrs["expired_removed"] = list(dict.fromkeys(expired_removed))
    if df.empty:
        return df

    df["持有數量"] = pd.to_numeric(df["持有數量"], errors="coerce").fillna(0.0)
    df["平均成本"] = pd.to_numeric(df["平均成本"], errors="coerce").fillna(0.0)
    if "投入總金額" not in df.columns:
        df["投入總金額"] = 0.0
    df["投入總金額"] = pd.to_numeric(df["投入總金額"], errors="coerce").fillna(0.0)
    if "最大虧損比例" not in df.columns:
        df["最大虧損比例"] = -100.0
    else:
        df["最大虧損比例"] = pd.to_numeric(df["最大虧損比例"], errors="coerce").fillna(-100.0)
    if "標的槓桿" not in df.columns:
        df["標的槓桿"] = 1.0
    else:
        df["標的槓桿"] = pd.to_numeric(df["標的槓桿"], errors="coerce").fillna(1.0)
    if "名稱" not in df.columns:
        df["名稱"] = ""
    df["名稱"] = df["名稱"].astype(str)
    if "種類" not in df.columns:
        df["種類"] = ""
    df["種類"] = df["種類"].astype(str)
    if "帳戶" not in df.columns:
        df["帳戶"] = ""
    df["帳戶"] = df["帳戶"].astype(str).str.strip()
    df = df[df["持有數量"] != 0].copy()
    return df


def _page4_is_tw_market(ticker: str) -> bool:
    """第四頁決定報價來源的條件，原樣保留。"""
    t_upper = str(ticker).upper()
    return (t_upper in ["TE", "TF"]) or (".TW" in t_upper) or (".TWO" in t_upper) or (len(t_upper) == 3 and t_upper.isalpha())


def _fetch_yahoo_price(ticker: str, is_tw_market: bool) -> float:
    if is_tw_market:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(f"https://tw.stock.yahoo.com/quote/{ticker}", headers=headers, timeout=5)
            match = re.search(r'"regularMarketPrice":(?:{"raw":)?([0-9.]+)', res.text)
            return float(match.group(1)) if match else float(yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1])
        except Exception:
            try:
                return float(yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1])
            except Exception:
                return np.nan
    else:
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            return float(hist["Close"].iloc[-1]) if not hist.empty else np.nan
        except Exception:
            return np.nan


def _quote_one_ex(ticker: str, twii_price: float, beta_fn, kind: str = ""):
    extras = {}
    try:
        t_upper = str(ticker).upper()
        kind_str = str(kind)
        is_opt = is_option_row({"種類": kind_str, "名稱": ticker, "標的": ticker})

        parsed_opt = taifex_quotes.parse_txo_symbol(ticker) if "美" not in kind_str else None
        if parsed_opt is not None:
            opt_q = taifex_quotes.get_option_quote(parsed_opt["strike"], parsed_opt["call_put"], expiry=parsed_opt.get("expiry"))
            price = opt_q.price if (opt_q and opt_q.price is not None) else np.nan
            extras["報價來源"] = opt_q.label if (opt_q and opt_q.price is not None) else "無報價，暫用成本價"
            extras["合約乘數"] = opt_q.multiplier if opt_q else 50.0

            tx_q = taifex_quotes.get_futures_quote("TX", fallback_price=twii_price)
            und_px = tx_q.price if (tx_q and tx_q.price is not None) else twii_price
            extras["標的現價"] = und_px

            if pd.notna(price) and price is not None and parsed_opt.get("expiry"):
                exp_date = taifex_quotes.option_expiry_date(parsed_opt["expiry"])
                if exp_date:
                    delta = taifex_quotes.option_delta_from_quote(price, und_px, parsed_opt["strike"], exp_date, parsed_opt["call_put"])
                    if delta is not None:
                        extras["Delta"] = delta
                        extras["Delta來源"] = "估算(Black-76)"

            b_val, s_val = 1.0, DETAILED_SECTOR_MAP.get("TX", "台指選擇權")
            extras["Beta來源"] = beta_source_label(kind_str, True, definition=True)
            return ticker, price, b_val, s_val, extras

        if t_upper in TAIEX_FUTURES:
            fut_q = taifex_quotes.get_futures_quote(t_upper, fallback_price=twii_price)
            price = fut_q.price if (fut_q and fut_q.price is not None) else np.nan
            extras["報價來源"] = fut_q.label if (fut_q and fut_q.price is not None) else "無報價，暫用成本價"
            extras["合約乘數"] = fut_q.multiplier if fut_q else futures_multiplier(t_upper)
            sector = DETAILED_SECTOR_MAP.get(t_upper, "臺指期貨")
            extras["Beta來源"] = beta_source_label(kind_str, True, definition=True)
            return ticker, price, 1.0, sector, extras

        if ("期" in kind_str) and not is_opt:
            fut_q = taifex_quotes.get_futures_quote(ticker)
            if fut_q and fut_q.price is not None:
                if getattr(beta_fn, "info", None):
                    b_val, s_val, ok = beta_fn.info(ticker, True)
                else:
                    b_val, s_val = beta_fn(ticker, True)
                    ok = True
                extras["報價來源"] = fut_q.label
                extras["合約乘數"] = fut_q.multiplier
                extras["Beta來源"] = beta_source_label(kind_str, ok)
                return ticker, fut_q.price, b_val, s_val, extras

            is_tw_market = _page4_is_tw_market(ticker)
            if getattr(beta_fn, "info", None):
                b_val, s_val, ok = beta_fn.info(ticker, is_tw_market)
            else:
                b_val, s_val = beta_fn(ticker, is_tw_market)
                ok = True
            px = _fetch_yahoo_price(ticker, is_tw_market)
            extras["報價來源"] = ("TW Yahoo(備援)" if is_tw_market else "Yahoo(備援)") if pd.notna(px) else "無報價，暫用成本價"
            extras["合約乘數"] = futures_multiplier(ticker)
            extras["Beta來源"] = beta_source_label(kind_str, ok)
            return ticker, px, b_val, s_val, extras

        is_tw_market = _page4_is_tw_market(ticker)
        if getattr(beta_fn, "info", None):
            b_val, s_val, ok = beta_fn.info(ticker, is_tw_market)
        else:
            b_val, s_val = beta_fn(ticker, is_tw_market)
            ok = True
        px = _fetch_yahoo_price(ticker, is_tw_market)
        extras["報價來源"] = ("TW Yahoo" if is_tw_market else "Yahoo") if pd.notna(px) else "無報價，暫用成本價"
        extras["Beta來源"] = beta_source_label(kind_str, ok)
        extras.update(fetch_underlying_beta(ticker, beta_fn))
        return ticker, px, b_val, s_val, extras

    except Exception:
        return ticker, np.nan, 1.0, "其他", {"報價來源": "無報價，暫用成本價"}


def _quote_one(ticker, twii_price: float, beta_fn, kind: str = ""):
    """單一代號的報價、Beta、產業。例外時的回退與第四頁逐檔迴圈相同。"""
    t, p, b, s, _ex = _quote_one_ex(ticker, twii_price, beta_fn, kind=kind)
    return t, p, b, s


def populate_market_fields(df: pd.DataFrame, beta_fn, twii_price: float, extras: dict | None = None):
    """抓即時報價、Beta、產業。期貨用台指現貨，其餘路徑與第四頁相同。"""
    prices, betas, sectors = {}, {}, {}
    if df is None or df.empty or "標的" not in df.columns:
        return prices, betas, sectors
    for ticker in df["標的"].unique().tolist():
        sub = df[df["標的"] == ticker]
        kind = str(sub["種類"].iloc[0]) if "種類" in sub.columns and not sub.empty else ""
        ticker, price, beta, sector, ex_dict = _quote_one_ex(ticker, twii_price, beta_fn, kind=kind)
        prices[ticker] = price
        betas[ticker] = beta
        sectors[ticker] = sector
        if extras is not None:
            extras[ticker] = ex_dict
    return prices, betas, sectors


def populate_market_fields_parallel(df: pd.DataFrame, beta_fn, twii_price: float, max_workers: int = 8, extras: dict | None = None):
    """同一套逐檔公式，只是同時下載。回傳的價格與 Beta 與序列版相同。"""
    if df is None or df.empty or "標的" not in df.columns:
        return {}, {}, {}
    tickers = df["標的"].unique().tolist()
    if len(tickers) <= 1:
        return populate_market_fields(df, beta_fn, twii_price, extras=extras)

    ticker_kinds = []
    for ticker in tickers:
        sub = df[df["標的"] == ticker]
        kind = str(sub["種類"].iloc[0]) if "種類" in sub.columns and not sub.empty else ""
        ticker_kinds.append((ticker, kind))

    from concurrent.futures import ThreadPoolExecutor

    prices, betas, sectors = {}, {}, {}
    workers = max(1, min(max_workers, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda tk: _quote_one_ex(tk[0], twii_price, beta_fn, kind=tk[1]), ticker_kinds)
        for ticker, price, beta, sector, ex_dict in results:
            prices[ticker] = price
            betas[ticker] = beta
            sectors[ticker] = sector
            if extras is not None:
                extras[ticker] = ex_dict
    return prices, betas, sectors


def ticker_kind_pairs(frame: pd.DataFrame) -> tuple:
    """持倉表裡不重複的 (標的, 種類)。報價快取一律用這個當鍵。"""
    if frame is None or frame.empty or "標的" not in frame.columns:
        return ()
    has_kind = "種類" in frame.columns
    pairs = []
    seen = set()
    for _, row in frame.iterrows():
        t = str(row.get("標的") or "").strip()
        k = str(row.get("種類") or "").strip() if has_kind else ""
        if t and (t, k) not in seen:
            seen.add((t, k))
            pairs.append((t, k))
    return tuple(pairs)


def coerce_ticker_kind_pairs(items) -> tuple:
    """把報價請求整理成 (標的, 種類)。

    舊呼叫端可能只傳代號字串，例如 ("FMF", "MUU")。直接用 `for t, k in items`
    會把字串拆成字元，出現 too many values to unpack。這裡把字串視為種類空白。
    """
    pairs = []
    for item in items or ():
        if isinstance(item, str):
            t, k = item, ""
        elif isinstance(item, (tuple, list)) and len(item) >= 1:
            t = item[0]
            k = item[1] if len(item) >= 2 else ""
        else:
            t, k = item, ""
        t = str(t if t is not None else "").strip()
        k = str(k if k is not None else "").strip()
        if t:
            pairs.append((t, k))
    return tuple(dict.fromkeys(pairs))


def _row_label(df: pd.DataFrame, idx) -> str:
    row = df.loc[idx]
    acct = str(row.get("帳戶", "") or "").strip()
    ticker = str(row.get("標的", "") or "").strip()
    try:
        sheet_row = f"第 {int(idx) + 2} 列"  # get_all_records 從第 2 列開始
    except (TypeError, ValueError):
        sheet_row = f"列 {idx}"
    who = " / ".join(x for x in (acct, ticker) if x)
    return f"{sheet_row}（{who}）" if who else sheet_row


def value_positions_safely(positions: pd.DataFrame, accounts: pd.DataFrame, usdtwd: float, prices: dict | None = None, betas: dict | None = None, sectors: dict | None = None, extras: dict | None = None, beta_fallbacks: dict | None = None):
    """attach_quotes + finalize_valuation，但單一壞列不會讓整頁失敗。

    整批成功就直接回傳。失敗時逐列試算，把會出錯的列排除後重算，
    回傳 (持倉表, 帳戶表, 指標, 警告清單)，警告會寫出是哪一列與錯誤訊息。
    """
    prices = prices or {}
    betas = betas or {}
    sectors = sectors or {}

    def run(frame):
        if frame is not None and not frame.empty and "標的" in frame.columns:
            frame = attach_quotes(frame, prices, betas, sectors, extras=extras, beta_fallbacks=beta_fallbacks)
        return finalize_valuation(frame, accounts, usdtwd)

    try:
        valued, acc, metrics = run(positions)
        return valued, acc, metrics, []
    except Exception as whole_err:
        if positions is None or positions.empty:
            raise
        warnings = []
        bad = []
        for idx in positions.index:
            try:
                run(positions.loc[[idx]])
            except Exception as row_err:
                bad.append(idx)
                warnings.append(f"{_row_label(positions, idx)} 無法計算，已暫時排除：{row_err}")
        if not bad or len(bad) == len(positions.index):
            # 沒有單一列能重現，或每一列都失敗：問題不在某一列，照原樣回報。
            raise whole_err
        kept = positions.drop(index=bad)
        kept.attrs = dict(positions.attrs)
        valued, acc, metrics = run(kept)
        return valued, acc, metrics, warnings


def attach_quotes(df: pd.DataFrame, prices: dict, betas: dict, sectors: dict, extras: dict | None = None, beta_fallbacks: dict | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    out = df.copy()
    if beta_fallbacks is not None:
        out.attrs["beta_fallbacks"] = dict(beta_fallbacks)
    out["即時報價"] = out["標的"].map(prices).fillna(out["平均成本"])
    out["Beta值"] = out["標的"].map(betas).fillna(1.0)
    out["產業別"] = out["標的"].map(sectors).fillna("其他")

    if extras:
        labels_map = out["標的"].map(lambda t: extras.get(t, {}).get("報價來源", ""))
        if "報價來源" not in out.columns:
            out["報價來源"] = labels_map
        else:
            out["報價來源"] = out["報價來源"].fillna(labels_map)

        has_sheet_delta = ("Delta" in out.columns) and out["Delta"].notna()

        for key in ("合約乘數", "標的現價", "Delta"):
            ex_series = out["標的"].map(lambda t: extras.get(t, {}).get(key, np.nan))
            if key not in out.columns:
                out[key] = ex_series
            else:
                out[key] = out[key].fillna(ex_series)

        delta_src_series = out["標的"].map(lambda t: extras.get(t, {}).get("Delta來源", ""))
        if "Delta來源" not in out.columns:
            out["Delta來源"] = np.where(has_sheet_delta, "試算表", delta_src_series)
        else:
            out["Delta來源"] = np.where(has_sheet_delta, "試算表", out["Delta來源"].fillna(delta_src_series))

        for key in ("Beta來源", "標的代號", "標的Beta", "標的Beta來源"):
            ex_series = out["標的"].map(lambda t: extras.get(t, {}).get(key, np.nan if key == "標的Beta" else ""))
            if key not in out.columns:
                out[key] = ex_series
            else:
                out[key] = out[key].fillna(ex_series)

    return out


def _futures_margin_row(ticker: str, qty_abs: float, notional_abs: float) -> tuple[float, float]:
    spec = FUTURES_MARGIN.get(str(ticker).upper())
    if spec is not None:
        return spec[0] * qty_abs, spec[1] * qty_abs
    return notional_abs * 0.135, notional_abs * 0.11


def _empty_metrics() -> dict:
    return {
        "nav": 0.0,
        "exposure": 0.0,
        "position_exposure": 0.0,
        "leverage": 0.0,
        "cash": 0.0,
        "cash_pct": 0.0,
        "debt": 0.0,
        "beta": 0.0,
        "tw_beta": 0.0,
        "us_beta": 0.0,
        "total_beta": 0.0,
        "tw_exposure": 0.0,
        "us_exposure": 0.0,
        "tw_nav": 0.0,
        "us_nav": 0.0,
        "beta_weighted_avg": 0.0,
        **beta_leverage_breakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "options_missing_delta": [],
        "expired_removed": [],
        "margin_ratio": None,
        "risk_indicator": None,
        "futures_equity": 0.0,
        "futures_maint": 0.0,
        "futures_initial": 0.0,
        "futures_cash_ledger": 0.0,
        "excess_margin": 0.0,
        "margin_call": False,
        "margin_call_distance": 0.0,
        "margin_call_points": None,
        "futures_price_labels": [],
        "sector_weights": {},
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
        "beta_fallbacks_used": [],
    }


def finalize_valuation(positions: pd.DataFrame, accounts: pd.DataFrame, usdtwd: float):
    """把已帶報價的持倉與帳戶，算成第四頁的市值、曝險、淨值、槓桿與 Beta。

    回傳 (持倉表, 帳戶表, 指標)。帳戶表的「槓桿率」「權益比率」已四捨五入，供第四頁直接顯示。
    指標裡的槓桿與現金水位保留完整精度，供試算前後比較。
    """
    df = positions.copy() if positions is not None else pd.DataFrame()
    account_data = accounts.copy() if accounts is not None else pd.DataFrame()
    usdtwd = float(usdtwd)

    if not df.empty:
        for col in ("持有數量", "平均成本", "投入總金額", "最大虧損比例", "標的槓桿", "即時報價", "Beta值"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        if "即時報價" not in df.columns:
            df["即時報價"] = df["平均成本"]
        else:
            df["即時報價"] = pd.to_numeric(df["即時報價"], errors="coerce").fillna(df["平均成本"])
        if "Beta值" not in df.columns:
            df["Beta值"] = 1.0
        else:
            df["Beta值"] = pd.to_numeric(df["Beta值"], errors="coerce").fillna(1.0)
        if "產業別" not in df.columns:
            df["產業別"] = "其他"
        else:
            df["產業別"] = df["產業別"].fillna("其他").astype(str)

        df["名目合約總值(原幣)"] = pd.to_numeric(df["投入總金額"], errors="coerce").fillna(0.0).astype(float)
        df["換算匯率"] = np.where(df["種類"].astype(str).str.contains("美"), usdtwd, 1.0).astype(float)
        df["實質佔用本金(原幣)"] = df["名目合約總值(原幣)"].abs().astype(float)
        df["維持保證金(原幣)"] = 0.0

        for idx, row in df.iterrows():
            if "期" in str(row["種類"]) and not is_option_row(row):
                # 賣方選擇權保證金未計入（TODO）
                occupied, maint = _futures_margin_row(str(row["標的"]), abs(float(row["持有數量"])), abs(float(row["名目合約總值(原幣)"])))
                df.at[idx, "實質佔用本金(原幣)"] = occupied
                df.at[idx, "維持保證金(原幣)"] = maint

        is_futures_mask = df["種類"].astype(str).str.contains("期", na=False)

        def calc_row_pnl(row):
            t_str = str(row["標的"]).strip().upper()
            is_opt = is_option_row(row)
            mult = futures_multiplier(t_str)
            if "合約乘數" in row and pd.notna(row["合約乘數"]):
                try:
                    c_mult = float(row["合約乘數"])
                    if c_mult > 0:
                        mult = c_mult
                except (ValueError, TypeError):
                    pass
            if is_opt:
                if "美" not in str(row["種類"]):
                    mult_opt = mult if ("合約乘數" in row and pd.notna(row["合約乘數"]) and float(row["合約乘數"]) > 0) else 50.0
                    return (float(row["即時報價"]) - float(row["平均成本"])) * float(row["持有數量"]) * mult_opt
                else:
                    return 0.0
            return (float(row["即時報價"]) - float(row["平均成本"])) * float(row["持有數量"]) * mult

        df["淨未實現損益(原幣)"] = np.where(is_futures_mask, df.apply(calc_row_pnl, axis=1), 0.0)

        stock_mask = ~is_futures_mask
        if stock_mask.any():
            for idx in df[stock_mask].index:
                row = df.loc[idx]
                if is_option_row(row) and "美" not in str(row["種類"]):
                    mult_opt = 50.0
                    if "合約乘數" in row and pd.notna(row["合約乘數"]):
                        try:
                            cm = float(row["合約乘數"])
                            if cm > 0:
                                mult_opt = cm
                        except (ValueError, TypeError):
                            pass
                    df.at[idx, "淨未實現損益(原幣)"] = (float(row["即時報價"]) - float(row["平均成本"])) * float(row["持有數量"]) * mult_opt
                else:
                    cost = float(row["平均成本"])
                    notional = float(row["名目合約總值(原幣)"])
                    price = float(row["即時報價"])
                    stock_latest = notional * (price / cost) if cost != 0 else notional
                    df.at[idx, "淨未實現損益(原幣)"] = (stock_latest - notional) - (abs(stock_latest) * STOCK_TAX_RATE)

        occupied_abs = df["實質佔用本金(原幣)"].abs()
        df["報酬率 (%)"] = np.where(occupied_abs != 0, (df["淨未實現損益(原幣)"] / occupied_abs) * 100.0, 0.0).astype(float)
        df["顯示_投入總金額(原幣)"] = np.where(is_futures_mask, df["實質佔用本金(原幣)"], df["名目合約總值(原幣)"]).astype(float)

        def calc_display_mv(row):
            is_opt = is_option_row(row)
            if is_opt and "美" not in str(row["種類"]):
                mult_opt = 50.0
                if "合約乘數" in row and pd.notna(row["合約乘數"]):
                    try:
                        cm = float(row["合約乘數"])
                        if cm > 0:
                            mult_opt = cm
                    except (ValueError, TypeError):
                        pass
                return float(row["即時報價"]) * float(row["持有數量"]) * mult_opt
            if "期" in str(row["種類"]):
                return float(row["實質佔用本金(原幣)"]) + float(row["淨未實現損益(原幣)"])
            return float(row["即時報價"]) * float(row["持有數量"])

        # 權益市值：期貨＝保證金＋未實現損益，只用來算帳戶淨值（舊的「即時市值」口徑）。
        df["權益市值(原幣)"] = df.apply(calc_display_mv, axis=1).astype(float)
        df["權益市值(台幣)"] = (df["權益市值(原幣)"].abs() * df["換算匯率"]).astype(float)

        def calc_market_value(row):
            # 持倉表顯示的市值：期貨列顯示名目市值（報價 × 乘數 × 口數），不是未實現損益。
            if "期" in str(row["種類"]) and not is_option_row(row):
                return abs(float(row["持有數量"])) * float(row["即時報價"]) * _row_multiplier(row)
            return float(row["權益市值(原幣)"])

        df["顯示_即時市值(原幣)"] = df.apply(calc_market_value, axis=1).astype(float)
        df["即時市值(台幣)"] = (df["顯示_即時市值(原幣)"].abs() * df["換算匯率"]).astype(float)

        options_missing_delta = []
        df["實質曝險(台幣)"] = 0.0
        df["Beta曝險(台幣)"] = 0.0
        df["採用槓桿"] = 1.0
        
        overrides = dict(df.attrs.get("beta_fallbacks") or {})
        if "有效Beta" not in df.columns:
            df["有效Beta"] = np.nan
        if "Beta來源" not in df.columns:
            df["Beta來源"] = ""
        if "標的代號" not in df.columns:
            df["標的代號"] = ""
        if "標的Beta" not in df.columns:
            df["標的Beta"] = np.nan
        # 標的Beta 只放即時抓到的值；實際採用的（含預設值）另存，重算時預設值不會被當成即時值。
        df["採用標的Beta"] = np.nan

        for idx, row in df.iterrows():
            ticker_str = str(row["標的"]).strip().upper()
            kind_str = str(row["種類"])
            qty = float(row["持有數量"])
            px = float(row["即時報價"])
            fx = float(row["換算匯率"])
            is_opt = is_option_row(row)
            is_fut = ("期" in kind_str) and not is_opt

            row_mult = float(row["合約乘數"]) if ("合約乘數" in df.columns and pd.notna(row["合約乘數"]) and float(row["合約乘數"]) > 0) else futures_multiplier(ticker_str)

            if is_opt:
                delta_val = None
                for dcol in ("Delta", "delta"):
                    if dcol in df.columns and pd.notna(row[dcol]):
                        try:
                            delta_val = float(row[dcol])
                            break
                        except (ValueError, TypeError):
                            pass

                if delta_val is None:
                    if ticker_str not in options_missing_delta:
                        options_missing_delta.append(ticker_str)
                    beta_exp = 0.0
                    gross_exp = 0.0
                else:
                    opt_mult = 100.0 if "美" in kind_str else 50.0
                    if "合約乘數" in df.columns and pd.notna(row["合約乘數"]):
                        try:
                            cm = float(row["合約乘數"])
                            if cm > 0:
                                opt_mult = cm
                        except (ValueError, TypeError):
                            pass
                    und_px = px
                    if "標的現價" in df.columns and pd.notna(row["標的現價"]):
                        try:
                            und_px = float(row["標的現價"])
                        except (ValueError, TypeError):
                            pass
                    beta_exp = delta_val * qty * opt_mult * und_px * fx
                    gross_exp = abs(beta_exp)

                df.at[idx, "實質曝險(台幣)"] = gross_exp
                df.at[idx, "Beta曝險(台幣)"] = beta_exp
                df.at[idx, "有效Beta"] = np.nan
                df.at[idx, "Beta來源"] = "選擇權 Delta 曝險"

            elif is_fut:
                # 期貨：曝險＝名目（報價 × 乘數 × 口數），Beta＝標的 Beta（臺指期定義 1）。槓桿已在名目裡，不再乘。
                gross_exp = abs(qty) * px * row_mult * fx
                df.at[idx, "實質曝險(台幣)"] = gross_exp
                df.at[idx, "採用槓桿"] = 1.0
                if is_taiex_future(ticker_str):
                    df.at[idx, "Beta曝險(台幣)"] = qty * px * row_mult * fx
                    df.at[idx, "有效Beta"] = 1.0
                    df.at[idx, "Beta來源"] = "定義 1.0"
                else:
                    mkt_val_twd = gross_exp
                    eb = effective_beta(ticker_str, kind_str, 1.0, row["Beta值"], row.get("標的Beta"), str(row.get("標的Beta來源") or ""), str(row.get("Beta來源") or ""), overrides)
                    sens = eb["sens"]
                    df.at[idx, "有效Beta"] = sens
                    df.at[idx, "Beta來源"] = eb["source"]
                    df.at[idx, "標的代號"] = eb["underlying"]
                    df.at[idx, "採用標的Beta"] = eb["underlying_beta"] if eb["underlying_beta"] is not None else np.nan
                    sign = 1.0 if qty > 0 else (-1.0 if qty < 0 else 0.0)
                    df.at[idx, "Beta曝險(台幣)"] = mkt_val_twd * sens * sign

            else:
                # 現股／ETF：名目曝險＝市值 × 槓桿；Beta 曝險＝市值 × 有效Beta（槓桿 × 標的一年 Beta）
                # ＝名目曝險 × 標的 Beta。槓桿只乘一次。
                mkt_val_twd = float(row["即時市值(台幣)"])
                eb = effective_beta(ticker_str, kind_str, row["標的槓桿"], row["Beta值"], row.get("標的Beta"), str(row.get("標的Beta來源") or ""), str(row.get("Beta來源") or ""), overrides)
                lev = float(eb["lev"]) if eb.get("lev") is not None else _row_float(row["標的槓桿"], 1.0)
                df.at[idx, "實質曝險(台幣)"] = mkt_val_twd * lev
                df.at[idx, "採用槓桿"] = lev
                sens = eb["sens"]
                df.at[idx, "有效Beta"] = sens
                df.at[idx, "Beta來源"] = eb["source"]
                df.at[idx, "標的代號"] = eb["underlying"]
                df.at[idx, "採用標的Beta"] = eb["underlying_beta"] if eb["underlying_beta"] is not None else np.nan
                sign = 1.0 if qty > 0 else (-1.0 if qty < 0 else 0.0)
                df.at[idx, "Beta曝險(台幣)"] = mkt_val_twd * sens * sign
        # 曝險Beta：套在名目曝險上的 Beta（槓桿 ETF＝標的 Beta；現股＝自身 Beta；臺指期＝1）。
        gross = df["實質曝險(台幣)"].astype(float)
        df["曝險Beta"] = np.where(gross != 0, df["Beta曝險(台幣)"].astype(float) / gross.where(gross != 0, 1.0), 0.0).astype(float)
    else:
        df = pd.DataFrame()

    if not account_data.empty and "帳戶名稱" in account_data.columns:
        account_data["帳戶名稱"] = account_data["帳戶名稱"].astype(str).str.strip()
        for col in ["現金餘額", "融資/質押借款"]:
            if col not in account_data.columns:
                account_data[col] = 0.0
            account_data[col] = pd.to_numeric(account_data[col], errors="coerce").fillna(0.0).astype(float)

        if not df.empty:
            acc_market = df.groupby("帳戶")["權益市值(台幣)"].sum()
            acc_exp = df.groupby("帳戶")["實質曝險(台幣)"].sum()
        else:
            acc_market, acc_exp = pd.Series(dtype=float), pd.Series(dtype=float)

        account_data["即時持股市值"] = account_data["帳戶名稱"].map(acc_market).fillna(0.0).astype(float)
        account_data["名目曝險"] = account_data["帳戶名稱"].map(acc_exp).fillna(0.0).astype(float)
        account_data["原始保證金總額"] = 0.0
        account_data["維持保證金總額"] = 0.0
        account_data["可動用保證金"] = 0.0
        account_data["距追繳金額"] = 0.0
        account_data["距追繳點數"] = np.nan
        account_data["帳戶淨值"] = 0.0

        for idx, row in account_data.iterrows():
            acc_name = str(row["帳戶名稱"]).strip()
            subset = df[df["帳戶"] == acc_name] if not df.empty else pd.DataFrame()

            initial_m = 0.0
            maint_m = 0.0
            taiex_lots_mult = 0.0

            if not subset.empty:
                # 賣方選擇權保證金未計入（TODO）
                fut_sub = subset[[("期" in str(r["種類"])) and not is_option_row(r) for _, r in subset.iterrows()]]
                if not fut_sub.empty:
                    initial_m = float(fut_sub["實質佔用本金(原幣)"].sum())
                    maint_m = float(fut_sub["維持保證金(原幣)"].sum())
                for _, frow in subset.iterrows():
                    t_str = str(frow["標的"]).strip().upper()
                    if is_taiex_future(t_str):
                        c_mult = float(frow["合約乘數"]) if ("合約乘數" in subset.columns and pd.notna(frow["合約乘數"]) and float(frow["合約乘數"]) > 0) else futures_multiplier(t_str)
                        taiex_lots_mult += abs(float(frow["持有數量"])) * c_mult

            account_data.at[idx, "原始保證金總額"] = initial_m
            account_data.at[idx, "維持保證金總額"] = maint_m

            if "元大期貨" in acc_name:
                if str(row.get("_yuanta_source") or "") == "ledger":
                    try:
                        cash_ledger = float(row["現金餘額"])
                    except (TypeError, ValueError):
                        cash_ledger = yuanta_cash_ledger(row["現金餘額"])
                else:
                    cash_ledger = yuanta_cash_ledger(row["現金餘額"])
                futures_pnl = 0.0
                option_val = 0.0
                if not subset.empty:
                    for _, srow in subset.iterrows():
                        fx = float(srow["換算匯率"])
                        is_opt = is_option_row(srow)
                        is_fut = ("期" in str(srow["種類"])) and not is_opt
                        if is_opt:
                            c_mult = 50.0
                            if "合約乘數" in subset.columns and pd.notna(srow["合約乘數"]):
                                try:
                                    cm = float(srow["合約乘數"])
                                    if cm > 0:
                                        c_mult = cm
                                except (ValueError, TypeError):
                                    pass
                            option_val += float(srow["即時報價"]) * float(srow["持有數量"]) * c_mult * fx
                        elif is_fut:
                            futures_pnl += float(srow["淨未實現損益(原幣)"]) * fx

                net_val = float(cash_ledger + futures_pnl + option_val)
                account_data.at[idx, "帳戶淨值"] = net_val
                account_data.at[idx, "現金餘額"] = cash_ledger
                avail = net_val - initial_m
                dist = net_val - maint_m
                account_data.at[idx, "可動用保證金"] = avail
                account_data.at[idx, "距追繳金額"] = dist
                if taiex_lots_mult > 0:
                    account_data.at[idx, "距追繳點數"] = dist / taiex_lots_mult
            else:
                cash = float(row["現金餘額"])
                mkt_val = float(row["即時持股市值"])
                debt = float(row["融資/質押借款"])
                net_val = cash + mkt_val - debt
                account_data.at[idx, "帳戶淨值"] = net_val
                if initial_m > 0 or maint_m > 0:
                    avail = net_val - initial_m
                    dist = net_val - maint_m
                    account_data.at[idx, "可動用保證金"] = avail
                    account_data.at[idx, "距追繳金額"] = dist
                    if taiex_lots_mult > 0:
                        account_data.at[idx, "距追繳點數"] = dist / taiex_lots_mult

        nav = float(account_data["帳戶淨值"].sum())
        account_data["槓桿率 (倍)"] = np.round(
            np.where(account_data["帳戶淨值"] != 0, account_data["名目曝險"] / account_data["帳戶淨值"], 0.0),
            2,
        ).astype(float)
        def _account_ratio(row) -> float:
            initial = float(row["原始保證金總額"])
            nav_value = float(row["帳戶淨值"])
            if initial > 0:
                ratio = equity_ratio_pct(nav_value, initial)
                return 0.0 if ratio is None else round(ratio, 1)
            if nav_value != 0:
                return round(float(row["現金餘額"]) / nav_value * 100.0, 1)
            return 0.0

        account_data["權益比率%/現金水位%"] = account_data.apply(_account_ratio, axis=1).astype(float)

        acc_market_types = {}
        for idx, row in account_data.iterrows():
            acc_name = str(row["帳戶名稱"]).strip()
            if "元大期貨" in acc_name:
                acc_market_types[acc_name] = "TW"
            else:
                subset = df[df["帳戶"] == acc_name] if not df.empty else pd.DataFrame()
                if not subset.empty:
                    is_us_flags = subset["種類"].astype(str).str.contains("美")
                    if is_us_flags.all():
                        acc_market_types[acc_name] = "US"
                    elif (~is_us_flags).all():
                        acc_market_types[acc_name] = "TW"
                    else:
                        acc_market_types[acc_name] = "MIXED"
                else:
                    if any(k in acc_name.upper() for k in ("美", "US", "複委託", "海外")):
                        acc_market_types[acc_name] = "US"
                    else:
                        acc_market_types[acc_name] = "TW"

        tw_nav = 0.0
        us_nav = 0.0
        for idx, row in account_data.iterrows():
            acc_name = str(row["帳戶名稱"]).strip()
            acc_nav = float(row["帳戶淨值"])
            mtype = acc_market_types[acc_name]
            if mtype == "TW":
                tw_nav += acc_nav
            elif mtype == "US":
                us_nav += acc_nav
            else:
                subset = df[df["帳戶"] == acc_name]
                us_subset = subset[subset["種類"].astype(str).str.contains("美")]
                us_mv = float(us_subset["權益市值(台幣)"].sum())
                us_nav += us_mv
                tw_nav += (acc_nav - us_mv)
    else:
        nav = 0.0
        tw_nav = 0.0
        us_nav = 0.0

    if not df.empty:
        df["真實風險權重 (%)"] = np.where(nav > 0, (df["實質曝險(台幣)"] / nav) * 100.0, 0.0).astype(float)

    expired_removed = positions.attrs.get("expired_removed", []) if positions is not None else []
    metrics = _metrics_from_frames(df, account_data, nav, tw_nav, us_nav, options_missing_delta if not df.empty else [], expired_removed)
    
    if not df.empty and "Beta來源" in df.columns:
        fallbacks = []
        for _, row in df.iterrows():
            src = str(row.get("Beta來源", ""))
            if "預設值" in src:
                fallbacks.append({
                    "標的": str(row.get("標的", "")),
                    "標的代號": str(row.get("標的代號", "")),
                    "Beta來源": src
                })
        metrics["beta_fallbacks_used"] = fallbacks

    return df, account_data, metrics


def _metrics_from_frames(
    df: pd.DataFrame,
    account_data: pd.DataFrame,
    nav: float,
    tw_nav: float = 0.0,
    us_nav: float = 0.0,
    options_missing_delta: list[str] | None = None,
    expired_removed: list[str] | None = None,
) -> dict:
    metrics = _empty_metrics()
    metrics["nav"] = float(nav)
    metrics["tw_nav"] = float(tw_nav)
    metrics["us_nav"] = float(us_nav)
    metrics["options_missing_delta"] = list(dict.fromkeys(options_missing_delta or []))
    metrics["expired_removed"] = list(expired_removed or [])

    position_exposure = float(df["實質曝險(台幣)"].sum()) if not df.empty and "實質曝險(台幣)" in df.columns else 0.0
    metrics["position_exposure"] = position_exposure
    metrics["sector_weights"] = _sector_weights(df)

    if not df.empty and "Beta曝險(台幣)" in df.columns:
        is_us = df["種類"].astype(str).str.contains("美")
        tw_exp = float(df[~is_us]["Beta曝險(台幣)"].sum())
        us_exp = float(df[is_us]["Beta曝險(台幣)"].sum())
    else:
        tw_exp = 0.0
        us_exp = 0.0

    # 舊欄位：tw_exposure / us_exposure 一直是「Beta 加權曝險」Σ(名目曝險 × Beta)，不是名目曝險。
    # 保留原意給下單試算（split_betas）與舊 API 使用者；新欄位見 beta_leverage_breakdown。
    metrics["tw_exposure"] = tw_exp
    metrics["us_exposure"] = us_exp
    metrics["tw_beta"] = (tw_exp / tw_nav) if tw_nav > 0 else 0.0
    metrics["us_beta"] = (us_exp / us_nav) if us_nav > 0 else 0.0
    total_beta = ((tw_exp + us_exp) / nav) if nav > 0 else 0.0
    metrics["total_beta"] = total_beta
    metrics["beta"] = total_beta

    tw_notional, us_notional = _notional_by_market(df, account_data)
    metrics.update(beta_leverage_breakdown(tw_notional, us_notional, tw_exp, us_exp, tw_nav, us_nav, nav))
    metrics["beta_weighted_avg"] = metrics["avg_beta"]

    if not df.empty and "報價來源" in df.columns:
        fut_rows = df[[("期" in str(r["種類"]) or is_option_row(r)) and "美" not in str(r["種類"]) for _, r in df.iterrows()]]
        if not fut_rows.empty:
            raw_labels = fut_rows["報價來源"].astype(str).tolist()
            metrics["futures_price_labels"] = list(dict.fromkeys([l for l in raw_labels if l and l != "nan"]))

    if account_data is None or account_data.empty or "帳戶淨值" not in account_data.columns:
        return metrics

    exposure = float(account_data["名目曝險"].sum())
    debt = float(account_data["融資/質押借款"].sum()) if "融資/質押借款" in account_data.columns else 0.0
    metrics["exposure"] = exposure
    metrics["debt"] = debt
    metrics["leverage"] = (exposure / nav) if nav > 0 else 0.0

    names = account_data["帳戶名稱"].astype(str)
    fut_mask = names.str.contains("元大期貨")
    if "原始保證金總額" in account_data.columns:
        fut_mask = fut_mask | (account_data["原始保證金總額"] > 0)
    elif "維持保證金總額" in account_data.columns:
        fut_mask = fut_mask | (account_data["維持保證金總額"] > 0)

    non_fut = account_data[~fut_mask]
    fut = account_data[fut_mask]

    non_fut_cash = float(non_fut["現金餘額"].sum()) if not non_fut.empty else 0.0

    if not fut.empty:
        equity = float(fut["帳戶淨值"].sum())
        initial = float(fut["原始保證金總額"].sum()) if "原始保證金總額" in fut.columns else 0.0
        maint = float(fut["維持保證金總額"].sum())
        cash_ledger_tot = float(fut["現金餘額"].sum())
        avail = equity - initial
        dist = equity - maint

        metrics["futures_equity"] = equity
        metrics["futures_initial"] = initial
        metrics["futures_maint"] = maint
        metrics["futures_cash_ledger"] = cash_ledger_tot
        metrics["excess_margin"] = avail
        metrics["margin_call_distance"] = dist

        if maint > 0:
            metrics["margin_ratio"] = equity / maint * 100.0
            metrics["margin_call"] = bool(equity < maint)
        if initial > 0:
            metrics["risk_indicator"] = equity_ratio_pct(equity, initial)

        if "距追繳點數" in fut.columns:
            pts = fut["距追繳點數"].dropna()
            if not pts.empty:
                metrics["margin_call_points"] = float(pts.iloc[0])

        total_cash = non_fut_cash + max(0.0, avail)
    else:
        total_cash = non_fut_cash

    metrics["cash"] = total_cash
    metrics["cash_pct"] = (total_cash / nav * 100.0) if nav > 0 else 0.0
    from futures_ledger import ledger_metrics_from_accounts

    metrics.update(ledger_metrics_from_accounts(account_data))
    return metrics


def _row_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _row_multiplier(row) -> float:
    ticker = str(row["標的"]).strip().upper()
    if "合約乘數" in row and pd.notna(row["合約乘數"]):
        try:
            cm = float(row["合約乘數"])
            if cm > 0:
                return cm
        except (TypeError, ValueError):
            pass
    return futures_multiplier(ticker)


def _notional_by_market(df: pd.DataFrame, account_data: pd.DataFrame | None) -> tuple[float, float]:
    """台股／美股名目曝險合計。只算帳戶表有的帳戶，跟 metrics["exposure"]（帳戶加總）同一口徑。"""
    if df is None or df.empty or "實質曝險(台幣)" not in df.columns:
        return 0.0, 0.0
    rows = df
    if account_data is not None and not account_data.empty and "帳戶名稱" in account_data.columns and "帳戶" in df.columns:
        names = set(account_data["帳戶名稱"].astype(str).str.strip())
        rows = df[df["帳戶"].astype(str).str.strip().isin(names)]
    is_us = rows["種類"].astype(str).str.contains("美")
    return float(rows[~is_us]["實質曝險(台幣)"].sum()), float(rows[is_us]["實質曝險(台幣)"].sum())


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator and denominator > 0 else 0.0


def beta_leverage_breakdown(tw_notional: float, us_notional: float, tw_beta_exp: float, us_beta_exp: float, tw_nav: float, us_nav: float, nav: float) -> dict:
    """台股／美股／合計的三個數字，口徑一致：

    - 名目曝險：現股／ETF＝市值 × 槓桿倍數；期貨＝報價 × 乘數 × 口數；選擇權＝|Delta 名目|。
    - Beta 曝險＝Σ(名目曝險 × Beta)。槓桿 ETF 的 Beta 用標的一年 Beta（槓桿已在名目裡，只乘一次）。
    - 槓桿＝名目曝險 ÷ 淨值
    - 部位平均 Beta＝Beta 曝險 ÷ 名目曝險
    - 帳戶 Beta＝Beta 曝險 ÷ 淨值＝槓桿 × 部位平均 Beta
    """
    notional = float(tw_notional) + float(us_notional)
    beta_exp = float(tw_beta_exp) + float(us_beta_exp)
    return {
        "tw_notional_exposure": float(tw_notional),
        "us_notional_exposure": float(us_notional),
        "notional_exposure": notional,
        "tw_beta_exposure": float(tw_beta_exp),
        "us_beta_exposure": float(us_beta_exp),
        "beta_exposure": beta_exp,
        "tw_leverage": _ratio(tw_notional, tw_nav),
        "us_leverage": _ratio(us_notional, us_nav),
        "total_leverage": _ratio(notional, nav),
        "tw_avg_beta": _ratio(tw_beta_exp, tw_notional),
        "us_avg_beta": _ratio(us_beta_exp, us_notional),
        "avg_beta": _ratio(beta_exp, notional),
        "tw_account_beta": _ratio(tw_beta_exp, tw_nav),
        "us_account_beta": _ratio(us_beta_exp, us_nav),
        "account_beta": _ratio(beta_exp, nav),
    }


BETA_LEVERAGE_NOTE = (
    "槓桿＝名目曝險 ÷ 淨值；部位平均 Beta＝Σ(名目曝險 × Beta) ÷ 名目曝險；帳戶 Beta＝Σ(名目曝險 × Beta) ÷ 淨值＝槓桿 × 部位平均 Beta。"
    "名目曝險：現股／ETF＝市值 × 槓桿倍數，期貨＝報價 × 乘數 × 口數。槓桿 ETF 的 Beta 用標的一年 Beta，槓桿只乘一次。"
)


def beta_leverage_rows(metrics: dict) -> list[dict]:
    """第四頁、第六頁共用的三列表：台股／美股／合計。"""
    m = metrics or {}
    out = []
    for label, prefix, nav_key, lev_key, avg_key, acct_key in (
        ("台股", "tw_", "tw_nav", "tw_leverage", "tw_avg_beta", "tw_account_beta"),
        ("美股", "us_", "us_nav", "us_leverage", "us_avg_beta", "us_account_beta"),
        ("合計", "", "nav", "total_leverage", "avg_beta", "account_beta"),
    ):
        out.append({
            "市場": label,
            "名目曝險": float(m.get(f"{prefix}notional_exposure", 0.0) or 0.0),
            "Beta曝險": float(m.get(f"{prefix}beta_exposure", 0.0) or 0.0),
            "淨值": float(m.get(nav_key, 0.0) or 0.0),
            "槓桿": float(m.get(lev_key, 0.0) or 0.0),
            "部位平均Beta": float(m.get(avg_key, 0.0) or 0.0),
            "帳戶Beta": float(m.get(acct_key, 0.0) or 0.0),
        })
    return out


def _sector_weights(df: pd.DataFrame) -> dict:
    if df is None or df.empty or "實質曝險(台幣)" not in df.columns:
        return {}
    total = float(df["實質曝險(台幣)"].sum())
    if total <= 0:
        return {}
    grouped = df.groupby("產業別")["實質曝險(台幣)"].sum()
    return {str(k): float(v) / total * 100.0 for k, v in grouped.items()}


def weighted_beta(exposure: pd.Series, beta: pd.Series) -> float:
    total = float(exposure.sum())
    if total <= 0:
        return 0.0
    return float((exposure / total * beta).sum())
