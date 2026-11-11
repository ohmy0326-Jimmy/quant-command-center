from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

from hedge_sim import HedgeParams, simulate
from hedge_sim_ui import (
    light_text,
    make_nav_figure,
    pick_txo_expiry,
    recommendation_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def test_pick_txo_expiry_closest_monthly():
    today = datetime.date(2026, 10, 1)
    dates_map = {
        "202610": datetime.date(2026, 10, 21),  # 20 days
        "202611": datetime.date(2026, 11, 18),  # 48 days (closest to 50)
        "202612": datetime.date(2026, 12, 16),  # 76 days
        "202610W1": datetime.date(2026, 10, 7),
    }

    def fn(code):
        return dates_map.get(code)

    expiries = ["202610W1", "202610", "202611", "202612"]
    code, exp_d = pick_txo_expiry(expiries, fn, today, tenor_days=50)
    assert code == "202611"
    assert exp_d == datetime.date(2026, 11, 18)


def test_pick_txo_expiry_ignores_past_and_empty():
    today = datetime.date(2026, 10, 1)
    dates_map = {
        "202609": datetime.date(2026, 9, 16),  # past
        "202610W2": datetime.date(2026, 10, 14),  # not monthly 6-digit
    }
    code, exp_d = pick_txo_expiry(["202609", "202610W2"], lambda c: dates_map.get(c), today, 50)
    assert code is None
    assert exp_d is None

    empty_code, empty_d = pick_txo_expiry([], lambda c: None, today, 50)
    assert empty_code is None
    assert empty_d is None


def test_light_text_all_cases():
    assert light_text(True) == "🔴 成立"
    assert light_text(False) == "🟢 未成立"
    assert light_text(None) == "⚪ 無資料"


def test_recommendation_rows_handles_none_quote():
    rec = {
        "legs": [
            {
                "strike": 22000.0,
                "is_long": True,
                "model_premium": 152.3,
                "quote": None,
                "used_premium": 152.3,
                "source": "BS 估計",
            },
            {
                "strike": 20500.0,
                "is_long": False,
                "model_premium": 50.1,
                "quote": 48.5,
                "used_premium": 48.5,
                "source": "報價",
            },
        ]
    }
    rows = recommendation_rows(rec)
    assert len(rows) == 2
    assert rows[0]["腳位"] == "買進賣權"
    assert rows[0]["即時報價"] == "無報價"
    assert rows[0]["BS 估計（點）"] == 152.3
    assert rows[1]["腳位"] == "賣出賣權"
    assert rows[1]["即時報價"] == "48.5"


def test_make_nav_figure_returns_figure_with_two_axes():
    rng = np.random.default_rng(3)
    dates = pd.date_range("2019-06-03", periods=600, freq="B")
    ret = rng.normal(0.0003, 0.012, size=len(dates))
    px = pd.Series(10000.0 * np.exp(np.cumsum(ret)), index=dates)

    params = HedgeParams()
    tools = ("none", "futures")
    res = simulate(px, params, tools=tools)

    fig = make_nav_figure(res, tools)
    try:
        assert isinstance(fig, plt.Figure)
        assert len(fig.axes) == 2
    finally:
        plt.close(fig)


def test_render_hedge_strategy_sim_smoke():
    script = f"""
import sys
sys.path.insert(0, {repr(str(ROOT))})

import unittest.mock as mock
import pandas as pd
import numpy as np
from types import SimpleNamespace
import hedge_sim_ui

rng = np.random.default_rng(42)
dates = pd.date_range("2021-01-04", periods=700, freq="B")
ret = rng.normal(0.0004, 0.011, size=len(dates))
fake_px = pd.Series(14000.0 * np.exp(np.cumsum(ret)), index=dates, name="^TWII")

snapshot = SimpleNamespace(
    metrics={{
        "nav": 3.45e6,
        "beta_exposure": 6.9e6,
        "tw_beta_exposure": 5.0e6,
        "account_beta": 2.0,
        "tw_account_beta": 1.6,
        "tw_nav": 3.0e6,
    }},
    twii=23000.0,
)

macro_view = {{
    "total": 2,
    "history": [{{"week": "2026-10-19", "total": 6}}],
    "score_updated_at": "2026-10-26",
}}

with mock.patch.object(hedge_sim_ui, "load_taiex_history", return_value=fake_px):
    with mock.patch.object(hedge_sim_ui, "txo_expiry", return_value=("202612", "2026-12-16")):
        with mock.patch.object(hedge_sim_ui, "txo_put_quote", return_value=None):
            hedge_sim_ui.render_hedge_strategy_sim(snapshot, macro_view)
"""
    at = AppTest.from_string(script)
    at.run(timeout=60)
    assert not at.exception

    radio = at.radio(key="hss_rec_tool")
    assert "期貨" in radio.value

    run_btn = at.button(key="hss_run")
    run_btn.click()
    at.run(timeout=60)
    assert not at.exception
    assert len(at.dataframe) >= 1


def test_hedge_sim_lives_in_combined_panel_not_strategy_simulator():
    ui = (ROOT / "nav_scenario_sim_ui.py").read_text(encoding="utf-8")
    assert "模擬工具" not in ui and "render_hedge_strategy_sim" not in ui
    page = (ROOT / "order_assistant_ui.py").read_text(encoding="utf-8")
    panel = (ROOT / "sim_panel_ui.py").read_text(encoding="utf-8")
    assert "render_combined_sim_panel(snapshot, view, float(rules.target_leverage))" in page
    assert "render_hedge_strategy_sim" in panel
    assert "render_nav_scenario_simulator" in panel
    tab = page[page.index("with tab_hedge:"):page.index("with tab_stress:")]
    assert "render_hedge_strategy_sim" not in tab


def test_futures_hedge_uses_tw_beta_exposure_only():
    src = (ROOT / "hedge_sim_ui.py").read_text(encoding="utf-8")
    assert 'metrics_dict.get("tw_beta_exposure"' in src
    assert 'metrics_dict.get("beta_exposure"' not in src
    assert "hss_scope" not in src


def test_hedge_tool_shows_no_quote():
    script = f"""
import sys
sys.path.insert(0, {repr(str(ROOT))})
import unittest.mock as mock
import numpy as np
import pandas as pd
from types import SimpleNamespace
import hedge_sim_ui

dates = pd.date_range("2021-01-04", periods=700, freq="B")
fake_px = pd.Series(np.linspace(14000.0, 23000.0, len(dates)), index=dates, name="^TWII")
snapshot = SimpleNamespace(metrics={{"nav": 3.45e6, "beta_exposure": 6.9e6, "tw_beta_exposure": 5.0e6}}, twii=23000.0, valued=None)
with mock.patch.object(hedge_sim_ui, "load_taiex_history", return_value=fake_px), \\
     mock.patch.object(hedge_sim_ui, "txo_expiry", return_value=("202612", "2026-12-16")), \\
     mock.patch.object(hedge_sim_ui, "txo_put_quote", return_value=None):
    hedge_sim_ui.render_hedge_strategy_sim(snapshot, None)
"""
    at = AppTest.from_string(script)
    at.session_state["hss_rec_tool"] = "月滾賣權"
    at.run(timeout=60)
    assert not at.exception
    cells = [str(v) for df in at.dataframe for v in df.value.to_numpy().ravel()]
    assert "無報價" in cells
