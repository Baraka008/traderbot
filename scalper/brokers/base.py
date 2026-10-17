"""Abstract broker interface implemented by the Deriv, MT5 and paper adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import Candle, ClosedTrade, Position, Quote, Side, SymbolInfo


class BrokerError(RuntimeError):
    pass


class ConnectionLost(BrokerError):
    """Transport-level failure: the engine reconnects instead of retrying the call."""


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def symbol_info(self, symbol: str) -> SymbolInfo: ...

    async def market_open(self, symbol: str) -> bool:
        """Whether the market currently accepts trades (default: always)."""
        return True

    @abstractmethod
    async def balance(self) -> float: ...

    @abstractmethod
    async def candles(self, symbol: str, timeframe_sec: int, count: int) -> List[Candle]:
        """Return the most recent ``count`` candles, oldest first. The last one may be forming."""

    @abstractmethod
    async def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def open_positions(self, symbol: str) -> List[Position]: ...

    @abstractmethod
    async def loss_per_unit(self, symbol: str, side: Side, entry: float, stop_loss: float) -> float:
        """Account-currency loss for one unit of volume when price goes from entry to stop."""

    @abstractmethod
    async def open_market(self, symbol: str, side: Side, volume: float, stop_loss: float,
                          take_profit: float, comment: str = "") -> Position: ...

    @abstractmethod
    async def modify_position(self, pos: Position, stop_loss: float, take_profit: float) -> Position: ...

    @abstractmethod
    async def close_position(self, pos: Position, reason: str = "") -> ClosedTrade: ...

    @abstractmethod
    async def closed_trade(self, position_id: str) -> Optional[ClosedTrade]:
        """Look up the result of a position that is no longer open (SL/TP hit). None if unknown yet."""
