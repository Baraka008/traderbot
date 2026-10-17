from scalper.backtest import generate_candles
from scalper.history import HistoryStore, validate_candles
from scalper.models import Candle


def test_validate_sorts_dedupes_and_reports_gaps():
    c = [Candle(120, 1, 2, 0.5, 1.5), Candle(60, 1, 2, 0.5, 1.5), Candle(60, 1, 3, 0.5, 2.0),
         Candle(300, 1, 2, 0.5, 1.5), Candle(360, 0, 2, 0.5, 1.5), Candle(420, 1, 0.5, 2, 1.5)]
    clean, issues = validate_candles(c, 60)
    assert [x.time for x in clean] == [60, 120, 300]
    assert clean[0].high == 3        # later duplicate wins
    assert any("dropped 2" in i for i in issues)
    assert any("duplicate" in i for i in issues)
    assert any("gap" in i for i in issues)


def test_store_append_fast_path_and_merge(tmp_path):
    store = HistoryStore(str(tmp_path / "data"))
    candles = generate_candles(500, seed=1)
    assert store.append("R_75", 60, candles[:300]) == 300
    assert store.append("R_75", 60, candles[250:400]) == 100      # overlap is de-duplicated
    assert store.last_time("R_75", 60) == candles[399].time
    # out-of-order insert triggers a merge rewrite
    assert store.append("R_75", 60, candles[100:120] + candles[450:460]) == 10
    loaded = store.load("R_75", 60)
    times = [c.time for c in loaded]
    assert times == sorted(set(times)) and len(loaded) == 410
    st = store.stats("R_75", 60)
    assert st["bars"] == 410 and st["first"] == candles[0].time
    assert store.list() == [("R_75", 60, 410)]
    assert store.append("R_75", 60, []) == 0
    assert store.load("missing", 60) == []


def test_snapshot_file(tmp_path):
    store = HistoryStore(str(tmp_path))
    store.snapshot("Volatility 75 Index", 60, {"time": 1, "close": 2.0, "rsi": 55.1})
    store.snapshot("Volatility 75 Index", 60, {"time": 2, "close": 2.1, "rsi": 56.0})
    text = open(store.snapshot_path("Volatility 75 Index", 60)).read().splitlines()
    assert text[0] == "time,close,rsi" and len(text) == 3
    assert "Volatility_75_Index_60_indicators.csv" in store.snapshot_path("Volatility 75 Index", 60)
