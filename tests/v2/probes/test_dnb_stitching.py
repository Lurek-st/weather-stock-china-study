from scripts.v2.probes.probe_dnb import stitch_series


def test_stitch_prefers_current_resource_and_reports_conflict():
    rows, conflicts = stitch_series(
        [{"trading_date": "2018-01-02", "close": 100}],
        [{"trading_date": "2018-01-02", "close": 101}, {"trading_date": "2018-01-03", "close": 102}],
    )
    assert [row["close"] for row in rows] == [101, 102]
    assert conflicts[0]["trading_date"] == "2018-01-02"
