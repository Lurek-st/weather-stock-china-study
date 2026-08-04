from scripts.v2.probes.probe_kospi import build_result


def test_missing_key_cannot_claim_quality_or_history(tmp_path, monkeypatch):
    monkeypatch.delenv("KOREA_DATA_GO_KR_SERVICE_KEY", raising=False)
    result = build_result(tmp_path)
    assert result["final_probe_status"] == "credential_required"
    assert result["data_quality"]["quality_gate_confirmed"] is False
    assert result["historical_coverage_confirmed"] is False
