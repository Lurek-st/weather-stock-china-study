from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import xarray as xr

from scripts.v2.core import (
    V2Error,
    assess_maturity,
    build_weather_windows,
    normalize_market_frame,
    normalize_weather_frame,
    resolve_market_candidates,
)
from scripts.v2.normalize_weather import read_input


def raw_weather(timestamps) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp_utc": timestamps,
            "temperature_k": [283.15] * len(timestamps),
            "dewpoint_k": [278.15] * len(timestamps),
            "precipitation_m": [0.001] * len(timestamps),
            "cloud_cover_fraction": [0.5] * len(timestamps),
            "wind_u_mps": [3.0] * len(timestamps),
            "wind_v_mps": [4.0] * len(timestamps),
            "wind_gust_mps": list(range(1, len(timestamps) + 1)),
            "solar_radiation_j_m2": [1_000_000] * len(timestamps),
        }
    )


@pytest.mark.parametrize(
    ("data_class", "is_final"),
    [("provisional_reanalysis", False), ("final_reanalysis", True), ("station_observation", False)],
)
def test_weather_status_and_unit_conversion(data_class, is_final):
    result = normalize_weather_frame(
        raw_weather(pd.date_range("2026-07-06", periods=3, freq="h", tz="UTC")),
        city_id="test",
        source_id="source",
        data_class=data_class,
    )
    assert bool(result["is_final"].all()) is is_final
    assert result.loc[0, "air_temperature_c"] == pytest.approx(10)
    assert result.loc[0, "precipitation_mm"] == pytest.approx(1)
    assert result.loc[0, "wind_speed_mps"] == pytest.approx(5)
    assert result.loc[0, "solar_radiation_mj_m2"] == pytest.approx(1)
    assert pd.notna(result.loc[0, "apparent_temperature_c"])
    assert 0 <= result.loc[0, "relative_humidity_pct"] <= 100


def test_duplicate_weather_hour_rejected():
    with pytest.raises(V2Error, match="duplicate"):
        normalize_weather_frame(
            raw_weather(["2026-07-06T00:00:00Z", "2026-07-06T00:00:00Z"]),
            city_id="x",
            source_id="x",
            data_class="provisional_reanalysis",
        )


def test_era5_netcdf_adapter(tmp_path):
    timestamps = pd.date_range("2026-07-06", periods=2, freq="h")
    dataset = xr.Dataset(
        {
            "t2m": ("valid_time", [283.15, 284.15]),
            "d2m": ("valid_time", [278.15, 279.15]),
            "tp": ("valid_time", [0.0, 0.001]),
            "tcc": ("valid_time", [0.2, 0.4]),
            "u10": ("valid_time", [3.0, 3.0]),
            "v10": ("valid_time", [4.0, 4.0]),
            "i10fg": ("valid_time", [6.0, 7.0]),
            "ssrd": ("valid_time", [0.0, 1_000_000.0]),
        },
        coords={"valid_time": timestamps},
    )
    path = tmp_path / "era5.nc"
    dataset.to_netcdf(path, engine="scipy")
    result = normalize_weather_frame(
        read_input(path),
        city_id="city",
        source_id="cds_era5t_hourly",
        data_class="provisional_reanalysis",
    )
    assert len(result) == 2
    assert result.iloc[1]["precipitation_mm"] == pytest.approx(1)


def market_config(timezone="Asia/Shanghai", breaks=None):
    return {
        "market_id": "market",
        "city_id": "city",
        "timezone": timezone,
        "session": {"open": "09:30", "close": "15:30", "breaks": breaks or []},
    }


def canonical_hourly(start, end, timezone="UTC"):
    raw = raw_weather(pd.date_range(start, end, freq="h", inclusive="left", tz=timezone))
    return normalize_weather_frame(
        raw, city_id="city", source_id="source", data_class="provisional_reanalysis"
    )


def test_utc_to_local_preopen_and_session_windows():
    hourly = canonical_hourly("2026-07-05", "2026-07-08")
    windows = build_weather_windows(hourly, market_config(), [date(2026, 7, 6)])
    assert windows.set_index("window").loc["pre_open", "hour_count"] == 2
    assert windows.set_index("window").loc["trading_session", "hour_count"] == 6


def test_midday_break_excluded():
    hourly = canonical_hourly("2026-07-05", "2026-07-08")
    windows = build_weather_windows(
        hourly, market_config(breaks=[["11:30", "13:30"]]), ["2026-07-06"]
    )
    assert windows.set_index("window").loc["trading_session", "hour_count"] == 4


def test_early_close_override_changes_session_window():
    hourly = canonical_hourly("2026-07-05", "2026-07-08")
    windows = build_weather_windows(
        hourly,
        market_config(),
        ["2026-07-06"],
        {"2026-07-06": {"close": "13:00", "calendar_status": "early_close"}},
    ).set_index("window")
    assert windows.loc["trading_session", "hour_count"] == 3
    assert windows.loc["trading_session", "calendar_status"] == "early_close"


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2026-03-08", 23), ("2026-11-01", 25)],
)
def test_dst_full_day_hour_count(day, expected):
    hourly = canonical_hourly("2026-03-07", "2026-11-03")
    windows = build_weather_windows(
        hourly, market_config(timezone="America/New_York"), [day]
    )
    assert windows.set_index("window").loc["full_day", "expected_hour_count"] == expected


