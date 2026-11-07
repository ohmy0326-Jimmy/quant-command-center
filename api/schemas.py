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
    market_value: float | None = Field(description="即時市值，台幣。第四頁欄位「即時市值(台幣)」。1.3.0 起期貨為名目市值（報價 × 乘數 × 口數），不再是保證金加未實現損益（舊值見 equity_value）。")
    pnl: float | None = Field(description="淨未實現損益，台幣。原幣損益乘上換算匯率。股票損益已含 0.3% 證交稅估算，不再從淨值扣一次。")
    equity_value: float | None = Field(default=None, description="1.3.0 新增。計入帳戶淨值的價值，台幣。現股＝市值；期貨＝保證金＋未實現損益（1.2.0 的 market_value）。")
    leverage: float | None = Field(default=None, description="1.3.0 新增。採用的槓桿倍數。現股 1、槓桿 ETF 2 或 3、期貨 1（槓桿已在名目裡）。")
    notional_exposure: float | None = Field(default=None, description="1.3.0 新增。名目曝險，台幣。現股／ETF＝market_value × leverage；期貨＝名目市值。")
    underlying: str = Field(default="", description="1.3.0 新增。槓桿 ETF 的標的（例 SOXL→SOXX）。非槓桿為空字串。")
    underlying_beta: float | None = Field(default=None, description="1.3.0 新增。槓桿 ETF 標的一年 Beta（對 S&P 500；台股槓桿 ETF 對加權指數定義 1）。非槓桿為 null。")
    beta: float | None = Field(default=None, description="1.3.0 新增。套在名目曝險上的 Beta＝beta_exposure ÷ notional_exposure。槓桿 ETF＝標的一年 Beta；現股＝自身一年 Beta；臺指期＝1。")
    effective_beta: float | None = Field(default=None, description="1.3.0 新增。套在市值上的 Beta＝leverage × beta（槓桿 ETF＝槓桿 × 標的 Beta）。")
    beta_exposure: float | None = Field(default=None, description="1.3.0 新增。Beta 曝險，台幣＝notional_exposure × beta＝market_value × effective_beta（現股）。槓桿只乘一次。空單為負。")
    beta_source: str = Field(default="", description="1.3.0 新增。Beta 來源說明。")
    product: str = Field(default="", description="1.5.0 新增。可轉債為 cb、可轉債資產交換為 cbas，其餘空字串。")
    delta: float | None = Field(default=None, description="1.5.0 新增。可轉債／CBAS 的股權 Delta（轉換股數的比例）。試算表有填就用試算表；可轉債否則用轉換價值／市價或債底估算。沒有資料時為 null。")
    delta_source: str = Field(default="", description="1.5.0 新增。Delta 來源：試算表、估算(轉換價值/市價)、估算(債底)、估算(Black-Scholes)。")
    conversion_value: float | None = Field(default=None, description="1.5.0 新增。整筆轉換價值，台幣，＝股價 × (面額／轉換價格) × 張數。張數為負時為負。算不出來是 null。")
    conversion_value_points: float | None = Field(default=None, description="1.5.0 新增。轉換價值的百元報價＝股價／轉換價格 × 100，跟可轉債報價同一單位，不乘張數。")
    parity_premium_pct: float | None = Field(default=None, description="1.5.0 新增。可轉債轉換溢價率（百分點）＝百元報價／轉換價值百元 − 1，再乘 100。CBAS 或資料不足時為 null。")


class BetaLeverageLegResponse(BaseModel):
    model_config = _MODEL

    notional_exposure: float = Field(description="名目曝險，台幣。現股／ETF＝市值 × 槓桿；期貨＝報價 × 乘數 × 口數；選擇權＝|Delta 名目|。")
    beta_exposure: float = Field(description="Beta 曝險＝Σ(名目曝險 × Beta)，台幣。")
    nav: float = Field(description="淨值，台幣。")
    leverage: float = Field(description="槓桿＝名目曝險 ÷ 淨值。")
    avg_beta: float = Field(description="部位平均 Beta＝Beta 曝險 ÷ 名目曝險。")
    account_beta: float = Field(description="帳戶 Beta＝Beta 曝險 ÷ 淨值＝leverage × avg_beta。")


class BetaLeverageResponse(BaseModel):
    model_config = _MODEL

    tw: BetaLeverageLegResponse = Field(description="台股（含臺指期、台股選擇權）。")
    us: BetaLeverageLegResponse = Field(description="美股。")
    total: BetaLeverageLegResponse = Field(description="合計。tw + us 的名目曝險、Beta 曝險、淨值分別相加等於 total。")


