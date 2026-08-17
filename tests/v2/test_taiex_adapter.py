from datetime import date

from scripts.v2.adapters.market.taiex import months_between, request_plan


def test_monthly_request_boundaries_and_dry_run_plan():
    assert months_between(date(2023, 5, 31), date(2023, 7, 1)) == ["20230501", "20230601", "20230701"]
    assert request_plan(date(2023, 6, 1), date(2023, 6, 30))["months"] == ["20230601"]
