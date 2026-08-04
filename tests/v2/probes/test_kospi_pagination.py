from scripts.v2.probes.probe_kospi import _items


def test_single_item_response_is_not_mistaken_for_a_mapping_of_records():
    assert _items({"response": {"body": {"items": {"item": {"idxNm": "KOSPI"}}}}}) == [{"idxNm": "KOSPI"}]
