"""Shared data types used by the strategy, risk manager, brokers and engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass
class Candle:
    time: int          # bar open time, epoch seconds (UTC / server time)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Quote:
    bid: float
    ask: float
    time: float        # epoch seconds

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass
class SymbolInfo:
    name: str
    point: float             # smallest price increment, e.g. 0.00001 or 0.01
    digits: int
    min_volume: float        # lots (MT5) or stake in account currency (Deriv)
    max_volume: float
    volume_step: float
    stops_level_points: int = 0   # broker minimum SL/TP distance in points
    description: str = ""

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def round_volume(self, volume: float) -> float:
        if self.volume_step <= 0:
            return volume
        steps = int(volume / self.volume_step + 1e-9)
        decimals = max(0, -int(f"{self.volume_step:e}".split("e")[1]))
        return round(steps * self.volume_step, decimals + 2)


@dataclass
class Signal:
    side: Side
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    reason: str


@dataclass
class Position:
    id: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    open_time: float          # epoch seconds
    unrealized_pnl: float = 0.0
    comment: str = ""
    be_done: bool = False     # breakeven already applied (engine-side flag)


@dataclass
class ClosedTrade:
    id: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    exit_price: float
    open_time: float
    close_time: float
    pnl: float                # net of commission / swap, in account currency
    reason: str = ""


@dataclass
class ManageAction:
    close: bool = False
    reason: str = ""
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None

    @property
    def modifies(self) -> bool:
        return self.new_sl is not None or self.new_tp is not None
