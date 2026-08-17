from scripts.v2.probes.common import semantic_hash


def test_semantic_hash_ignores_row_and_key_order():
    assert semantic_hash([{"date": "2020-01-02", "close": 1}, {"date": "2020-01-01", "close": 2}]) == semantic_hash([{"close": 2, "date": "2020-01-01"}, {"close": 1, "date": "2020-01-02"}])
