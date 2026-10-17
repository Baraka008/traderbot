"""Parameter optimisation with an in-sample / out-of-sample split.

The grid is evaluated on the first ``split`` fraction of the data (in-sample). The best candidates
are then re-run on the remaining bars (out-of-sample) which they have never seen. A parameter set is
only worth trading if it still makes money out of sample; the ``robust`` flag marks those.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

from .backtest import BacktestConfig, Backtester
from .models import Candle, SymbolInfo
from .risk import RiskConfig, RiskManager
from .strategy import ScalpStrategy, StrategyConfig

DEFAULT_GRID: Dict[str, List[Any]] = {
    "entry_mode": ["cross", "momentum"],
    "ema_fast": [5, 9],
    "ema_slow": [21, 34],
    "sl_atr_mult": [0.8, 1.0, 1.3],
    "tp_atr_mult": [1.0, 1.5, 2.0],
    "max_hold_bars": [10, 20],
}


@dataclass
class OptimizeResult:
    params: Dict[str, Any]
    insample: Dict[str, float]
    outsample: Dict[str, float]
    score: float
    robust: bool

    def row(self) -> str:
        i, o = self.insample, self.outsample
        p = " ".join(f"{k}={v}" for k, v in self.params.items())
        return (f"score={self.score:7.2f} | IS trades={int(i['trades']):4d} pf={i['profit_factor']:5.2f} "
                f"ret={i['return_pct']:+6.2f}% dd={i['max_drawdown_pct']:5.2f}% | OOS trades={int(o['trades']):4d} "
                f"pf={o['profit_factor']:5.2f} ret={o['return_pct']:+6.2f}% dd={o['max_drawdown_pct']:5.2f}% "
                f"| {'ROBUST' if self.robust else '      '} | {p}")


def score_metrics(m: Dict[str, float], min_trades: int) -> float:
    """Return-per-drawdown score, scaled down when there are too few trades to trust it."""
    if m["trades"] <= 0:
        return -1e9
    pf = min(m["profit_factor"], 5.0) if math.isfinite(m["profit_factor"]) else 5.0
    confidence = min(1.0, m["trades"] / max(1, min_trades))
    return (m["return_pct"] / (1.0 + m["max_drawdown_pct"])) * confidence * (1.0 if pf > 1 else 0.5)


def _run_one(args) -> Dict[str, float]:
    params, base_cfg, risk_cfg, info, bt_cfg, candles = args
    scfg = replace(base_cfg, **params)
    try:
        strat = ScalpStrategy(scfg)
    except ValueError:
        return {"trades": 0, "profit_factor": 0.0, "return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0,
                "expectancy": 0.0, "win_rate": 0.0, "invalid": 1.0}
    risk = RiskManager(dataclasses.replace(risk_cfg))
    return Backtester(strat, risk, info, bt_cfg).run(candles).metrics


def expand_grid(grid: Dict[str, Sequence[Any]], max_combos: int = 0, seed: int = 1) -> List[Dict[str, Any]]:
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    combos = [c for c in combos if not ("ema_fast" in c and "ema_slow" in c and c["ema_fast"] >= c["ema_slow"])]
    if max_combos and len(combos) > max_combos:
        random.Random(seed).shuffle(combos)
        combos = combos[:max_combos]
    return combos


def optimize(candles: Sequence[Candle], base_cfg: StrategyConfig, risk_cfg: RiskConfig, info: SymbolInfo,
             bt_cfg: BacktestConfig, grid: Optional[Dict[str, Sequence[Any]]] = None, split: float = 0.7,
             min_trades: int = 30, top: int = 10, max_combos: int = 0, workers: int = 0,
             progress=None) -> List[OptimizeResult]:
    grid = grid or DEFAULT_GRID
    combos = expand_grid(grid, max_combos)
    cut = int(len(candles) * split)
    insample, outsample = list(candles[:cut]), list(candles[cut:])
    if len(outsample) < 200:
        raise ValueError("not enough data for an out-of-sample segment; need more candles or a smaller split")

    # Disable daily/cooldown gates while optimising so every parameter set gets the same opportunities.
    risk_cfg = dataclasses.replace(risk_cfg, cooldown_after_loss_sec=0, max_consecutive_losses=0, max_trades_per_day=0,
                                   max_daily_loss_pct=0, max_daily_profit_pct=0)
    jobs = [(c, base_cfg, risk_cfg, info, bt_cfg, insample) for c in combos]
    is_metrics: List[Dict[str, float]] = []
    if workers and workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for n, m in enumerate(ex.map(_run_one, jobs, chunksize=2), 1):
                is_metrics.append(m)
                if progress:
                    progress(n, len(jobs))
    else:
        for n, j in enumerate(jobs, 1):
            is_metrics.append(_run_one(j))
            if progress:
                progress(n, len(jobs))

    ranked = sorted(zip(combos, is_metrics), key=lambda cm: score_metrics(cm[1], min_trades), reverse=True)
    results: List[OptimizeResult] = []
    for params, im in ranked[:top]:
        if im.get("invalid"):
            continue
        om = _run_one((params, base_cfg, risk_cfg, info, bt_cfg, outsample))
        robust = (im["trades"] >= min_trades and om["trades"] >= max(5, min_trades // 4)
                  and om["profit_factor"] > 1.0 and im["profit_factor"] > 1.0 and om["return_pct"] > 0)
        results.append(OptimizeResult(params=params, insample=im, outsample=om,
                                      score=score_metrics(im, min_trades), robust=robust))
    return results
