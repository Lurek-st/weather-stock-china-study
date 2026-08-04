import json
from pathlib import Path


def test_korean_mobile_registration_blocker_keeps_kospi_credential_required():
    root = Path(__file__).resolve().parents[3]
    result = json.loads((root / "data/audits/v2/source-probes/kospi/probe-result.json").read_text(encoding="utf-8"))
    assert result["final_probe_status"] == "credential_required"
    assert "foreign_user_registration_requires_korean_mobile" in result["blocking_issues"]
    assert result["registration_attempt"]["api_requests_run"] is False
    assert result["registration_attempt"]["cost_incurred"] is False
