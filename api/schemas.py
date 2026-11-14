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
    notional_exposure: float | None = Field(default=None, description="1.3.0 新增。名目曝險，台幣。現股／ETF＝market_value × leverage；期貨＝名目市值。1.6.0 起，選擇權或可轉債／CBAS 沒有 Delta 時是 null，不是 0。Delta 為 0 仍是 0。")
    exposure_known: bool = Field(default=True, description="1.6.0 新增。false 表示這筆名目曝險未知，notional_exposure 是 null。")
    delta_status: Literal["known", "missing", "not_applicable"] = Field(default="not_applicable", description="1.6.0 新增。known 含 Delta 剛好是 0；missing 是沒有 Delta；not_applicable 是現股、ETF、期貨。")
    underlying: str = Field(default="", description="1.3.0 新增。槓桿 ETF 的標的（例 SOXL→SOXX）。非槓桿為空字串。")
    underlying_beta: float | None = Field(default=None, description="1.3.0 新增。槓桿 ETF 標的一年 Beta（對 S&P 500；台股槓桿 ETF 對加權指數定義 1）。非槓桿為 null。")
    beta: float | None = Field(default=None, description="1.3.0 新增。套在名目曝險上的 Beta＝beta_exposure ÷ notional_exposure。槓桿 ETF＝標的一年 Beta；現股＝自身一年 Beta；臺指期＝1。")
    effective_beta: float | None = Field(default=None, description="1.3.0 新增。套在市值上的 Beta＝leverage × beta（槓桿 ETF＝槓桿 × 標的 Beta）。")
    beta_exposure: float | None = Field(default=None, description="1.3.0 新增。Beta 曝險，台幣＝notional_exposure × beta＝market_value × effective_beta（現股）。槓桿只乘一次。空單為負。")
    beta_source: str = Field(default="", description="1.3.0 新增。Beta 來源說明。1.8.0 起，資料不足會寫「Beta 未計入」，不會把缺資料顯示成 Beta＝1。")
    beta_quality: Literal["estimated", "fallback", "definition", "missing"] = Field(default="missing", description="1.8.0 新增。estimated＝這次回歸；fallback＝已標示的預設或手動值；definition＝臺指或加權定義 1；missing＝沒有計入。")
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
    account_beta: float = Field(description="本地基準敏感度＝Beta 曝險 ÷ 這一列使用的淨值。全部 Beta 都估到時等於 leverage × avg_beta。台股對加權、美股對 S&P 500，合計不是單一指數 Beta。")


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


class CreditRowResponse(BaseModel):
    model_config = _MODEL

    id: str
    name: str
    series_id: str = ""
    value: float | None = None
    unit: str = ""
    date: str | None = None
    change: float | None = None
    change_label: str = ""
    light: Literal["green", "yellow", "red", "missing", "stale", "unavailable"]
    light_text: str
    reason: str = ""


class CreditTransmissionResponse(BaseModel):
    """1.9.0。與第一頁信用傳導面板同一套 THRESHOLDS，不計入牛熊總分。"""

    model_config = _MODEL

    stage: str = Field(description="階段句。只有放款量與信用價格同時惡化才是傳導確認。")
    stage_code: Literal["equity_only", "price_first", "confirmed", "volume_only", "insufficient"]
    stage_trigger: Literal["price", "standards", "both"] | None = Field(
        default=None,
        description="1.9.0。stage_code 為 price_first 時：price＝信用價格惡化；standards＝只有放款意願收緊；both＝價格與放款意願一起轉緊。其餘階段是 null。",
    )
    lending_light: Literal["green", "yellow", "red"] | None = Field(description="TOTLL 與工商放款 TOTCI（皆為週資料，52 週年增、13 週年化、21 天過舊）的較差燈。要整體綠燈時兩條都得是綠。TOTCI 抓不到才改用 BUSLOANS，該列標明月資料。")
    price_light: Literal["green", "yellow", "red"] | None = Field(description="高收益 OAS、投資級 OAS、NFCI 的較差燈。STLFSI4 有黃或紅時會把價格燈拉差。")
    standards_light: Literal["green", "yellow", "red"] | None = Field(description="SLOOS 放款意願。缺失或過舊是 null，不算收緊。")
    disclaimer: str
    rule_note: str = Field(description="門檻與快速減速的定義，與畫面上的註解相同。")
    cds: str = Field(description="固定說明沒有免費 CDS，不帶數字。")
    thresholds: dict = Field(description="credit_transmission.THRESHOLDS。燈號只讀這張表。")
    rows: list[CreditRowResponse]


