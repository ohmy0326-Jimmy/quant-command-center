# Quant Command Center

Streamlit 戰情室（`Dashboard.py`，六頁）與另外部署的 API（`api/`）。

- API 文件：[docs/readonly_api.md](docs/readonly_api.md)
- 每日淨值快照 `POST /v1/nav-snapshot`、環境變數 `QCC_SNAPSHOT_KEY`、`Cash_Flows` 分頁格式：[docs/nav_snapshot.md](docs/nav_snapshot.md)
- 元大期貨帳本：[docs/futures_ledger.md](docs/futures_ledger.md)
- 下單輔助系統：[docs/order_assistant_v2.md](docs/order_assistant_v2.md)

測試：`python -m pytest -q`
