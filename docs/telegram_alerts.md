# Telegram 即時警報（只有即時警報，沒有每日摘要）

`POST /v1/alert-check` 由 GCP Cloud Scheduler 每 15 分鐘呼叫一次。每次重算下列狀態，
**只有狀態改變時才推播**，狀態存在 `Quant_Database` 的 `Alert_State` 分頁（沒有會自動建立表頭）。

| key | 內容 | 觸發 | 解除（遲滯） | 等級 |
|---|---|---|---|---|
| `margin` | 元大期貨追繳距離（`order_insights.futures_margin_call_level`） | 台指再變動 ±5% 內會追繳；已追繳時每小時再提醒 | 距離拉開到 6% 以上 | 🚨 緊急 |
| `drawdown` | 回撤煞車燈（第四頁同一條淨值曲線） | 🟡 ≤ −8%、🔴 ≤ −12% | 回升 1 個百分點才降級 | 🔴 緊急 |
| `macro` | 第一頁宏觀風險燈（`macro_risk.py`） | 🟡／🔴 分數門檻 | 少 1 分才降級；⚪ 資料不足時不判斷 | 🔴 緊急 |
| `hedge` | 第六頁避險燈（`hedge_sim.trigger_status`，預設參數） | 任一燈亮 | 收盤站回 20 日線 | 一般 |
| `stop_loss` | 新觸發停損的部位 | 新部位報酬 ≤ 停損線 | 回升到停損線 +2pp 以上才移出名單 | 一般 |
| `leverage` | 合計槓桿 | > 1.5x | < 1.45x | 一般 |
| `cash` | 現金水位 | < 40% | > 42% | 一般 |
| `regime` | 第五頁多空狀態 | 狀態改變 | — | 一般 |
| `quadrant` | 第五頁產業象限 | 任一產業換象限 | — | 一般 |

- **靜音時段 00:00–07:00（台北）**：只有追繳與 🔴 有聲音，其他照送但 `disable_notification=True`。
- **每日上限 12 則**（台北日期）。超過上限或送失敗的警報不推進狀態，下一輪／隔天會再送；緊急的先送。
- 某項資料讀不到時，該項維持上一次狀態、不推播，不會誤報「解除」。
- 第一次執行只記錄多空狀態與產業象限的基準；其他項目以「正常」為基準，所以已經是異常的狀態會推一次。
- 訊息用 `parse_mode=HTML`（內容已跳脫）。第一頁手動推播的 `send_to_telegram` 也改用同一套，`**粗體**` 不再原樣出現。

## 設定（複製貼上）

```bash
PROJECT=quant-dashboard-512010
REGION=asia-east1
SERVICE=quant-command-center-api
# API 的執行身分（Cloud Run 服務帳戶）
SA="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format 'value(spec.template.spec.serviceAccountName)')"
```

### 1. 建立推播金鑰 `qcc-push-key`

```bash
QCC_PUSH_KEY="$(openssl rand -base64 32)"   # 不要 echo
printf '%s' "$QCC_PUSH_KEY" | gcloud secrets create qcc-push-key \
  --project "$PROJECT" --replication-policy automatic --data-file=-
unset QCC_PUSH_KEY
```

### 2. Telegram Bot Token 與 Chat ID 也放進 Secret Manager（已有就跳過）

```bash
read -rs TELEGRAM_BOT_TOKEN   # 貼上後按 Enter，不會顯示
printf '%s' "$TELEGRAM_BOT_TOKEN" | gcloud secrets create telegram-bot-token \
  --project "$PROJECT" --replication-policy automatic --data-file=-
unset TELEGRAM_BOT_TOKEN
read -rs TELEGRAM_CHAT_ID
printf '%s' "$TELEGRAM_CHAT_ID" | gcloud secrets create telegram-chat-id \
  --project "$PROJECT" --replication-policy automatic --data-file=-
unset TELEGRAM_CHAT_ID
```

### 3. 讓 API 服務帳戶可以讀這三個 secret

```bash
for s in qcc-push-key telegram-bot-token telegram-chat-id; do
  gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" \
    --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor
done
```

### 4. 掛到 Cloud Run（用 `--update-secrets`，不要用 `--set-secrets`，否則會拿掉既有的金鑰）

```bash
gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --update-secrets "QCC_PUSH_KEY=qcc-push-key:latest,TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest"
```

沒掛 `QCC_PUSH_KEY` 時端點回 503；金鑰不對回 401；Telegram 沒設定回 503。

### 5. Cloud Scheduler：每 15 分鐘（Asia/Taipei）

```bash
gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT"
SERVICE_URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
gcloud scheduler jobs create http qcc-alert-check \
  --project "$PROJECT" --location "$REGION" \
  --schedule "*/15 * * * *" --time-zone "Asia/Taipei" \
  --uri "${SERVICE_URL}/v1/alert-check" --http-method POST \
  --headers "X-Push-Key=$(gcloud secrets versions access latest --secret qcc-push-key --project "$PROJECT")" \
  --attempt-deadline 180s
```

換金鑰後要用 `gcloud scheduler jobs update http qcc-alert-check ... --update-headers "X-Push-Key=..."` 同步更新。
手動測一次：`gcloud scheduler jobs run qcc-alert-check --project "$PROJECT" --location "$REGION"`。

### 6. 試算表權限（Alert_State 需要寫入）

`Alert_State` 由 `google-service-key` 那把服務帳戶金鑰（JSON 的 `client_email`）寫入。
請把 `Quant_Database` 共用給這個 `client_email`，權限設 **編輯者**；只有檢視者時端點回 502。

## 費用

每 15 分鐘一次 ≈ 每月 2,900 次呼叫。Cloud Scheduler 每個帳號前 3 個 job 免費；Cloud Run 每次幾秒，
落在免費額度內；Telegram Bot API 免費。不呼叫 Gemini。
