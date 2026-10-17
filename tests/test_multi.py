"""Portfolio (multi-symbol) engine, symbol-list config and per-symbol Deriv multipliers."""
import asyncio
from typing import Dict, List

import pytest
import yaml

from scalper.backtest import generate_candles
from scalper.brokers.base import Broker
from scalper.brokers.paper import PaperBroker
from scalper.config import Config
from scalper.engine import Engine, EngineConfig, MultiEngine
from scalper.models import Candle, Quote, Side, SymbolInfo
from scalper.risk import RiskConfig, RiskManager
from scalper.strategy import ScalpStrategy, StrategyConfig


class MultiReplayBroker(Broker):
    """One shared data feed serving several symbols, one bar per quote() call per symbol."""
    name = "replay"

    def __init__(self, feeds: Dict[str, List[Candle]], spread: float = 0.05):
        self.feeds = feeds
        self.i = {s: 100 for s in feeds}
        self.spread = spread
        self.connects = 0

    async def connect(self):
        self.connects += 1

    async def close(self):
        pass

    async def symbol_info(self, symbol):
        return SymbolInfo(symbol, point=0.01, digits=2, min_volume=0.01, max_volume=100, volume_step=0.01)

    async def balance(self):
        return 0.0

    async def candles(self, symbol, tf, count):
        i = self.i[symbol]
        return self.feeds[symbol][max(0, i - count): i + 1]

    async def quote(self, symbol):
        if self.i[symbol] < len(self.feeds[symbol]) - 1:
            self.i[symbol] += 1
        c = self.feeds[symbol][self.i[symbol]]
        return Quote(bid=c.open - self.spread / 2, ask=c.open + self.spread / 2, time=float(c.time))

    async def open_positions(self, symbol):
        return []

    async def loss_per_unit(self, symbol, side, entry, sl):
        return abs(entry - sl) / 0.01

    async def open_market(self, *a, **k):
        raise NotImplementedError

    async def modify_position(self, *a, **k):
        raise NotImplementedError

    async def close_position(self, *a, **k):
        raise NotImplementedError

    async def closed_trade(self, pid):
        return None


