from bmw_bin_editor.testo_mcp import flatten_numeric_fields, find_named_list, _jsonish
from bmw_bin_editor.logdata import LiveTestOLog, SignalCatalog


def test_flatten_numeric_fields_shapes():
    data = {"structuredContent": {"results": [
        {"name": "STAT_MOTORDREHZAHL_WERT", "value": 2345.0, "unit": "rpm"},
        {"resultName": "STAT_LADEDRUCK_WERT", "wert": "1875,5"},
    ]}}
    out = flatten_numeric_fields(data)
    assert out["STAT_MOTORDREHZAHL_WERT"] == 2345.0
    assert out["STAT_LADEDRUCK_WERT"] == 1875.5


def test_find_named_list_direct_records():
    assert find_named_list({"ecus": [{"name": "DDE5"}, {"name": "MSD80"}]}, ("ecus",))[:2] == ["DDE5", "MSD80"]


def test_live_log_append():
    log = LiveTestOLog(SignalCatalog())
    log.append_sample(0.0, {"A": 1.0})
    log.append_sample(0.1, {"A": 2.0, "B": 7.0})
    assert log.sample_count == 2
    assert list(log.series("A")) == [1.0, 2.0]
    assert str(log.series("B")[0]) == "nan"
    assert log.series("B")[1] == 7.0
