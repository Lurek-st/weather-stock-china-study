from scripts.v2.probes.probe_kospi import normalize_items


def test_normalizes_documented_kospi_fields_and_commas():
    payload = {"response": {"body": {"items": {"item": {"basDt": "20230601", "idxNm": "KOSPI", "mkp": "2,570.00", "hipr": "2,600", "lopr": "2,560", "clpr": "2,569", "fltRt": "0.12", "trqu": "123"}}}}}
    row = normalize_items(payload)[0]
    assert row["index_name"] == "KOSPI"
    assert row["open"] == 2570.0
    assert row["close"] == 2569.0
