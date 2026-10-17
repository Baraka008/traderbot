"""Claude-assisted performance analysis.

Claude never trades. This module compiles what the bot has recorded (trade journal, indicator
snapshots, stored candles, a fresh backtest) into a compact statistics pack, asks Claude to review
it, and returns a written report plus optional parameter suggestions that you can backtest with
``scalper backtest`` before using them.
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .backtest import BacktestConfig, Backtester
from .history import HistoryStore
from .models import Candle, SymbolInfo
from .risk import RiskConfig, RiskManager
from .strategy import ScalpStrategy, StrategyConfig

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are a quantitative trading analyst reviewing a rule-based scalping bot.
You receive a statistics pack: the bot's current strategy/risk configuration, its trade journal,
per-bar indicator snapshots, stored price history and a backtest on that history.

Write a review for the bot's operator. Ground every claim in the numbers you were given; when the
data is too thin to support a conclusion, say so instead of guessing. Cover, in this order:
1. Verdict: is the current configuration worth running on this symbol right now, and why.
2. Where the losses come from (exit reason, side, hour of day, hold time, spread/volatility regime).
3. What is working.
4. Concrete parameter changes, each with the evidence for it and the expected effect.
5. Risks and data-quality caveats (gaps, small samples, regime changes, overfitting).

Finish with a fenced ```json block containing exactly:
{"verdict": "trade" | "paper_only" | "do_not_trade",
 "confidence": 0.0-1.0,
 "suggested_strategy_changes": {<parameter>: <value>, ...},
 "suggested_risk_changes": {<parameter>: <value>, ...},
 "summary": "<one sentence>"}
Only use parameter names that appear in the configuration section. Suggest nothing you cannot justify
from the data. The operator will backtest every suggestion before using it."""


# ---------------------------------------------------------------------- statistics pack
def _ts(v: float) -> str:
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(v)


def load_journal(path: str, limit: int = 500) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({
                    "id": row.get("id", ""), "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                    "volume": float(row.get("volume") or 0), "entry_price": float(row.get("entry_price") or 0),
                    "exit_price": float(row.get("exit_price") or 0), "open_time": float(row.get("open_time") or 0),
                    "close_time": float(row.get("close_time") or 0), "pnl": float(row.get("pnl") or 0),
                    "reason": row.get("reason", ""),
                })
            except ValueError:
                continue
    return rows[-limit:]


def journal_stats(trades: Sequence[Dict[str, Any]], timeframe_sec: int) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    def bucket(key_fn):
        agg: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
        for t in trades:
            k = key_fn(t)
            agg[k]["trades"] += 1
            agg[k]["pnl"] += t["pnl"]
            agg[k]["wins"] += 1 if t["pnl"] > 0 else 0
        out = {}
        for k, v in sorted(agg.items(), key=lambda kv: str(kv[0])):
            out[str(k)] = {"trades": v["trades"], "pnl": round(v["pnl"], 2),
                           "win_rate": round(v["wins"] / v["trades"], 3)}
        return out

    streak = max_loss_streak = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        max_loss_streak = max(max_loss_streak, streak)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    holds = [(t["close_time"] - t["open_time"]) / timeframe_sec for t in trades if t["close_time"] > t["open_time"]]
    return {
        "trades": len(trades),
        "period": f"{_ts(trades[0]['open_time'])} .. {_ts(trades[-1]['close_time'])} UTC",
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(trades), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss": round(statistics.mean(losses), 2) if losses else 0.0,
        "largest_loss": round(min(pnls), 2),
        "largest_win": round(max(pnls), 2),
        "max_consecutive_losses": max_loss_streak,
        "max_drawdown_currency": round(max_dd, 2),
        "avg_bars_held": round(statistics.mean(holds), 1) if holds else None,
        "by_exit_reason": bucket(lambda t: t["reason"] or "unknown"),
        "by_side": bucket(lambda t: t["side"]),
        "by_hour_utc": bucket(lambda t: datetime.fromtimestamp(t["open_time"], tz=timezone.utc).hour),
        "by_weekday": bucket(lambda t: datetime.fromtimestamp(t["open_time"], tz=timezone.utc).strftime("%a")),
        "last_20": [{"t": _ts(t["open_time"]), "side": t["side"], "pnl": round(t["pnl"], 2), "reason": t["reason"]}
                    for t in trades[-20:]],
    }


def snapshot_stats(path: str, limit: int = 2000) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"rows": 0}
    rows: List[Dict[str, float]] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({k: float(v) for k, v in row.items() if v not in ("", None)})
            except ValueError:
                continue
    rows = rows[-limit:]
    if not rows:
        return {"rows": 0}

    def col(name):
        return [r[name] for r in rows if name in r]

    def summary(vals):
        if not vals:
            return None
        q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [min(vals), statistics.median(vals), max(vals)]
        return {"min": round(min(vals), 6), "p25": round(q[0], 6), "median": round(q[1], 6), "p75": round(q[2], 6),
                "max": round(max(vals), 6), "mean": round(statistics.mean(vals), 6)}

    atrs, spreads = col("atr"), col("spread")
    ratio = [s / a for s, a in zip(spreads, atrs) if a > 0] if atrs and spreads else []
    trend_up = sum(1 for r in rows if "ema_fast" in r and "ema_slow" in r and r["ema_fast"] > r["ema_slow"])
    return {
        "rows": len(rows),
        "period": f"{_ts(rows[0].get('time', 0))} .. {_ts(rows[-1].get('time', 0))} UTC",
        "atr": summary(atrs), "spread": summary(spreads), "spread_to_atr": summary(ratio), "rsi": summary(col("rsi")),
        "share_of_bars_fast_above_slow": round(trend_up / len(rows), 3),
    }


