import math

from scalper.models import Candle, Position, Quote, Side, SymbolInfo
from scalper.strategy import ScalpStrategy, StrategyConfig

INFO = SymbolInfo(name="X", point=0.01, digits=2, min_volume=0.01, max_volume=100, volume_step=0.01)


def _series(prices, tf=60):
    out = []
    for i, p in enumerate(prices):
        out.append(Candle(i * tf, p, p + 0.3, p - 0.3, p))
    return out


def _cross_up_prices():
    # 60 gently declining bars then a noisy rally -> fast EMA crosses above slow, close above trend EMA
    prices = [100 - i * 0.05 for i in range(60)]
    last = prices[-1]
    prices += [last + i * 0.2 + 0.2 * math.sin(i * 1.3) for i in range(1, 40)]
    return prices


def _cross_down_prices():
    return [200 - p for p in _cross_up_prices()]


def _first_signal(strat, candles, spread=0.02):
    for n in range(strat.warmup_bars(), len(candles) + 1):
        hist = candles[:n]
        q = Quote(bid=hist[-1].close, ask=hist[-1].close + spread, time=hist[-1].time + 60)
        sig = strat.evaluate(hist, q, INFO)
        if sig:
            return n, sig
    return None, None


def test_cross_up_generates_buy_signal_with_atr_stops():
    strat = ScalpStrategy(StrategyConfig())
    candles = _series(_cross_up_prices())
    n, sig = _first_signal(strat, candles)
    assert sig is not None and sig.side is Side.BUY
    assert sig.entry == candles[n - 1].close + 0.02      # buys at the ask
    assert sig.stop_loss < sig.entry < sig.take_profit
    assert math.isclose(sig.take_profit - sig.entry, 1.5 * (sig.entry - sig.stop_loss), rel_tol=0.05)


def test_signal_only_fires_on_the_cross_bar():
    strat = ScalpStrategy(StrategyConfig())
    candles = _series(_cross_up_prices())
    first, _ = _first_signal(strat, candles)
    assert first is not None
    hist = candles[: first + 1]
    q = Quote(bid=hist[-1].close, ask=hist[-1].close + 0.02, time=0)
    assert strat.evaluate(hist, q, INFO) is None


def test_spread_filter_blocks_entry():
    candles = _series(_cross_up_prices())
    n, _ = _first_signal(ScalpStrategy(StrategyConfig()), candles)
    strat = ScalpStrategy(StrategyConfig(max_spread_points=1))
    hist = candles[:n]
    assert strat.evaluate(hist, Quote(bid=hist[-1].close, ask=hist[-1].close + 0.5, time=0), INFO) is None
    assert strat.evaluate(hist, Quote(bid=hist[-1].close, ask=hist[-1].close + 0.005, time=0), INFO) is not None


def test_cross_down_generates_sell_signal():
    strat = ScalpStrategy(StrategyConfig())
    candles = _series(_cross_down_prices())
    _, sig = _first_signal(strat, candles)
    assert sig is not None and sig.side is Side.SELL
    assert sig.take_profit < sig.entry < sig.stop_loss


def test_manage_time_exit_and_breakeven_and_trailing():
    cfg = StrategyConfig(max_hold_bars=5, breakeven_atr_mult=0.5, trail_activate_atr_mult=1.0, trail_atr_mult=0.5,
                         exit_on_opposite_cross=False)
    strat = ScalpStrategy(cfg)
    candles = [Candle(i * 60, 100, 101, 99, 100) for i in range(40)]   # ATR = 2.0
    pos = Position(id="1", symbol="X", side=Side.BUY, volume=1, entry_price=100.0, stop_loss=98.0, take_profit=103.0,
                   open_time=candles[-1].time)
    # no move, same bar -> nothing
    act = strat.manage(pos, candles, Quote(100.0, 100.02, candles[-1].time + 30), False, INFO)
    assert not act.close and not act.modifies
    # price up 1.2 ATR -> breakeven then trailing (trail = price - 0.5*ATR = 102.4-1.0=101.4 > entry)
    act = strat.manage(pos, candles, Quote(102.4, 102.42, candles[-1].time + 30), False, INFO)
    assert act.new_sl is not None and act.new_sl > pos.entry_price
    assert math.isclose(act.new_sl, 101.4, abs_tol=0.02)
    # time exit after 5 closed bars
    later = candles + [Candle((40 + i) * 60, 100, 101, 99, 100) for i in range(5)]
    act = strat.manage(pos, later, Quote(100.0, 100.02, later[-1].time + 60), True, INFO)
    assert act.close and act.reason == "max_hold"


