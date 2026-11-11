# 每日淨值快照（NAV_Snapshots）

`Daily_NAV` 分頁已移除，淨值改由 App 即時計算。為了畫淨值曲線、算最大回撤與上／下檔捕獲率，API 提供一個**唯一的寫入端點**，由你自己的排程器每天呼叫一次，把當天淨值寫進試算表 `Quant_Database` 的 `NAV_Snapshots` 分頁。

## POST /v1/nav-snapshot

```bash
curl -fsS -X POST "${SERVICE_URL}/v1/nav-snapshot" \
  -H "X-Snapshot-Key: ${QCC_SNAPSHOT_KEY}"
```

- 不需要本文。金鑰放標頭 `X-Snapshot-Key`，不要放網址。
- **金鑰與唯讀金鑰分開**：比對環境變數 `QCC_SNAPSHOT_KEY`（SHA-256 後常數時間比較）。`READONLY_API_KEY` 不能拿來寫；`X-API-Key` 標頭在這裡無效。
- **冪等**：依台北日期 upsert 一列。同一天呼叫幾次都只會覆蓋當天那一列，可以放心重試。
- 估價與第四頁相同：`normalize_positions` → 即時報價（`populate_market_fields_parallel`）→ `value_positions_safely`。
- 建議時間：台股收盤後（例如台北 14:00–20:00）。此時 `^GSPC` 用的是前一個美國交易日收盤，捕獲率計算已對齊這個時差。

| 狀態 | `detail` | 意思 |
| --- | --- | --- |
| 200 | — | 寫入成功，回傳寫入的那一列 |
| 401 | `unauthorized` | 沒帶或帶錯 `X-Snapshot-Key` |
| 503 | `nav snapshot disabled` | 服務沒設定 `QCC_SNAPSHOT_KEY`（端點停用） |
| 502 | `snapshot failed` | 讀試算表／報價／寫入失敗，或淨值不是正數。細節只寫在伺服器日誌（祕密已塗掉） |

### 回應

```json
{
  "schema_version": "1.6.0",
  "as_of": "2026-11-04T14:30:05+08:00",
  "status": "written",
  "date": "2026-11-04",
  "action": "inserted",
  "sheet": "NAV_Snapshots",
  "sheet_created": false,
  "row_number": 42,
  "row": {
    "日期": "2026-11-04",
    "總淨值": 12345678,
    "台股淨值": 8000000,
    "美股淨值": 4345678,
    "名目曝險": 18000000,
    "槓桿": 1.4581,
    "帳戶Beta": 1.2034,
    "出入金": 30000,
    "寫入時間": "2026-11-04 14:30:05",
    "USDTWD": 32.1,
    "匯率來源": "live"
  },
  "flow_sources": {"futures_ledger": 0, "cash_flows": 30000},
  "header_migrated": false,
  "usdtwd": 32.1,
  "usdtwd_source": "live",
  "warnings": []
}
```

`action` 是 `inserted`（新增）或 `updated`（同一天已有，覆蓋）。`warnings` 包含匯率不是即時值、表頭補欄、估價時被排除的壞列與 Cash_Flows 看不懂的列。

`macro_history` 是同一天寫進 `Macro_Score_History` 的結果。淨值已經寫入之後才做；這一段失敗時快照仍回 200，`macro_history.status` 是 `failed`。

| status | 意思 |
| --- | --- |
| `written` | 新增或更新台北當天那一列 |
| `kept` | 當天已有相同總分，或這次沒有新分數所以保留原列 |
| `skipped` | 日期或總分不合法，沒有寫 |
| `failed` | 讀寫歷史時發生例外。細節在伺服器日誌，回應不帶例外原文 |

總分只在試算表 `Macro_Score` 九項都有合法分數時才寫。缺項時 `total` 與 `regime` 是 null，儲存格留空，不當 0，也不用倉庫示例補。九項齊時總分用加權（評價權重 0.5，其餘 1）再換算回全部分數。`regime` 由總分對應，不是產業四象限。同一列另外寫 `coverage`（已有權重 / 8.5）；缺項時這欄仍寫，總分維持空白。同一天重跑只覆蓋當天那一列。分頁不存在時會建立並寫 `week`、`total`、`regime`、`coverage`。舊的三欄表頭會在右邊補 `coverage`。不新增排程，沿用既有的每日淨值呼叫。

### 匯率退回值

匯率依序取 即時 → 本程序 3 天內最近成功值 → `NAV_Snapshots` 最後一筆即時匯率 → 預設 32.5（來源定義見 [readonly_api.md](readonly_api.md) 的「匯率來源（1.4.0）」）。不是即時值時**仍會寫入**，但：

- 該列 `匯率來源` 記下 `cached`／`last_known`／`fallback`，`USDTWD` 記實際用的數字；
- 回應 `usdtwd_source` 同值，`warnings` 第一句說明；
- 排程器看到 `usdtwd_source != "live"` 可以稍後重跑，同一天會覆蓋成即時匯率。

## NAV_Snapshots 欄位

分頁不存在時會自動建立並寫表頭。表頭若被改成別的欄位，端點會拒寫（502），避免寫錯欄。

| 欄 | 內容 |
| --- | --- |
| 日期 | 台北日期 yyyy-mm-dd |
| 總淨值 | 台幣，第四頁「淨值」 |
| 台股淨值、美股淨值 | 台幣，分區淨值（捕獲率混合基準的權重） |
| 名目曝險 | 台幣，Σ 名目曝險（期貨為名目市值） |
| 槓桿 | 名目曝險 ÷ 淨值 |
| 帳戶Beta | Σ(名目曝險 × Beta) ÷ 淨值 |
| 出入金 | 當天淨出入金，台幣，入金正、出金負 |
| 寫入時間 | 台北時間 |
| USDTWD | 這次估價用的美元／台幣 |
| 匯率來源 | `live`／`cached`／`last_known`／`fallback` |

