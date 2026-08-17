from scripts.v2.probes.probe_taiex import with_previous_close


def test_previous_close_uses_prior_valid_trading_row_across_weekend():
    rows = with_previous_close([
        {"trading_date": "2023-06-02", "close": "100"},
        {"trading_date": "2023-06-05", "close": "110"},
    ])
    assert rows[0]["boundary_missing"] is True
    assert rows[0]["previous_close"] is None
    assert rows[1]["boundary_missing"] is False
    assert rows[1]["previous_close"] == 100.0
    assert rows[1]["return_pct"] == 10.0


def test_first_window_row_is_marked_when_no_prior_source_row_exists():
    row = with_previous_close([{"trading_date": "2023-06-05", "close": "110"}])[0]
    assert row["boundary_missing"] is True
    assert row["return_pct"] is None
