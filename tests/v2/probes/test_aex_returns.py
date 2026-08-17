from scripts.v2.probes.probe_dnb import with_previous_close


def test_aex_return_uses_prior_valid_record_not_prior_calendar_day():
    rows = with_previous_close([
        {"trading_date": "2023-06-02", "close": 100},
        {"trading_date": "2023-06-05", "close": 102},
    ])
    assert rows[0]["boundary_missing"] is True
    assert rows[1]["previous_close"] == 100.0
    assert rows[1]["return_pct"] == 2.0
