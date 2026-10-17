import pytest

from scalper.models import ClosedTrade, Side, SymbolInfo
from scalper.risk import RiskConfig, RiskManager

INFO = SymbolInfo(name="X", point=0.00001, digits=5, min_volume=0.01, max_volume=100, volume_step=0.01)


def _trade(pnl):
    return ClosedTrade("1", "X", Side.BUY, 0.1, 1.0, 1.0, 0, 0, pnl)


def test_volume_sizing_rounds_down_to_step():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0))
    # 10k balance, 1% = 100 risk, loss per lot 1234 -> 0.081 -> 0.08
    assert rm.volume_for(10_000, 1234.0, INFO) == 0.08


def test_volume_min_lot_and_overshoot():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_risk_overshoot=2.0))
    # intended risk 100, min lot risks 150 -> allowed (<= 200)
    assert rm.volume_for(10_000, 15_000.0, INFO) == 0.01
    # min lot risks 250 -> skip
    assert rm.volume_for(10_000, 25_000.0, INFO) == 0.0
    assert rm.volume_for(10_000, 0.0, INFO) == 0.0


def test_fixed_volume_and_max_clamp():
    rm = RiskManager(RiskConfig(fixed_volume=0.5))
    assert rm.volume_for(100, 1e9, INFO) == 0.5
    rm = RiskManager(RiskConfig(risk_per_trade_pct=50))
    assert rm.volume_for(1_000_000, 1.0, INFO) == 100.0


def test_daily_loss_limit_and_reset():
    now = [1_700_000_000.0]
    rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0, cooldown_after_loss_sec=0, max_consecutive_losses=0),
                     clock=lambda: now[0])
    rm.start_day(10_000)
    assert rm.can_trade(10_000, 0) == (True, "ok")
    rm.record_trade(_trade(-200))
    assert rm.can_trade(9_800, 0)[0]
    rm.record_trade(_trade(-150))
    assert rm.can_trade(9_650, 0) == (False, "daily_loss_limit")
    now[0] += 86_400
    assert rm.can_trade(9_650, 0)[0]   # new UTC day resets the stats


def test_cooldown_consecutive_losses_and_caps():
    now = [1_700_000_000.0]
    rm = RiskManager(RiskConfig(cooldown_after_loss_sec=60, max_consecutive_losses=2, consecutive_loss_pause_sec=600,
                                max_trades_per_day=3, max_open_positions=1), clock=lambda: now[0])
    rm.start_day(1000)
    assert rm.can_trade(1000, 1) == (False, "max_open_positions")
    rm.record_trade(_trade(-1))
    assert rm.can_trade(1000, 0) == (False, "cooldown")
    now[0] += 61
    assert rm.can_trade(1000, 0)[0]
    rm.record_trade(_trade(-1))          # second consecutive loss -> pause
    now[0] += 61
    assert rm.can_trade(1000, 0) == (False, "consecutive_loss_pause")
    now[0] += 600
    assert rm.can_trade(1000, 0)[0]
    for _ in range(3):
        rm.on_open()
    assert rm.can_trade(1000, 0) == (False, "max_trades_per_day")


def test_profit_target():
    rm = RiskManager(RiskConfig(max_daily_profit_pct=2.0, cooldown_after_loss_sec=0))
    rm.start_day(1000)
    rm.record_trade(_trade(25))
    assert rm.can_trade(1025, 0) == (False, "daily_profit_target")


def test_invalid_config():
    with pytest.raises(ValueError):
        RiskManager(RiskConfig(risk_per_trade_pct=0, fixed_volume=0))
