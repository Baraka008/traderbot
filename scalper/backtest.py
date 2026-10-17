"""Bar-based backtester sharing the live strategy and risk code.

Fill model (conservative):
  * entries fill at the closing price of the signal bar plus/minus half the spread;
  * stop-loss / take-profit are checked against the next bars' high/low, and if both are
    touched inside the same bar the stop-loss is assumed to have been hit first;
  * management exits (max hold, opposite cross) fill at bar close.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .models import Candle, ClosedTrade, Position, Quote, Side, SymbolInfo
from .risk import RiskManager
from .strategy import ScalpStrategy


@dataclass
class BacktestConfig:
    instrument: str = "cfd"       # "cfd" (lots/tick_value) or "multiplier" (stake/multiplier, Deriv)
    balance: float = 10_000.0
    spread_points: float = 10.0
    slippage_points: float = 0.0
    commission_per_unit: float = 0.0   # per lot (cfd) or per 1 unit of stake (multiplier), round trip
    tick_value: float = 1.0            # cfd: account currency per point per 1.0 lot
    multiplier: int = 100


@dataclass
class BacktestResult:
    trades: List[ClosedTrade]
    equity_curve: List[float]
    metrics: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"bars             : {int(m['bars'])}",
            f"trades           : {int(m['trades'])}  (wins {int(m['wins'])}, losses {int(m['losses'])})",
            f"win rate         : {m['win_rate']:.1%}",
            f"net pnl          : {m['net_pnl']:+.2f}  ({m['return_pct']:+.2f}%)",
            f"profit factor    : {m['profit_factor']:.2f}",
            f"avg win / loss   : {m['avg_win']:+.2f} / {m['avg_loss']:+.2f}",
            f"expectancy/trade : {m['expectancy']:+.2f}",
            f"max drawdown     : {m['max_drawdown_pct']:.2f}%",
            f"avg bars held    : {m['avg_bars_held']:.1f}",
        ]
        return "\n".join(lines)


def load_csv(path: str) -> List[Candle]:
    """Load candles from CSV with columns time,open,high,low,close[,volume].

    ``time`` may be epoch seconds or an ISO-8601 / "YYYY-MM-DD HH:MM[:SS]" string.
    """
    from datetime import datetime, timezone

    out: List[Candle] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            raise ValueError(f"CSV is missing one of columns {names}; has {list(cols)}")

        t_c, o_c, h_c, l_c, c_c = col("time", "timestamp", "date", "datetime"), col("open"), col("high"), col("low"), col("close")
        v_c = cols.get("volume") or cols.get("tick_volume")
        for row in reader:
            raw = row[t_c].strip()
            try:
                ts = int(float(raw))
                if ts > 10_000_000_000:   # milliseconds
                    ts //= 1000
            except ValueError:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
            out.append(Candle(time=ts, open=float(row[o_c]), high=float(row[h_c]), low=float(row[l_c]),
                              close=float(row[c_c]), volume=float(row[v_c]) if v_c and row.get(v_c) else 0.0))
    out.sort(key=lambda c: c.time)
    return out


def save_csv(path: str, candles: Sequence[Candle]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time, c.open, c.high, c.low, c.close, c.volume])


def generate_candles(n: int = 5000, start_price: float = 1000.0, timeframe_sec: int = 60, seed: int = 42,
                     vol: float = 0.0006, start_time: int = 1_700_000_000) -> List[Candle]:
    """Synthetic random-walk candles with volatility clustering and mild trends (smoke-test data)."""
    rng = random.Random(seed)
    start_time = int(start_time // timeframe_sec) * timeframe_sec   # bar times aligned to the timeframe
    price = start_price
    sigma = vol
    drift = 0.0
    out: List[Candle] = []
    for i in range(n):
        if i % 240 == 0:
            drift = rng.uniform(-0.00008, 0.00008)
        sigma = max(vol * 0.3, min(vol * 3, sigma * 0.97 + vol * 0.03 + rng.gauss(0, vol * 0.05)))
        o = price
        path = [o]
        for _ in range(12):
            path.append(path[-1] * (1 + drift / 12 + rng.gauss(0, sigma / math.sqrt(12))))
        c = path[-1]
        h = max(path)
        l = min(path)
        out.append(Candle(time=start_time + i * timeframe_sec, open=round(o, 5), high=round(h, 5), low=round(l, 5),
                          close=round(c, 5), volume=float(rng.randint(50, 500))))
        price = c
    return out


class Backtester:
    def __init__(self, strategy: ScalpStrategy, risk: RiskManager, info: SymbolInfo, cfg: BacktestConfig):
        self.strategy = strategy
        self.risk = risk
        self.info = info
        self.cfg = cfg
        self.tf = strategy.cfg.timeframe_sec

    # ------------------------------------------------------------------ pnl helpers
    def loss_per_unit(self, entry: float, stop: float) -> float:
        if self.cfg.instrument == "multiplier":
            return self.cfg.multiplier * abs(entry - stop) / entry if entry > 0 else 0.0
        return abs(entry - stop) / self.info.point * self.cfg.tick_value

    def pnl(self, pos: Position, exit_price: float) -> float:
        diff = (exit_price - pos.entry_price) if pos.side is Side.BUY else (pos.entry_price - exit_price)
        if self.cfg.instrument == "multiplier":
            gross = pos.volume * self.cfg.multiplier * diff / pos.entry_price
        else:
            gross = diff / self.info.point * self.cfg.tick_value * pos.volume
        return gross - self.cfg.commission_per_unit * pos.volume

    # ------------------------------------------------------------------ run
    def run(self, candles: Sequence[Candle]) -> BacktestResult:
        half_spread = self.cfg.spread_points * self.info.point / 2.0
        slip = self.cfg.slippage_points * self.info.point
        balance = self.cfg.balance
        equity = [balance]
        trades: List[ClosedTrade] = []
        pos: Optional[Position] = None
        next_id = 1
        clock = {"t": candles[0].time if candles else 0}
        self.risk.clock = lambda: clock["t"]
        self.risk.start_day(balance)
        warm = self.strategy.warmup_bars()
        lookback = self.strategy.lookback_bars()

        def close(p: Position, price: float, reason: str, when: int) -> None:
            nonlocal balance
            realized = self.pnl(p, price)
            balance += realized
            ct = ClosedTrade(id=p.id, symbol=p.symbol, side=p.side, volume=p.volume, entry_price=p.entry_price,
                             exit_price=price, open_time=p.open_time, close_time=when, pnl=realized, reason=reason)
            trades.append(ct)
            self.risk.record_trade(ct)
            equity.append(balance)

        for i in range(warm, len(candles)):
            bar = candles[i]
            clock["t"] = bar.time + self.tf      # "now" = close of bar i
            history = candles[max(0, i + 1 - lookback): i + 1]

            # 1) intrabar stop / target check on this bar for a position opened earlier
            if pos is not None and pos.open_time < bar.time:
                if pos.side is Side.BUY:
                    sl_hit = pos.stop_loss > 0 and bar.low - half_spread <= pos.stop_loss
                    tp_hit = pos.take_profit > 0 and bar.high - half_spread >= pos.take_profit
                else:
                    sl_hit = pos.stop_loss > 0 and bar.high + half_spread >= pos.stop_loss
                    tp_hit = pos.take_profit > 0 and bar.low + half_spread <= pos.take_profit
                if sl_hit:
                    close(pos, pos.stop_loss, "sl", bar.time + self.tf)
                    pos = None
                elif tp_hit:
                    close(pos, pos.take_profit, "tp", bar.time + self.tf)
                    pos = None

            quote = Quote(bid=bar.close - half_spread, ask=bar.close + half_spread, time=bar.time + self.tf)

            # 2) management at the close of bar i
            if pos is not None:
                act = self.strategy.manage(pos, history, quote, True, self.info)
                if act.close:
                    price = quote.bid if pos.side is Side.BUY else quote.ask
                    close(pos, price, act.reason, quote.time)
                    pos = None
                elif act.modifies:
                    if act.new_sl is not None:
                        pos.stop_loss = act.new_sl
                    if act.new_tp is not None:
                        pos.take_profit = act.new_tp

            # 3) entries at the close of bar i
            if pos is None:
                ok, _ = self.risk.can_trade(balance, 0)
                if ok:
                    sig = self.strategy.evaluate(history, quote, self.info)
                    if sig is not None:
                        vol = self.risk.volume_for(balance, self.loss_per_unit(sig.entry, sig.stop_loss), self.info)
                        if vol > 0:
                            fill = sig.entry + slip if sig.side is Side.BUY else sig.entry - slip
                            pos = Position(id=str(next_id), symbol=self.info.name, side=sig.side, volume=vol,
                                           entry_price=self.info.round_price(fill), stop_loss=sig.stop_loss,
                                           take_profit=sig.take_profit, open_time=bar.time)
                            next_id += 1
                            self.risk.on_open()

        if pos is not None:
            last = candles[-1]
            price = last.close - half_spread if pos.side is Side.BUY else last.close + half_spread
            close(pos, price, "end_of_data", last.time + self.tf)

        return BacktestResult(trades=trades, equity_curve=equity, metrics=self.metrics(trades, equity, len(candles)))

    # ------------------------------------------------------------------ metrics
    def metrics(self, trades: List[ClosedTrade], equity: List[float], bars: int) -> Dict[str, float]:
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        net = sum(t.pnl for t in trades)
        peak = equity[0] if equity else 0.0
        max_dd = 0.0
        for e in equity:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak * 100.0)
        bars_held = [(t.close_time - t.open_time) / self.tf for t in trades]
        return {
            "bars": float(bars),
            "trades": float(len(trades)),
            "wins": float(len(wins)),
            "losses": float(len(losses)),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "net_pnl": net,
            "return_pct": net / self.cfg.balance * 100.0 if self.cfg.balance else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "expectancy": net / len(trades) if trades else 0.0,
            "max_drawdown_pct": max_dd,
            "final_balance": equity[-1] if equity else self.cfg.balance,
            "avg_bars_held": sum(bars_held) / len(bars_held) if bars_held else 0.0,
        }
