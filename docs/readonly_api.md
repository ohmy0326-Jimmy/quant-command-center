# 唯讀 API

給另一個助理做每日簡報用。不經過戰情室的密碼頁。GET 全部唯讀；唯一的寫入是每日淨值快照 `POST /v1/nav-snapshot`，用另一把金鑰，見 [nav_snapshot.md](nav_snapshot.md)。

計算仍走 `portfolio_risk.py` 的 `finalize_valuation`（淨值、實質曝險、槓桿、現金水位、Beta、元大期貨權益與維持保證金）。AI 文字仍走 `ai_advice.py`，提示詞與第四頁「AI 避險基金經理人」按鈕相同，模型是 `gemini-3.8-flash`。

這個服務要另外部署，不要蓋掉正式站 `quant-command-center`。映像用 `api/Dockerfile`，建置上下文是倉庫根目錄，才拷得到 `portfolio_risk.py`。根目錄的 `Dockerfile` 仍是 Streamlit。

## 網址

部署完成後，Cloud Run 會給一個基底網址，下面寫成 `SERVICE_URL`：

```text
GET {SERVICE_URL}/v1/health
GET {SERVICE_URL}/v1/portfolio
GET {SERVICE_URL}/v1/ai-advice
POST {SERVICE_URL}/v1/nav-snapshot   # 寫入，另一把金鑰
```

沒有其他路徑。除了 `POST /v1/nav-snapshot` 之外沒有 POST、PUT、PATCH、DELETE。金鑰放在標頭，不要放在網址（存取日誌會記下 query string）。

| 路徑 | 金鑰 | 做什麼 |
| --- | --- | --- |
| `/v1/health` | 不要 | 活著就回 `status: ok` |
| `/v1/portfolio` | 要 | 第四頁同一套部位數字 |
| `/v1/ai-advice` | 要 | 呼叫第四頁同一套 Gemini 風控建議 |
| `POST /v1/nav-snapshot` | `X-Snapshot-Key`（`QCC_SNAPSHOT_KEY`） | 寫入當天淨值到 `NAV_Snapshots`，見 [nav_snapshot.md](nav_snapshot.md) |

OpenAPI（不含部位資料）：`{SERVICE_URL}/openapi.json` 與 `{SERVICE_URL}/docs`。

## 標頭

```http
X-API-Key: <READONLY_API_KEY>
```

伺服器用 SHA-256 後做常數時間比較，對照環境變數 `READONLY_API_KEY`。這個變數由 Secret Manager 的 `readonly-api-key` 掛上來。沒帶、帶錯、或伺服器沒設定金鑰，都回 **401**，本文是 `unauthorized`，不會把你送進來的值寫進回應或日誌。

## 共通欄位

每個 JSON 都有：

- `schema_version`：目前 `1.13.0`。1.7.0 已有 `greeks` 與 `beta_targets`。1.8.0 再加上 Beta 品質與混合帳戶：`beta_complete`、`unknown_beta_symbols`、`sleeve_nav_complete`、`unallocated_nav`、`beta_basis`、`beta_label`，部位多 `beta_quality`。1.9.0 加 `credit_transmission`（階段、放款量燈、信用價格燈、放款意願燈、各列的值／日期／變化／燈號、門檻表）。這塊不計入淨值或 Beta。`beta` 是台股對加權、美股對 S&P 500 的敏感度相加後除以總淨值，不是單一指數 Beta。資料不足的 Beta 不計入，也不是 1。1.6.0 的缺 Delta 規則仍在。
- `as_of`：`Asia/Taipei` 的 ISO 8601，例如 `2026-10-29T22:00:00+08:00`

機器可讀的 JSON Schema 在 [`readonly_api.schema.json`](readonly_api.schema.json)，由 `api/schemas.py` 的 Pydantic 模型產生。

## GET /v1/health

不需要金鑰。

```json
{
  "schema_version": "1.7.0",
  "as_of": "2026-10-29T22:00:00+08:00",
  "status": "ok"
}
```

## GET /v1/portfolio

### 匯率來源（1.4.0）

`usdtwd_source` 說明這次用的美元／台幣從哪來，依序嘗試：

