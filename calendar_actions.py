"""第三頁手動 AI 與 Telegram 文案。

載入這個模組不會呼叫 Gemini，也不會發送訊息。
按鈕按下後才由呼叫端執行。
"""

from __future__ import annotations

from ai_advice import RISK_MANAGER_MODEL

import calendar_events


def analysis_block_reason(event: dict, api_key: str) -> str:
    title = str(event.get("title") or "").strip()
    when = str(event.get("when_text") or "").strip()
    if not title or not when:
        return "這個事件資料缺漏，沒有送出解析。"
    if not str(api_key or "").strip():
        return "沒有 GEMINI_API_KEY，這次沒有呼叫 AI。"
    return ""


def analysis_prompt(event: dict) -> str:
    impacts = event.get("impacts") or ["沒有對到部位"]
    return (
        "身為避險基金經理人，只依下面這一則行事曆事件給出配置與風控建議。\n"
        f"事件：{event.get('title')}\n"
        f"時間：{event.get('when_text')}｜{event.get('session') or '時段未知'}\n"
        f"確定性：{event.get('certainty') or '未知'}｜來源：{event.get('source') or '未知'}\n"
        "預估："
        + calendar_events.format_number(event.get("estimate"))
        + "｜前值："
        + calendar_events.format_number(event.get("previous"))
        + "｜實際："
        + calendar_events.format_number(event.get("actual"))
        + "\n"
        f"說明：{event.get('note') or '無'}\n"
        "可能相關部位（不是漲跌預測）：\n"
        + "\n".join(impacts)
        + "\n缺的數字不要補 0，也不要編造實際值、利多或利空。不要寫入這一則以外的事件。"
    )


def event_notice(event: dict) -> tuple[str, str] | None:
    title = str(event.get("title") or "").strip()
    when = str(event.get("when_text") or "").strip()
    if not title or not when:
        return None
    lines = [
        f"時間：{when}",
        f"時段：{event.get('session') or '時段未知'}",
        f"確定性：{event.get('certainty') or '未知'}",
        f"來源：{event.get('source') or '未知'}",
        "預估："
        + calendar_events.format_number(event.get("estimate"))
        + "｜前值："
        + calendar_events.format_number(event.get("previous"))
        + "｜實際："
        + calendar_events.format_number(event.get("actual")),
    ]
    if event.get("note"):
        lines.append(f"說明：{event['note']}")
    impacts = event.get("impacts") or []
    if impacts:
        lines.append("部位：")
        lines.extend(str(line) for line in impacts)
    return f"行事曆提醒：{title}", "\n".join(lines)


def memo_status(memo, event: dict) -> str:
    if not isinstance(memo, dict) or not str(memo.get("text") or "").strip():
        return "missing"
    if memo.get("fingerprint") != event.get("fingerprint"):
        return "stale"
    return "ready"


def advice_notice(event: dict, memo) -> tuple[str, str] | None:
    if memo_status(memo, event) != "ready":
        return None
    title = str(event.get("title") or "").strip()
    if not title:
        return None
    return f"行事曆戰術建議：{title}", str(memo["text"]).strip()


def complete_event_analysis(prompt: str, api_key: str) -> str:
    """按下 AI 解析後才呼叫。沒有金鑰或沒有文字就失敗，不回傳空建議。"""
    if not str(api_key or "").strip():
        raise RuntimeError("沒有 GEMINI_API_KEY")
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=RISK_MANAGER_MODEL, contents=prompt)
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("解析未完成")
    return text.strip()
