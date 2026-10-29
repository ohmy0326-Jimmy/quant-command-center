"""第四頁與下單輔助共用的部位評價。

槓桿、Beta、保證金與淨值的算法與「部位監控與量化風控」相同，
避免兩個頁面各算各的。本模組不做下單、不讀取終端機輸出的金鑰。
"""

from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# 第四頁寫死的元大期貨權益起算點（未實現損益另外加計）。
# 下單輔助沿用同一個常數，試算結果才對得上第四頁。
YUANTA_FUTURES_BASE_EQUITY = 170422.0

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
    return (
        pd.DataFrame(spreadsheet.worksheet("Active_Positions").get_all_records()),
        pd.DataFrame(spreadsheet.worksheet("Daily_NAV").get_all_records()),
        pd.DataFrame(spreadsheet.worksheet("持倉統計").get_all_records()),
    )


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


def market_beta_and_sector(ticker, is_tw: bool) -> tuple[float, str]:
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

    if t_upper in ["TX", "MTX", "FMF"]:
        return 1.0, str(refined_sector)

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
        else:
            beta = 1.0
        return float(round(beta, 2)), str(refined_sector)
    except Exception:
        return 1.0, str(refined_sector)


def normalize_positions(quant_data: pd.DataFrame) -> pd.DataFrame:
    """清洗持倉表。欄位定義與第四頁進入報價迴圈之前相同。"""
    if quant_data is None or quant_data.empty or "標的" not in quant_data.columns:
        return pd.DataFrame()

    df = quant_data.copy()
    df["標的"] = df["標的"].astype(str).str.strip().str.upper()
    df = df[df["標的"] != ""].copy()
    if df.empty:
        return pd.DataFrame()

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


def populate_market_fields(df: pd.DataFrame, beta_fn, twii_price: float):
    """抓即時報價、Beta、產業。期貨用台指現貨，其餘路徑與第四頁相同。"""
    prices, betas, sectors = {}, {}, {}
    if df is None or df.empty or "標的" not in df.columns:
        return prices, betas, sectors

    for ticker in df["標的"].unique().tolist():
        try:
            t_upper = str(ticker).upper()
            if t_upper in ["TX", "MTX", "FMF", "TMF"]:
                prices[ticker] = twii_price
                betas[ticker] = 1.0
                sectors[ticker] = "微型臺指期" if t_upper == "FMF" else ("小型臺指" if t_upper == "MTX" else "臺指期貨")
                continue

            is_tw_market = _page4_is_tw_market(ticker)
            b_val, s_val = beta_fn(ticker, is_tw_market)
            betas[ticker] = b_val
            sectors[ticker] = s_val

            if is_tw_market:
                try:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    res = requests.get(f"https://tw.stock.yahoo.com/quote/{ticker}", headers=headers, timeout=5)
                    match = re.search(r'"regularMarketPrice":(?:{"raw":)?([0-9.]+)', res.text)
                    prices[ticker] = float(match.group(1)) if match else float(yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1])
                except Exception:
                    prices[ticker] = float(yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1])
            else:
                hist = yf.Ticker(ticker).history(period="5d")
                prices[ticker] = float(hist["Close"].iloc[-1]) if not hist.empty else np.nan
        except Exception:
            prices[ticker] = np.nan
            betas[ticker] = 1.0
            sectors[ticker] = "其他"
    return prices, betas, sectors


