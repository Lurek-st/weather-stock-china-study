from scripts.v2.probes.probe_dnb import quality_gate


def test_aex_quality_gate_rejects_duplicate_date():
    assert quality_gate([
        {"trading_date": "2023-06-01", "close": 10},
        {"trading_date": "2023-06-01", "close": 11},
    ]) is False
