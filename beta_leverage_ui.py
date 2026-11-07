"""第四頁、第六頁共用的「槓桿／部位平均 Beta／帳戶 Beta」三列表（台股、美股、合計）。"""

from __future__ import annotations

import pandas as pd

from portfolio_risk import BETA_LEVERAGE_NOTE, beta_leverage_rows


def beta_leverage_frame(metrics: dict) -> pd.DataFrame:
    return pd.DataFrame(beta_leverage_rows(metrics))


def render_beta_leverage_table(st, metrics: dict) -> pd.DataFrame:
    frame = beta_leverage_frame(metrics)
    st.dataframe(
        frame.style.format({
            "名目曝險": "NT$ {:,.0f}",
            "Beta曝險": "NT$ {:,.0f}",
            "淨值": "NT$ {:,.0f}",
            "槓桿": "{:.2f}x",
            "部位平均Beta": "{:.2f}",
            "帳戶Beta": "{:.2f}",
        }),
        width="stretch",
        hide_index=True,
    )
    st.caption(BETA_LEVERAGE_NOTE)
    from portfolio_risk import beta_quality_warning

    quality = beta_quality_warning(metrics)
    if quality:
        st.warning(quality)
    return frame