def attach_quotes(df: pd.DataFrame, prices: dict, betas: dict, sectors: dict) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    out = df.copy()
    out["即時報價"] = out["標的"].map(prices).fillna(out["平均成本"])
    out["Beta值"] = out["標的"].map(betas).fillna(1.0)
    out["產業別"] = out["標的"].map(sectors).fillna("其他")
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
        "margin_ratio": None,
        "futures_equity": 0.0,
        "futures_maint": 0.0,
        "excess_margin": 0.0,
        "margin_call": False,
        "sector_weights": {},
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

        # 期貨認定只看「種類」是否含「期」，與第四頁相同。
        for idx, row in df.iterrows():
            if "期" in str(row["種類"]):
                occupied, maint = _futures_margin_row(str(row["標的"]), abs(float(row["持有數量"])), abs(float(row["名目合約總值(原幣)"])))
                df.at[idx, "實質佔用本金(原幣)"] = occupied
                df.at[idx, "維持保證金(原幣)"] = maint

        is_futures_mask = df["種類"].astype(str).str.contains("期", na=False)

        def calc_futures_pnl(row):
            mult = futures_multiplier(row["標的"])
            return (row["即時報價"] - row["平均成本"]) * row["持有數量"] * mult

        df["淨未實現損益(原幣)"] = np.where(is_futures_mask, df.apply(calc_futures_pnl, axis=1), 0.0)

        stock_mask = ~is_futures_mask
        if stock_mask.any():
            cost = df.loc[stock_mask, "平均成本"]
            notional = df.loc[stock_mask, "名目合約總值(原幣)"]
            price = df.loc[stock_mask, "即時報價"]
            stock_latest = np.where(cost != 0, notional * (price / cost), notional)
            df.loc[stock_mask, "淨未實現損益(原幣)"] = (stock_latest - notional) - (np.abs(stock_latest) * STOCK_TAX_RATE)

        occupied_abs = df["實質佔用本金(原幣)"].abs()
        df["報酬率 (%)"] = np.where(occupied_abs != 0, (df["淨未實現損益(原幣)"] / occupied_abs) * 100.0, 0.0).astype(float)
        df["顯示_投入總金額(原幣)"] = np.where(is_futures_mask, df["實質佔用本金(原幣)"], df["名目合約總值(原幣)"]).astype(float)
        df["顯示_即時市值(原幣)"] = np.where(
            is_futures_mask,
            df["實質佔用本金(原幣)"] + df["淨未實現損益(原幣)"],
            df["即時報價"] * df["持有數量"],
        ).astype(float)
        df["即時市值(台幣)"] = (df["顯示_即時市值(原幣)"].abs() * df["換算匯率"]).astype(float)
        df["實質曝險(台幣)"] = (df["即時市值(台幣)"] * df["標的槓桿"]).astype(float)
    else:
        df = pd.DataFrame()

    if not account_data.empty and "帳戶名稱" in account_data.columns:
        account_data["帳戶名稱"] = account_data["帳戶名稱"].astype(str).str.strip()
        for col in ["現金餘額", "融資/質押借款"]:
            if col not in account_data.columns:
                account_data[col] = 0.0
            account_data[col] = pd.to_numeric(account_data[col], errors="coerce").fillna(0.0).astype(float)

        if not df.empty:
            acc_market = df.groupby("帳戶")["即時市值(台幣)"].sum()
            acc_exp = df.groupby("帳戶")["實質曝險(台幣)"].sum()
            acc_maint = df.groupby("帳戶")["維持保證金(原幣)"].sum()
        else:
            acc_market, acc_exp, acc_maint = pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)

        account_data["即時持股市值"] = account_data["帳戶名稱"].map(acc_market).fillna(0.0).astype(float)
        account_data["名目曝險"] = account_data["帳戶名稱"].map(acc_exp).fillna(0.0).astype(float)
        account_data["維持保證金總額"] = account_data["帳戶名稱"].map(acc_maint).fillna(0.0).astype(float)
        account_data["帳戶淨值"] = 0.0

        for idx, row in account_data.iterrows():
            acc_name = str(row["帳戶名稱"]).strip()
            if "元大期貨" in acc_name:
                subset = df[df["帳戶"] == acc_name] if not df.empty else pd.DataFrame()
                if not subset.empty:
                    contrib = float(subset["淨未實現損益(原幣)"].sum() * subset["換算匯率"].iloc[0])
                else:
                    contrib = 0.0
                net_val = float(YUANTA_FUTURES_BASE_EQUITY + contrib)
                maint_m = float(row["維持保證金總額"])
                account_data.at[idx, "帳戶淨值"] = net_val
                account_data.at[idx, "現金餘額"] = float(max(0.0, net_val - maint_m))
            else:
                cash = float(row["現金餘額"])
                mkt_val = float(row["即時持股市值"])
                debt = float(row["融資/質押借款"])
                account_data.at[idx, "帳戶淨值"] = cash + mkt_val - debt

        nav = float(account_data["帳戶淨值"].sum())
        account_data["槓桿率 (倍)"] = np.round(
            np.where(account_data["帳戶淨值"] != 0, account_data["名目曝險"] / account_data["帳戶淨值"], 0.0),
            2,
        ).astype(float)
        account_data["權益比率%/現金水位%"] = np.round(
            np.where(
                account_data["維持保證金總額"] > 0,
                (account_data["帳戶淨值"] / account_data["維持保證金總額"]) * 100.0,
                np.where(account_data["帳戶淨值"] != 0, (account_data["現金餘額"] / account_data["帳戶淨值"]) * 100.0, 0.0),
            ),
            1,
        ).astype(float)
    else:
        nav = 0.0

    if not df.empty:
        df["真實風險權重 (%)"] = np.where(nav > 0, (df["實質曝險(台幣)"] / nav) * 100.0, 0.0).astype(float)

    metrics = _metrics_from_frames(df, account_data, nav)
    return df, account_data, metrics


def _metrics_from_frames(df: pd.DataFrame, account_data: pd.DataFrame, nav: float) -> dict:
    metrics = _empty_metrics()
    metrics["nav"] = float(nav)
    position_exposure = float(df["實質曝險(台幣)"].sum()) if not df.empty and "實質曝險(台幣)" in df.columns else 0.0
    metrics["position_exposure"] = position_exposure
    if position_exposure > 0:
        metrics["beta"] = float((df["實質曝險(台幣)"] / position_exposure * df["Beta值"]).sum())
    metrics["sector_weights"] = _sector_weights(df)

    if account_data is None or account_data.empty or "帳戶淨值" not in account_data.columns:
        return metrics

    exposure = float(account_data["名目曝險"].sum())
    cash = float(account_data["現金餘額"].sum())
    debt = float(account_data["融資/質押借款"].sum()) if "融資/質押借款" in account_data.columns else 0.0
    metrics["exposure"] = exposure
    metrics["cash"] = cash
    metrics["debt"] = debt
    metrics["leverage"] = (exposure / nav) if nav > 0 else 0.0
    metrics["cash_pct"] = (cash / nav * 100.0) if nav > 0 else 0.0

    names = account_data["帳戶名稱"].astype(str)
    fut = account_data[names.str.contains("元大期貨")]
    if fut.empty and "維持保證金總額" in account_data.columns:
        fut = account_data[account_data["維持保證金總額"] > 0]
    if not fut.empty:
        equity = float(fut["帳戶淨值"].sum())
        maint = float(fut["維持保證金總額"].sum())
        metrics["futures_equity"] = equity
        metrics["futures_maint"] = maint
        metrics["excess_margin"] = float(fut["現金餘額"].sum())
        if maint > 0:
            metrics["margin_ratio"] = equity / maint * 100.0
            metrics["margin_call"] = bool(equity < maint)
    return metrics


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