class CountingPaper(PaperBroker):
    """Paper broker that records the portfolio-wide open-position high-water mark."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.max_open_seen = 0

    async def open_market(self, *a, **k):
        pos = await super().open_market(*a, **k)
        self.max_open_seen = max(self.max_open_seen, len(self._positions))
        return pos


def _run_portfolio(max_open: int, per_symbol: int = 1, iterations: int = 1400):
    feeds = {"R_75": generate_candles(1600, seed=11), "R_100": generate_candles(1600, seed=12),
             "frxEURUSD": generate_candles(1600, seed=13)}
    data = MultiReplayBroker(feeds)
    paper = CountingPaper(data, balance=10_000, instrument="cfd", tick_value=1.0)
    risk = RiskManager(RiskConfig(cooldown_after_loss_sec=0, max_consecutive_losses=0, max_trades_per_day=0,
                                  max_open_positions=max_open, max_positions_per_symbol=per_symbol))
    engines = [Engine(paper, ScalpStrategy(StrategyConfig()), risk,
                      EngineConfig(symbol=s, timeframe_sec=60, poll_interval_sec=0, candle_count=300,
                                   max_iterations=iterations, max_quote_age_sec=0)) for s in feeds]
    multi = MultiEngine(paper, engines, EngineConfig(symbol="*", max_iterations=iterations, close_positions_on_exit=True))
    asyncio.run(multi.run())
    return data, paper, risk, engines, multi


def test_multi_engine_trades_several_symbols_with_shared_risk():
    data, paper, risk, engines, multi = _run_portfolio(max_open=3)
    assert data.connects == 1                                    # one shared connection
    traded = {t.symbol for t in multi.closed_trades}
    assert len(traded) >= 2                                      # positions opened on several charts
    assert all(e.iterations == 1400 for e in engines)
    assert risk.stats.trades_opened == sum(1 for t in multi.closed_trades if t.reason != "shutdown") + \
        sum(1 for t in multi.closed_trades if t.reason == "shutdown")
    assert abs(risk.stats.realized_pnl - sum(t.pnl for t in multi.closed_trades)) < 1e-6   # portfolio-wide P/L
    assert paper.max_open_seen <= 3
    assert multi.closed_trades == sorted(multi.closed_trades, key=lambda t: t.close_time)


def test_portfolio_cap_limits_simultaneous_positions():
    _, paper, _, _, multi = _run_portfolio(max_open=1)
    assert paper.max_open_seen == 1
    assert len({t.symbol for t in multi.closed_trades}) >= 2      # still rotates across symbols


def test_config_symbols_list_with_overrides(tmp_path):
    raw = {
        "broker": "paper", "timeframe_sec": 60,
        "symbols": ["R_75", {"symbol": "frxEURUSD", "multiplier": 500, "strategy": {"max_spread_points": 20}},
                    {"symbol": "R_100", "timeframe_sec": 300}, "R_75"],
        "strategy": {"ema_fast": 7}, "risk": {"max_open_positions": 1},
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    cfg = Config.load(str(p))
    assert [s.symbol for s in cfg.symbols] == ["R_75", "frxEURUSD", "R_100"]      # de-duplicated, order kept
    assert cfg.symbols[0].strategy.ema_fast == 7 and cfg.symbols[1].strategy.ema_fast == 7
    assert cfg.symbols[1].strategy.max_spread_points == 20 and cfg.symbols[0].strategy.max_spread_points == 0
    assert cfg.symbols[1].multiplier == 500 and cfg.deriv_multipliers == {"frxEURUSD": 500}
    assert cfg.symbols[2].timeframe_sec == 300 and cfg.symbols[2].strategy.timeframe_sec == 300
    assert cfg.symbols[2].engine.symbol == "R_100" and cfg.symbols[2].engine.timeframe_sec == 300
    assert cfg.risk.max_open_positions == 3                 # raised to the number of symbols
    assert cfg.symbol == "R_75" and cfg.engine.symbol == "R_75"
    assert [s.symbol for s in cfg.select("R_100")] == ["R_100"]
    with pytest.raises(ValueError):
        cfg.select("XAUUSD")
    single = Config({"symbol": "EURUSD"})
    assert [s.symbol for s in single.symbols] == ["EURUSD"] and single.risk.max_open_positions == 1
    csv_style = Config({"symbols": "R_10, R_25"})
    assert [s.symbol for s in csv_style.symbols] == ["R_10", "R_25"]


def test_deriv_per_symbol_multiplier():
    from tests.test_deriv import FakeDeriv, FakeWS

    async def go():
        ws = FakeWS()
        b = FakeDeriv(ws, multiplier=77, symbol_multipliers={"R_75": 100})
        await b.connect()
        await b.symbol_info("R_75")                              # 100 is allowed even though default 77 is not
        assert b.mult("R_75") == 100 and b.mult("R_50") == 77
        assert await b.loss_per_unit("R_75", Side.BUY, 1234.5, 1228.3275) == pytest.approx(0.5)
        pos = await b.open_market("R_75", Side.BUY, 10.0, 1228.3275, 1243.75875)
        assert [m for m in ws.sent if "buy" in m][-1]["parameters"]["multiplier"] == 100
        assert pos.stop_loss == pytest.approx(1228.3275)
        await b.close()

    asyncio.run(go())


def test_risk_per_symbol_cap():
    rm = RiskManager(RiskConfig(max_open_positions=4, max_positions_per_symbol=1))
    rm.start_day(1000)
    assert rm.can_trade(1000, 2, 0)[0]
    assert rm.can_trade(1000, 2, 1) == (False, "max_positions_per_symbol")
    assert rm.can_trade(1000, 4, 0) == (False, "max_open_positions")
    with pytest.raises(ValueError):
        RiskManager(RiskConfig(max_positions_per_symbol=0))
