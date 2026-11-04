"""第一、二頁 K 線歷史資料的抓取工具。

快取策略：成功的結果交給 st.cache_data 快取 5 分鐘；抓不到或空資料一律丟例外，
st.cache_data 不會快取例外，所以下次重整會重抓，不會把「沒資料」卡住 5 分鐘。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

import pandas as pd


class EmptyHistoryError(RuntimeError):
    """抓到的 K 線是空的（或沒有收盤價）。"""


def _has_close(frame) -> bool:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return False
    if "Close" not in frame.columns:
        return False
    return bool(pd.to_numeric(frame["Close"], errors="coerce").notna().any())


def fetch_history_strict(ticker: str, period: str, downloader: Callable | None = None) -> pd.DataFrame:
    """抓一檔 K 線；失敗或空資料丟例外（讓呼叫端的快取不要存下失敗結果）。"""
    if downloader is None:
        import yfinance as yf

        def downloader(t, p):
            return yf.Ticker(t).history(period=p)

    frame = downloader(ticker, period)
    if not _has_close(frame):
        raise EmptyHistoryError(f"{ticker} ({period}) 沒有有效 K 線資料")
    return frame


def fetch_history_map(pairs: Iterable, loader: Callable, max_workers: int = 8) -> dict:
    """平行呼叫 loader(ticker, period)。單檔失敗回空 DataFrame，不影響其他檔。

    loader 通常是有 st.cache_data 的單檔函式：成功的會被快取，失敗的丟例外不被快取。
    """
    pairs = list(dict.fromkeys(tuple(p) for p in pairs))
    if not pairs:
        return {}

    def one(pair):
        ticker, period = pair
        try:
            frame = loader(ticker, period)
        except Exception:
            return pair, pd.DataFrame()
        return pair, frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()

    found = {}
    workers = max(1, min(max_workers, len(pairs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for pair, frame in pool.map(one, pairs):
            found[pair] = frame
    return found
