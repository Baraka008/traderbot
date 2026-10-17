"""Pure-Python technical indicators (no numpy dependency).

Each function returns a list aligned with its input; entries are ``None`` until the
indicator has enough history to be defined.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .models import Candle


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1.0)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, n):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def true_range(candle: Candle, prev_close: Optional[float]) -> float:
    hl = candle.high - candle.low
    if prev_close is None:
        return hl
    return max(hl, abs(candle.high - prev_close), abs(candle.low - prev_close))


def atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """Wilder's Average True Range."""
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return out
    trs = [true_range(candles[0], None)]
    for i in range(1, n):
        trs.append(true_range(candles[i], candles[i - 1].close))
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out
