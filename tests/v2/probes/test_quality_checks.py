from scripts.v2.probes.common import quality_check


def test_quality_check_detects_duplicate_weekend_and_ohlc_error():
    result = quality_check([
        {"trading_date": "2023-06-03", "open": "10", "high": "9", "low": "11", "close": "10"},
        {"trading_date": "2023-06-03", "open": "10", "high": "11", "low": "9", "close": "10"},
    ])
    assert result["duplicate_dates"] == 1
    assert result["weekend_records"] == 2
    assert len(result["ohlc_anomalies"]) == 1
