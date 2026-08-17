from scripts.v2.probes.probe_zero_barrier_candidates import classify, score


def candidate(**changes):
    base = {"coverage": "2020_claimed", "eod_definition": "end of day", "rights": "yes", "third_party": "yes", "technical_probe": "json", "calendar_source": "official"}
    base.update(changes)
    return base


def test_key_login_phone_and_paid_candidates_are_rejected():
    for field in ("requires_key", "requires_login", "requires_phone", "requires_payment"):
        assert classify(candidate(**{field: True}), technical_ok=True) == "rejected"


def test_missing_coverage_definition_rights_or_stable_request_cannot_be_tier_one():
    assert classify(candidate(coverage="unknown"), technical_ok=True) == "rejected"
    assert classify(candidate(eod_definition=""), technical_ok=True) == "rejected"
    assert classify(candidate(rights="unclear"), technical_ok=True) == "tier_2_rights_review_needed"
    assert classify(candidate(third_party="unclear"), technical_ok=True) == "tier_2_rights_review_needed"
    assert classify(candidate(), technical_ok=False) == "tier_3_technical_risk"


def test_score_is_one_hundred_only_for_all_clear_tier_one_candidate():
    row = candidate()
    assert score(row, "tier_1_ready_for_full_probe") == 100
    assert score(candidate(rights="unclear", third_party="unclear"), "tier_2_rights_review_needed") < 100
