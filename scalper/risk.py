"""Risk management: position sizing, daily loss / profit limits, trade caps, cooldowns."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .models import ClosedTrade, SymbolInfo


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5      # % of balance risked between entry and stop
    fixed_volume: float = 0.0            # > 0 overrides risk-based sizing (lots or stake)
    max_risk_overshoot: float = 2.0      # allow min-volume trades up to this x intended risk
    max_daily_loss_pct: float = 3.0      # stop trading for the day after this drawdown
    max_daily_profit_pct: float = 0.0    # 0 = no profit cap
    max_trades_per_day: int = 30
    max_open_positions: int = 1          # across ALL symbols (portfolio cap)
    max_positions_per_symbol: int = 1
    max_consecutive_losses: int = 3      # pause for cooldown after this many losses in a row
    cooldown_after_loss_sec: int = 120   # wait after any loss before re-entering
    consecutive_loss_pause_sec: int = 1800

    def validate(self) -> None:
        if self.risk_per_trade_pct <= 0 and self.fixed_volume <= 0:
            raise ValueError("risk_per_trade_pct must be > 0 unless fixed_volume is set")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.max_positions_per_symbol < 1:
            raise ValueError("max_positions_per_symbol must be >= 1")


@dataclass
class DayStats:
    day: str = ""
    start_balance: float = 0.0
    realized_pnl: float = 0.0
    trades_opened: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    last_loss_time: float = 0.0
    paused_until: float = 0.0


class RiskManager:
    def __init__(self, cfg: RiskConfig, clock: Callable[[], float] = time.time):
        cfg.validate()
        self.cfg = cfg
        self.clock = clock
        self.stats = DayStats()

    # ------------------------------------------------------------------ day handling
    def _day_key(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def start_day(self, balance: float) -> None:
        self.stats = DayStats(day=self._day_key(), start_balance=balance)

    def _roll_day(self, balance: float) -> None:
        if self.stats.day != self._day_key():
            self.start_day(balance)

    # ------------------------------------------------------------------ events
    def on_open(self) -> None:
        self.stats.trades_opened += 1

    def record_trade(self, trade: ClosedTrade) -> None:
        s = self.stats
        s.realized_pnl += trade.pnl
        if trade.pnl >= 0:
            s.wins += 1
            s.consecutive_losses = 0
        else:
            s.losses += 1
            s.consecutive_losses += 1
            s.last_loss_time = self.clock()
            if self.cfg.max_consecutive_losses > 0 and s.consecutive_losses >= self.cfg.max_consecutive_losses:
                s.paused_until = self.clock() + self.cfg.consecutive_loss_pause_sec
                s.consecutive_losses = 0

    # ------------------------------------------------------------------ gates
    def can_trade(self, balance: float, open_positions: int, symbol_positions: Optional[int] = None) -> Tuple[bool, str]:
        """``open_positions`` is the portfolio-wide count, ``symbol_positions`` the count on this symbol."""
        self._roll_day(balance)
        s, c, now = self.stats, self.cfg, self.clock()
        if open_positions >= c.max_open_positions:
            return False, "max_open_positions"
        if symbol_positions is not None and symbol_positions >= c.max_positions_per_symbol:
            return False, "max_positions_per_symbol"
        if c.max_trades_per_day > 0 and s.trades_opened >= c.max_trades_per_day:
            return False, "max_trades_per_day"
        base = s.start_balance if s.start_balance > 0 else balance
        if c.max_daily_loss_pct > 0 and s.realized_pnl <= -base * c.max_daily_loss_pct / 100.0:
            return False, "daily_loss_limit"
        if c.max_daily_profit_pct > 0 and s.realized_pnl >= base * c.max_daily_profit_pct / 100.0:
            return False, "daily_profit_target"
        if now < s.paused_until:
            return False, "consecutive_loss_pause"
        if c.cooldown_after_loss_sec > 0 and s.last_loss_time and now - s.last_loss_time < c.cooldown_after_loss_sec:
            return False, "cooldown"
        return True, "ok"

    # ------------------------------------------------------------------ sizing
    def volume_for(self, balance: float, loss_per_unit: float, info: SymbolInfo) -> float:
        """Volume (lots or stake) so that hitting the stop loses ``risk_per_trade_pct`` of balance.

        ``loss_per_unit`` is the account-currency loss for ONE unit of volume if price travels
        from entry to the stop-loss (brokers compute it, since it depends on the instrument).
        """
        c = self.cfg
        if c.fixed_volume > 0:
            vol = c.fixed_volume
            risk_amount = float("inf")
        else:
            if loss_per_unit <= 0 or balance <= 0:
                return 0.0
            risk_amount = balance * c.risk_per_trade_pct / 100.0
            vol = risk_amount / loss_per_unit
        vol = info.round_volume(vol)
        if vol < info.min_volume:
            if info.min_volume * loss_per_unit <= risk_amount * c.max_risk_overshoot:
                vol = info.min_volume
            else:
                return 0.0
        if info.max_volume > 0:
            vol = min(vol, info.max_volume)
        return vol