| 值 | 意思 |
| --- | --- |
| `live` | Yahoo `USDTWD=X` 最近收盤（20～50 之間才算數） |
| `cached` | 即時抓不到，用這個程序 3 天內最近一次成功的即時匯率 |
| `last_known` | 用 `NAV_Snapshots` 最後一筆「匯率來源＝live」的 `USDTWD` |
| `fallback` | 以上都沒有，用預設 32.5；美股市值與淨值可能失真 |

不是 `live` 時 `warnings` 會有一句說明。第四頁也會顯示同一句黃色警告。

成功時 **200**。數字與第四頁相同，幣別是台幣。`leverage` 是名目曝險 ÷ 淨值（畫面上會再四捨五入到小數兩位）。`cash_pct` 是現金 ÷ 淨值 × 100 的百分點（畫面上四捨五入到小數一位），`9.5` 表示 9.5%。`total_beta` 為總組合 Beta（(台股與台指期貨 Beta 曝險 + 美股 Beta 曝險) ÷ 總淨值），`beta` 欄位亦設為 `total_beta`。`tw_beta` 與 `us_beta` 分別為台股/台指期貨與美股分區 Beta（分區 Beta 曝險 ÷ 分區淨值）。`tw_nav` 與 `us_nav` 分別為台股/台指期貨與美股分區淨值。`tw_exposure` 與 `us_exposure` 分別為台股/台指期貨與美股的實質 Beta 曝險（臺指期貨曝險現為名目合約總值：口數 × 指數點 × 乘數）。

### 槓桿與 Beta 的口徑（1.3.0）

| 名詞 | 定義 |
|---|---|
| 名目曝險 | 現股／ETF＝市值 × 槓桿倍數（SOXL 3、MUU 2、00663L 2、一般股票 1）；期貨＝報價 × 乘數 × 口數；選擇權＝\|Delta × 口數 × 乘數 × 標的價\|；可轉債／CBAS＝\|Delta × 轉換價值\| |
| Beta | 套在名目曝險上的 Beta。槓桿 ETF＝**標的**一年 Beta（SOXL→SOXX 對 S&P 500；台股槓桿 ETF 對加權指數定義 1）；一般股票＝自身一年 Beta；臺指期＝1 |
| Beta 曝險 | Σ(名目曝險 × Beta)。等於 市值 × (槓桿 × 標的 Beta)，**槓桿只乘一次** |
| 槓桿 | 名目曝險 ÷ 淨值 |
| 部位平均 Beta | Beta 曝險 ÷ 名目曝險 |
| 帳戶 Beta | Beta 曝險 ÷ 淨值＝槓桿 × 部位平均 Beta |

`beta_leverage.tw`／`.us`／`.total` 各有 `notional_exposure`、`beta_exposure`、`nav`、`leverage`、`avg_beta`、`account_beta`。台股＋美股＝合計（名目曝險、Beta 曝險、淨值都可直接相加）。

舊欄位對照：`exposure`＝`beta_leverage.total.notional_exposure`、`leverage`＝`beta_leverage.total.leverage`、`beta`／`total_beta`＝`beta_leverage.total.account_beta`、`tw_beta`／`us_beta`＝分區帳戶 Beta。**`tw_exposure`／`us_exposure` 一直是 Beta 曝險，不是名目曝險**，所以兩者相加不等於 `exposure`；要對名目曝險請用 `beta_leverage.tw.notional_exposure` 與 `beta_leverage.us.notional_exposure`。

部位層新增：`equity_value`（計入淨值的價值；期貨＝保證金＋未實現損益）、`leverage`、`notional_exposure`、`underlying`、`underlying_beta`、`beta`（套在名目上）、`effective_beta`（套在市值上＝槓桿 × 標的 Beta）、`beta_exposure`、`beta_source`。

1.5.0 再加可轉債／CBAS（`product` 為 `cb` 或 `cbas`，其他部位是空字串）：`delta`、`delta_source`、`conversion_value`（整筆轉換價值，台幣）、`conversion_value_points`（百元報價，不乘張數）、`parity_premium_pct`（只有可轉債；CBAS 為 null）。市值進淨值，名目曝險是 Delta × 轉換價值，不加標的槓桿，也不把 CB 與 CBAS 互抵。欄位怎麼填見 [cb_holdings.md](cb_holdings.md)。

