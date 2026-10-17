"""Integration test: engine + paper broker over a scripted price feed."""
import asyncio
from typing import List

from scalper.backtest import generate_candles
from scalper.brokers.base import Broker
from scalper.brokers.paper import PaperBroker
from scalper.engine import Engine, EngineConfig
from scalper.models import Candle, Quote, Side, SymbolInfo
from scalper.risk import RiskConfig, RiskManager
from scalper.strategy import ScalpStrategy, StrategyConfig


class ReplayBroker(Broker):
    """Feeds pre-generated candles one bar per quote() call (ticks at bar close)."""
    name = "replay"

    def __init__(self, candles: List[Candle], spread: float = 0.0):
        self.all = candles
        self.i = 100
        self.spread = spread
        self.connected = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def symbol_info(self, symbol):
        return SymbolInfo(symbol, point=0.01, digits=2, min_volume=0.01, max_volume=100, volume_step=0.01)

    async def balance(self):
        return 0.0

    async def candles(self, symbol, tf, count):
        return self.all[max(0, self.i - count): self.i + 1]

    async def quote(self, symbol):
        if self.i < len(self.all) - 1:
            self.i += 1
        c = self.all[self.i]
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


def test_engine_trades_on_paper_broker(tmp_path):
    candles = generate_candles(1500, seed=11)
    data = ReplayBroker(candles, spread=0.05)
    paper = PaperBroker(data, balance=10_000, instrument="cfd", tick_value=1.0)
    strat = ScalpStrategy(StrategyConfig())
    risk = RiskManager(RiskConfig(cooldown_after_loss_sec=0, max_consecutive_losses=0, max_trades_per_day=0))
    journal = tmp_path / "j.csv"
    cfg = EngineConfig(symbol="SYN", timeframe_sec=60, poll_interval_sec=0, candle_count=300, journal_csv=str(journal),
                       max_iterations=1300, close_positions_on_exit=True)
    engine = Engine(paper, strat, risk, cfg)
    asyncio.run(engine.run())
    assert engine.iterations == 1300
    assert len(engine.closed_trades) >= 3
    assert not data.connected   # broker closed on shutdown
    text = journal.read_text().splitlines()
    assert text[0].startswith("id,symbol,side") and len(text) == len(engine.closed_trades) + 1
    assert abs(risk.stats.realized_pnl - sum(t.pnl for t in engine.closed_trades)) < 1e-6
    reasons = {t.reason for t in engine.closed_trades}
    assert reasons & {"sl", "tp", "max_hold", "opposite_cross", "shutdown"}
    for t in engine.closed_trades:
        assert t.side in (Side.BUY, Side.SELL)


class FrozenBroker(ReplayBroker):
    """Price feed that stops updating after the first quote (simulates a frozen feed)."""
    def __init__(self, candles):
        super().__init__(candles)
        self.frozen = None

    async def quote(self, symbol):
        if self.frozen is None:
            self.frozen = await super().quote(symbol)
        return self.frozen


def test_engine_blocks_entries_on_stale_feed_and_persists_history(tmp_path):
    import time
    candles = generate_candles(600, seed=2)
    data = ReplayBroker(candles, spread=0.05)
    paper = PaperBroker(data, balance=10_000, instrument="cfd", tick_value=1.0)
    cfg = EngineConfig(symbol="SYN", timeframe_sec=60, poll_interval_sec=0, candle_count=300, max_iterations=400,
                       history_dir=str(tmp_path / "hist"), max_quote_age_sec=0)
    engine = Engine(paper, ScalpStrategy(StrategyConfig()), RiskManager(RiskConfig(cooldown_after_loss_sec=0)), cfg)
    asyncio.run(engine.run())
    from scalper.history import HistoryStore
    store = HistoryStore(str(tmp_path / "hist"))
    assert store.stats("SYN", 60)["bars"] > 300
    snap = open(store.snapshot_path("SYN", 60)).read().splitlines()
    assert snap[0].startswith("time,close,ema_fast") and len(snap) > 100

    # stale feed: quote time never changes -> after max_quote_age the engine must not open trades
    frozen = FrozenBroker(candles)
    paper2 = PaperBroker(frozen, balance=10_000, instrument="cfd", tick_value=1.0)
    cfg2 = EngineConfig(symbol="SYN", timeframe_sec=60, poll_interval_sec=0.001, candle_count=300, max_iterations=50,
                        max_quote_age_sec=0.01)
    engine2 = Engine(paper2, ScalpStrategy(StrategyConfig()), RiskManager(RiskConfig()), cfg2)
    asyncio.run(engine2.run())
    assert engine2._stale_logged is True
    assert engine2.risk.stats.trades_opened == 0


def test_engine_isolates_transient_broker_errors():
    from scalper.brokers.base import BrokerError

    class FlakyBroker(ReplayBroker):
        def __init__(self, candles):
            super().__init__(candles)
            self.calls = 0

        async def quote(self, symbol):
            self.calls += 1
            if self.calls % 7 == 0:
                raise BrokerError("transient")
            return await super().quote(symbol)

    candles = generate_candles(400, seed=3)
    flaky = FlakyBroker(candles)
    paper = PaperBroker(flaky, balance=10_000, instrument="cfd", tick_value=1.0)
    cfg = EngineConfig(symbol="SYN", timeframe_sec=60, poll_interval_sec=0, candle_count=300, max_iterations=60,
                       max_quote_age_sec=0)
    engine = Engine(paper, ScalpStrategy(StrategyConfig()), RiskManager(RiskConfig()), cfg)
    asyncio.run(engine.run())
    assert engine.iterations == 60            # errors were logged, loop kept going, no reconnect storm
    assert engine._errors_in_row == 0
