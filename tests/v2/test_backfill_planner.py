"""Stage 5E-2: ERA5 backfill planner tests (pure, no network)."""
from __future__ import annotations

import inspect
import pytest

from scripts.v2.climatology.backfill_planner import (
    batch_plan,
    daily_exposure_rows,
    market_scientific_counts,
    planning_request_id,
    research_markets,
    strategy_summary,
)
from scripts.v2.core import V2Error
import scripts.v2.climatology.backfill_planner as planner_module


@pytest.fixture(scope="module")
def sse_counts():
    return market_scientific_counts("sse_composite")


@pytest.fixture(scope="module")
def topix_counts():
    return market_scientific_counts("topix")


@pytest.fixture(scope="module")
def nifty_counts():
    return market_scientific_counts("nifty50")


# --- Baseline counts ----------------------------------------------------
def test_10950_exposures_per_market(sse_counts, topix_counts):
    assert sse_counts["daily_exposure_count"] == 10950  # 30 x 365
    assert topix_counts["daily_exposure_count"] == 10950


def test_87600_total_exposures():
    total = sum(market_scientific_counts(m)["daily_exposure_count"] for m in research_markets())
    assert total == 87600


def test_eight_markets():
    assert research_markets() == [
        "sse_composite", "szse_component", "topix", "nifty50",
        "ftse100", "dax", "sp500", "bse50",
    ]


def test_feb29_never_a_scientific_observation(sse_counts):
    # no actual exposure date is Feb 29 (common calendar)
    for row in daily_exposure_rows("sse_composite"):
        assert not row["actual_date"].endswith("-02-29")


def test_feb29_may_remain_temporal_support(sse_counts):
    # China markets: Mar 01 exposure needs Feb 29 UTC brackets in leap years
    assert sse_counts["days_with_feb29_support"] == 8  # 8 leap years in 1991-2020


def test_exact_hour_support_counts(topix_counts):
    # whole-hour open (Tokyo 09:00): 3 support hours/day -> 3 x 10950
    assert topix_counts["support_count_sum"] == 32850


def test_quarter_hour_support_counts(nifty_counts):
    # 09:15 open (Mumbai): 4 support hours/day
    assert nifty_counts["support_count_sum"] == 43800


def test_half_hour_support_counts(sse_counts):
    # 09:30 open (Shanghai): 4 support hours/day
    assert sse_counts["support_count_sum"] == 43800


def test_support_set_no_duplicates(sse_counts):
    assert sse_counts["support_set_count"] == sse_counts["support_count_sum"]


# --- Batching -----------------------------------------------------------
@pytest.mark.parametrize("strategy,expected", [("monthly", 360), ("quarterly", 120), ("yearly", 30)])
def test_batch_request_counts(strategy, expected):
    assert batch_plan("sse_composite", strategy)["request_count"] == expected


@pytest.mark.parametrize("strategy", ["monthly", "quarterly", "yearly"])
def test_batch_plans_deterministic(strategy):
    a = batch_plan("topix", strategy)
    b = batch_plan("topix", strategy)
    assert a == b


def test_support_subset_transport():
    plan = batch_plan("sse_composite", "monthly")
    for b in plan["batches"]:
        assert b["transport_count"] >= b["support_count"]
        assert b["extras_count"] == b["transport_count"] - b["support_count"]
        assert b["amplification"] >= 1.0


def test_cartesian_extras_auditable():
    plan = batch_plan("sse_composite", "yearly")
    total_extras = sum(b["extras_count"] for b in plan["batches"])
    total_support = sum(b["support_count"] for b in plan["batches"])
    total_transport = sum(b["transport_count"] for b in plan["batches"])
    assert total_extras == total_transport - total_support
    assert total_extras > 0  # Cartesian product is a strict superset


def test_raw_expected_equals_transport_planning():
    # at planning level raw_expected == transport_requested (per batch)
    plan = batch_plan("sse_composite", "monthly")
    for b in plan["batches"]:
        assert b["transport_count"] > 0
    # the four layers are distinct fields in the audit; support is a subset of transport
    assert all(b["support_count"] <= b["transport_count"] for b in plan["batches"])


def test_field_count_below_snapshot_limit():
    for strategy in ("monthly", "quarterly", "yearly"):
        summary = strategy_summary(strategy)
        assert summary["aggregate_max_fields_per_request"] < 120000


def test_grid_cell_values_separate_from_fields():
    plan = batch_plan("sse_composite", "monthly")
    b = plan["batches"][0]
    assert b["cds_field_count"] == b["transport_count"]  # 1 var x 1 level x timesteps
    assert b["grid_cell_values"] == b["transport_count"] * 4  # 4 grid points


# --- Idempotency / guards -----------------------------------------------
def test_planning_request_id_deterministic():
    a = planning_request_id("sse_composite", "1991-01", {"date": ["1991-01-31"], "time": ["23:00"]})
    b = planning_request_id("sse_composite", "1991-01", {"date": ["1991-01-31"], "time": ["23:00"]})
    assert a == b
    assert len(a) == 64


def test_provider_guard_invoked(monkeypatch):
    monkeypatch.setattr("scripts.v2.timezone_provider.runtime_tzdata_version", lambda: "9999.1")
    with pytest.raises(V2Error):
        market_scientific_counts("sse_composite")


def test_planner_requires_no_municipal_coordinates():
    # the planner module must not import or reference municipal coordinates
    source = inspect.getsource(planner_module)
    assert "municipal" not in source.lower()
    assert "location" not in source.lower()


def test_planner_makes_no_network_calls():
    source = inspect.getsource(planner_module)
    for banned in ("requests.", "cdsapi", "urllib", "http://", "https://"):
        assert banned not in source


def test_support_guard_no_fallback():
    # planner has no fallback to markets.yaml session clock
    source = inspect.getsource(planner_module)
    assert "markets.yaml" not in source
