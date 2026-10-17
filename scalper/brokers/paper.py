"""Paper broker: real market data from another broker (or a replay source), simulated fills.

Fills are simulated at the quoted bid/ask plus optional slippage. Stop-loss / take-profit are
checked on every quote refresh. Use it to validate the bot on live Deriv / MT5 prices with
zero risk before enabling real orders.
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Dict, List, Optional

from ..models import Candle, ClosedTrade, Position, Quote, Side, SymbolInfo
from .base import Broker, BrokerError

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, data: Broker, balance: float = 10_000.0, slippage_points: float = 0.0,
                 commission_per_unit: float = 0.0, instrument: str = "cfd", multiplier: int = 100,
                 tick_value: float = 1.0, min_volume: float = 0.01, max_volume: float = 100.0,
                 volume_step: float = 0.01):
        self.data = data
        self._balance = float(balance)
        self.slippage_points = float(slippage_points)
        self.commission_per_unit = float(commission_per_unit)
        self.instrument = instrument            # "cfd" (lots, tick_value) | "multiplier" (stake, multiplier)
        self.multiplier = int(multiplier)
        self.tick_value = float(tick_value)     # account currency per point per 1.0 lot (cfd mode)
        self.min_volume, self.max_volume, self.volume_step = min_volume, max_volume, volume_step
        self._positions: Dict[str, Position] = {}
        self._closed: Dict[str, ClosedTrade] = {}
        self._ids = itertools.count(1)
        self._info: Dict[str, SymbolInfo] = {}
        self._last_quote: Dict[str, Quote] = {}

    async def connect(self) -> None:
        await self.data.connect()

    async def close(self) -> None:
        await self.data.close()

    async def symbol_info(self, symbol: str) -> SymbolInfo:
        base = await self.data.symbol_info(symbol)
        info = SymbolInfo(name=base.name, point=base.point, digits=base.digits, min_volume=self.min_volume,
                          max_volume=self.max_volume, volume_step=self.volume_step,
                          stops_level_points=base.stops_level_points, description=base.description)
        self._info[symbol] = info
        return info

    async def balance(self) -> float:
        return self._balance

    async def market_open(self, symbol: str) -> bool:
        return await self.data.market_open(symbol)

    async def candles(self, symbol: str, timeframe_sec: int, count: int) -> List[Candle]:
        return await self.data.candles(symbol, timeframe_sec, count)

    async def quote(self, symbol: str) -> Quote:
        q = await self.data.quote(symbol)
        self._last_quote[symbol] = q
        self._check_stops(symbol, q)
        return q

    async def open_positions(self, symbol: str) -> List[Position]:
        return [p for p in self._positions.values() if p.symbol == symbol]

    def _pnl(self, pos: Position, exit_price: float, info: SymbolInfo) -> float:
        diff = (exit_price - pos.entry_price) if pos.side is Side.BUY else (pos.entry_price - exit_price)
        if self.instrument == "multiplier":
            gross = pos.volume * self.multiplier * diff / pos.entry_price
        else:
            gross = diff / info.point * self.tick_value * pos.volume
        return gross - self.commission_per_unit * pos.volume

    async def loss_per_unit(self, symbol: str, side: Side, entry: float, stop_loss: float) -> float:
        if self.instrument == "multiplier":
            return self.multiplier * abs(entry - stop_loss) / entry if entry > 0 else 0.0
        info = self._info.get(symbol) or await self.symbol_info(symbol)
        return abs(entry - stop_loss) / info.point * self.tick_value

    def _close(self, pos: Position, exit_price: float, reason: str, when: float) -> ClosedTrade:
        info = self._info[pos.symbol]
        pnl = self._pnl(pos, exit_price, info)
        self._balance += pnl
        ct = ClosedTrade(id=pos.id, symbol=pos.symbol, side=pos.side, volume=pos.volume, entry_price=pos.entry_price,
                         exit_price=exit_price, open_time=pos.open_time, close_time=when, pnl=round(pnl, 2), reason=reason)
        self._positions.pop(pos.id, None)
        self._closed[pos.id] = ct
        log.info("PAPER close %s %s @ %.5f pnl=%.2f (%s) balance=%.2f", pos.side.value, pos.symbol, exit_price, pnl,
                 reason, self._balance)
        return ct

    def _check_stops(self, symbol: str, q: Quote) -> None:
        for pos in list(self._positions.values()):
            if pos.symbol != symbol:
                continue
            if pos.side is Side.BUY:
                if pos.stop_loss > 0 and q.bid <= pos.stop_loss:
                    self._close(pos, q.bid, "sl", q.time)
                elif pos.take_profit > 0 and q.bid >= pos.take_profit:
                    self._close(pos, q.bid, "tp", q.time)
            else:
                if pos.stop_loss > 0 and q.ask >= pos.stop_loss:
                    self._close(pos, q.ask, "sl", q.time)
                elif pos.take_profit > 0 and q.ask <= pos.take_profit:
                    self._close(pos, q.ask, "tp", q.time)

    async def open_market(self, symbol: str, side: Side, volume: float, stop_loss: float,
                          take_profit: float, comment: str = "") -> Position:
        info = self._info.get(symbol) or await self.symbol_info(symbol)
        q = self._last_quote.get(symbol) or await self.quote(symbol)
        slip = self.slippage_points * info.point
        price = (q.ask + slip) if side is Side.BUY else (q.bid - slip)
        pos = Position(id=str(next(self._ids)), symbol=symbol, side=side, volume=volume, entry_price=info.round_price(price),
                       stop_loss=stop_loss, take_profit=take_profit, open_time=q.time, comment=comment)
        self._positions[pos.id] = pos
        log.info("PAPER open %s %s vol=%.2f @ %.5f sl=%.5f tp=%.5f", side.value, symbol, volume, pos.entry_price,
                 stop_loss, take_profit)
        return pos

    async def modify_position(self, pos: Position, stop_loss: float, take_profit: float) -> Position:
        live = self._positions.get(pos.id)
        if live is None:
            raise BrokerError(f"paper position {pos.id} is not open")
        live.stop_loss = stop_loss
        live.take_profit = take_profit
        pos.stop_loss, pos.take_profit = stop_loss, take_profit
        return live

    async def close_position(self, pos: Position, reason: str = "") -> ClosedTrade:
        live = self._positions.get(pos.id)
        if live is None:
            ct = self._closed.get(pos.id)
            if ct is not None:
                return ct
            raise BrokerError(f"paper position {pos.id} is not open")
        q = self._last_quote.get(pos.symbol) or await self.quote(pos.symbol)
        price = q.bid if pos.side is Side.BUY else q.ask
        return self._close(live, price, reason or "manual", q.time)

    async def closed_trade(self, position_id: str) -> Optional[ClosedTrade]:
        return self._closed.get(position_id)
