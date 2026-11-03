import pytest
from beta_rules import (
    FALLBACK_UNDERLYING_BETA,
    fallback_table_rows,
    overrides_from_rows,
)
from order_assistant import build_sample_frames, build_sample_snapshot
from order_assistant_ui import _with_beta_fallbacks
from portfolio_risk import value_positions_safely
import us_hedge


def test_fallback_table_rows_defaults():
    rows = fallback_table_rows()
    assert len(rows) == len(FALLBACK_UNDERLYING_BETA)
    soxx_row = next(r for r in rows if r["標的"] == "SOXX")
    assert soxx_row["預設 Beta"] == 1.6
    assert "SOXL" in soxx_row["對應槓桿 ETF"]
    assert soxx_row["已手動修改"] is False


def test_fallback_table_rows_with_overrides():
    rows = fallback_table_rows({"SOXX": 2.0})
    soxx_row = next(r for r in rows if r["標的"] == "SOXX")
    assert soxx_row["預設 Beta"] == 2.0
    assert soxx_row["已手動修改"] is True


def test_overrides_from_rows():
    rows = fallback_table_rows({"SOXX": 2.0})
    # Add an invalid row and an unchanged row
    rows.append({"標的": "AMD", "預設 Beta": -1.0})
    rows.append({"標的": "QQQ", "預設 Beta": float("nan")})
    rows.append({"標的": "", "預設 Beta": 1.5})
    res = overrides_from_rows(rows)
    assert res == {"SOXX": 2.0}


def test_value_positions_safely_beta_fallbacks():
    positions, accounts, usdtwd, twii = build_sample_frames()
    prices = dict(zip(positions["標的"], positions["即時報價"]))
    betas = dict(zip(positions["標的"], positions["Beta值"]))
    sectors = dict(zip(positions["標的"], positions["產業別"]))
    valued, _acc, _metrics, _warnings = value_positions_safely(
        positions, accounts, usdtwd, prices, betas, sectors, beta_fallbacks={"SOXX": 2.0}
    )
    soxl_row = valued[valued["標的"] == "SOXL"].iloc[0]
    assert soxl_row["有效Beta"] == pytest.approx(6.0)


def test_with_beta_fallbacks_and_hedge():
    default_snap = build_sample_snapshot()
    mod_snap = _with_beta_fallbacks(default_snap, {"SOXX": 2.0})
    assert mod_snap.metrics["us_beta"] > default_snap.metrics["us_beta"]

    exp = us_hedge.us_spx_exposure(mod_snap)
    assert exp["us_beta_spx"] == pytest.approx(mod_snap.metrics["us_beta"])
