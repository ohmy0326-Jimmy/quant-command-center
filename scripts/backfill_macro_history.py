"""重建每週自動分數。預設只印計畫，不寫試算表。

使用者要寫入時才加 --write。已有總分的日期會跳過，新列的 source 是 backfill。
部署不會執行這個檔案。
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="寫入 Macro_Score_History。不加這個參數就只印說明。")
    args = parser.parse_args()
    print(json.dumps({
        "write": bool(args.write),
        "note": "這個檔案不讀金鑰。第五頁「寫入回補列」才會寫試算表，而且略過已有總分的日期。",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
