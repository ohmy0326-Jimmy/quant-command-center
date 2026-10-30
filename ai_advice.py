"""第四頁 AI 避險基金經理人。

提示詞與戰情室按鈕相同，模型名稱也只留在這裡。
本模組不寫入持倉、不推播 Telegram、不讀取終端機輸出的金鑰。
"""

from __future__ import annotations

RISK_MANAGER_MODEL = "gemini-3.8-flash"

_PROMPT_COLUMNS = (
    "標的",
    "名稱",
    "產業別",
    "Beta值",
    "真實風險權重 (%)",
    "標的槓桿",
    "報酬率 (%)",
)


def page4_advice_figures(metrics: dict) -> tuple[float, float, float, float]:
    """第四頁按鈕送進提示詞前的四個數字。

    槓桿四捨五入到小數兩位、現金水位到小數一位，與畫面上
    ``global_leverage`` / ``global_cash_pct`` 的算法相同。
    回傳 (淨值, 槓桿, Beta, 現金水位百分點)。
    """
    nav = float(metrics["nav"])
    leverage = round(float(metrics["leverage"]), 2) if nav > 0 else 0.0
    cash_pct = round(float(metrics["cash_pct"]), 1) if nav > 0 else 0.0
    beta = float(metrics["beta"])
    return nav, leverage, beta, cash_pct


def build_risk_manager_prompt(df, total_net_liq, global_leverage, portfolio_beta, global_cash_pct) -> str:
    """組出與第四頁按鈕相同的提示詞。數字請先用頁面上那組變數。"""
    if df is None or df.empty:
        positions_summary = "無持倉"
    else:
        positions_summary = df[list(_PROMPT_COLUMNS)].to_string(index=False)
    return (
        f"總帳戶淨值: NT$ {total_net_liq:,.0f}\n"
        f"合併總槓桿率: {global_leverage}x\n"
        f"投資組合加權Beta: {portfolio_beta:.2f}\n"
        f"總現金水位: {global_cash_pct}%\n"
        f"持倉明細:\n{positions_summary}\n"
        f"請給予 3 點風控建議。"
    )


def complete_risk_manager_prompt(prompt: str, api_key: str, timeout_seconds: float | None = None) -> str:
    """呼叫 Gemini，與第四頁按鈕同一條。

    ``timeout_seconds`` 不傳進 SDK。客戶端逾時會變成要求伺服器提前中斷的標頭，
    時限偏短時 Gemini 直接回 500，這支 API 就變成 502。
    時間上限由呼叫端的 ``call_with_timeout`` 負責。
    """
    del timeout_seconds
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=RISK_MANAGER_MODEL, contents=prompt)
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("AI 沒有回傳文字")
    return text


def generate_risk_manager_advice(
    df,
    total_net_liq,
    global_leverage,
    portfolio_beta,
    global_cash_pct,
    api_key: str,
    timeout_seconds: float | None = None,
) -> str:
    """第四頁按鈕與唯讀 API 共用的一輪建議。"""
    prompt = build_risk_manager_prompt(df, total_net_liq, global_leverage, portfolio_beta, global_cash_pct)
    return complete_risk_manager_prompt(prompt, api_key=api_key, timeout_seconds=timeout_seconds)
