"""策略劇情卡：JSON 載入、S0 優先、硬規則橫幅、加第十二張卡。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from strategy_cards import (
    DATA_PATH,
    RadarContext,
    block_text_for,
    builtin_settlement_dates,
    evaluate_radar,
    hard_rule_banner,
    load_strategy_cards,
    locked_hard_rules,
    portfolio_drawdown_pct,
)

ROOT = Path(__file__).resolve().parents[1]


def _deck():
    deck = load_strategy_cards()
    for card in deck["cards"]:
        if card["card_id"] == "S0":
            card["priority"] = 99
    return deck


def _calm(**extra):
    metrics = {"leverage": 1.2, "account_beta": 0.4, "cash_pct": 30.0, "nav": 1_000_000}
    metrics.update(extra)
    return metrics


def _row(rows, card_id):
    return next(row for row in rows if row["card"]["card_id"] == card_id)


def test_seed_loads_s0_through_s11_from_json():
    deck = load_strategy_cards()
    assert deck["errors"] == []
    assert [card["card_id"] for card in deck["cards"]] == [f"S{i}" for i in range(12)]
    by_id = {card["card_id"]: card for card in deck["cards"]}
    assert by_id["S0"]["title"] == "先活下來"
    assert by_id["S0"]["category"] == "defense"
    assert by_id["S0"]["status"] == "live"
    assert by_id["S1"]["category"] == "settlement"
    assert by_id["S7"]["trigger_rules"] == "VIX_peak>35 AND drawdown_from_peak>=15%"
    assert by_id["S9"]["data_sources"] == "新聞手動標記"
    assert deck["hard_rules"]["max_leverage_bullish"] == 1.8
    assert deck["hard_rules"]["min_cash_pct"] == 12
    assert deck["hard_rules"]["drawdown_brakes"] == [-8, -12, -16, -20]
    assert deck["ui"]["panel_title"] == "策略卡／劇情雷達"


def test_card_copy_is_not_hardcoded_in_python():
    banned = ("先活下來", "先處理 S0", "結算地板Put價差", "國安基金進場", "死亡螺旋")
    for name in ("strategy_cards.py", "strategy_cards_ui.py", "order_assistant_ui.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, name


def test_hard_rule_banner_ignores_overrides():
    deck = load_strategy_cards()
    overrides = {"max_leverage_bullish": 2.5, "min_cash_pct": 1, "drawdown_brakes": [-1]}
    banner = hard_rule_banner(deck, overrides)
    assert banner.startswith("硬規則（不可覆蓋）")
    assert "1.8" in banner
    assert "2.5" not in banner
    assert "現金 ≥ 12%" in banner
    for level in ("−8", "−12", "−16", "−20"):
        assert level in banner
    assert locked_hard_rules(deck, overrides) == {
        "max_leverage_bullish": 1.8,
        "min_cash_pct": 12.0,
        "drawdown_brakes": [-8.0, -12.0, -16.0, -20.0],
    }


def test_s0_red_sorts_first_and_blocks_other_cards():
    deck = _deck()
    rows = evaluate_radar(deck, RadarContext(metrics=_calm(leverage=2.4)))
    assert rows[0]["card"]["card_id"] == "S0"
    assert rows[0]["light"] == "red"
    assert "lev>regime_cap" in rows[0]["reasons"]
    phrase = block_text_for(deck, "S0")
    assert phrase == "先處理 S0"
    assert all(row["block_text"] == phrase for row in rows if row["card"]["card_id"] != "S0")
    assert _row(rows, "S7")["block_text"] == phrase

    calm = evaluate_radar(deck, RadarContext(metrics=_calm()))
    assert _row(calm, "S0")["light"] == "green"
    assert calm[0]["card"]["card_id"] != "S0"
    assert all(row["block_text"] == "" for row in calm)


def test_s0_fires_on_cash_drawdown_or_panic_not_on_a_short_book():
    deck = load_strategy_cards()
    cash = evaluate_radar(deck, RadarContext(metrics=_calm(cash_pct=11.9)))
    assert _row(cash, "S0")["light"] == "red"
    assert "cash<min" in _row(cash, "S0")["reasons"]

    exact = evaluate_radar(deck, RadarContext(metrics=_calm(leverage=1.8, cash_pct=12)))
    assert _row(exact, "S0")["light"] == "green"

    deep = evaluate_radar(deck, RadarContext(metrics=_calm(), drawdown_pct=-8))
    assert _row(deep, "S0")["light"] == "red"
    assert "drawdown_brakes" in _row(deep, "S0")["reasons"]
    shallow = evaluate_radar(deck, RadarContext(metrics=_calm(), drawdown_pct=-7.9))
    assert _row(shallow, "S0")["light"] == "green"

    panic = evaluate_radar(deck, RadarContext(metrics=_calm(), panic_urge=True))
    assert _row(panic, "S0")["light"] == "red"
    assert "panic_urge" in _row(panic, "S0")["reasons"]

    short = evaluate_radar(deck, RadarContext(metrics=_calm(leverage=3.0, account_beta=-0.4)))
    assert _row(short, "S0")["light"] == "green"


def test_s1_warmup_s7_vix_and_s9_manual_mark():
    deck = load_strategy_cards()
    today = date(2026, 11, 6)
    on_day = evaluate_radar(
        deck,
        RadarContext(metrics=_calm(), settlement_dates=[today], today=today, vix_series=[[18.0, 19.0]]),
    )
    assert _row(on_day, "S1")["light"] == "yellow"
    assert _row(on_day, "S1")["mode"] == "auto"

    far = date(2026, 12, 16)
    assert (far - today).days > 7
    later = evaluate_radar(
        deck,
        RadarContext(metrics=_calm(), settlement_dates=[far], today=today, vix_series=[[18.0, 19.0]]),
    )
    assert _row(later, "S1")["light"] == "green"

    missing = evaluate_radar(deck, RadarContext(metrics=_calm(), settlement_dates=None, vix_series=None))
    assert _row(missing, "S1")["mode"] == "manual"
    assert _row(missing, "S7")["mode"] == "manual"

    peaked = [20.0] * 8 + [40.0, 30.0]
    vix_red = evaluate_radar(
        deck,
        RadarContext(metrics=_calm(), settlement_dates=[], today=today, vix_series=[peaked]),
    )
    assert _row(vix_red, "S7")["light"] == "red"

    vix_warm = evaluate_radar(
        deck,
        RadarContext(metrics=_calm(), settlement_dates=[], today=today, vix_series=[[40.0, 36.0]]),
    )
    assert _row(vix_warm, "S7")["light"] == "yellow"

    marked = evaluate_radar(deck, RadarContext(metrics=_calm(), manual_marks={"S9": True}, vix_series=[[10.0]]))
    assert _row(marked, "S9")["light"] == "red"
    assert _row(marked, "S9")["mode"] == "mark"
    unmarked = evaluate_radar(deck, RadarContext(metrics=_calm(), vix_series=[[10.0]]))
    assert _row(unmarked, "S9")["light"] == "off"


def test_twelfth_card_appears_when_added_to_json(tmp_path):
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    raw["cards"].append(
        {
            "card_id": "S12",
            "title": "第十二張",
            "category": "swing",
            "priority": 12,
            "status": "draft",
            "trigger_rules": ["new_rule"],
            "trigger_zh": "新卡觸發",
            "actions": ["先觀察", "再下手"],
            "max_loss_rule": "新卡上限",
            "data_sources": "manual",
            "notes": "",
            "notes_url": "https://example.test/s12",
            "extra_flag": True,
        }
    )
    path = tmp_path / "strategy_cards.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    deck = load_strategy_cards(path)
    card = next(item for item in deck["cards"] if item["card_id"] == "S12")
    assert card["title"] == "第十二張"
    assert card["trigger_rules"] == "new_rule"
    assert card["actions"] == "先觀察；再下手"
    assert card["extra_flag"] is True
    assert card["notes_url"] == "https://example.test/s12"
    rows = evaluate_radar(deck, RadarContext(metrics=_calm(), vix_series=[[10.0]], settlement_dates=[]))
    added = _row(rows, "S12")
    assert added["light"] == "off"
    assert added["mode"] == "manual"
    assert len(rows) == 13


def test_bad_card_is_skipped_and_drawdown_uses_risk_helper():
    import pytest

    from strategy_cards import StrategyCardError, _card

    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    raw["cards"].append({"card_id": "SX", "title": "壞", "category": "nope", "status": "live", "priority": 1})
    with pytest.raises(StrategyCardError):
        _card(raw["cards"][-1])

    frame = pd.DataFrame({"日期": ["2026-11-01", "2026-11-02"], "總淨值": [100.0, 100.0], "出入金": [0.0, 0.0]})
    assert portfolio_drawdown_pct(frame, 92.0) == pytest.approx(-8.0)
    assert portfolio_drawdown_pct(None, 92.0) is None

    dates = builtin_settlement_dates(date(2026, 11, 6))
    assert date(2026, 11, 18) in dates
    assert all(day.weekday() == 2 and 15 <= day.day <= 21 for day in dates if day != date(2026, 11, 6))


def test_radar_panel_renders_banner_and_page6_tab():
    from unittest.mock import patch

    from streamlit.testing.v1 import AppTest

    page = (ROOT / "order_assistant_ui.py").read_text(encoding="utf-8")
    assert "render_strategy_radar(snapshot, nav_hist, compact=True)" in page
    assert "render_strategy_radar(snapshot, nav_hist, compact=False)" in page
    dashboard = (ROOT / "Dashboard.py").read_text(encoding="utf-8")
    assert "第七頁" not in dashboard
    assert dashboard.count("第六頁：下單輔助系統") >= 1

    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from order_assistant import build_sample_snapshot
from strategy_cards_ui import render_strategy_radar
render_strategy_radar(build_sample_snapshot(), None, compact=False)
"""
    with patch("strategy_cards_ui._vix_histories", return_value=[[18.0, 19.0]]):
        app = AppTest.from_string(script, default_timeout=60).run()
    assert not app.exception
    text = " ".join(str(item.value) for item in list(app.warning) + list(app.error) + list(app.caption))
    labels = " ".join(str(item.label) for item in app.expander)
    assert "1.8" in text
    assert "先處理 S0" in text
    assert "先處理 S0" in labels
    assert "只顯示燈號" in " ".join(str(item.label) for item in app.checkbox)

    app.checkbox(key="radar_lights_only").set_value(True)
    app.run()
    assert not app.exception
    assert list(app.expander) == []
    narrow = " ".join(str(item.value) for item in list(app.caption) + list(app.error))
    assert "先處理 S0" in narrow
    assert "1.8" in " ".join(str(item.value) for item in app.warning)
