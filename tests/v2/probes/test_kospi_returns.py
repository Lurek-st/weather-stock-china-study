from scripts.v2.probes.probe_kospi import with_previous_close


def test_previous_close_uses_prior_valid_trading_record_not_calendar_day():
    rows = with_previous_close([{"trading_date": "2023-06-05", "close": 110.0}, {"trading_date": "2023-06-02", "close": 100.0}])
    assert rows[0]["boundary_missing"] is True
    assert rows[1]["previous_close"] == 100.0
    assert rows[1]["return_pct_computed"] == 10.000000000000009