1.6.0：選擇權、可轉債、CBAS 沒有 Delta 時，`delta_status` 是 `missing`，`exposure_known` 是 false，`notional_exposure` 與 `beta_exposure` 是 null。Delta 剛好是 0 時 `delta_status` 是 `known`，名目曝險是 0。現股、ETF、期貨的 `delta_status` 是 `not_applicable`。`exposure`、`leverage`、`beta`、`beta_leverage` 只加總已知部位。`exposure_complete` 為 false、`exposure_scope` 為 `partial` 時，這些合計不完整，`warnings` 會寫明，不能把缺的部位當成沒有曝險。舊客戶仍讀得到同一個數字欄位。

1.7.0：`greeks.tw` 與 `greeks.us` 分開。`delta_notional` 是原生貨幣的有號名目（台股 TWD、美股 USD）。`delta_points` 是這份名目 ÷ 該市場指數價；沒有指數價就是 null。`theta_per_day` 是每日時間價值；選擇權沒有 Theta 時整側是 null。`beta_targets` 的區間寫在 `portfolio_greeks.DEFAULT_BETA_BANDS`，是程式預設。狀態未知時不給目標，也不估算避險口數。台股口數只用台指期或小台，美股只用 SPX、XSP 或 SPY 的名目。

1.8.0：美股 Beta 對 S&P 500（`^GSPC`），看種類是不是美股，不再用「代號剛好三個字母」判成台股。同一輪所有美股共用一次下載的基準，取最近 252 個基準交易日，至少 60 個重疊日報酬；共變異與變異都用樣本數減一；數字存完整精度，畫面才四捨五入到兩位。不足時 `beta_quality` 是 `missing`，`beta_exposure` 是 null，不是 1，`beta_complete` 為 false。已知槓桿／反向 ETF（含 SOXS＝−3）用契約倍數，名目取絕對值，Beta 只乘一次，不用 ETF 自己的報酬。選擇權的標的價不用權利金代替；缺 Delta 或標的價整筆不算。空單淨值是負的市值。`tw_nav`／`us_nav` 只含整戶同一市場的帳戶；混合帳戶放 `unallocated_nav`，此時 `beta_basis` 是 `total_nav`，台股與美股敏感度都除以總淨值。舊欄位名稱都還在。1.7.0 的 `greeks` 與 `beta_targets` 保留。PR47 讀的實際 Beta 就是這裡算出來的數字。

1.13.0：`bear_warning`。三層預警，不改牛熊分數，也不改 REGIME_RISK。`suggested_cap` 是牛熊上限與預警調整後上限的較低者。子指標缺資料是 `unknown`，不算通過。廣度沒有成分股收盤時是未知。台股廣度若要算，代理是 0050 加 0051，不是全體加權成分。

1.12.0：`deleverage`。偏多上限 2.0 倍、降槓期間 1.5 倍，是使用者的降槓目標，不是 REGIME_RISK 的偏多 1.8。`leverage` 用已扣質押借款的淨值。`targets[].reduction_twd` 是要降到該倍數還需減少的名目。`swaps` 只列實際持有、且在對照表裡的槓桿 ETF，依降低名目由大到小。沒有報價時 `reduction_twd` 是 null，不是 0。00663L 的 1 倍對應是加權指數 `^TWII`，不是 0050。

1.11.0：`pledge` 與 `margin_financing`。`pledge` 是股票質押借款（Active_Positions「融資/質押借款」），淨值已經扣過。`maintenance_pct` 是質押品市值 ÷ 借款 × 100；Sheet 沒標質押品時 `estimated` 為 true，用該戶台股現股市值。`drop_to_call_pct` 是距 130% 追繳還能跌幾個百分點，已低於追繳線時是負數。`light` 低於 130% 是 red、130–160% 是 yellow、高於 160% 是 green。門檻在 `thresholds`，是程式預設。`margin_financing.label` 在沒有融資部位時是「無融資」。整戶 130% 只套融資，不套質押。

