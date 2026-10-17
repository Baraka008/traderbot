"""YAML configuration loading with ${ENV_VAR} substitution and dataclass binding."""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional, Type, TypeVar

import yaml

from .backtest import BacktestConfig
from .engine import EngineConfig
from .risk import RiskConfig
from .strategy import StrategyConfig

T = TypeVar("T")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m):
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            return ""
        return _ENV.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    return value


def bind(cls: Type[T], data: Dict[str, Any] | None, **overrides) -> T:
    """Build dataclass ``cls`` from a dict, ignoring unknown keys and coercing basic types."""
    data = dict(data or {})
    data.update({k: v for k, v in overrides.items() if v is not None})
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if v is None:
            continue
        if f.type in ("int", int) or f.type == "int":
            v = int(float(v))
        elif f.type in ("float", float) or f.type == "float":
            v = float(v)
        elif f.type in ("bool", bool) or f.type == "bool":
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif f.type in ("str", str) or f.type == "str":
            v = str(v)
        kwargs[f.name] = v
    return cls(**kwargs)


@dataclass
class SymbolConfig:
    """One chart to trade: the symbol plus optional per-symbol overrides."""
    symbol: str
    strategy: StrategyConfig
    engine: EngineConfig
    backtest: BacktestConfig
    multiplier: Optional[int] = None     # Deriv multiplier override
    timeframe_sec: int = 60


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = _substitute(raw)
        r = self.raw
        self.broker = str(r.get("broker", "paper")).lower()
        self.timeframe_sec = int(r.get("timeframe_sec", 60))
        self.log_level = str(r.get("log_level", "INFO"))
        self.risk = bind(RiskConfig, r.get("risk"))
        self.deriv: Dict[str, Any] = dict(r.get("deriv") or {})
        self.mt5: Dict[str, Any] = dict(r.get("mt5") or {})
        self.paper: Dict[str, Any] = dict(r.get("paper") or {})

        # `symbols:` (list of names or mappings with overrides) wins over the single `symbol:`.
        entries = r.get("symbols")
        if not entries:
            entries = [str(r.get("symbol", "R_75"))]
        if isinstance(entries, (str, int)):
            entries = [str(x).strip() for x in str(entries).split(",") if str(x).strip()]
        self.symbols: List[SymbolConfig] = []
        seen = set()
        for entry in entries:
            if isinstance(entry, dict):
                name = str(entry.get("symbol") or entry.get("name") or "").strip()
                if not name:
                    raise ValueError("each `symbols` mapping needs a `symbol` key")
                overrides = entry
            else:
                name = str(entry).strip()
                overrides = {}
            if not name or name in seen:
                continue
            seen.add(name)
            tf = int(overrides.get("timeframe_sec", self.timeframe_sec))
            strategy = bind(StrategyConfig, {**(r.get("strategy") or {}), **(overrides.get("strategy") or {})},
                            timeframe_sec=tf)
            engine = bind(EngineConfig, {**r, **(r.get("engine") or {}), **(overrides.get("engine") or {})},
                          symbol=name, timeframe_sec=tf)
            backtest = bind(BacktestConfig, {**(r.get("backtest") or {}), **(overrides.get("backtest") or {})})
            mult = overrides.get("multiplier")
            self.symbols.append(SymbolConfig(symbol=name, strategy=strategy, engine=engine, backtest=backtest,
                                             multiplier=int(mult) if mult is not None else None, timeframe_sec=tf))
        if not self.symbols:
            raise ValueError("no symbols configured")
        if len(self.symbols) > 1 and self.risk.max_open_positions < 2:
            # a portfolio cap of 1 would make the extra charts pointless; log-free sane default
            self.risk.max_open_positions = len(self.symbols)

        # Backwards-compatible single-symbol views (first entry).
        first = self.symbols[0]
        self.symbol = first.symbol
        self.engine = first.engine
        self.strategy = first.strategy
        self.backtest = first.backtest

    def select(self, symbol: Optional[str]) -> List[SymbolConfig]:
        if not symbol:
            return list(self.symbols)
        hits = [s for s in self.symbols if s.symbol == symbol]
        if not hits:
            raise ValueError(f"symbol '{symbol}' is not in the config (have {[s.symbol for s in self.symbols]})")
        return hits

    @property
    def deriv_multipliers(self) -> Dict[str, int]:
        return {s.symbol: s.multiplier for s in self.symbols if s.multiplier is not None}

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("config root must be a mapping")
        return cls(raw)
