from scripts.v2.probes.probe_kospi import redact_text, service_key_status


def test_key_is_redacted_and_status_never_returns_its_value(monkeypatch):
    monkeypatch.setenv("KOREA_DATA_GO_KR_SERVICE_KEY", "secret-value")
    assert service_key_status() == "set"
    assert redact_text("secret-value") == "[REDACTED]"
