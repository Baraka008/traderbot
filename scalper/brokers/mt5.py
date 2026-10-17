"""MetaTrader 5 adapter (works with HFM / HotForex MT5, Deriv MT5 and any other MT5 broker).

Requires the ``MetaTrader5`` Python package and a running MT5 terminal, which is Windows
only (``pip install MetaTrader5``). ``volume`` for this broker is in lots.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..models import Candle, ClosedTrade, Position, Quote, Side, SymbolInfo
from .base import Broker, BrokerError, ConnectionLost

log = logging.getLogger(__name__)

_TIMEFRAMES = {60: "TIMEFRAME_M1", 120: "TIMEFRAME_M2", 180: "TIMEFRAME_M3", 300: "TIMEFRAME_M5",
               600: "TIMEFRAME_M10", 900: "TIMEFRAME_M15", 1800: "TIMEFRAME_M30", 3600: "TIMEFRAME_H1",
               14400: "TIMEFRAME_H4", 86400: "TIMEFRAME_D1"}


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, login: int = 0, password: str = "", server: str = "", path: str = "",
                 magic: int = 777123, deviation_points: int = 20, comment: str = "scalper",
                 mt5_module: Any = None):
        self.login = int(login) if login else 0
        self.password = password
        self.server = server
        self.path = path
        self.magic = int(magic)
        self.deviation = int(deviation_points)
        self.comment = comment[:31]
        self._mt5 = mt5_module
        self._symbol_cache: Dict[str, SymbolInfo] = {}

    # ------------------------------------------------------------------ plumbing
    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def connect(self) -> None:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except ImportError as e:
                raise BrokerError("MetaTrader5 package not available. It only runs on Windows next to an "
                                  "installed MT5 terminal: pip install MetaTrader5") from e
            self._mt5 = mt5
        mt5 = self._mt5
        kwargs: Dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=self.login, password=self.password, server=self.server)
        ok = await self._run(mt5.initialize, **kwargs)
        if not ok:
            raise BrokerError(f"MT5 initialize failed: {mt5.last_error()}")
        info = await self._run(mt5.account_info)
        if info is None:
            raise BrokerError(f"MT5 account_info failed: {mt5.last_error()}")
        log.info("MT5 connected: login=%s server=%s balance=%.2f %s", info.login, info.server, info.balance, info.currency)

    async def close(self) -> None:
        if self._mt5 is not None:
            try:
                await self._run(self._mt5.shutdown)
            except Exception:
                pass

    # ------------------------------------------------------------------ market data
    async def symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        mt5 = self._mt5
        if not await self._run(mt5.symbol_select, symbol, True):
            raise BrokerError(f"MT5 symbol '{symbol}' not found / cannot be selected: {mt5.last_error()}")
        si = await self._run(mt5.symbol_info, symbol)
        if si is None:
            raise BrokerError(f"MT5 symbol_info({symbol}) failed: {mt5.last_error()}")
        info = SymbolInfo(name=symbol, point=float(si.point), digits=int(si.digits), min_volume=float(si.volume_min),
                          max_volume=float(si.volume_max), volume_step=float(si.volume_step),
                          stops_level_points=int(si.trade_stops_level), description=str(si.description))
        self._symbol_cache[symbol] = info
        return info

    async def balance(self) -> float:
        acct = await self._run(self._mt5.account_info)
        if acct is None:
            raise ConnectionLost(f"MT5 account_info failed (terminal disconnected?): {self._mt5.last_error()}")
        return float(acct.balance)

    async def market_open(self, symbol: str) -> bool:
        mt5 = self._mt5
        si = await self._run(mt5.symbol_info, symbol)
        if si is None:
            return False
        if si.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED or si.trade_mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
            return False
        term = await self._run(mt5.terminal_info)
        return bool(term and term.connected)

    async def candles(self, symbol: str, timeframe_sec: int, count: int) -> List[Candle]:
        tf_name = _TIMEFRAMES.get(int(timeframe_sec))
        if tf_name is None:
            raise BrokerError(f"unsupported MT5 timeframe {timeframe_sec}s; use one of {sorted(_TIMEFRAMES)}")
        tf = getattr(self._mt5, tf_name)
        rates = await self._run(self._mt5.copy_rates_from_pos, symbol, tf, 0, int(count))
        if rates is None:
            raise BrokerError(f"MT5 copy_rates_from_pos failed: {self._mt5.last_error()}")
        return [Candle(time=int(r["time"]), open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                       close=float(r["close"]), volume=float(r["tick_volume"])) for r in rates]

    async def quote(self, symbol: str) -> Quote:
        t = await self._run(self._mt5.symbol_info_tick, symbol)
        if t is None:
            raise ConnectionLost(f"MT5 symbol_info_tick failed: {self._mt5.last_error()}")
        ts = (t.time_msc / 1000.0) if getattr(t, "time_msc", 0) else float(t.time)
        return Quote(bid=float(t.bid), ask=float(t.ask), time=ts)

    # ------------------------------------------------------------------ positions
    def _to_position(self, p: Any) -> Position:
        side = Side.BUY if p.type == self._mt5.POSITION_TYPE_BUY else Side.SELL
        return Position(id=str(p.ticket), symbol=p.symbol, side=side, volume=float(p.volume),
                        entry_price=float(p.price_open), stop_loss=float(p.sl), take_profit=float(p.tp),
                        open_time=float(p.time), unrealized_pnl=float(p.profit), comment=str(p.comment))

    async def open_positions(self, symbol: str) -> List[Position]:
        positions = await self._run(self._mt5.positions_get, symbol=symbol)
        if positions is None:
            return []
        return [self._to_position(p) for p in positions if int(p.magic) == self.magic]

    async def loss_per_unit(self, symbol: str, side: Side, entry: float, stop_loss: float) -> float:
        mt5 = self._mt5
        otype = mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL
        profit = await self._run(mt5.order_calc_profit, otype, symbol, 1.0, entry, stop_loss)
        if profit is not None:
            return abs(float(profit))
        si = await self._run(mt5.symbol_info, symbol)
        if si is None or si.trade_tick_size <= 0:
            return 0.0
        return abs(entry - stop_loss) / si.trade_tick_size * si.trade_tick_value

    async def _filling_modes(self, symbol: str) -> List[int]:
        mt5 = self._mt5
        si = await self._run(mt5.symbol_info, symbol)
        modes = []
        fm = int(si.filling_mode) if si is not None else 0
        if fm & 1:
            modes.append(mt5.ORDER_FILLING_FOK)
        if fm & 2:
            modes.append(mt5.ORDER_FILLING_IOC)
        modes.append(mt5.ORDER_FILLING_RETURN)
        return modes

    async def _send(self, request: Dict[str, Any]) -> Any:
        mt5 = self._mt5
        result = await self._run(mt5.order_send, request)
        if result is None:
            raise BrokerError(f"MT5 order_send returned None: {mt5.last_error()}")
        return result

    async def open_market(self, symbol: str, side: Side, volume: float, stop_loss: float,
                          take_profit: float, comment: str = "") -> Position:
        mt5 = self._mt5
        info = await self.symbol_info(symbol)
        tick = await self.quote(symbol)
        is_buy = side is Side.BUY
        price = tick.ask if is_buy else tick.bid
        min_dist = info.stops_level_points * info.point
        # Respect the broker's minimum stop distance.
        if is_buy:
            stop_loss = min(stop_loss, price - min_dist) if stop_loss > 0 else 0.0
            take_profit = max(take_profit, price + min_dist) if take_profit > 0 else 0.0
        else:
            stop_loss = max(stop_loss, price + min_dist) if stop_loss > 0 else 0.0
            take_profit = min(take_profit, price - min_dist) if take_profit > 0 else 0.0
        base = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL, "price": float(price),
            "sl": info.round_price(stop_loss), "tp": info.round_price(take_profit), "deviation": self.deviation,
            "magic": self.magic, "comment": (comment or self.comment)[:31], "type_time": mt5.ORDER_TIME_GTC,
        }
        last = None
        for mode in await self._filling_modes(symbol):
            result = await self._send(dict(base, type_filling=mode))
            last = result
            if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL):
                break
            if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                raise BrokerError(f"MT5 order failed: retcode={result.retcode} {result.comment}")
        else:
            raise BrokerError(f"MT5 order failed for every filling mode: {last.retcode} {last.comment}")
        log.info("MT5 %s %s vol=%.2f price=%.5f sl=%.5f tp=%.5f order=%s deal=%s", side.value, symbol, volume,
                 result.price or price, base["sl"], base["tp"], result.order, result.deal)
        pos = await self._find_position(symbol, result)
        if pos is None:
            raise BrokerError("MT5 order executed but position could not be located")
        return pos

    async def _find_position(self, symbol: str, result: Any) -> Optional[Position]:
        mt5 = self._mt5
        for _ in range(10):
            if result.deal:
                deals = await self._run(mt5.history_deals_get, ticket=int(result.deal))
                if deals:
                    pos_id = int(deals[0].position_id)
                    positions = await self._run(mt5.positions_get, ticket=pos_id)
                    if positions:
                        return self._to_position(positions[0])
            positions = await self._run(mt5.positions_get, ticket=int(result.order))
            if positions:
                return self._to_position(positions[0])
            await asyncio.sleep(0.2)
        # Fall back to the newest position with our magic number on this symbol.
        positions = await self._run(mt5.positions_get, symbol=symbol)
        ours = [p for p in (positions or []) if int(p.magic) == self.magic]
        if ours:
            return self._to_position(max(ours, key=lambda p: p.time_msc))
        return None

    async def modify_position(self, pos: Position, stop_loss: float, take_profit: float) -> Position:
        mt5 = self._mt5
        info = await self.symbol_info(pos.symbol)
        request = {"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": int(pos.id),
                   "sl": info.round_price(stop_loss), "tp": info.round_price(take_profit), "magic": self.magic}
        result = await self._send(request)
        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            raise BrokerError(f"MT5 modify failed: retcode={result.retcode} {result.comment}")
        pos.stop_loss = request["sl"]
        pos.take_profit = request["tp"]
        return pos

    async def close_position(self, pos: Position, reason: str = "") -> ClosedTrade:
        mt5 = self._mt5
        tick = await self.quote(pos.symbol)
        is_buy = pos.side is Side.BUY
        base = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": float(pos.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY, "position": int(pos.id),
            "price": float(tick.bid if is_buy else tick.ask), "deviation": self.deviation, "magic": self.magic,
            "comment": f"close:{reason}"[:31], "type_time": mt5.ORDER_TIME_GTC,
        }
        for mode in await self._filling_modes(pos.symbol):
            result = await self._send(dict(base, type_filling=mode))
            if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL):
                break
            if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                raise BrokerError(f"MT5 close failed: retcode={result.retcode} {result.comment}")
        else:
            raise BrokerError("MT5 close failed for every filling mode")
        for _ in range(20):
            ct = await self.closed_trade(pos.id)
            if ct is not None:
                ct.reason = reason
                return ct
            await asyncio.sleep(0.25)
        raise BrokerError(f"MT5 position {pos.id} closed but history not available yet")

    async def closed_trade(self, position_id: str) -> Optional[ClosedTrade]:
        mt5 = self._mt5
        deals = await self._run(mt5.history_deals_get, position=int(position_id))
        if not deals:
            # Some terminals need an explicit date range.
            now = datetime.now()
            deals = await self._run(mt5.history_deals_get, now - timedelta(days=30), now + timedelta(days=1),
                                    position=int(position_id))
        if not deals:
            return None
        deals = sorted(deals, key=lambda d: (d.time_msc, d.ticket))
        ins = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
        outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY)]
        if not outs:
            return None
        open_vol = sum(d.volume for d in ins)
        closed_vol = sum(d.volume for d in outs)
        if closed_vol + 1e-9 < open_vol:
            return None   # partially closed, still open
        pnl = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        first, last = (ins[0] if ins else deals[0]), outs[-1]
        side = Side.BUY if first.type == mt5.DEAL_TYPE_BUY else Side.SELL
        return ClosedTrade(id=position_id, symbol=first.symbol, side=side, volume=float(open_vol or first.volume),
                           entry_price=float(first.price), exit_price=float(last.price), open_time=float(first.time),
                           close_time=float(last.time), pnl=float(pnl),
                           reason="tp" if "tp" in str(last.comment).lower() else ("sl" if "sl" in str(last.comment).lower() else ""))
