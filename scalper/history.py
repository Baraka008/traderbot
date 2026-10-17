"""Persistent chart history: candles and per-bar indicator snapshots stored as CSV files.

One file per symbol/timeframe (``data/R_75_60.csv``). Appends are de-duplicated by bar time, so the
live engine, the ``fetch`` command and backtests all share the same growing history.
"""
from __future__ import annotations

import csv
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Candle

_FIELDS = ["time", "open", "high", "low", "close", "volume"]


def validate_candles(candles: Sequence[Candle], timeframe_sec: int) -> Tuple[List[Candle], List[str]]:
    """Sort, de-duplicate and sanity-check candles. Returns (clean candles, list of issues found)."""
    issues: List[str] = []
    seen: Dict[int, Candle] = {}
    dropped = 0
    for c in candles:
        if c.time <= 0 or c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
            dropped += 1
            continue
        if c.high < c.low or c.high < max(c.open, c.close) - 1e-12 or c.low > min(c.open, c.close) + 1e-12:
            dropped += 1
            continue
        if timeframe_sec > 0 and c.time % timeframe_sec != 0:
            c = Candle(time=int(c.time // timeframe_sec * timeframe_sec), open=c.open, high=c.high, low=c.low,
                       close=c.close, volume=c.volume)
        seen[c.time] = c          # later duplicates win (fresher data)
    if dropped:
        issues.append(f"dropped {dropped} malformed candle(s)")
    dupes = len(candles) - dropped - len(seen)
    if dupes > 0:
        issues.append(f"removed {dupes} duplicate bar time(s)")
    clean = [seen[t] for t in sorted(seen)]
    if timeframe_sec > 0 and len(clean) > 1:
        gaps = 0
        biggest = 0
        for a, b in zip(clean, clean[1:]):
            d = b.time - a.time
            if d > timeframe_sec:
                gaps += 1
                biggest = max(biggest, d)
        if gaps:
            issues.append(f"{gaps} gap(s) in history, largest {biggest // 60} min (weekend/market close or missing data)")
    return clean, issues


class HistoryStore:
    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    @staticmethod
    def _safe(symbol: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol).strip("_") or "symbol"

    def path(self, symbol: str, timeframe_sec: int) -> str:
        return os.path.join(self.directory, f"{self._safe(symbol)}_{int(timeframe_sec)}.csv")

    def snapshot_path(self, symbol: str, timeframe_sec: int) -> str:
        return os.path.join(self.directory, f"{self._safe(symbol)}_{int(timeframe_sec)}_indicators.csv")

    # ------------------------------------------------------------------ candles
    def load(self, symbol: str, timeframe_sec: int) -> List[Candle]:
        p = self.path(symbol, timeframe_sec)
        if not os.path.exists(p):
            return []
        out: List[Candle] = []
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out.append(Candle(time=int(float(row["time"])), open=float(row["open"]), high=float(row["high"]),
                                      low=float(row["low"]), close=float(row["close"]),
                                      volume=float(row.get("volume") or 0.0)))
                except (KeyError, ValueError):
                    continue
        clean, _ = validate_candles(out, timeframe_sec)
        return clean

    def last_time(self, symbol: str, timeframe_sec: int) -> Optional[int]:
        p = self.path(symbol, timeframe_sec)
        if not os.path.exists(p):
            return None
        last = None
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                last = row
        if not last:
            return None
        try:
            return int(float(last["time"]))
        except (KeyError, ValueError):
            return None

    def append(self, symbol: str, timeframe_sec: int, candles: Iterable[Candle]) -> int:
        """Merge candles into the store. Returns the number of new bars written."""
        new, _ = validate_candles(list(candles), timeframe_sec)
        if not new:
            return 0
        p = self.path(symbol, timeframe_sec)
        last = self.last_time(symbol, timeframe_sec)
        if last is None or new[0].time > last:
            # pure append (fast path)
            rows = new if last is None else [c for c in new if c.time > last]
            write_header = last is None and not os.path.exists(p)
            with open(p, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(_FIELDS)
                for c in rows:
                    w.writerow([c.time, c.open, c.high, c.low, c.close, c.volume])
            return len(rows)
        existing = self.load(symbol, timeframe_sec)
        before = len(existing)
        merged, _ = validate_candles(existing + new, timeframe_sec)
        tmp = p + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(_FIELDS)
            for c in merged:
                w.writerow([c.time, c.open, c.high, c.low, c.close, c.volume])
        os.replace(tmp, p)
        return len(merged) - before

    def stats(self, symbol: str, timeframe_sec: int) -> Dict[str, object]:
        candles = self.load(symbol, timeframe_sec)
        if not candles:
            return {"bars": 0}
        _, issues = validate_candles(candles, timeframe_sec)
        return {"bars": len(candles), "first": candles[0].time, "last": candles[-1].time,
                "path": self.path(symbol, timeframe_sec), "issues": issues}

    def list(self) -> List[Tuple[str, int, int]]:
        out = []
        for name in sorted(os.listdir(self.directory)):
            m = re.match(r"^(.+)_(\d+)\.csv$", name)
            if not m or name.endswith("_indicators.csv"):
                continue
            symbol, tf = m.group(1), int(m.group(2))
            out.append((symbol, tf, len(self.load(symbol, tf))))
        return out

    # ------------------------------------------------------------------ indicator snapshots
    def snapshot(self, symbol: str, timeframe_sec: int, row: Dict[str, object]) -> None:
        p = self.snapshot_path(symbol, timeframe_sec)
        exists = os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if not exists:
                w.writeheader()
            w.writerow(row)