1.10.0：`greeks.tw.hedge_contracts`。用台股 `delta_points`（每點台幣）除以大台 200、小台 50、微台 10，得到全避險與半避險（50%）要放空的小數口數，以及四捨五入後的建議口數。`available` 為 false 時 `rows` 是空的，`reason` 說明點數不完整或不大於 0。`note` 寫明只定期貨，選擇權口數暫不提供。已持有的台指期空單已在 `delta_points`，不另扣。不進 Telegram，也不進總經分數。

1.9.0：`credit_transmission`。`stage`／`stage_code` 是階段；`stage_trigger` 在 `price_first` 時是 `price`、`standards` 或 `both`，其餘是 null。`lending_light`、`price_light`、`standards_light` 是 green、yellow、red 或 null。放款量是 TOTLL 與工商放款 TOTCI，都是週資料。TOTCI 抓不到時該列改為 BUSLOANS，並標明月資料。`rows` 每一列有 `id`、`name`、`series_id`、`value`、`unit`、`date`、`change`、`change_label`、`light`、`light_text`、`reason`。`thresholds` 與第一頁 `credit_transmission.THRESHOLDS` 是同一張表。`cds` 固定說明沒有免費來源。抓不到時 `value` 是 null、`light` 是 `missing`，不沿用舊值。這塊不進牛熊分數，也不推播。

`price` 是原幣報價（臺指期是指數點，美股是美元）。`market_value` 與 `pnl` 是台幣：分別對應第四頁的「即時市值(台幣)」與「淨未實現損益(原幣) × 換算匯率」。股票損益已含 0.3% 證交稅估算，不會再從淨值扣一次。1.3.0 起期貨 `market_value` 是名目市值（報價 × 乘數 × 口數），舊的「保證金加未實現損益」改放 `equity_value`。

`yuanta_futures.equity` 是元大期貨權益數＝本日餘額（`cash_ledger`）＋期貨未實現損益（期交所即時／夜盤價）。本日餘額與第四頁同一條：有 `Futures_Ledger` 校正點就自動推算（校正餘額＋校正日之後的 `Trade_Log` 已實現損益−手續費−期交稅±出入金）；分頁或校正點缺少時退回 Active_Positions 元大期貨現金欄（目前是 B7）；現金欄也無效才用備援 168308。欄位格式見 [`futures_ledger.md`](futures_ledger.md)。

`initial_margin`／`maintenance_margin` 是原始／維持保證金。`risk_indicator_pct` 是權益數 ÷ 原始保證金 × 100（對應元大「風險指標」）；`margin_ratio_pct` 是權益數 ÷ 維持保證金 × 100。`excess_margin` 是可動用保證金＝權益數－原始保證金（可為負）。`margin_call_distance` 是權益數－維持保證金，`margin_call_points` 換算成臺指點數。`margin_call` 在權益低於維持保證金時為 true。

```json
{
  "schema_version": "1.7.0",
  "as_of": "2026-10-29T22:00:00+08:00",
  "currency": "TWD",
  "usdtwd": 30.0,
  "usdtwd_source": "live",
  "warnings": [],
  "net_value": 525808.0,
  "exposure": 4332500.0,
  "leverage": 8.2397,
  "cash_pct": 9.509174,
  "beta": 8.28154,
  "tw_beta": 8.357965,
  "us_beta": 3.0,
  "total_beta": 8.28154,
  "tw_nav": 518308.0,
  "us_nav": 7500.0,
  "tw_exposure": 4332000.0,
  "us_exposure": 22500.0,
  "positions": [
    {
      "symbol": "2330",
      "name": "2330",
      "qty": 1000.0,
      "price": 110.0,
      "market_value": 110000.0,
      "pnl": 9670.0
    }
  ],
  "yuanta_futures": {
    "equity": 368308.0,
    "maintenance_margin": 538000.0,
    "margin_ratio_pct": 68.458736,
    "excess_margin": -332692.0,
    "margin_call": true,
    "cash_ledger": 168308.0,
    "initial_margin": 701000.0,
    "risk_indicator_pct": 52.540371,
    "margin_call_distance": -169692.0,
    "margin_call_points": -848.46
  }
}
```

