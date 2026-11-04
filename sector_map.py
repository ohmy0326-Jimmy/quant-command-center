"""第四頁與第五頁共用的產業分類。只在這裡改。

TICKER_INDUSTRY：個股與 ETF 對到第五頁二十個產業的名稱（例如 SOXL、MUU）。
OTHER_SECTOR：對不到第五頁產業的指數型 ETF、期貨，第四頁照舊顯示這些標籤。
"""

from __future__ import annotations

# ETF 與個股 → 第五頁產業。槓桿 ETF 依主要成分或單一標的歸類。
ETF_INDUSTRY_MAP: dict[str, str] = {
    "SOXL": "AI 算力",  # 費半三倍，前幾大是 NVDA、AVGO、AMD
    "MUU": "記憶體",  # MU 兩倍
    "SNXX": "記憶體",  # SanDisk 兩倍
    "AMUU": "AI 算力",  # AMD 兩倍
    "LITX": "光通訊/CPO",  # LITE 兩倍
}

STOCK_INDUSTRY_MAP: dict[str, str] = {
    "2330": "先進製程",
    "TSM": "先進製程",
    "NVDA": "AI 算力",
    "AMD": "AI 算力",
    "AVGO": "AI 算力",
    "MSFT": "軟體雲端",
    "AAPL": "消費電子",
    "金居": "PCB/載板",
}

# 不屬於二十個產業的部位：第四頁沿用原本標籤，第五頁不歸類。
OTHER_SECTOR: dict[str, str] = {
    "KORU": "韓國指數",
    "GOOGL": "數位廣告與雲端",
    "META": "社群與AI",
    "00631L": "台股槓桿ETF",
    "00663L": "台股槓桿ETF",
    "0050": "台灣50",
    "006208": "台灣50",
    "TX": "臺指期貨",
    "MTX": "小型臺指",
    "FMF": "微型臺指期",
    "TMF": "微型臺指期",
}

TICKER_INDUSTRY: dict[str, str] = {**STOCK_INDUSTRY_MAP, **ETF_INDUSTRY_MAP}

# 第四頁用的完整表（原本的 DETAILED_SECTOR_MAP）。
DETAILED_SECTOR_MAP: dict[str, str] = {**OTHER_SECTOR, **TICKER_INDUSTRY}

_SUFFIXES = (".TWO", ".TW", ".US", ".HK")


def _key(ticker) -> str:
    text = str(ticker or "").strip().upper()
    for suffix in _SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def industry_for_ticker(ticker) -> str | None:
    """第五頁產業名稱；對不到回傳 None。"""
    return TICKER_INDUSTRY.get(_key(ticker))


def sector_for_ticker(ticker, default: str | None = None) -> str | None:
    """第四頁產業別。"""
    return DETAILED_SECTOR_MAP.get(_key(ticker), default)
