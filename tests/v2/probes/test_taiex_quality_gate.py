from scripts.v2.probes.run_all_probes import taiex_quality_gate


def _window(window_id: str) -> dict:
    return {
        "window_id": window_id,
        "record_count": 2,
        "quality": {
            "valid_trading_record_count": 2,
            "missing_counts": {"close": 0},
            "date_parse_failures": 0,
            "duplicate_dates": 0,
            "weekend_records": 0,
            "ohlc_anomalies": [],
        },
        "return_calculation": {"calculated_return_count": 2, "boundary_missing_count": 0},
    }


def test_all_window_return_and_repeatability_checks_open_quality_gate():
    live = {
        "windows": [_window("recent_30"), _window("2023_06"), _window("2020_10")],
        "repeatability": {"a": {"byte_status": "byte_identical", "semantic_status": "semantic_identical"}},
    }
    assert taiex_quality_gate(live) is True


def test_conditional_status_cannot_keep_false_quality_gate_when_taiex_checks_pass():
    live = {
        "windows": [_window("recent_30"), _window("2023_06"), _window("2020_10")],
        "repeatability": {"a": {"byte_status": "byte_identical", "semantic_status": "semantic_identical"}},
    }
    assert taiex_quality_gate(live) is True
    live["windows"][0]["quality"]["missing_counts"]["close"] = 1
    assert taiex_quality_gate(live) is False
