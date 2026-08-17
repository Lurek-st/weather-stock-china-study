from scripts.v2.probes.probe_dnb import describe_json, quality_gate


def test_json_structure_is_described_without_guessing_fields():
    description = describe_json({"meta": {"frequency": "daily"}, "data": [{"date": "2023-01-02", "value": 1}]})
    assert description["top_level_type"] == "object"
    assert description["record_container_candidates"] == ["data"]


def test_dnb_quality_gate_rejects_missing_or_nonpositive_close():
    assert quality_gate([{"trading_date": "2023-01-02", "close": 1.0}]) is True
    assert quality_gate([{"trading_date": "2023-01-02", "close": 0}]) is False
