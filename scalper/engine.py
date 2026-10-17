"""Live trading loop: polls quotes, evaluates the strategy on closed bars, manages positions."""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .brokers.base import Broker, BrokerError, ConnectionLost
from .history import HistoryStore, validate_candles
from .models import ClosedTrade, Position
from .risk import RiskManager
from .strategy import ScalpStrategy

log = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    symbol: str
    timeframe_sec: int = 60
    poll_interval_sec: float = 1.0
    candle_count: int = 300
    close_positions_on_exit: bool = False
    journal_csv: str = ""
    reconnect_backoff_max_sec: float = 60.0
    history_dir: str = ""           # persist candles + indicator snapshots here ("" = off)
    log_indicators: bool = True     # write per-bar indicator snapshots to history_dir
    max_quote_age_sec: float = 30.0 # block entries when the quote has not changed for this long (0 = off)
    max_consecutive_errors: int = 5 # reconnect after this many broker errors in a row
    max_iterations: int = 0     # testing hook: stop after N loop iterations (0 = run forever)


class Journal:
    FIELDS = ["id", "symbol", "side", "volume", "entry_price", "exit_price", "open_time", "close_time", "pnl", "reason"]

    def __init__(self, path: str):
        self.path = path
        if path and not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, t: ClosedTrade) -> None:
        if not self.path:
            return
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow({
                "id": t.id, "symbol": t.symbol, "side": t.side.value, "volume": t.volume, "entry_price": t.entry_price,
                "exit_price": t.exit_price, "open_time": int(t.open_time), "close_time": int(t.close_time),
                "pnl": round(t.pnl, 2), "reason": t.reason})