class PledgeAccountResponse(BaseModel):
    """1.11.0。單一帳戶的股票質押維持率。"""

    model_config = _MODEL

    account: str
    balance: float = Field(description="質押借款餘額，台幣。來自 Active_Positions「融資/質押借款」，淨值已扣除。")
    collateral_value: float = Field(description="質押品市值，台幣。")
    collateral_basis: Literal["sheet", "estimated_tw_spot"] = Field(description="sheet＝試算表有標質押品；estimated_tw_spot＝沒標，改用該戶台股現股市值。")
    maintenance_pct: float = Field(description="質押維持率＝質押品市值 ÷ 質押借款 × 100。")
    light: Literal["green", "yellow", "red"]
    drop_to_call_pct: float | None = Field(default=None, description="距 130% 追繳線，質押品還能跌幾個百分點。已低於追繳線時是負數。質押品市值是 0 時是 null。")


class PledgeLoanResponse(BaseModel):
    """1.11.0。股票質押借款。門檻是程式預設，要對照券商契約。"""

    model_config = _MODEL

    balance: float = Field(description="質押借款合計，台幣。沒有質押時是 0。")
    collateral_value: float | None = Field(default=None, description="計入維持率的質押品市值合計。沒有借款時是 null。")
    collateral_basis: Literal["sheet", "estimated_tw_spot", "none"] = "none"
    maintenance_pct: float | None = Field(default=None, description="合計維持率。沒有借款時是 null，不是 0。")
    light: Literal["green", "yellow", "red"] | None = None
    drop_to_call_pct: float | None = Field(default=None, description="距追繳還可跌幾個百分點。已低於 130% 時是負數。")
    estimated: bool = Field(default=False, description="true 表示至少有一戶的質押品是用台股現股市值估算的。")
    thresholds: dict = Field(description="pledge_loan.PLEDGE_THRESHOLDS。call_below_pct 130、yellow_high_pct 160。")
    note: str
    accounts: list[PledgeAccountResponse] = Field(default_factory=list)


class MarginFinancingAccountResponse(BaseModel):
    """1.11.0。真正的融資，不含質押借款。"""

    model_config = _MODEL

    account: str
    balance: float
    market_value: float
    maintenance_pct: float
    drop_to_call_pct: float
    already_call: bool


class MarginFinancingResponse(BaseModel):
    """1.11.0。沒有融資部位時 label 是「無融資」、balance 是 0。"""

    model_config = _MODEL

    balance: float
    label: str
    status: Literal["none", "active"]
    accounts: list[MarginFinancingAccountResponse] = Field(default_factory=list)


class BearIndicatorResponse(BaseModel):
    """1.13.0。單一預警子指標。未知不是通過。"""

    model_config = _MODEL

    name: str
    state: Literal["green", "yellow", "red", "unknown"]
    value: float | None = None
    value_text: str = ""
    threshold: str = ""
    source: str = ""


class BearLayerResponse(BaseModel):
    """1.13.0。一層預警。state 由子指標彙總，未知不算通過。"""

    model_config = _MODEL

    id: str
    name: str
    state: Literal["green", "yellow", "red", "unknown"]
    indicators: list[BearIndicatorResponse]


class BearWarningResponse(BaseModel):
    """1.13.0。熊市預警。不改牛熊分數，也不改 REGIME_RISK。"""

    model_config = _MODEL

    caption: str
    note: str
    regime: str | None = None
    regime_cap: float | None = Field(default=None, description="目前牛熊狀態在 REGIME_RISK 的槓桿上限。狀態未知是 null。")
    warning_cap: float | None = Field(default=None, description="預警調整後的上限。")
    suggested_cap: float | None = Field(default=None, description="min(regime_cap, warning_cap)。無法比較時是 null，不是 0。")
    suggested_action: str = ""
    overall: Literal["green", "yellow", "red", "unknown"] = Field(description="1.14.0。已知各層裡最差的一盞。全未知是 unknown，不是綠。")
    thresholds: dict
    leaders: list[str] = Field(default_factory=list)
    breadth: dict
    layers: list[BearLayerResponse]


class DeleverageTargetResponse(BaseModel):
    """1.12.0。使用者的降槓目標，不是 REGIME_RISK。"""

    model_config = _MODEL

    id: str
    label: str
    target_leverage: float
    reduction_twd: float | None = Field(default=None, description="要降到目標還需減少的名目，台幣。已在目標內是 0。名目不完整是 null，不是 0。")
    progress: float | None = Field(default=None, description="目標倍數 ÷ 目前倍數，0 到 1。滿格代表已落到目標。未知是 null。")
    reached: bool | None = None
    reason: str = ""