def history_backtest(candles: Sequence[Candle], strategy_cfg: StrategyConfig, risk_cfg: RiskConfig,
                     info: SymbolInfo, bt_cfg: BacktestConfig) -> Dict[str, Any]:
    if len(candles) < 300:
        return {"bars": len(candles), "note": "not enough stored candles for a backtest"}
    risk = RiskManager(RiskConfig(**{**asdict(risk_cfg), "cooldown_after_loss_sec": 0, "max_consecutive_losses": 0,
                                     "max_trades_per_day": 0, "max_daily_loss_pct": 0, "max_daily_profit_pct": 0}))
    res = Backtester(ScalpStrategy(strategy_cfg), risk, info, bt_cfg).run(list(candles))
    m = {k: (round(v, 4) if isinstance(v, float) and v != float("inf") else v) for k, v in res.metrics.items()}
    m["period"] = f"{_ts(candles[0].time)} .. {_ts(candles[-1].time)} UTC"
    m["exit_reasons"] = dict(Counter(t.reason for t in res.trades))
    if not res.trades:
        m["note"] = ("no trades were taken: either no signals passed the filters or position sizing returned 0 "
                     "(check backtest.instrument / tick_value / point against the risk settings)")
    return m


def build_statistics_pack(symbol: str, timeframe_sec: int, strategy_cfg: StrategyConfig, risk_cfg: RiskConfig,
                          bt_cfg: BacktestConfig, journal_csv: str, history_dir: str,
                          info: Optional[SymbolInfo] = None) -> Dict[str, Any]:
    pack: Dict[str, Any] = {
        "symbol": symbol, "timeframe_sec": timeframe_sec, "generated_at": _ts(datetime.now(tz=timezone.utc).timestamp()),
        "strategy_config": asdict(strategy_cfg), "risk_config": asdict(risk_cfg),
        "journal": journal_stats(load_journal(journal_csv), timeframe_sec),
    }
    if history_dir:
        store = HistoryStore(history_dir)
        candles = store.load(symbol, timeframe_sec)
        st = store.stats(symbol, timeframe_sec)
        pack["history"] = {"bars": st.get("bars", 0), "first": _ts(st["first"]) if st.get("first") else None,
                           "last": _ts(st["last"]) if st.get("last") else None, "issues": st.get("issues", [])}
        pack["indicator_snapshots"] = snapshot_stats(store.snapshot_path(symbol, timeframe_sec))
        if candles:
            if info is None:
                from .cli import _infer_point
                point = _infer_point(candles)
                digits = max(0, -int(f"{point:e}".split("e")[1])) if point < 1 else 0
                info = SymbolInfo(symbol, point=point, digits=digits, min_volume=0.01 if bt_cfg.instrument == "cfd" else 1.0,
                                  max_volume=100.0 if bt_cfg.instrument == "cfd" else 1000.0, volume_step=0.01)
            pack["backtest_on_history"] = history_backtest(candles, strategy_cfg, risk_cfg, info, bt_cfg)
    return pack


def pack_to_text(pack: Dict[str, Any]) -> str:
    return json.dumps(pack, indent=1, default=str)


# ---------------------------------------------------------------------- Claude call
def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Return the last fenced JSON object in a markdown report, or None."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def analyze_with_claude(pack: Dict[str, Any], model: str = DEFAULT_MODEL, effort: str = "high",
                        client: Any = None, max_tokens: int = 16000,
                        on_text=None) -> Tuple[str, Optional[Dict[str, Any]], Dict[str, Any]]:
    """Send the statistics pack to Claude. Returns (report markdown, parsed suggestions, usage)."""
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("the 'anthropic' package is not installed: pip install -e '.[ai]'") from e
        client = anthropic.Anthropic()

    user_text = ("Statistics pack for the scalping bot (JSON). Review it as instructed.\n\n```json\n"
                 + pack_to_text(pack) + "\n```")
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        if on_text is not None:
            for chunk in stream.text_stream:
                on_text(chunk)
        response = stream.get_final_message()

    usage: Dict[str, Any] = {}
    u = getattr(response, "usage", None)
    if u is not None:
        usage = {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None)}
    usage["model"] = getattr(response, "model", model)
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        reason = getattr(details, "explanation", None) or getattr(details, "category", None) or "refusal"
        raise RuntimeError(f"Claude declined to produce the analysis: {reason}")
    report = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    if getattr(response, "stop_reason", None) == "max_tokens":
        report += "\n\n_(report truncated: raise --max-tokens)_"
    return report, extract_json_block(report), usage