def test_manage_short_trailing_direction():
    cfg = StrategyConfig(max_hold_bars=0, breakeven_atr_mult=0, trail_activate_atr_mult=1.0, trail_atr_mult=0.5,
                         exit_on_opposite_cross=False)
    strat = ScalpStrategy(cfg)
    candles = [Candle(i * 60, 100, 101, 99, 100) for i in range(40)]
    pos = Position(id="1", symbol="X", side=Side.SELL, volume=1, entry_price=100.0, stop_loss=102.0, take_profit=97.0,
                   open_time=candles[-1].time)
    act = strat.manage(pos, candles, Quote(97.5, 97.52, 0), False, INFO)
    assert act.new_sl is not None and act.new_sl < pos.stop_loss and act.new_sl > 97.52


def test_config_validation():
    import pytest
    with pytest.raises(ValueError):
        ScalpStrategy(StrategyConfig(ema_fast=30, ema_slow=20))
    with pytest.raises(ValueError):
        ScalpStrategy(StrategyConfig(entry_mode="bogus"))


def test_session_filter_blocks_outside_hours_and_days():
    import pytest
    from scalper.strategy import in_session, _parse_hours, _parse_days
    hours = _parse_hours("07:00-20:00")
    assert in_session(1_700_000_000, hours, None) is False            # 2023-11-14 22:13 UTC
    assert in_session(1_700_000_000 - 8 * 3600, hours, None) is True  # 14:13 UTC
    night = _parse_hours("22:00-06:00")
    assert in_session(1_700_000_000, night, None) is True
    assert in_session(1_700_000_000, None, _parse_days("mon,tue")) is True   # Tuesday
    assert in_session(1_700_000_000, None, _parse_days("sat,sun")) is False
    with pytest.raises(ValueError):
        StrategyConfig(trade_hours="7-20").validate()
    with pytest.raises(ValueError):
        StrategyConfig(trade_days="funday").validate()
    strat = ScalpStrategy(StrategyConfig(trade_days="sat,sun"))
    candles = _series(_cross_up_prices())
    n, _ = _first_signal(ScalpStrategy(StrategyConfig()), candles)
    hist = candles[:n]
    q = Quote(bid=hist[-1].close, ask=hist[-1].close + 0.02, time=1_700_000_000)
    assert strat.evaluate(hist, q, INFO) is None and strat.last_skip_reason == "outside_session"


def test_aggregate_and_htf_bias():
    from scalper.strategy import aggregate
    candles = [Candle(i * 60, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(12)]
    htf = aggregate(candles, 60, 5)
    assert len(htf) == 2                       # third M5 bar (bars 10,11) is incomplete
    assert htf[0].time == 0 and htf[0].open == 100 and htf[0].close == 104.5 and htf[0].high == 105 and htf[0].low == 99
    strat = ScalpStrategy(StrategyConfig(htf_multiplier=5, htf_ema=3))
    up = [Candle(i * 60, 100 + i * 0.1, 100.2 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1) for i in range(60)]
    assert strat.htf_bias(up) == 1
    down = [Candle(i * 60, 100 - i * 0.1, 100.2 - i * 0.1, 99.9 - i * 0.1, 100.1 - i * 0.1) for i in range(60)]
    assert strat.htf_bias(down) == -1
    assert strat.htf_bias(up[:10]) is None
    assert ScalpStrategy(StrategyConfig()).htf_bias(up) == 0


def test_max_atr_filter():
    candles = _series(_cross_up_prices())
    n, _ = _first_signal(ScalpStrategy(StrategyConfig()), candles)
    strat = ScalpStrategy(StrategyConfig(max_atr_points=1))      # ATR here is ~0.6 price units = 60 points
    hist = candles[:n]
    q = Quote(bid=hist[-1].close, ask=hist[-1].close + 0.02, time=0)
    assert strat.evaluate(hist, q, INFO) is None and strat.last_skip_reason == "atr_too_high"
