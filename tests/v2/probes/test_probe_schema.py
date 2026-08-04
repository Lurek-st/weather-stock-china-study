import json
from pathlib import Path

import jsonschema
import pytest


def test_committed_probe_results_validate_against_schema():
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "schemas/v2/source-probe-result-v1.schema.json").read_text(encoding="utf-8"))
    for path in (root / "data/audits/v2/source-probes").glob("*/probe-result.json"):
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(json.loads(path.read_text(encoding="utf-8")))


def test_conditional_probe_status_requires_a_true_quality_gate():
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "schemas/v2/source-probe-result-v1.schema.json").read_text(encoding="utf-8"))
    result = json.loads((root / "data/audits/v2/source-probes/taiex/probe-result.json").read_text(encoding="utf-8"))
    result["data_quality"]["quality_gate_confirmed"] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(result)