上面這組數字是單元測試用的假持倉，不是線上帳戶；部位陣列這裡只示範一列。報價失敗時，該欄位可能是 `null`。

## GET /v1/ai-advice

成功時 **200**。會先讀同一份部位，再送出與第四頁按鈕相同的提示詞。

```json
{
  "schema_version": "1.0.0",
  "as_of": "2026-10-29T22:05:00+08:00",
  "text": "建議內文",
  "model": "gemini-3.8-flash",
  "cached": false,
  "generated_at": "2026-10-29T22:05:00+08:00"
}
```

`cached: true` 表示這次沒有再呼叫 Gemini。預設記住 30 分鐘（`AI_ADVICE_CACHE_TTL_SECONDS`，預設 `1800`）。同一個實例兩次模型呼叫至少間隔 60 秒（`AI_ADVICE_MIN_INTERVAL_SECONDS`）。模型呼叫超過 25 秒就放棄（`AI_ADVICE_TIMEOUT_SECONDS`）。這些上限記在該實例的記憶體，重啟或換一台就重新計算。快取期限內部位若有變動，回的仍是上一則建議。

回應都帶 `Cache-Control: no-store`。

## 錯誤

失敗時同樣有 `schema_version`、`as_of`、`detail`。`detail` 是固定短句，不含持倉、也不含金鑰。

| HTTP | detail | 意思 |
| --- | --- | --- |
| 401 | `unauthorized` | 沒帶、帶錯，或服務沒掛 `READONLY_API_KEY` |
| 429 | `rate limited` | 冷卻中且沒有可回的建議。標頭有 `Retry-After`（秒） |
| 502 | `portfolio unavailable` 或 `ai advice unavailable` | 讀取或模型失敗 |

讀取持倉失敗時，伺服器日誌（`api.main` WARNING）會記錄路由、例外型別、例外訊息與最後 5 層堆疊（上限 2,000 字）。寫入前會塗掉祕密：名稱像祕密的環境變數（`*_KEY`、`*_TOKEN`、`*SECRET*`、`*PASSWORD*`、`*CREDENTIALS*`…）的值、PEM 私鑰與 `private_key` 欄位、`api_key=`／`token:`／`Authorization` 之類的值、Bearer 權杖、常見金鑰格式（`AIza…`、`ghp_…`、`sk-…`、`ya29.…`、JWT），以及 32 字以上混合大小寫與數字的 base64 類字串。回應本體仍只有 `portfolio unavailable`，不含任何細節。
| 503 | `ai advice is not configured` | 沒有 `GEMINI_API_KEY` |
| 504 | `ai advice timed out` | 超過 `AI_ADVICE_TIMEOUT_SECONDS` |

## 環境變數

| 變數 | 來源 | 用途 |
| --- | --- | --- |
| `READONLY_API_KEY` | Secret Manager `readonly-api-key` | 標頭比對 |
| `GEMINI_API_KEY` | 與第四頁相同，掛成環境變數 | 只給 `/v1/ai-advice` |
| `AI_ADVICE_TIMEOUT_SECONDS` | 選填，預設 25 | 模型硬逾時 |
| `AI_ADVICE_CACHE_TTL_SECONDS` | 選填，預設 1800 | 建議快取 |
| `AI_ADVICE_MIN_INTERVAL_SECONDS` | 選填，預設 60 | 兩次模型呼叫的最短間隔 |
| `QCC_SNAPSHOT_KEY` | 選填，Secret Manager（例 `qcc-snapshot-key`） | 只給 `POST /v1/nav-snapshot`；沒設定時該端點回 503 |

Google 試算表仍用正式服務那把服務帳戶去讀 Secret Manager 的 `google-service-key`，程式路徑與第四頁相同（`load_service_account_info` → `fetch_quant_sheets`）。GET 只讀；`POST /v1/nav-snapshot` 會寫 `NAV_Snapshots` 分頁，服務帳戶需要 `Quant_Database` 的編輯者權限（見 [nav_snapshot.md](nav_snapshot.md)）。

## 部署