class DeleverageSwapResponse(BaseModel):
    """1.12.0。持有的槓桿 ETF 換成 1 倍標的。只列實際持倉。"""

    model_config = _MODEL

    symbol: str
    name: str = ""
    underlying: str | None = None
    multiple: float | None = None
    multiple_source: str = ""
    note: str = ""
    market_value_twd: float | None = None
    notional_twd: float | None = None
    reduction_twd: float | None = Field(default=None, description="換成 1 倍可降低的名目。沒有報價是 null，不是 0。")
    leverage_after: float | None = None
    cumulative_leverage: float | None = Field(default=None, description="由降低名目大的先換，換完之後的組合槓桿。")


class DeleverageResponse(BaseModel):
    """1.12.0。降槓進度。淨值已扣質押借款。"""

    model_config = _MODEL

    label: str
    note: str
    net_value: float | None = None
    notional_exposure: float | None = None
    leverage: float | None = None
    exposure_complete: bool = False
    targets: list[DeleverageTargetResponse] = Field(default_factory=list)
    swaps: list[DeleverageSwapResponse] = Field(default_factory=list)


class PortfolioResponse(BaseModel):
    model_config = _MODEL

    schema_version: str = Field(examples=[SCHEMA_VERSION])
    as_of: str = Field(description="Asia/Taipei，這次回應產生的時間。")
    currency: Literal["TWD"] = Field(description="淨值、曝險、市值、損益的幣別。")
    usdtwd: float = Field(description="這次估價用的美元兌台幣，與第四頁同一來源。")
    usdtwd_source: Literal["live", "cached", "last_known", "fallback"] = Field(default="live", description="1.4.0 新增。live＝即時；cached＝本程序最近一次成功值（3 天內）；last_known＝NAV_Snapshots 最後一筆即時匯率；fallback＝預設 32.5（美股市值可能失真）。")
    warnings: list[str] = Field(default_factory=list, description="1.4.0 新增。估價警告，例如匯率不是即時值。1.6.0 起，缺 Delta 時也會說明合計不完整。")
    exposure_complete: bool = Field(default=True, description="1.6.0 新增。false 表示有部位缺 Delta，exposure、leverage、beta 與 beta_leverage 只加總已知部位。")
    exposure_scope: Literal["complete", "partial"] = Field(default="complete", description="1.6.0 新增。partial＝已知部位的部分合計，不是確認後的全部曝險。")
    unknown_delta_symbols: list[str] = Field(default_factory=list, description="1.6.0 新增。缺少 Delta、因此沒有計入名目曝險的代號。")
    net_value: float = Field(description="總帳戶淨值，台幣。第四頁 metrics.nav。")
    exposure: float = Field(description="已知名目曝險合計，台幣。exposure_complete 為 false 時這是部分合計，缺 Delta 的部位沒有算進去，也不是 0。＝beta_leverage.total.notional_exposure。")
    leverage: float = Field(description="已知名目曝險 ÷ 淨值。exposure_complete 為 false 時不完整。畫面上四捨五入到小數兩位。＝beta_leverage.total.leverage。")
    cash_pct: float = Field(description="現金 ÷ 淨值 × 100，百分點。13.4 表示 13.4%。畫面上四捨五入到小數一位。")
    beta: float = Field(description="本地基準敏感度合計＝(tw_exposure + us_exposure) ÷ 總淨值。台股那段對加權指數，美股那段對 S&P 500，相加後不是單一指數 Beta。第四頁 metrics.beta。＝beta_leverage.total.account_beta。")
    tw_beta: float = Field(description="台股敏感度。sleeve_nav_complete 為 true 時＝tw_exposure ÷ tw_nav；有混合帳戶時＝tw_exposure ÷ 總淨值。＝beta_leverage.tw.account_beta。")
    us_beta: float = Field(description="美股對 S&P 500 的敏感度。sleeve_nav_complete 為 true 時＝us_exposure ÷ us_nav；有混合帳戶時＝us_exposure ÷ 總淨值。＝beta_leverage.us.account_beta。")
    total_beta: float = Field(description="同 beta。本地基準敏感度合計，不是單一指數 Beta。")
    tw_nav: float = Field(description="只含整戶都是台股的帳戶淨值（含該戶現金與元大期貨）。混合帳戶不拆進來。")
    us_nav: float = Field(description="只含整戶都是美股的帳戶淨值（含該戶現金）。混合帳戶不拆進來。")
    sleeve_nav_complete: bool = Field(default=True, description="1.8.0 新增。false 表示有帳戶同時含台股與美股，現金沒有歸屬，tw_nav + us_nav 小於總淨值。")
    unallocated_nav: float = Field(default=0.0, description="1.8.0 新增。混合帳戶的淨值。tw_nav + us_nav + unallocated_nav＝net_value。")
    beta_basis: Literal["sleeve_nav", "total_nav"] = Field(default="sleeve_nav", description="1.8.0 新增。sleeve_nav＝台股／美股各除以自己的淨值；total_nav＝因為現金拆不開，兩邊都除以總淨值。")
    beta_label: str = Field(default="本地基準敏感度合計。台股對加權指數，美股對 S&P 500，不是單一指數 Beta。", description="1.8.0 新增。beta／total_beta 的讀法。")
    beta_complete: bool = Field(default=True, description="1.8.0 新增。false 表示有部位 Beta 沒估到。那些部位的名目仍可計入槓桿，但 Beta 不是 1，也沒有算進 beta。")
    unknown_beta_symbols: list[str] = Field(default_factory=list, description="1.8.0 新增。名目已知、Beta 未計入的代號。")
    tw_exposure: float = Field(description="舊欄位，保留相容。台股 **Beta 曝險** Σ(名目曝險 × Beta)，台幣；不是名目曝險，所以 tw_exposure + us_exposure ≠ exposure。名目曝險請用 beta_leverage.tw.notional_exposure。")
    us_exposure: float = Field(description="舊欄位，保留相容。美股 **Beta 曝險**，台幣；同上。")
    beta_leverage: BetaLeverageResponse = Field(description="1.3.0 新增。台股／美股／合計的槓桿、部位平均 Beta、帳戶 Beta。")
    positions: list[PositionResponse]
    yuanta_futures: YuantaFuturesResponse
    greeks: dict = Field(default_factory=dict, description="1.7.0 新增。台股與美股分開的 Delta／Theta。缺值是 null，合計不完整時 delta_complete 或 theta_complete 為 false，缺的部位沒有當成 0。兩市場不加總。1.10.0 起 greeks.tw.hedge_contracts 是台指期避險口數：大台 200、小台 50、微台 10（每點台幣）。full／half 是小數口數，full_lots／half_lots 是四捨五入。點數不完整或不大於 0 時 available 為 false、rows 是空的。已持有空單已在 delta_points，不另扣。選擇權不給口數。")
    beta_targets: dict = Field(default_factory=dict, description="1.7.0 新增。依第五頁多空狀態的目標 Beta 區間。區間是程式預設。狀態未知時 tw 與 us 是 null。避險只建議同市場工具。")
    credit_transmission: CreditTransmissionResponse = Field(description="1.9.0 新增。信用傳導。各指標的值、日期、較前期變化、燈號，以及階段。門檻與第一頁相同。不計入部位或牛熊分數。")
    pledge: PledgeLoanResponse = Field(description="1.11.0 新增。股票質押借款與質押維持率。Active_Positions「融資/質押借款」整欄都算質押，淨值已扣除，這裡不再扣一次。門檻是程式預設。")
    margin_financing: MarginFinancingResponse = Field(description="1.11.0 新增。真正的融資（「融資」欄）。目前沒有融資部位時 label 為「無融資」。整戶 130% 維持率只套在這個欄，不套質押借款。")
    deleverage: DeleverageResponse = Field(description="1.12.0 新增。使用者降槓目標：偏多上限 2.0 倍、降槓期間 1.5 倍。不是 REGIME_RISK 的偏多 1.8。槓桿用已扣質押借款的淨值。缺報價的降低名目是 null。1.14.0 起同一代號跨帳戶合成一列。")
    bear_warning: BearWarningResponse = Field(description="1.13.0 新增，1.14.0 加 overall。熊市預警燈。不改牛熊分數與 REGIME_RISK。suggested_cap 是牛熊上限與預警調整後上限的較低者。子指標缺資料是 unknown，不算通過。")


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
    macro_history: dict = Field(default_factory=dict, description="同一天寫入 Macro_Score_History 的結果。status 為 written、kept、skipped 或 failed。失敗不會讓淨值快照失敗。total 缺值是 null，不是 0。")
    bear_breadth: dict = Field(default_factory=dict, description="1.14.0。同一天寫入 Bear_Breadth 的廣度。失敗或跳過不會讓淨值快照失敗。沒有成分股收盤時不寫 0。")


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
