"""唯讀 API 的回應模型。欄位說明同時寫進 OpenAPI 與 JSON Schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from portfolio_briefing import SCHEMA_VERSION

_MODEL = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei，ISO 8601，含時區偏移。")
    status: Literal["ok"]


class PositionResponse(BaseModel):
    model_config = _MODEL

    symbol: str = Field(description="標的代號，對應第四頁「標的」。")
    name: str = Field(description="名稱。試算表沒填時為空字串。")
    qty: float | None = Field(description="持有數量。")
    price: float | None = Field(description="即時報價，原幣。臺指期是指數點數，美股是美元。")
    market_value: float | None = Field(description="即時市值，台幣。第四頁欄位「即時市值(台幣)」。期貨為保證金加未實現損益。")
    pnl: float | None = Field(description="淨未實現損益，台幣。原幣損益乘上換算匯率。股票損益已含 0.3% 證交稅估算，不再從淨值扣一次。")


class YuantaFuturesResponse(BaseModel):
    model_config = _MODEL

    equity: float = Field(description="元大期貨權益，台幣。基期 170422 再加未實現損益，與第四頁相同。")
    maintenance_margin: float = Field(description="維持保證金合計，台幣。")
    margin_ratio_pct: float | None = Field(description="權益 ÷ 維持保證金 × 100。沒有保證金時為 null。")
    excess_margin: float = Field(description="期貨帳戶現金餘額（權益高於維持保證金的部分）。")
    margin_call: bool = Field(description="權益低於維持保證金時為 true。")


class PortfolioResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei，這次回應產生的時間。")
    currency: Literal["TWD"] = Field(description="淨值、曝險、市值、損益的幣別。")
    usdtwd: float = Field(description="這次估價用的美元兌台幣，與第四頁同一來源。")
    net_value: float = Field(description="總帳戶淨值，台幣。第四頁 metrics.nav。")
    exposure: float = Field(description="總實質曝險，台幣。第四頁 metrics.exposure。")
    leverage: float = Field(description="實質曝險 ÷ 淨值。畫面上四捨五入到小數兩位。")
    cash_pct: float = Field(description="現金 ÷ 淨值 × 100，百分點。13.4 表示 13.4%。畫面上四捨五入到小數一位。")
    beta: float = Field(description="曝險加權 Beta。第四頁 metrics.beta。")
    positions: list[PositionResponse]
    yuanta_futures: YuantaFuturesResponse


class AdviceResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei，這次回應產生的時間。")
    text: str = Field(description="AI 避險基金經理人的風控建議，與第四頁按鈕同一套提示詞。")
    model: str = Field(description="Gemini 模型名稱。")
    cached: bool = Field(description="true 表示這次沒有重新呼叫模型。")
    generated_at: str = Field(description="Asia/Taipei，這段文字實際產生的時間。")


class ErrorResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei。")
    detail: str


def export_json_schema() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "header": "X-API-Key",
        "models": {
            "health": HealthResponse.model_json_schema(),
            "portfolio": PortfolioResponse.model_json_schema(),
            "ai_advice": AdviceResponse.model_json_schema(),
            "error": ErrorResponse.model_json_schema(),
        },
    }
