import json
from pathlib import Path

import jsonschema


def test_committed_probe_results_validate_against_schema():
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "schemas/v2/source-probe-result-v1.schema.json").read_text(encoding="utf-8"))
    for path in (root / "data/audits/v2/source-probes").glob("*/probe-result.json"):
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(json.loads(path.read_text(encoding="utf-8")))