class Engine:
    def __init__(self, broker: Broker, strategy: ScalpStrategy, risk: RiskManager, cfg: EngineConfig):
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.cfg = cfg
        self.journal = Journal(cfg.journal_csv)
        self.stop_event = asyncio.Event()
        self.tracked: Dict[str, Position] = {}
        self._pending_close: Dict[str, int] = {}
        self._last_block_reason = ""
        self.iterations = 0
        self.closed_trades: list = []
        self.history = HistoryStore(cfg.history_dir) if cfg.history_dir else None
        self._last_quote_time: float = 0.0
        self._last_quote_seen_at: float = 0.0
        self._stale_logged = False
        self._errors_in_row = 0

    # ------------------------------------------------------------------ lifecycle
    def request_stop(self) -> None:
        log.info("stop requested")
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):   # Windows
                signal.signal(sig, lambda *_: self.request_stop())

    async def run(self) -> None:
        self._install_signal_handlers()
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                await self._session()
                backoff = 1.0
                if self.stop_event.is_set() or (self.cfg.max_iterations and self.iterations >= self.cfg.max_iterations):
                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # connection / broker errors -> reconnect with backoff
                log.error("session error: %s (reconnecting in %.0fs)", e, backoff, exc_info=log.isEnabledFor(logging.DEBUG))
                try:
                    await self.broker.close()
                except Exception:
                    pass
                if self.cfg.max_iterations:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.reconnect_backoff_max_sec)
        await self._shutdown()

    async def _shutdown(self) -> None:
        try:
            if self.cfg.close_positions_on_exit:
                for pos in list(self.tracked.values()):
                    try:
                        ct = await self.broker.close_position(pos, "shutdown")
                        self._on_closed(ct)
                    except Exception as e:
                        log.error("could not close %s on exit: %s", pos.id, e)
        finally:
            try:
                await self.broker.close()
            except Exception:
                pass
        s = self.risk.stats
        log.info("stopped. day pnl=%.2f trades=%d wins=%d losses=%d", s.realized_pnl, s.trades_opened, s.wins, s.losses)

    # ------------------------------------------------------------------ main loop
    async def _session(self) -> None:
        cfg = self.cfg
        await self.broker.connect()
        info = await self.broker.symbol_info(cfg.symbol)
        balance = await self.broker.balance()
        if self.risk.stats.day == "":
            self.risk.start_day(balance)
        self.tracked = {p.id: p for p in await self.broker.open_positions(cfg.symbol)}
        log.info("engine started: %s (%s) tf=%ds balance=%.2f open_positions=%d broker=%s", info.name, info.description,
                 cfg.timeframe_sec, balance, len(self.tracked), self.broker.name)

        last_bar: Optional[int] = None
        candles = []
        tf = cfg.timeframe_sec
        count = max(cfg.candle_count, self.strategy.lookback_bars())
        while not self.stop_event.is_set():
            self.iterations += 1
            try:
                quote = await self.broker.quote(cfg.symbol)
                bar_id = int(quote.time // tf)
                new_bar = False
                if bar_id != last_bar:
                    raw = await self.broker.candles(cfg.symbol, tf, count)
                    candles, issues = validate_candles([c for c in raw if c.time < bar_id * tf], tf)
                    for issue in issues:
                        log.warning("candle data: %s", issue)
                    new_bar = last_bar is not None
                    last_bar = bar_id
                    if candles:
                        self._on_closed_bar(candles, quote)

                stale = self._quote_is_stale(quote)      # checked every poll so a frozen feed is reported
                await self._reconcile()
                await self._manage(candles, quote, new_bar, info)
                if new_bar and not stale:
                    await self._maybe_enter(candles, quote, info)
                self._errors_in_row = 0
            except ConnectionLost:
                raise
            except BrokerError as e:
                self._errors_in_row += 1
                log.error("broker error (%d in a row): %s", self._errors_in_row, e)
                if self._errors_in_row >= cfg.max_consecutive_errors:
                    raise ConnectionLost(f"too many consecutive broker errors: {e}")
                await asyncio.sleep(min(5.0, cfg.poll_interval_sec * 2 + 1))

            if cfg.max_iterations and self.iterations >= cfg.max_iterations:
                return
            await asyncio.sleep(cfg.poll_interval_sec)

    def _on_closed_bar(self, candles, quote) -> None:
        """Persist history and indicator snapshots for the newest closed bar."""
        if self.history is None:
            return
        try:
            self.history.append(self.cfg.symbol, self.cfg.timeframe_sec, candles[-5:])
            if self.cfg.log_indicators and len(candles) >= self.strategy.warmup_bars():
                self.history.snapshot(self.cfg.symbol, self.cfg.timeframe_sec, self.strategy.snapshot(candles, quote))
        except OSError as e:
            log.warning("history write failed: %s", e)

    def _quote_is_stale(self, quote) -> bool:
        """True when the broker keeps returning the same quote time (feed frozen / market closed)."""
        if self.cfg.max_quote_age_sec <= 0:
            return False
        now = time.monotonic()
        if quote.time != self._last_quote_time:
            self._last_quote_time = quote.time
            self._last_quote_seen_at = now
            if self._stale_logged:
                log.info("quote feed resumed")
                self._stale_logged = False
            return False
        if self._last_quote_seen_at and now - self._last_quote_seen_at > self.cfg.max_quote_age_sec:
            if not self._stale_logged:
                log.warning("quote feed stale for %.0fs; entries blocked", now - self._last_quote_seen_at)
                self._stale_logged = True
            return True
        return False

    async def _reconcile(self) -> None:
        """Detect positions closed by the broker (SL/TP hit) and book their result."""
        current = {p.id: p for p in await self.broker.open_positions(self.cfg.symbol)}
        for pid in list(self.tracked):
            if pid in current:
                continue
            ct = await self.broker.closed_trade(pid)
            if ct is None:
                tries = self._pending_close.get(pid, 0) + 1
                self._pending_close[pid] = tries
                if tries > 30:
                    log.warning("position %s vanished without a trade record; dropping it", pid)
                    self.tracked.pop(pid, None)
                    self._pending_close.pop(pid, None)
                continue
            self._pending_close.pop(pid, None)
            self.tracked.pop(pid, None)
            self._on_closed(ct)
        for pid, pos in current.items():
            if pid in self.tracked:
                pos.be_done = self.tracked[pid].be_done
            self.tracked[pid] = pos

    def _on_closed(self, ct: ClosedTrade) -> None:
        self.risk.record_trade(ct)
        self.journal.write(ct)
        self.closed_trades.append(ct)
        s = self.risk.stats
        log.info("CLOSED %s %s vol=%s entry=%.5f exit=%.5f pnl=%+.2f reason=%s | day pnl=%+.2f (%dW/%dL)", ct.side.value,
                 ct.symbol, ct.volume, ct.entry_price, ct.exit_price, ct.pnl, ct.reason, s.realized_pnl, s.wins, s.losses)

    async def _manage(self, candles, quote, new_bar, info) -> None:
        for pos in list(self.tracked.values()):
            act = self.strategy.manage(pos, candles, quote, new_bar, info)
            if act.close:
                try:
                    ct = await self.broker.close_position(pos, act.reason)
                except BrokerError as e:
                    log.error("close %s failed: %s", pos.id, e)
                    continue
                self.tracked.pop(pos.id, None)
                self._on_closed(ct)
            elif act.modifies:
                new_sl = act.new_sl if act.new_sl is not None else pos.stop_loss
                new_tp = act.new_tp if act.new_tp is not None else pos.take_profit
                try:
                    await self.broker.modify_position(pos, new_sl, new_tp)
                    log.info("MODIFY %s sl=%.5f tp=%.5f (price bid=%.5f ask=%.5f)", pos.id, new_sl, new_tp, quote.bid, quote.ask)
                except BrokerError as e:
                    log.warning("modify %s failed: %s", pos.id, e)

    def _blocked(self, why: str) -> None:
        if why != self._last_block_reason:
            log.info("entries blocked: %s", why)
            self._last_block_reason = why

    async def _maybe_enter(self, candles, quote, info) -> None:
        if not await self.broker.market_open(self.cfg.symbol):
            self._blocked("market_closed")
            return
        balance = await self.broker.balance()
        ok, why = self.risk.can_trade(balance, len(self.tracked))
        if not ok:
            self._blocked(why)
            return
        sig = self.strategy.evaluate(candles, quote, info)
        if sig is None:
            if self.strategy.last_skip_reason in ("outside_session", "spread_too_wide", "atr_too_high", "atr_too_low",
                                                  "warming_up", "htf_warming_up"):
                self._blocked(self.strategy.last_skip_reason)
            else:
                self._last_block_reason = ""
            return
        self._last_block_reason = ""
        lpu = await self.broker.loss_per_unit(self.cfg.symbol, sig.side, sig.entry, sig.stop_loss)
        volume = self.risk.volume_for(balance, lpu, info)
        if volume <= 0:
            log.info("signal %s skipped: computed volume is 0 (loss_per_unit=%.4f balance=%.2f)", sig.reason, lpu, balance)
            return
        try:
            pos = await self.broker.open_market(self.cfg.symbol, sig.side, volume, sig.stop_loss, sig.take_profit,
                                                comment=sig.reason[:31])
        except BrokerError as e:
            log.error("open %s failed: %s", sig.side.value, e)
            return
        self.risk.on_open()
        self.tracked[pos.id] = pos
        log.info("OPEN %s %s vol=%s entry=%.5f sl=%.5f tp=%.5f atr=%.5f (%s)", pos.side.value, pos.symbol, volume,
                 pos.entry_price, pos.stop_loss, pos.take_profit, sig.atr, sig.reason)