class YuantaFuturesResponse(BaseModel):
    model_config = _MODEL

    equity: float = Field(description="元大期貨權益數，台幣。本日餘額（試算表現金欄，空白時用 168308）加期貨未實現損益（期交所報價）。")
    cash_ledger: float = Field(default=0.0, description="元大期貨本日餘額（現金賬）。")
    initial_margin: float = Field(default=0.0, description="原始保證金總額，台幣。")
    maintenance_margin: float = Field(description="維持保證金合計，台幣。")
    risk_indicator_pct: float | None = Field(default=None, description="權益比率＝權益數 ÷ 原始保證金 × 100。")
    margin_ratio_pct: float | None = Field(description="權益 ÷ 維持保證金 × 100。沒有保證金時為 null。")
    excess_margin: float = Field(description="可動用保證金＝權益數−原始保證金（可為負）。")
    margin_call_distance: float = Field(default=0.0, description="距追繳金額＝權益數 − 維持保證金（低於維持保證金時為負）。")
    margin_call_points: float | None = Field(default=None, description="距追繳點數＝距追繳金額 ÷ 臺指期每點市值總合。無期貨部位時為 null。")
    margin_call: bool = Field(description="權益低於維持保證金時為 true。")


class PortfolioResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei，這次回應產生的時間。")
    currency: Literal["TWD"] = Field(description="淨值、曝險、市值、損益的幣別。")
    usdtwd: float = Field(description="這次估價用的美元兌台幣，與第四頁同一來源。")
    usdtwd_source: Literal["live", "cached", "last_known", "fallback"] = Field(default="live", description="1.4.0 新增。live＝即時；cached＝本程序最近一次成功值（3 天內）；last_known＝NAV_Snapshots 最後一筆即時匯率；fallback＝預設 32.5（美股市值可能失真）。")
    warnings: list[str] = Field(default_factory=list, description="1.4.0 新增。估價警告，例如匯率不是即時值。")
    net_value: float = Field(description="總帳戶淨值，台幣。第四頁 metrics.nav。")
    exposure: float = Field(description="總名目曝險，台幣。第四頁 metrics.exposure。＝beta_leverage.total.notional_exposure。")
    leverage: float = Field(description="名目曝險 ÷ 淨值。畫面上四捨五入到小數兩位。＝beta_leverage.total.leverage。")
    cash_pct: float = Field(description="現金 ÷ 淨值 × 100，百分點。13.4 表示 13.4%。畫面上四捨五入到小數一位。")
    beta: float = Field(description="帳戶 Beta（合計）＝(tw_exposure + us_exposure) ÷ 總淨值＝Σ(名目曝險 × Beta) ÷ 淨值。第四頁 metrics.beta。＝beta_leverage.total.account_beta。")
    tw_beta: float = Field(description="台股帳戶 Beta＝tw_exposure ÷ tw_nav。＝beta_leverage.tw.account_beta。")
    us_beta: float = Field(description="美股帳戶 Beta＝us_exposure ÷ us_nav。＝beta_leverage.us.account_beta。")
    total_beta: float = Field(description="同 beta。")
    tw_nav: float = Field(description="台股淨值（台股市值 + 元大期貨權益 + 台股現金 - 融資/質押）。")
    us_nav: float = Field(description="美股淨值（US 市值 + US 現金 - US 借款）。")
    tw_exposure: float = Field(description="舊欄位，保留相容。台股 **Beta 曝險** Σ(名目曝險 × Beta)，台幣；不是名目曝險，所以 tw_exposure + us_exposure ≠ exposure。名目曝險請用 beta_leverage.tw.notional_exposure。")
    us_exposure: float = Field(description="舊欄位，保留相容。美股 **Beta 曝險**，台幣；同上。")
    beta_leverage: BetaLeverageResponse = Field(description="1.3.0 新增。台股／美股／合計的槓桿、部位平均 Beta、帳戶 Beta。")
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


class NavSnapshotResponse(BaseModel):
    """POST /v1/nav-snapshot。寫入（或覆蓋）NAV_Snapshots 當天那一列。"""

    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str
    status: Literal["written"]
    date: str = Field(description="快照的台北日期 yyyy-mm-dd。")
    action: Literal["inserted", "updated"] = Field(description="inserted＝新增一列；updated＝同一天已有，覆蓋。")
    sheet: str
    sheet_created: bool
    row_number: int
    row: dict = Field(description="寫入的那一列，欄名與試算表相同：日期、總淨值、台股淨值、美股淨值、名目曝險、槓桿、帳戶Beta、出入金、寫入時間、USDTWD、匯率來源。")
    flow_sources: dict = Field(description="出入金拆分：futures_ledger、cash_flows（台幣）。")
    header_migrated: bool = Field(default=False, description="舊 9 欄表頭這次補上了 USDTWD、匯率來源。")
    usdtwd: float
    usdtwd_source: Literal["live", "cached", "last_known", "fallback"] = Field(default="live", description="live＝即時；cached＝本程序最近一次成功值；last_known＝NAV_Snapshots 最後一筆即時匯率；fallback＝預設 32.5。非 live 時 warnings 會說明。")
    warnings: list[str]


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
