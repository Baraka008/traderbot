from scalper.backtest import BacktestConfig, generate_candles
from scalper.models import SymbolInfo
from scalper.optimize import expand_grid, optimize, score_metrics
from scalper.risk import RiskConfig
from scalper.strategy import StrategyConfig


def test_expand_grid_drops_invalid_ema_pairs_and_samples():
    combos = expand_grid({"ema_fast": [5, 30], "ema_slow": [21, 34]})
    assert {(c["ema_fast"], c["ema_slow"]) for c in combos} == {(5, 21), (5, 34), (30, 34)}
    assert len(expand_grid({"a": [1, 2, 3, 4]}, max_combos=2)) == 2


def test_score_prefers_profitable_low_drawdown():
    good = {"trades": 50, "profit_factor": 1.5, "return_pct": 10, "max_drawdown_pct": 2}
    bad = {"trades": 50, "profit_factor": 0.8, "return_pct": -5, "max_drawdown_pct": 10}
    few = {"trades": 3, "profit_factor": 3.0, "return_pct": 10, "max_drawdown_pct": 2}
    assert score_metrics(good, 30) > score_metrics(few, 30) > score_metrics(bad, 30)
    assert score_metrics({"trades": 0, "profit_factor": 0, "return_pct": 0, "max_drawdown_pct": 0}, 30) < -1e8


def test_optimize_returns_ranked_results_with_oos():
    candles = generate_candles(2500, seed=5)
    info = SymbolInfo("SYN", point=0.01, digits=2, min_volume=0.01, max_volume=100, volume_step=0.01)
    grid = {"entry_mode": ["cross", "momentum"], "tp_atr_mult": [1.5, 2.0]}
    res = optimize(candles, StrategyConfig(), RiskConfig(), info, BacktestConfig(instrument="cfd", spread_points=5),
                   grid=grid, split=0.7, min_trades=10, top=4, workers=1)
    assert len(res) == 4
    assert [r.score for r in res] == sorted((r.score for r in res), reverse=True)
    for r in res:
        assert set(r.params) == {"entry_mode", "tp_atr_mult"}
        assert "trades" in r.insample and "trades" in r.outsample
        assert "IS trades" in r.row()
