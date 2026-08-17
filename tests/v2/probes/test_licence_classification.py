from scripts.v2.probes.common import classify


def test_unresolved_rights_cannot_pass_even_when_other_gates_pass():
    values = {key: "yes" for key in ("download_allowed", "automated_access_allowed", "academic_research_allowed", "transformation_allowed", "public_raw_redistribution_allowed", "public_derived_panel_allowed")}
    assert classify(values, "unclear", True, True, True) == "rights_unresolved"


def test_unclear_public_redistribution_cannot_pass():
    values = {key: "yes" for key in ("download_allowed", "automated_access_allowed", "academic_research_allowed", "transformation_allowed", "public_raw_redistribution_allowed", "public_derived_panel_allowed")}
    values["public_raw_redistribution_allowed"] = "unclear"
    assert classify(values, "yes", True, True, True) == "rights_unresolved"
