"""YAML configuration loading with ${ENV_VAR} substitution and dataclass binding."""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import fields
from typing import Any, Dict, Type, TypeVar

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


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = _substitute(raw)
        r = self.raw
        self.broker = str(r.get("broker", "paper")).lower()
        self.symbol = str(r.get("symbol", "R_75"))
        self.timeframe_sec = int(r.get("timeframe_sec", 60))
        self.log_level = str(r.get("log_level", "INFO"))
        self.engine = bind(EngineConfig, {**r, **(r.get("engine") or {})}, symbol=self.symbol,
                           timeframe_sec=self.timeframe_sec)
        self.strategy = bind(StrategyConfig, r.get("strategy"), timeframe_sec=self.timeframe_sec)
        self.risk = bind(RiskConfig, r.get("risk"))
        self.backtest = bind(BacktestConfig, r.get("backtest"))
        self.deriv: Dict[str, Any] = dict(r.get("deriv") or {})
        self.mt5: Dict[str, Any] = dict(r.get("mt5") or {})
        self.paper: Dict[str, Any] = dict(r.get("paper") or {})

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("config root must be a mapping")
        return cls(raw)
