from scalper.indicators import atr, ema, rsi, sma
from scalper.models import Candle


def test_ema_matches_manual_computation():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    out = ema(vals, 3)
    assert out[:2] == [None, None]
    assert abs(out[2] - 2.0) < 1e-9
    k = 0.5
    expected = 2.0
    for i in range(3, 10):
        expected = vals[i] * k + expected * (1 - k)
        assert abs(out[i] - expected) < 1e-9


def test_sma_and_short_inputs():
    assert sma([1, 2, 3, 4], 2)[1:] == [1.5, 2.5, 3.5]
    assert ema([1, 2], 5) == [None, None]
    assert rsi([1, 2], 14) == [None, None]


def test_rsi_bounds_and_direction():
    up = [float(i) for i in range(40)]
    r = rsi(up, 14)
    assert r[-1] == 100.0
    down = [float(40 - i) for i in range(40)]
    assert rsi(down, 14)[-1] == 0.0
    mixed = [10 + (i % 3) for i in range(60)]
    assert all(0 <= v <= 100 for v in rsi(mixed, 14) if v is not None)


def test_atr_positive_and_wilder_smoothing():
    candles = [Candle(i * 60, 100, 101, 99, 100) for i in range(30)]
    a = atr(candles, 14)
    assert a[13] is None and a[14] is not None
    assert all(abs(v - 2.0) < 1e-9 for v in a if v is not None)
