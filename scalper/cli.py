"""Command line interface: run (live/paper), backtest, fetch, symbols."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from . import __version__
from .backtest import Backtester, generate_candles, load_csv, save_csv
from .brokers.base import Broker, BrokerError
from .brokers.paper import PaperBroker
from .config import Config
from .engine import Engine, MultiEngine
from .history import HistoryStore, validate_candles
from .models import SymbolInfo
from .optimize import DEFAULT_GRID, optimize
from .risk import RiskManager
from .strategy import ScalpStrategy


def _setup_logging(level: str, logfile: Optional[str] = None) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        handlers.append(logging.FileHandler(logfile))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", handlers=handlers)


def make_data_broker(cfg: Config, kind: str) -> Broker:
    kind = kind.lower()
    if kind == "deriv":
        from .brokers.deriv import DerivBroker
        d = cfg.deriv
        return DerivBroker(token=str(d.get("token", "")), app_id=int(d.get("app_id", 1089)),
                           endpoint=str(d.get("endpoint", "wss://ws.derivws.com/websockets/v3")),
                           multiplier=int(d.get("multiplier", 100)), min_stake=float(d.get("min_stake", 1.0)),
                           max_stake=float(d.get("max_stake", 1000.0)), stake_step=float(d.get("stake_step", 0.01)),
                           symbol_multipliers=cfg.deriv_multipliers)
    if kind == "mt5":
        from .brokers.mt5 import MT5Broker
        m = cfg.mt5
        return MT5Broker(login=int(m.get("login") or 0), password=str(m.get("password", "")),
                         server=str(m.get("server", "")), path=str(m.get("path", "")),
                         magic=int(m.get("magic", 777123)), deviation_points=int(m.get("deviation_points", 20)),
                         comment=str(m.get("comment", "scalper")))
    raise BrokerError(f"unknown broker '{kind}' (expected deriv, mt5 or paper)")


def make_broker(cfg: Config) -> Broker:
    if cfg.broker == "paper":
        p = cfg.paper
        source = str(p.get("data_source", "deriv"))
        data = make_data_broker(cfg, source)
        instrument = str(p.get("instrument", "multiplier" if source == "deriv" else "cfd"))
        return PaperBroker(data, balance=float(p.get("balance", 10_000)), slippage_points=float(p.get("slippage_points", 0)),
                           commission_per_unit=float(p.get("commission_per_unit", 0)), instrument=instrument,
                           multiplier=int(cfg.deriv.get("multiplier", 100)), tick_value=float(p.get("tick_value", 1.0)),
                           min_volume=float(p.get("min_volume", 1.0 if instrument == "multiplier" else 0.01)),
                           max_volume=float(p.get("max_volume", 1000.0 if instrument == "multiplier" else 100.0)),
                           volume_step=float(p.get("volume_step", 0.01)))
    return make_data_broker(cfg, cfg.broker)


# ---------------------------------------------------------------------- commands
def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    if args.broker:
        cfg.broker = args.broker
    _setup_logging(cfg.log_level, args.log_file)
    log = logging.getLogger("scalper")
    broker = make_broker(cfg)
    risk = RiskManager(cfg.risk)                 # shared: portfolio-wide daily limits and position caps
    symbols = cfg.select(args.symbol)
    engines = [Engine(broker, ScalpStrategy(sc.strategy), risk, sc.engine) for sc in symbols]
    runner = engines[0] if len(engines) == 1 else MultiEngine(broker, engines, engines[0].cfg)
    mode = "PAPER" if cfg.broker == "paper" else "LIVE"
    log.info("scalper %s starting in %s mode on %s via %s (max %d open, %d per symbol)", __version__, mode,
             ", ".join(sc.symbol for sc in symbols), broker.name, cfg.risk.max_open_positions,
             cfg.risk.max_positions_per_symbol)
    if mode == "LIVE":
        log.warning("LIVE trading enabled: real orders will be sent. Risk per trade %.2f%%, daily loss cap %.2f%% "
                    "(portfolio-wide)", cfg.risk.risk_per_trade_pct, cfg.risk.max_daily_loss_pct)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    return 0


def _load_candles(cfg: Config, sc, args):
    """CSV file > history store > synthetic data. Returns (candles, source label)."""
    if getattr(args, "csv", None):
        return load_csv(args.csv), f"csv:{args.csv}"
    store = HistoryStore(sc.engine.history_dir) if sc.engine.history_dir else None
    if store is not None and not getattr(args, "synthetic_only", False):
        candles = store.load(sc.symbol, sc.timeframe_sec)
        if len(candles) >= 300:
            return candles, f"history:{store.path(sc.symbol, sc.timeframe_sec)}"
    n = getattr(args, "synthetic", 5000) or 5000
    print(f"NOTE: no CSV or stored history for {sc.symbol}, using SYNTHETIC random-walk data (mechanics only, "
          "no edge). Run `scalper fetch` first for real results.", file=sys.stderr)
    seed = getattr(args, "seed", 42) + sum(ord(c) for c in sc.symbol)
    return generate_candles(n=n, timeframe_sec=sc.timeframe_sec, seed=seed), "synthetic"


def _symbol_info_for_backtest(cfg: Config, sc, candles, point_arg):
    point = point_arg if point_arg else _infer_point(candles)
    digits = max(0, -int(f"{point:e}".split("e")[1])) if point < 1 else 0
    bt = sc.backtest
    if bt.instrument == "multiplier":
        info = SymbolInfo(name=sc.symbol, point=point, digits=digits, min_volume=float(cfg.deriv.get("min_stake", 1.0)),
                          max_volume=float(cfg.deriv.get("max_stake", 1000.0)), volume_step=0.01)
        bt.multiplier = int(sc.multiplier or cfg.deriv.get("multiplier", bt.multiplier))
    else:
        info = SymbolInfo(name=sc.symbol, point=point, digits=digits, min_volume=0.01, max_volume=100.0, volume_step=0.01)
    return info, point


def cmd_backtest(args) -> int:
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "WARNING")
    symbols = cfg.select(args.symbol)
    if args.csv and len(symbols) > 1:
        print("--csv applies to one symbol; add --symbol", file=sys.stderr)
        return 2
    totals = {"trades": 0, "net_pnl": 0.0, "wins": 0}
    journal = None
    if args.trades_out:
        from .engine import Journal
        journal = Journal(args.trades_out)
    for sc in symbols:
        candles, source = _load_candles(cfg, sc, args)
        candles, issues = validate_candles(candles, sc.timeframe_sec)
        for issue in issues:
            print(f"{sc.symbol} data: {issue}", file=sys.stderr)
        if len(candles) < 100:
            print(f"{sc.symbol}: not enough candles ({len(candles)})", file=sys.stderr)
            continue
        info, point = _symbol_info_for_backtest(cfg, sc, candles, args.point)
        result = Backtester(ScalpStrategy(sc.strategy), RiskManager(cfg.risk), info, sc.backtest).run(candles)
        print(f"symbol={sc.symbol} instrument={sc.backtest.instrument} point={point} spread={sc.backtest.spread_points}pts "
              f"candles={len(candles)} source={source}")
        print(result.summary())
        print()
        totals["trades"] += int(result.metrics["trades"])
        totals["net_pnl"] += result.metrics["net_pnl"]
        totals["wins"] += int(result.metrics["wins"])
        if journal:
            for t in result.trades:
                journal.write(t)
    if len(symbols) > 1:
        wr = totals["wins"] / totals["trades"] if totals["trades"] else 0.0
        print(f"PORTFOLIO ({len(symbols)} symbols, backtested independently): trades={totals['trades']} "
              f"win rate={wr:.1%} net pnl={totals['net_pnl']:+.2f}")
    if journal:
        print(f"trades written to {args.trades_out}")
    return 0


def _infer_point(candles) -> float:
    """Guess the price increment from the number of decimals seen in the data."""
    decimals = 0
    for c in candles[:500]:
        for v in (c.open, c.high, c.low, c.close):
            s = f"{v:.10f}".rstrip("0")
            if "." in s:
                decimals = max(decimals, len(s.split(".")[1]))
    return 10 ** (-decimals) if decimals else 1.0


def cmd_fetch(args) -> int:
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "INFO")
    source = args.broker or (cfg.broker if cfg.broker != "paper" else str(cfg.paper.get("data_source", "deriv")))
    broker = make_data_broker(cfg, source)
    store = HistoryStore(cfg.engine.history_dir or "data")

    symbols = cfg.select(args.symbol)
    if args.out and len(symbols) > 1:
        print("--out applies to one symbol; add --symbol (the history store is updated for all)", file=sys.stderr)
        return 2

    async def go():
        import time as _t
        await broker.connect()
        try:
            for sc in symbols:
                try:
                    candles = await broker.candles(sc.symbol, sc.timeframe_sec, args.count)
                except BrokerError as e:
                    print(f"{sc.symbol}: fetch failed: {e}", file=sys.stderr)
                    continue
                candles, issues = validate_candles(candles, sc.timeframe_sec)
                for issue in issues:
                    print(f"{sc.symbol} data: {issue}", file=sys.stderr)
                if candles and candles[-1].time + sc.timeframe_sec > _t.time():
                    candles = candles[:-1]           # the newest bar is still forming
                if args.out:
                    save_csv(args.out, candles)
                    print(f"saved {len(candles)} candles for {sc.symbol} ({sc.timeframe_sec}s) to {args.out}")
                added = store.append(sc.symbol, sc.timeframe_sec, candles)
                st = store.stats(sc.symbol, sc.timeframe_sec)
                print(f"{sc.symbol}: +{added} new bars, {st['bars']} total ({_fmt_ts(st['first'])} .. "
                      f"{_fmt_ts(st['last'])}) -> {st['path']}")
                for issue in st.get("issues", []):
                    print(f"    - {issue}")
        finally:
            await broker.close()

    asyncio.run(go())
    return 0


def _fmt_ts(ts) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts)


def cmd_history(args) -> int:
    cfg = Config.load(args.config)
    store = HistoryStore(cfg.engine.history_dir or "data")
    rows = store.list()
    if not rows:
        print(f"no history in {store.directory}; run `scalper fetch -c {args.config}` or the bot itself to build it")
        return 0
    for symbol, tf, n in rows:
        st = store.stats(symbol, tf)
        print(f"{symbol:<24} tf={tf:<5d} bars={n:<7d} {_fmt_ts(st['first'])} .. {_fmt_ts(st['last'])}  {st['path']}")
        for issue in st.get("issues", []):
            print(f"    - {issue}")
    return 0


def cmd_optimize(args) -> int:
    import json
    import os
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "WARNING")
    symbols = cfg.select(args.symbol)
    if len(symbols) > 1:
        print(f"optimising {len(symbols)} symbols one after another; use --symbol to pick one", file=sys.stderr)
    rc = 0
    for sc in symbols:
        rc = max(rc, _optimize_symbol(cfg, sc, args))
    return rc


def _optimize_symbol(cfg: Config, sc, args) -> int:
    import json
    import os
    candles, source = _load_candles(cfg, sc, args)
    candles, _ = validate_candles(candles, sc.timeframe_sec)
    if len(candles) < 1000:
        print(f"{sc.symbol}: need at least 1000 candles to optimise, have {len(candles)}", file=sys.stderr)
        return 2
    info, point = _symbol_info_for_backtest(cfg, sc, candles, args.point)
    grid = dict(DEFAULT_GRID)
    if args.grid:
        with open(args.grid) as f:
            grid = json.load(f)
    workers = args.workers if args.workers is not None else max(1, (os.cpu_count() or 2) - 1)

    def progress(n, total):
        if n == total or n % max(1, total // 20) == 0:
            print(f"\r  in-sample {n}/{total}", end="", file=sys.stderr, flush=True)

    print(f"optimising {sc.symbol} on {len(candles)} candles ({source}), split={args.split}, "
          f"grid={ {k: len(v) for k, v in grid.items()} }", file=sys.stderr)
    results = optimize(candles, sc.strategy, cfg.risk, info, sc.backtest, grid=grid, split=args.split,
                       min_trades=args.min_trades, top=args.top, max_combos=args.max_combos, workers=workers,
                       progress=progress)
    print("", file=sys.stderr)
    print(f"symbol={sc.symbol} instrument={sc.backtest.instrument} point={point} spread={sc.backtest.spread_points}pts")
    print("rank  (IS = in-sample, OOS = out-of-sample, score = IS return / (1 + drawdown) scaled by trade count)")
    for i, r in enumerate(results, 1):
        print(f"{i:>3}. {r.row()}")
    robust = [r for r in results if r.robust]
    if not robust:
        print("\nNo parameter set was profitable out-of-sample. Do not trade this symbol/timeframe with this strategy "
              "until a longer history or different filters produce a ROBUST row.")
    else:
        best = robust[0]
        print(f"\nbest robust parameters for {sc.symbol}: {best.params}")
        if args.write:
            import yaml
            merged = dict(cfg.raw)
            if len(cfg.symbols) > 1:
                # keep per-symbol parameters as overrides inside the `symbols` list
                entries = []
                for e in merged.get("symbols") or [s.symbol for s in cfg.symbols]:
                    e = dict(e) if isinstance(e, dict) else {"symbol": str(e)}
                    if e.get("symbol") == sc.symbol:
                        e["strategy"] = {**(e.get("strategy") or {}), **best.params}
                    entries.append(e)
                merged["symbols"] = entries
            else:
                merged["strategy"] = {**(merged.get("strategy") or {}), **best.params}
            out = args.write
            with open(out, "w") as f:
                yaml.safe_dump(merged, f, sort_keys=False)
            cfg.raw = merged          # later symbols accumulate into the same file
            print(f"written to {out}")
    print()
    return 0


def cmd_analyze(args) -> int:
    """Compile journal/history statistics and (optionally) have Claude review them."""
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "WARNING")
    symbols = cfg.select(args.symbol)
    if len(symbols) > 1:
        print(f"analysing {len(symbols)} symbols one after another; use --symbol to pick one", file=sys.stderr)
    rc = 0
    for sc in symbols:
        rc = max(rc, _analyze_symbol(cfg, sc, args))
    return rc


def _analyze_symbol(cfg: Config, sc, args) -> int:
    import yaml
    from .analysis import analyze_with_claude, build_statistics_pack, pack_to_text
    pack = build_statistics_pack(sc.symbol, sc.timeframe_sec, sc.strategy, cfg.risk, sc.backtest,
                                 sc.engine.journal_csv, sc.engine.history_dir or "data")
    if args.stats_only:
        print(pack_to_text(pack))
        return 0
    if pack["journal"].get("trades", 0) == 0 and not pack.get("backtest_on_history"):
        print(f"{sc.symbol}: nothing to analyse yet: no trades in the journal and no stored history. Run "
              "`scalper fetch` and/or paper trade first.", file=sys.stderr)
        return 2
    print(f"asking {args.model} to review {pack['journal'].get('trades', 0)} journal trades and "
          f"{pack.get('history', {}).get('bars', 0)} stored bars for {sc.symbol} ...\n", file=sys.stderr)
    try:
        report, suggestions, usage = analyze_with_claude(
            pack, model=args.model, effort=args.effort, max_tokens=args.max_tokens,
            on_text=(lambda t: print(t, end="", flush=True)) if not args.out else None)
    except RuntimeError as e:
        print(f"analysis failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # SDK errors (auth, rate limit, network)
        print(f"Claude API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.out:
        out = args.out if len(cfg.symbols) == 1 else args.out.replace(".md", "") + f".{sc.symbol}.md"
        with open(out, "w") as f:
            f.write(report)
        print(f"report written to {out}", file=sys.stderr)
    else:
        print()
    print(f"\n[usage] model={usage.get('model')} input_tokens={usage.get('input_tokens')} "
          f"output_tokens={usage.get('output_tokens')}", file=sys.stderr)
    if suggestions and args.write:
        merged = dict(cfg.raw)
        changes = suggestions.get("suggested_strategy_changes") or {}
        if len(cfg.symbols) > 1:
            entries = []
            for e in merged.get("symbols") or [s.symbol for s in cfg.symbols]:
                e = dict(e) if isinstance(e, dict) else {"symbol": str(e)}
                if e.get("symbol") == sc.symbol:
                    e["strategy"] = {**(e.get("strategy") or {}), **changes}
                entries.append(e)
            merged["symbols"] = entries
        else:
            merged["strategy"] = {**(merged.get("strategy") or {}), **changes}
        merged["risk"] = {**(merged.get("risk") or {}), **(suggestions.get("suggested_risk_changes") or {})}
        with open(args.write, "w") as f:
            yaml.safe_dump(merged, f, sort_keys=False)
        cfg.raw = merged
        print(f"suggested config written to {args.write} - backtest it before use: "
              f"scalper backtest -c {args.write}", file=sys.stderr)
    elif args.write:
        print("no machine-readable suggestions found in the report; nothing written", file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    """Pre-flight: validate config, connect, and print everything that could go wrong before trading."""
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "WARNING")
    problems = 0

    def ok(msg):
        print(f"  OK    {msg}")

    def warn(msg):
        nonlocal problems
        problems += 1
        print(f"  WARN  {msg}")

    symbols = cfg.select(args.symbol)
    print(f"config: broker={cfg.broker} symbols={[s.symbol for s in symbols]} timeframe={cfg.timeframe_sec}s "
          f"max_open={cfg.risk.max_open_positions} per_symbol={cfg.risk.max_positions_per_symbol}")
    try:
        for sc in symbols:
            ScalpStrategy(sc.strategy)
        ok("strategy parameters valid")
    except ValueError as e:
        print(f"  FAIL  strategy config: {e}")
        return 1
    try:
        risk = RiskManager(cfg.risk)
        ok("risk parameters valid")
    except ValueError as e:
        print(f"  FAIL  risk config: {e}")
        return 1
    if cfg.risk.risk_per_trade_pct > 2:
        warn(f"risk_per_trade_pct={cfg.risk.risk_per_trade_pct}% is aggressive for scalping (0.25-1% is typical)")
    if cfg.risk.max_daily_loss_pct <= 0:
        warn("max_daily_loss_pct is disabled; a daily loss cap is strongly recommended")
    if any(sc.strategy.tp_atr_mult < sc.strategy.sl_atr_mult for sc in symbols):
        warn("take-profit is smaller than stop-loss (reward:risk < 1); needs a high win rate to be profitable")
    if len(symbols) > 1:
        worst = cfg.risk.risk_per_trade_pct * cfg.risk.max_open_positions
        if worst > cfg.risk.max_daily_loss_pct:
            warn(f"{cfg.risk.max_open_positions} simultaneous positions x {cfg.risk.risk_per_trade_pct}% risk = {worst:.1f}% "
                 f"at once, more than the {cfg.risk.max_daily_loss_pct}% daily cap; lower risk_per_trade_pct or max_open_positions")
        else:
            ok(f"worst case {cfg.risk.max_open_positions} stops at once = {worst:.2f}% of balance")
    if cfg.broker == "deriv" and not cfg.deriv.get("token"):
        print("  FAIL  deriv.token is empty (set DERIV_TOKEN)")
        return 1
    if cfg.broker == "mt5" and not (cfg.mt5.get("login") and cfg.mt5.get("server")):
        warn("mt5.login / mt5.server empty: the adapter will attach to whatever account the terminal has open")

    broker = make_broker(cfg)

    async def go():
        nonlocal problems
        await broker.connect()
        try:
            balance = await broker.balance()
            ok(f"balance {balance:.2f}")
            if balance <= 0:
                warn("balance is zero")
            for sc in symbols:
                print(f"--- {sc.symbol}")
                await _check_symbol(cfg, sc, broker, balance, ok, warn)
        finally:
            await broker.close()

    try:
        asyncio.run(go())
    except BrokerError as e:
        print(f"  FAIL  {e}")
        return 1
    print("result:", "ready" if problems == 0 else f"{problems} warning(s) - review before trading")
    return 0


async def _check_symbol(cfg: Config, sc, broker, balance, ok, warn) -> None:
    from .models import Side
    strategy = ScalpStrategy(sc.strategy)
    risk = RiskManager(cfg.risk)
    try:
        info = await broker.symbol_info(sc.symbol)
    except BrokerError as e:
        warn(f"symbol: {e}")
        return
    ok(f"symbol {info.name} ({info.description}) point={info.point} digits={info.digits} "
       f"volume {info.min_volume}..{info.max_volume} step {info.volume_step} stops_level={info.stops_level_points}pts")
    if not await broker.market_open(sc.symbol):
        warn("market is closed / trading suspended right now")
    candles = await broker.candles(sc.symbol, sc.timeframe_sec, max(sc.engine.candle_count, strategy.lookback_bars()))
    candles, issues = validate_candles(candles, sc.timeframe_sec)
    for issue in issues:
        warn(f"candle data: {issue}")
    if len(candles) < strategy.warmup_bars():
        warn(f"only {len(candles)} candles available, strategy needs {strategy.warmup_bars()}")
    else:
        ok(f"{len(candles)} candles, last close {candles[-1].close} at {_fmt_ts(candles[-1].time)}")
    quote = await broker.quote(sc.symbol)
    ok(f"quote bid={quote.bid} ask={quote.ask} spread={quote.spread / info.point:.1f}pts at {_fmt_ts(quote.time)}")
    a = strategy.current_atr(candles) if len(candles) > sc.strategy.atr_period else None
    if a:
        ratio = quote.spread / a if a > 0 else 0
        msg = f"ATR={a / info.point:.1f}pts spread/ATR={ratio:.2f} (limit {sc.strategy.max_spread_atr_ratio})"
        if sc.strategy.max_spread_atr_ratio and ratio > sc.strategy.max_spread_atr_ratio:
            warn(msg + " -> entries would be blocked by the spread filter right now")
        else:
            ok(msg)
        sl = quote.ask - sc.strategy.sl_atr_mult * a
        lpu = await broker.loss_per_unit(sc.symbol, Side.BUY, quote.ask, sl)
        vol = risk.volume_for(balance, lpu, info)
        if vol <= 0:
            warn(f"position sizing yields 0 volume: a 1-ATR stop risks {lpu:.2f} per unit, "
                 f"budget {balance * cfg.risk.risk_per_trade_pct / 100:.2f}; raise balance/risk or lower multiplier/stop")
        else:
            ok(f"example size for a {sc.strategy.sl_atr_mult}xATR stop: volume={vol} "
               f"(risk {vol * lpu:.2f} = {vol * lpu / balance * 100:.2f}% of balance)")
    if not strategy.in_session(quote.time):
        warn(f"outside trade_hours/trade_days ({sc.strategy.trade_hours or 'any'} / {sc.strategy.trade_days or 'any'})")
    positions = await broker.open_positions(sc.symbol)
    ok(f"{len(positions)} open position(s) on {sc.symbol}")


def cmd_symbols(args) -> int:
    cfg = Config.load(args.config)
    _setup_logging(args.log_level or "WARNING")
    from .brokers.deriv import DerivBroker
    broker = make_data_broker(cfg, "deriv")
    assert isinstance(broker, DerivBroker)

    async def go():
        await broker.connect()
        try:
            rows = await broker.list_symbols(args.market)
            print(f"{'symbol':<14} {'market':<16} {'open':<5} {'multipliers':<28} display name")
            for s in rows:
                mults = await broker.multiplier_range(s["symbol"]) if args.check_multipliers else []
                if args.check_multipliers and not mults:
                    continue
                print(f"{s['symbol']:<14} {s.get('market',''):<16} {str(bool(s.get('exchange_is_open'))):<5} "
                      f"{','.join(map(str, mults)):<28} {s.get('display_name','')}")
        finally:
            await broker.close()

    asyncio.run(go())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scalper", description="EMA/RSI/ATR scalping bot for Deriv and MT5 (HFM)")
    p.add_argument("--version", action="version", version=f"scalper {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the bot (live or paper, per config)")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    r.add_argument("--broker", choices=["deriv", "mt5", "paper"], help="override broker from config")
    r.add_argument("--log-file")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("backtest", help="backtest the strategy on CSV or synthetic candles")
    b.add_argument("-c", "--config", required=True)
    b.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    b.add_argument("--csv", help="CSV with time,open,high,low,close[,volume]")
    b.add_argument("--synthetic", type=int, default=5000, help="number of synthetic bars when no CSV is given")
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--point", type=float, help="price increment (inferred from data when omitted)")
    b.add_argument("--trades-out", help="write the trade list to this CSV")
    b.add_argument("--synthetic-only", action="store_true", help="ignore stored history, use synthetic data")
    b.add_argument("--log-level")
    b.set_defaults(fn=cmd_backtest)

    o = sub.add_parser("optimize", help="grid-search strategy parameters with an out-of-sample check")
    o.add_argument("-c", "--config", required=True)
    o.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    o.add_argument("--csv", help="candles CSV (default: stored history)")
    o.add_argument("--grid", help="JSON file {param: [values...]} (default: built-in grid)")
    o.add_argument("--split", type=float, default=0.7, help="in-sample fraction (default 0.7)")
    o.add_argument("--min-trades", type=int, default=30)
    o.add_argument("--top", type=int, default=10)
    o.add_argument("--max-combos", type=int, default=0, help="randomly sample at most N combinations")
    o.add_argument("--workers", type=int, help="parallel processes (default: CPUs-1)")
    o.add_argument("--point", type=float)
    o.add_argument("--write", help="write a config YAML with the best robust parameters")
    o.add_argument("--synthetic", type=int, default=5000)
    o.add_argument("--seed", type=int, default=42)
    o.add_argument("--log-level")
    o.set_defaults(fn=cmd_optimize)

    a = sub.add_parser("analyze", help="have Claude review the trade journal, history and backtest")
    a.add_argument("-c", "--config", required=True)
    a.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    a.add_argument("--model", default="claude-opus-5-5")
    a.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    a.add_argument("--max-tokens", type=int, default=16000)
    a.add_argument("--out", help="write the report to this markdown file instead of printing it")
    a.add_argument("--write", help="write a config YAML with Claude's suggested parameters (backtest it first!)")
    a.add_argument("--stats-only", action="store_true", help="print the statistics pack without calling Claude")
    a.add_argument("--log-level")
    a.set_defaults(fn=cmd_analyze)

    h = sub.add_parser("history", help="show stored chart history")
    h.add_argument("-c", "--config", required=True)
    h.set_defaults(fn=cmd_history)

    k = sub.add_parser("check", help="pre-flight checks: config, connectivity, symbol, sizing, spread")
    k.add_argument("-c", "--config", required=True)
    k.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    k.add_argument("--log-level")
    k.set_defaults(fn=cmd_check)

    f = sub.add_parser("fetch", help="download recent candles from the broker to CSV")
    f.add_argument("-c", "--config", required=True)
    f.add_argument("--symbol", help="only this symbol from the config's `symbols` list")
    f.add_argument("--out", help="also write a standalone CSV (history store is always updated)")
    f.add_argument("--count", type=int, default=5000)
    f.add_argument("--broker", choices=["deriv", "mt5"])
    f.add_argument("--log-level")
    f.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("symbols", help="list Deriv symbols (and which support multipliers)")
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--market", default="synthetic_index", help="market filter, e.g. synthetic_index, forex; '' for all")
    s.add_argument("--check-multipliers", action="store_true", help="query contracts_for each symbol (slower)")
    s.add_argument("--log-level")
    s.set_defaults(fn=cmd_symbols)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "market", None) == "":
        args.market = None
    try:
        return args.fn(args)
    except BrokerError as e:
        print(f"broker error: {e}", file=sys.stderr)
        return 1
