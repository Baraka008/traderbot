import os

from scalper.backtest import BacktestConfig, Backtester, generate_candles, load_csv, save_csv
from scalper.models import SymbolInfo
from scalper.risk import RiskConfig, RiskManager
from scalper.strategy import ScalpStrategy, StrategyConfig


def test_backtest_runs_and_metrics_are_consistent(tmp_path):
    candles = generate_candles(3000, seed=7)
    info = SymbolInfo("SYN", point=0.01, digits=2, min_volume=0.01, max_volume=100, volume_step=0.01)
    strat = ScalpStrategy(StrategyConfig())
    risk = RiskManager(RiskConfig(cooldown_after_loss_sec=0, max_consecutive_losses=0, max_trades_per_day=0))
    bt = Backtester(strat, risk, info, BacktestConfig(instrument="cfd", spread_points=5, tick_value=1.0))
    res = bt.run(candles)
    m = res.metrics
    assert m["trades"] > 10
    assert abs(sum(t.pnl for t in res.trades) - m["net_pnl"]) < 1e-6
    assert abs(res.equity_curve[-1] - (10_000 + m["net_pnl"])) < 1e-6
    assert 0 <= m["win_rate"] <= 1
    assert all(t.reason in ("sl", "tp", "max_hold", "opposite_cross", "end_of_data") for t in res.trades)
    for t in res.trades:
        assert t.close_time > t.open_time
    # stop-loss exits never lose (much) more than the configured risk per trade
    sl_losses = [t.pnl for t in res.trades if t.reason == "sl"]
    assert sl_losses and min(sl_losses) > -10_000 * 0.005 * 2.5

    path = tmp_path / "c.csv"
    save_csv(str(path), candles[:50])
    loaded = load_csv(str(path))
    assert len(loaded) == 50 and loaded[0].close == candles[0].close


def test_multiplier_instrument_pnl():
    candles = generate_candles(2500, seed=3, start_price=5000.0)
    info = SymbolInfo("R_75", point=0.01, digits=2, min_volume=1.0, max_volume=1000, volume_step=0.01)
    strat = ScalpStrategy(StrategyConfig())
    risk = RiskManager(RiskConfig(cooldown_after_loss_sec=0, max_consecutive_losses=0, max_trades_per_day=0))
    res = Backtester(strat, risk, info, BacktestConfig(instrument="multiplier", spread_points=0, multiplier=100)).run(candles)
    assert res.metrics["trades"] > 5
    assert "profit factor" in res.summary()


def test_load_csv_iso_times(tmp_path):
    p = tmp_path / "iso.csv"
    p.write_text("time,open,high,low,close\n2024-01-01T00:00:00Z,1,2,0.5,1.5\n2024-01-01 00:01:00,1.5,2,1,1.2\n")
    c = load_csv(str(p))
    assert c[1].time - c[0].time == 60