**舊表頭升級**：已存在的 9 欄表頭（到「寫入時間」為止）第一次寫入時會自動在 J、K 欄補上 `USDTWD`、`匯率來源`，不動任何既有資料（舊列這兩格留空，視為沒有匯率紀錄）。分頁欄數不夠會先加欄。其他不認得的表頭仍然拒寫。

## 出入金

當天（台北日期）的淨出入金 ＝

1. `Futures_Ledger` 中 `類型` 為 `出入金`／`入金`／`出金` 的列（`出金` 一律算負、`入金` 一律算正；`出入金` 依金額正負），加上
2. 選填的 `Cash_Flows` 分頁。

### Cash_Flows 分頁格式

第一列為表頭：

| 日期 | 帳戶 | 金額 | 幣別 | 備註 |
| --- | --- | --- | --- | --- |
| 2026-11-04 | 美股複委託 | 1000 | USD | 電匯入金 |
| 2026-11-04 | 台股 | -50000 | TWD | 提領 |

- **日期**：yyyy-mm-dd（也接受 2026/11/04）。錢實際進出帳戶的那天。
- **帳戶**：自由文字，只做紀錄。
- **金額**：入金為正、出金為負。可含千分位逗號。
- **幣別**：`TWD`（空白、`NTD`、`台幣` 也可）或 `USD`（`美元`、`美金` 也可）。USD 依當下 USDTWD 換算成台幣。其他幣別會略過並出現在 `warnings`。
- **備註**：自由文字。
- 期貨帳戶的出入金已記在 `Futures_Ledger`，**不要在 Cash_Flows 重複記一次**。

## 第四頁「風險與績效」

- 淨值曲線顯示原始淨值；**最大回撤與回撤煞車燈改用扣除出入金的淨值指數**：I₀＝NAV₀，Iₜ＝Iₜ₋₁ ×（NAVₜ − 出入金ₜ）÷ NAVₜ₋₁。入金不算上漲、出金不算回撤。今天的即時點只會扣掉已寫進快照的當天出入金。
- 淨值曲線、最大回撤、回撤煞車讀 `NAV_Snapshots`；沒有才退回舊的 `Daily_NAV`（若還存在）；兩者都沒有時顯示「淨值歷史無法取得」。今天的點一律用即時淨值。
- 上／下檔捕獲率：
  - 日報酬 ＝ (淨值 − 當天出入金) ÷ 前一筆淨值 − 1，按月連乘成月報酬。
  - 基準：`^TWII`（取快照當天收盤）、`^GSPC`（取前一天以前最近收盤）、以及依前一天台股／美股淨值加權的混合基準。
  - 上檔捕獲 ＝ 基準上漲月份的組合平均月報酬 ÷ 基準平均月報酬 × 100；下檔同理（用基準下跌月份）。上檔 >100% 表示漲時比基準多；下檔 <100% 表示跌時比基準少。
  - 平日報酬少於 60 個時顯示「樣本不足（目前 N 個交易日，需 60）」，也不會去抓基準資料。

## 設定

1. **寫入金鑰**：產生一把新金鑰放進 Secret Manager（例 `qcc-snapshot-key`），掛到 API 服務 `quant-command-center-api` 成環境變數 `QCC_SNAPSHOT_KEY`。沒掛時端點回 503。

   ```bash
   PROJECT=quant-dashboard-512010
   REGION=asia-east1
   SERVICE=quant-command-center-api
   QCC_SNAPSHOT_KEY="$(openssl rand -base64 32)"   # 不要 echo
   printf '%s' "$QCC_SNAPSHOT_KEY" | gcloud secrets create qcc-snapshot-key \
     --project "$PROJECT" --replication-policy automatic --data-file=-
   unset QCC_SNAPSHOT_KEY
   gcloud secrets add-iam-policy-binding qcc-snapshot-key --project "$PROJECT" \
     --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor   # SA＝API 的執行身分
   gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
     --update-secrets "QCC_SNAPSHOT_KEY=qcc-snapshot-key:latest"
   ```

   用 `--update-secrets`，不要用 `--set-secrets`（會拿掉 `READONLY_API_KEY`）。

2. **試算表寫入權限**：寫入用的是 `google-service-key` 那把服務帳戶金鑰（JSON 裡的 `client_email`），不是 Cloud Run 的執行身分。把 `Quant_Database` 用「共用」分享給這個 `client_email`，權限設 **編輯者**。只有檢視者權限時，寫入會 403，端點回 502。

3. **OAuth 範圍**：`portfolio_risk.SHEETS_SCOPES` 現在是 `https://www.googleapis.com/auth/spreadsheets`（讀寫試算表）＋ `https://www.googleapis.com/auth/drive.readonly`（依檔名找到 `Quant_Database`）。舊的範圍（`spreadsheets.google.com/feeds` ＋ 完整 `drive`）本來就允許寫入，所以範圍不是限制；實際的限制是上面的分享權限。新範圍拿掉了完整 Drive 權限，較小。

4. **排程器**：在你自己的機器每天呼叫一次上面的 curl。金鑰從安全的地方讀進變數，不要寫進指令列歷史或日誌，curl 不要加 `-v`。
