# 唯讀 API

給另一個助理做每日簡報用。不經過戰情室的密碼頁，也不提供任何寫入。

計算仍走 `portfolio_risk.py` 的 `finalize_valuation`（淨值、實質曝險、槓桿、現金水位、Beta、元大期貨權益與維持保證金）。AI 文字仍走 `ai_advice.py`，提示詞與第四頁「AI 避險基金經理人」按鈕相同，模型是 `gemini-3.8-flash`。

這個服務要另外部署，不要蓋掉正式站 `quant-command-center`。映像用 `api/Dockerfile`，建置上下文是倉庫根目錄，才拷得到 `portfolio_risk.py`。根目錄的 `Dockerfile` 仍是 Streamlit。

## 網址

部署完成後，Cloud Run 會給一個基底網址，下面寫成 `SERVICE_URL`：

```text
GET {SERVICE_URL}/v1/health
GET {SERVICE_URL}/v1/portfolio
GET {SERVICE_URL}/v1/ai-advice
```

沒有其他路徑。沒有 POST、PUT、PATCH、DELETE。金鑰放在標頭，不要放在網址（存取日誌會記下 query string）。

| 路徑 | 金鑰 | 做什麼 |
| --- | --- | --- |
| `/v1/health` | 不要 | 活著就回 `status: ok` |
| `/v1/portfolio` | 要 | 第四頁同一套部位數字 |
| `/v1/ai-advice` | 要 | 呼叫第四頁同一套 Gemini 風控建議 |

OpenAPI（不含部位資料）：`{SERVICE_URL}/openapi.json` 與 `{SERVICE_URL}/docs`。

## 標頭

```http
X-API-Key: <READONLY_API_KEY>
```

伺服器用 SHA-256 後做常數時間比較，對照環境變數 `READONLY_API_KEY`。這個變數由 Secret Manager 的 `readonly-api-key` 掛上來。沒帶、帶錯、或伺服器沒設定金鑰，都回 **401**，本文是 `unauthorized`，不會把你送進來的值寫進回應或日誌。

## 共通欄位

每個 JSON 都有：

- `schema_version`：目前 `1.0.0`
- `as_of`：`Asia/Taipei` 的 ISO 8601，例如 `2026-10-29T22:00:00+08:00`

機器可讀的 JSON Schema 在 [`readonly_api.schema.json`](readonly_api.schema.json)，由 `api/schemas.py` 的 Pydantic 模型產生。

## GET /v1/health

不需要金鑰。

```json
{
  "schema_version": "1.0.0",
  "as_of": "2026-10-29T22:00:00+08:00",
  "status": "ok"
}
```

## GET /v1/portfolio

成功時 **200**。數字與第四頁相同，幣別是台幣。`leverage` 是實質曝險 ÷ 淨值（畫面上會再四捨五入到小數兩位）。`cash_pct` 是現金 ÷ 淨值 × 100 的百分點（畫面上四捨五入到小數一位），`9.5` 表示 9.5%。`beta` 是曝險加權 Beta。

`price` 是原幣報價（臺指期是指數點，美股是美元）。`market_value` 與 `pnl` 是台幣：分別對應第四頁的「即時市值(台幣)」與「淨未實現損益(原幣) × 換算匯率」。股票損益已含 0.3% 證交稅估算，不會再從淨值扣一次。期貨市值是佔用保證金加未實現損益。

`yuanta_futures.equity` 是元大期貨權益（170422 起算再加未實現損益）。`maintenance_margin` 是維持保證金。`margin_ratio_pct` 是權益 ÷ 維持保證金 × 100。`margin_call` 在權益低於維持保證金時為 true。

```json
{
  "schema_version": "1.0.0",
  "as_of": "2026-10-29T22:00:00+08:00",
  "currency": "TWD",
  "usdtwd": 30.0,
  "net_value": 527922.0,
  "exposure": 1033500.0,
  "leverage": 1.957675,
  "cash_pct": 9.471247,
  "beta": 1.064828,
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
    "equity": 370422.0,
    "maintenance_margin": 538000.0,
    "margin_ratio_pct": 68.852416,
    "excess_margin": 0.0,
    "margin_call": true
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

Google 試算表仍用正式服務那把服務帳戶去讀 Secret Manager 的 `google-service-key`，程式路徑與第四頁相同（`load_service_account_info` → `fetch_quant_sheets`）。這支 API 只呼叫讀取。

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
