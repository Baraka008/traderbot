"""EMA-cross / momentum scalping strategy with ATR-based stops and trade management.

Entry (evaluated once per *closed* bar):
  * ``cross`` mode   - fast EMA crosses the slow EMA, price on the right side of the
                       trend EMA, RSI inside the momentum band.
  * ``momentum`` mode- fast EMA above/below slow EMA, price pulled back to the fast EMA
                       on the previous bar and closed back in trend direction.
Filters: spread vs ATR ratio, absolute spread cap, minimum ATR (volatility floor).
Exits  : ATR stop-loss / take-profit, breakeven, ATR trailing stop, max bars held and
         optional opposite-cross exit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .indicators import atr, ema, rsi
from .models import Candle, ManageAction, Position, Quote, Side, Signal, SymbolInfo


@dataclass
class StrategyConfig:
    timeframe_sec: int = 60
    entry_mode: str = "cross"          # "cross" | "momentum"
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_buy_min: float = 50.0
    rsi_buy_max: float = 75.0
    rsi_sell_min: float = 25.0
    rsi_sell_max: float = 50.0
    atr_period: int = 14
    sl_atr_mult: float = 1.0
    tp_atr_mult: float = 1.5
    min_atr_points: float = 0.0        # 0 disables the volatility floor
    max_atr_points: float = 0.0        # 0 disables; skip entries during abnormal volatility (news spikes)
    trade_hours: str = ""              # e.g. "07:00-20:00" or "22:00-06:00" (broker timestamp zone); "" = always
    trade_days: str = ""               # e.g. "mon,tue,wed,thu,fri"; "" = every day
    htf_multiplier: int = 0            # 0 disables; e.g. 5 = confirm trend on 5x timeframe (M5 when trading M1)
    htf_ema: int = 50                  # EMA length on the higher timeframe
    max_spread_atr_ratio: float = 0.25 # 0 disables
    max_spread_points: float = 0.0     # 0 disables
    max_hold_bars: int = 15            # 0 disables the time exit
    breakeven_atr_mult: float = 0.8    # 0 disables; move SL to entry (+buffer) after this move
    breakeven_buffer_points: float = 2.0
    trail_activate_atr_mult: float = 1.0  # start trailing after this much favourable move
    trail_atr_mult: float = 0.6        # 0 disables trailing
    exit_on_opposite_cross: bool = True

    def validate(self) -> None:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        if self.sl_atr_mult <= 0 or self.tp_atr_mult <= 0:
            raise ValueError("sl_atr_mult and tp_atr_mult must be positive")
        if self.entry_mode not in ("cross", "momentum"):
            raise ValueError("entry_mode must be 'cross' or 'momentum'")
        if self.timeframe_sec <= 0:
            raise ValueError("timeframe_sec must be positive")
        if self.htf_multiplier < 0 or self.htf_multiplier == 1:
            raise ValueError("htf_multiplier must be 0 (off) or >= 2")
        _parse_hours(self.trade_hours)
        _parse_days(self.trade_days)


_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hours(spec: str) -> Optional[Tuple[int, int]]:
    """'HH:MM-HH:MM' -> (start_minute, end_minute); None when empty."""
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        a, b = spec.split("-")
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except ValueError as e:
        raise ValueError(f"trade_hours must look like '07:00-20:00', got '{spec}'") from e
    if not (0 <= ah < 24 and 0 <= bh <= 24 and 0 <= am < 60 and 0 <= bm < 60):
        raise ValueError(f"trade_hours out of range: '{spec}'")
    return ah * 60 + am, bh * 60 + bm


def _parse_days(spec: str) -> Optional[set]:
    spec = (spec or "").strip().lower()
    if not spec:
        return None
    days = set()
    for part in spec.split(","):
        part = part.strip()[:3]
        if part not in _DAYS:
            raise ValueError(f"trade_days must be a comma list of mon..sun, got '{spec}'")
        days.add(_DAYS[part])
    return days


def in_session(ts: float, hours: Optional[Tuple[int, int]], days: Optional[set]) -> bool:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if days is not None and dt.weekday() not in days:
        return False
    if hours is None:
        return True
    minute = dt.hour * 60 + dt.minute
    start, end = hours
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end       # overnight window


def aggregate(candles: Sequence[Candle], timeframe_sec: int, multiplier: int) -> List[Candle]:
    """Build higher-timeframe candles (only fully formed ones) from lower-timeframe candles."""
    size = timeframe_sec * multiplier
    out: List[Candle] = []
    cur: Optional[Candle] = None
    for c in candles:
        bucket = int(c.time // size) * size
        if cur is None or cur.time != bucket:
            if cur is not None:
                out.append(cur)
            cur = Candle(time=bucket, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
        else:
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
    if cur is not None:
        last_lt = candles[-1].time
        if last_lt == cur.time + size - timeframe_sec:   # the HTF bar is complete
            out.append(cur)
    return out


class ScalpStrategy:
    def __init__(self, cfg: StrategyConfig):
        cfg.validate()
        self.cfg = cfg
        self._hours = _parse_hours(cfg.trade_hours)
        self._days = _parse_days(cfg.trade_days)
        self.last_skip_reason = ""

    # ------------------------------------------------------------------ helpers
    def warmup_bars(self) -> int:
        c = self.cfg
        base = max(c.ema_fast, c.ema_slow, c.ema_trend, c.rsi_period + 1, c.atr_period + 1) + 5
        if c.htf_multiplier >= 2:
            base = max(base, (c.htf_ema + 2) * c.htf_multiplier)
        return base

    def lookback_bars(self) -> int:
        """How many candles the strategy wants to see (enough for EMAs to converge)."""
        return max(300, self.warmup_bars() * 2)

    def in_session(self, ts: float) -> bool:
        return in_session(ts, self._hours, self._days)

    def htf_bias(self, candles: Sequence[Candle]) -> Optional[int]:
        """+1 / -1 trend direction on the higher timeframe, 0 when disabled, None when not enough data."""
        c = self.cfg
        if c.htf_multiplier < 2:
            return 0
        htf = aggregate(candles, c.timeframe_sec, c.htf_multiplier)
        if len(htf) < c.htf_ema + 1:
            return None
        e = ema([k.close for k in htf], c.htf_ema)
        if e[-1] is None:
            return None
        return 1 if htf[-1].close > e[-1] else -1

    def snapshot(self, candles: Sequence[Candle], quote: Quote) -> Dict[str, object]:
        """Indicator values of the last closed bar, for chart tracking / journaling."""
        fast, slow, trend, rsi_v, atr_v = self._indicators(candles)
        last = candles[-1]
        def f(x):
            return round(x, 6) if x is not None else ""
        return {"time": last.time, "close": last.close, "ema_fast": f(fast[-1]), "ema_slow": f(slow[-1]),
                "ema_trend": f(trend[-1]), "rsi": f(rsi_v[-1]), "atr": f(atr_v[-1]),
                "spread": round(quote.spread, 6), "bid": quote.bid, "ask": quote.ask}

    def _indicators(self, candles: Sequence[Candle]):
        closes = [c.close for c in candles]
        c = self.cfg
        return (
            ema(closes, c.ema_fast),
            ema(closes, c.ema_slow),
            ema(closes, c.ema_trend),
            rsi(closes, c.rsi_period),
            atr(candles, c.atr_period),
        )

    def current_atr(self, candles: Sequence[Candle]) -> Optional[float]:
        if len(candles) <= self.cfg.atr_period:
            return None
        return atr(candles, self.cfg.atr_period)[-1]

    def _spread_ok(self, quote: Quote, atr_value: float, info: SymbolInfo) -> bool:
        c = self.cfg
        spread = quote.spread
        if c.max_spread_points > 0 and spread > c.max_spread_points * info.point:
            return False
        if c.max_spread_atr_ratio > 0 and spread > c.max_spread_atr_ratio * atr_value:
            return False
        return True

    def _min_stop_distance(self, info: SymbolInfo, quote: Quote) -> float:
        """Broker stops level plus the spread, so SL/TP are always accepted."""
        return max(info.stops_level_points * info.point, 0.0) + quote.spread

    # ------------------------------------------------------------------ entries
    def evaluate(self, candles: Sequence[Candle], quote: Quote, info: SymbolInfo) -> Optional[Signal]:
        """Return a Signal if the latest *closed* candle triggers an entry."""
        c = self.cfg
        self.last_skip_reason = ""
        if len(candles) < self.warmup_bars():
            self.last_skip_reason = "warming_up"
            return None
        if info.point <= 0:
            self.last_skip_reason = "bad_symbol_info"
            return None
        if not self.in_session(quote.time):
            self.last_skip_reason = "outside_session"
            return None
        fast, slow, trend, rsi_v, atr_v = self._indicators(candles)
        for series in (fast, slow, trend, rsi_v, atr_v):
            if series[-1] is None or series[-2] is None:
                self.last_skip_reason = "warming_up"
                return None
        a = atr_v[-1]
        if a is None or a <= 0:
            self.last_skip_reason = "zero_atr"
            return None
        if c.min_atr_points > 0 and a < c.min_atr_points * info.point:
            self.last_skip_reason = "atr_too_low"
            return None
        if c.max_atr_points > 0 and a > c.max_atr_points * info.point:
            self.last_skip_reason = "atr_too_high"
            return None
        if not self._spread_ok(quote, a, info):
            self.last_skip_reason = "spread_too_wide"
            return None
        bias = self.htf_bias(candles)
        if bias is None:
            self.last_skip_reason = "htf_warming_up"
            return None

        last, prev = candles[-1], candles[-2]
        long_setup = short_setup = False
        reason = ""
        if c.entry_mode == "cross":
            cross_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
            cross_dn = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
            long_setup = cross_up and last.close > trend[-1]
            short_setup = cross_dn and last.close < trend[-1]
            reason = "ema_cross"
        else:  # momentum / pullback
            up_trend = fast[-1] > slow[-1] > trend[-1]
            dn_trend = fast[-1] < slow[-1] < trend[-1]
            long_setup = up_trend and prev.low <= fast[-2] and last.close > fast[-1] and last.close > last.open
            short_setup = dn_trend and prev.high >= fast[-2] and last.close < fast[-1] and last.close < last.open
            reason = "pullback"

        if bias == 1:
            short_setup = False
        elif bias == -1:
            long_setup = False

        r = rsi_v[-1]
        min_dist = self._min_stop_distance(info, quote)
        sl_dist = max(c.sl_atr_mult * a, min_dist)
        tp_dist = max(c.tp_atr_mult * a, min_dist)

        if long_setup and c.rsi_buy_min <= r <= c.rsi_buy_max:
            entry = quote.ask
            return Signal(Side.BUY, entry, info.round_price(entry - sl_dist),
                          info.round_price(entry + tp_dist), a, f"{reason}_up rsi={r:.1f}")
        if short_setup and c.rsi_sell_min <= r <= c.rsi_sell_max:
            entry = quote.bid
            return Signal(Side.SELL, entry, info.round_price(entry + sl_dist),
                          info.round_price(entry - tp_dist), a, f"{reason}_down rsi={r:.1f}")
        return None

    # ------------------------------------------------------------------ management
    def manage(self, pos: Position, candles: Sequence[Candle], quote: Quote,
               new_bar: bool, info: SymbolInfo) -> ManageAction:
        """Decide whether to close or re-protect an open position."""
        c = self.cfg
        act = ManageAction()
        is_buy = pos.side is Side.BUY
        price = quote.bid if is_buy else quote.ask
        a = self.current_atr(candles)

        # --- time-based exit and opposite-cross exit, only on a fresh closed bar
        if new_bar and candles:
            if c.max_hold_bars > 0:
                open_bar = int(pos.open_time // c.timeframe_sec) * c.timeframe_sec
                bars_held = (candles[-1].time - open_bar) // c.timeframe_sec + 1
                if bars_held >= c.max_hold_bars:
                    return ManageAction(close=True, reason="max_hold")
            if c.exit_on_opposite_cross and len(candles) >= self.cfg.ema_slow + 2:
                closes = [k.close for k in candles]
                fast = ema(closes, c.ema_fast)
                slow = ema(closes, c.ema_slow)
                if fast[-1] is not None and slow[-1] is not None and fast[-2] is not None and slow[-2] is not None:
                    if is_buy and fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
                        return ManageAction(close=True, reason="opposite_cross")
                    if not is_buy and fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
                        return ManageAction(close=True, reason="opposite_cross")

        if a is None or a <= 0:
            return act

        move = (price - pos.entry_price) if is_buy else (pos.entry_price - price)
        new_sl: Optional[float] = None

        # --- breakeven
        if c.breakeven_atr_mult > 0 and move >= c.breakeven_atr_mult * a:
            buffer = c.breakeven_buffer_points * info.point + quote.spread
            be = pos.entry_price + buffer if is_buy else pos.entry_price - buffer
            if (is_buy and pos.stop_loss < be) or (not is_buy and (pos.stop_loss > be or pos.stop_loss == 0)):
                new_sl = be

        # --- ATR trailing stop
        if c.trail_atr_mult > 0 and move >= c.trail_activate_atr_mult * a:
            trail = price - c.trail_atr_mult * a if is_buy else price + c.trail_atr_mult * a
            if is_buy and trail > max(pos.stop_loss, new_sl or 0.0):
                new_sl = trail
            elif not is_buy and (pos.stop_loss == 0 or trail < min(pos.stop_loss, new_sl or float("inf"))):
                new_sl = trail

        if new_sl is not None:
            new_sl = info.round_price(new_sl)
            min_dist = self._min_stop_distance(info, quote)
            # never place the stop inside the broker's minimum distance from price
            if is_buy and new_sl > price - min_dist:
                new_sl = info.round_price(price - min_dist)
            if not is_buy and new_sl < price + min_dist:
                new_sl = info.round_price(price + min_dist)
            if abs(new_sl - pos.stop_loss) >= info.point:
                act.new_sl = new_sl
        return act
