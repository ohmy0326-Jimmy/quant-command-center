"""Telegram 推播共用：HTML parse_mode 與跳脫。

Telegram 預設不解析任何格式，舊版把 ``**粗體**`` 原樣送出。這裡一律用 parse_mode=HTML，
先把 <、>、& 跳脫，再把 Markdown 的 **粗體** 轉成 <b>粗體</b>。
"""

from __future__ import annotations

import html
import re

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram 上限 4096，保留餘裕

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def escape(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def markdown_to_html(text) -> str:
    """跳脫後把 **粗體** 轉成 <b>；其餘 Markdown 標記照字面保留（已跳脫，不會壞掉）。"""
    return _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", escape(text))


def titled_message(title, body_markdown) -> str:
    out = f"🤖 <b>【{escape(title)}】</b>\n\n{markdown_to_html(body_markdown)}"
    return out if len(out) <= MAX_MESSAGE_CHARS else out[: MAX_MESSAGE_CHARS - 1] + "…"


def send_message(bot_token: str, chat_id: str, html_text: str, *, disable_notification: bool = False, post=None, timeout: float = 10):
    """送出一則 HTML 訊息，回傳 requests 的 Response。不記錄 token。"""
    if post is None:
        import requests

        post = requests.post
    return post(
        f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": bool(disable_notification),
        },
        timeout=timeout,
    )