def test_missing_hour_flagged():
    hourly = canonical_hourly("2026-07-05", "2026-07-08")
    hourly = hourly.loc[hourly["timestamp_utc"] != pd.Timestamp("2026-07-06T04:00:00Z")]
    windows = build_weather_windows(hourly, market_config(), ["2026-07-06"])
    assert "missing_hours" in windows.set_index("window").loc["full_day", "quality_flags"]


def test_accumulated_and_instantaneous_aggregation():
    # pre_open window is 07:30-09:30 local. Under the strict accumulation
    # semantics only the whole 08:00-09:00 interval (row stamped 09:00) lies
    # fully inside; the 07:00-08:00 interval partially overlaps and is
    # excluded (partial_accumulation_interval_excluded).
    hourly = canonical_hourly("2026-07-05", "2026-07-08")
    windows = build_weather_windows(hourly, market_config(), ["2026-07-06"]).set_index("window")
    assert windows.loc["pre_open", "precipitation_mm"] == pytest.approx(1)
    assert windows.loc["pre_open", "solar_radiation_mj_m2"] == pytest.approx(1)
    assert windows.loc["pre_open", "max_gust_mps"] > 0
    assert "partial_accumulation_interval_excluded" in windows.loc["pre_open", "quality_flags"]
    # trading_session 09:30-15:30: whole intervals 11:00..15:00 (5 hours);
    # the 09:00-10:00 interval starts before the window and is excluded
    assert windows.loc["trading_session", "precipitation_mm"] == pytest.approx(5)


def market_frame(statuses=("final", "provisional")):
    return pd.DataFrame(
        [
            {
                "market_id": "m",
                "trading_date": f"2026-07-0{6 + index}",
                "official_close": 100 + 10 * index,
                "source_timestamp": f"2026-07-0{6 + index}T20:00:00Z",
                "value_status": status,
                "calendar_status": "verified",
                "calendar_source_id": "calendar",
                "source_id": "source",
                "revision": index + 1,
                "is_final": status in {"final", "corrected"},
                "currency": "CNY",
                "quality_flags": [],
            }
            for index, status in enumerate(statuses)
        ]
    )


def test_previous_close_and_return_are_recomputed():
    result = normalize_market_frame(market_frame())
    assert pd.isna(result.loc[result.index[0], "previous_official_close"])
    assert result.iloc[1]["close_to_close_return_pct"] == pytest.approx(10)


@pytest.mark.parametrize("status", ["final", "provisional", "corrected"])
def test_supported_market_value_status(status):
    result = normalize_market_frame(market_frame((status,)))
    assert result.iloc[0]["value_status"] == status


def test_official_correction_revision_retained():
    result = normalize_market_frame(market_frame(("final", "corrected")))
    assert list(result["revision"]) == [1, 2]


def test_same_day_revision_uses_previous_distinct_day_close():
    frame = market_frame(("final", "final"))
    correction = dict(frame.iloc[1])
    correction["official_close"] = 112
    correction["revision"] = 3
    correction["value_status"] = "corrected"
    result = normalize_market_frame(pd.concat([frame, pd.DataFrame([correction])], ignore_index=True))
    corrected = result.loc[result["revision"] == 3].iloc[0]
    assert corrected["previous_official_close"] == pytest.approx(100)
    assert corrected["close_to_close_return_pct"] == pytest.approx(12)


@pytest.mark.parametrize(
    "calendar_status",
    ["holiday", "weekend", "early_close", "special_trading", "temporary_halt"],
)
def test_calendar_statuses_remain_distinct(calendar_status):
    frame = market_frame(("final",))
    frame.loc[0, "calendar_status"] = calendar_status
    assert normalize_market_frame(frame).iloc[0]["calendar_status"] == calendar_status


def test_primary_failure_uses_registered_backup():
    chosen = resolve_market_candidates(
        [{"authority": "registered_backup", "official_close": 99, "value_status": "final"}]
    )
    assert chosen["official_close"] == 99


def test_official_final_wins():
    chosen = resolve_market_candidates(
        [
            {"authority": "registered_backup", "official_close": 99},
            {"authority": "official_final", "official_close": 100},
        ]
    )
    assert chosen["official_close"] == 100


def test_same_authority_conflict_is_not_silenced():
    chosen = resolve_market_candidates(
        [
            {"authority": "official_final", "official_close": 100},
            {"authority": "official_final", "official_close": 101},
        ]
    )
    assert chosen["value_status"] == "conflicting"


def test_news_cannot_become_formal_value():
    with pytest.raises(V2Error, match="news"):
        resolve_market_candidates(
            [{"authority": "news_explanation_only", "official_close": 100}]
        )


@pytest.mark.parametrize(
    ("weather", "markets", "calendar", "expected"),
    [
        ("provisional_reanalysis", ["provisional"], True, "provisional_ready"),
        ("provisional_reanalysis", ["final"], True, "research_ready"),
        ("final_reanalysis", ["final", "corrected"], True, "frozen"),
        ("provisional_reanalysis", ["pending"], True, "not_ready"),
        ("provisional_reanalysis", ["conflicting"], True, "not_ready"),
        ("provisional_reanalysis", ["unavailable"], True, "not_ready"),
        ("provisional_reanalysis", ["final"], False, "not_ready"),
        ("station_observation", ["final"], True, "not_ready"),
    ],
)
def test_maturity_states(weather, markets, calendar, expected):
    assert (
        assess_maturity(
            weather_class=weather, market_statuses=markets, calendar_verified=calendar
        )["status"]
        == expected
    )