在倉庫根目錄、這條分支上執行。不要合併到 `main`。完整指令也寫在 PR 說明裡。

```bash
set -euo pipefail
set +x
trap 'unset READONLY_API_KEY' EXIT

PROJECT=quant-dashboard-512010
REGION=asia-east1
SERVICE=quant-command-center-api

# 產生金鑰。不要 echo、不要 set -x。
READONLY_API_KEY="$(openssl rand -base64 32)"
printf '%s' "$READONLY_API_KEY" | gcloud secrets create readonly-api-key \
  --project "$PROJECT" \
  --replication-policy=automatic \
  --data-file=-
unset READONLY_API_KEY

SA="$(gcloud run services describe quant-command-center \
  --project "$PROJECT" \
  --region "$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [ -z "${SA}" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "runtime service account: ${SA}"

gcloud secrets add-iam-policy-binding readonly-api-key \
  --project "$PROJECT" \
  --member "serviceAccount:${SA}" \
  --role "roles/secretmanager.secretAccessor"

gcloud artifacts repositories describe cloud-run-source-deploy \
  --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 || \
gcloud artifacts repositories create cloud-run-source-deploy \
  --project "$PROJECT" --location "$REGION" \
  --repository-format=docker \
  --description="Cloud Run images"

TAG="$(date -u +%Y%m%d%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:${TAG}"

gcloud builds submit \
  --project "$PROJECT" \
  --config api/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}"

gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --image "$IMAGE" \
  --platform managed \
  --port 8080 \
  --service-account "${SA}" \
  --allow-unauthenticated \
  --timeout 180 \
  --memory 2Gi \
  --cpu 1 \
  --max-instances 2 \
  --quiet \
  --set-secrets "READONLY_API_KEY=readonly-api-key:latest"
```

`--allow-unauthenticated` 只打開 Cloud Run 的入口。`/v1/portfolio` 與 `/v1/ai-advice` 仍要 `X-API-Key`。`--set-secrets` 會清掉這顆服務上沒寫進這條旗標的 secret；第一次部署這樣即可。

`/v1/ai-advice` 還需要 `GEMINI_API_KEY`。先只列出 secret 名稱，不要印出內容：

```bash
gcloud secrets list --project "$PROJECT" --format="value(name)"
```

名稱對上第四頁那把 Gemini 金鑰之後，用 `--update-secrets` 補上（不要再下一次 `--set-secrets`，否則會拿掉 `READONLY_API_KEY`）：

```bash
gcloud run services update "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --update-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest"
```

若 secret 名稱不是 `GEMINI_API_KEY`，只改等號右邊。

## 測試

金鑰從 Secret Manager 讀進變數。不要 `echo`，curl 不要加 `-v`。

```bash
set +x
URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --format='value(status.url)')"

curl -sS "${URL}/v1/health"
echo

code="$(curl -sS -o /tmp/unauth.json -w "%{http_code}" "${URL}/v1/portfolio")"
echo "portfolio without key: ${code}"
rm -f /tmp/unauth.json

READONLY_API_KEY="$(gcloud secrets versions access latest --secret=readonly-api-key --project="$PROJECT")"
curl -sS -H "X-API-Key: ${READONLY_API_KEY}" "${URL}/v1/portfolio"
echo
curl -sS -H "X-API-Key: ${READONLY_API_KEY}" "${URL}/v1/ai-advice"
echo
unset READONLY_API_KEY
```

沒帶金鑰的部位請求應為 `401`。後面兩行應為 `200`。`/v1/ai-advice` 會真的呼叫 Gemini；30 分鐘內再打一次會看到 `"cached": true`。

本機（不連網）：

```bash
python3 -m pip install -r api/requirements.txt httpx
python3 -m unittest tests.test_readonly_api tests.test_order_assistant -v
```

若 `readonly-api-key` 已經存在，不要重跑 `secrets create`。改版本時用下面這行，一樣不要印出金鑰：

```bash
READONLY_API_KEY="$(openssl rand -base64 32)"
printf '%s' "$READONLY_API_KEY" | gcloud secrets versions add readonly-api-key \
  --project "$PROJECT" \
  --data-file=-
unset READONLY_API_KEY
```
