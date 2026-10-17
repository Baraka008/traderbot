"""Deriv adapter using the official WebSocket API (Multiplier contracts).

Scalping on Deriv's native platform is done with MULTUP / MULTDOWN multiplier contracts,
which support stop-loss and take-profit and can be closed at any time. ``volume`` for this
broker is the *stake* in account currency; leverage comes from the ``multiplier``.

Get an API token at https://app.deriv.com/account/api-token (scopes: Read, Trade) and
register an app id at https://api.deriv.com (app id 1089 is Deriv's public test id).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from ..models import Candle, ClosedTrade, Position, Quote, Side, SymbolInfo
from .base import Broker, BrokerError, ConnectionLost

log = logging.getLogger(__name__)


class DerivError(BrokerError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DerivBroker(Broker):
    name = "deriv"

    def __init__(self, token: str, app_id: int = 1089,
                 endpoint: str = "wss://ws.derivws.com/websockets/v3",
                 multiplier: int = 100, min_stake: float = 1.0, max_stake: float = 1000.0,
                 stake_step: float = 0.01, request_timeout: float = 20.0):
        if not token:
            raise BrokerError("Deriv API token is required (set DERIV_TOKEN)")
        self.token = token
        self.app_id = int(app_id)
        self.endpoint = endpoint
        self.multiplier = int(multiplier)
        self.min_stake = float(min_stake)
        self.max_stake = float(max_stake)
        self.stake_step = float(stake_step)
        self.request_timeout = request_timeout

        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._contracts: Dict[str, Dict[str, Any]] = {}   # contract_id -> latest proposal_open_contract
        self._virtual: Dict[str, Dict[str, float]] = {}    # contract_id -> {"sl": price} stops Deriv cannot hold
        self._last_limit: Dict[str, Dict[str, float]] = {} # contract_id -> last limit_order sent
        self._selling: set = set()                          # contract ids with a sell in flight
        self._ticks: Dict[str, Quote] = {}
        self._tick_events: Dict[str, asyncio.Event] = {}
        self._tick_subscribed: set = set()
        self._balance: Optional[float] = None
        self._currency = "USD"
        self._symbol_cache: Dict[str, SymbolInfo] = {}
        self._active_symbols: Dict[str, Dict[str, Any]] = {}
        self._closed = False

    # ------------------------------------------------------------------ transport
    async def _open_ws(self):
        import websockets  # local import so other adapters work without it
        url = f"{self.endpoint}?app_id={self.app_id}"
        return await websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024)

    async def connect(self) -> None:
        self._closed = False
        self._ws = await self._open_ws()
        self._reader = asyncio.create_task(self._read_loop(), name="deriv-reader")
        auth = await self._call({"authorize": self.token})
        acct = auth["authorize"]
        self._currency = acct.get("currency", "USD")
        log.info("Deriv authorized: loginid=%s currency=%s balance=%s",
                 acct.get("loginid"), self._currency, acct.get("balance"))
        bal = await self._call({"balance": 1, "subscribe": 1})
        self._balance = float(bal["balance"]["balance"])
        # Recover open multiplier contracts (e.g. after a restart).
        pf = await self._call({"portfolio": 1})
        for c in pf.get("portfolio", {}).get("contracts", []):
            if c.get("contract_type") in ("MULTUP", "MULTDOWN"):
                try:
                    await self._track(str(c["contract_id"]))
                except DerivError as e:
                    log.warning("could not track contract %s: %s", c.get("contract_id"), e)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._call({"forget_all": ["ticks", "proposal_open_contract", "balance"]}), 5)
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        self._reader = None
        self._ws = None
        self._tick_subscribed.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionLost("connection closed"))
        self._pending.clear()

    async def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        for attempt in range(4):
            try:
                return await self._call_once(payload)
            except DerivError as e:
                if e.code == "RateLimit" and attempt < 3:
                    wait = 1.5 * (attempt + 1)
                    log.warning("Deriv rate limit hit, retrying in %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue
                raise
        raise BrokerError("unreachable")

    async def _call_once(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._ws is None:
            raise ConnectionLost("not connected")
        self._req_id += 1
        req_id = self._req_id
        payload = dict(payload, req_id=req_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, self.request_timeout)
        except asyncio.TimeoutError:
            raise ConnectionLost(f"Deriv request timed out: {list(payload)[0]}")
        except (ConnectionError, OSError) as e:
            raise ConnectionLost(f"Deriv send failed: {e}")
        except Exception as e:
            if type(e).__name__ in ("ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK"):
                raise ConnectionLost(f"Deriv connection closed: {e}")
            raise
        finally:
            self._pending.pop(req_id, None)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection dropped
            if not self._closed:
                log.warning("Deriv websocket reader stopped: %s", e)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionLost(f"connection lost: {e}"))

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("req_id")
        fut = self._pending.get(req_id) if req_id is not None else None
        if fut is not None and not fut.done():
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(DerivError(err.get("code", "unknown"), err.get("message", "")))
            else:
                fut.set_result(msg)
            # the first response of a subscription also carries data we want cached
        if "error" in msg:
            if fut is None:
                log.warning("Deriv stream error: %s", msg["error"])
            return
        mtype = msg.get("msg_type")
        if mtype == "tick":
            self._on_tick(msg["tick"])
        elif mtype == "proposal_open_contract":
            poc = msg.get("proposal_open_contract") or {}
            if poc.get("contract_id") is not None:
                self._contracts[str(poc["contract_id"])] = poc
        elif mtype == "balance":
            b = msg.get("balance") or {}
            if "balance" in b:
                self._balance = float(b["balance"])

    def _on_tick(self, t: Dict[str, Any]) -> None:
        symbol = t.get("symbol")
        if not symbol:
            return
        quote = float(t.get("quote"))
        bid = float(t.get("bid", quote) or quote)
        ask = float(t.get("ask", quote) or quote)
        if ask < bid:
            bid, ask = ask, bid
        self._ticks[symbol] = Quote(bid=bid, ask=ask, time=float(t.get("epoch", time.time())))
        ev = self._tick_events.setdefault(symbol, asyncio.Event())
        ev.set()

    async def _track(self, contract_id: str) -> Dict[str, Any]:
        resp = await self._call({"proposal_open_contract": 1, "contract_id": int(contract_id), "subscribe": 1})
        poc = resp["proposal_open_contract"]
        self._contracts[contract_id] = poc
        return poc

    # ------------------------------------------------------------------ market data
    async def _load_active_symbols(self, force: bool = False) -> None:
        if self._active_symbols and not force:
            return
        resp = await self._call({"active_symbols": "brief", "product_type": "basic"})
        self._active_symbols = {s["symbol"]: s for s in resp.get("active_symbols", [])}
        self._symbols_loaded_at = time.time()

    async def market_open(self, symbol: str) -> bool:
        if time.time() - getattr(self, "_symbols_loaded_at", 0.0) > 300:
            try:
                await self._load_active_symbols(force=True)
            except BrokerError as e:
                log.warning("could not refresh active_symbols: %s", e)
        meta = self._active_symbols.get(symbol)
        if meta is None:
            return True
        return bool(meta.get("exchange_is_open", 1)) and not meta.get("is_trading_suspended", 0)

    async def list_symbols(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        await self._load_active_symbols()
        out = []
        for s in self._active_symbols.values():
            if market and s.get("market") != market:
                continue
            out.append(s)
        return sorted(out, key=lambda s: (s.get("market", ""), s["symbol"]))

    async def multiplier_range(self, symbol: str) -> List[int]:
        resp = await self._call({"contracts_for": symbol, "currency": self._currency, "product_type": "basic"})
        for avail in resp.get("contracts_for", {}).get("available", []):
            if avail.get("contract_category") == "multiplier":
                return [int(m) for m in avail.get("multiplier_range", [])]
        return []

    async def symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        await self._load_active_symbols()
        meta = self._active_symbols.get(symbol)
        if meta is None:
            raise BrokerError(f"unknown Deriv symbol '{symbol}' (run `symbols` command to list)")
        if meta.get("is_trading_suspended"):
            log.warning("Deriv reports trading suspended for %s", symbol)
        pip = float(meta.get("pip", 0.01))
        digits = max(0, -int(f"{pip:e}".split("e")[1])) if pip < 1 else 0
        mults = await self.multiplier_range(symbol)
        if not mults:
            raise BrokerError(f"Deriv symbol '{symbol}' does not offer multiplier contracts")
        if self.multiplier not in mults:
            raise BrokerError(f"multiplier {self.multiplier} not available for {symbol}; choose one of {mults}")
        info = SymbolInfo(name=symbol, point=pip, digits=digits, min_volume=self.min_stake,
                          max_volume=self.max_stake, volume_step=self.stake_step,
                          stops_level_points=0, description=meta.get("display_name", symbol))
        self._symbol_cache[symbol] = info
        return info

    async def balance(self) -> float:
        if self._balance is None:
            resp = await self._call({"balance": 1})
            self._balance = float(resp["balance"]["balance"])
        return self._balance

    async def candles(self, symbol: str, timeframe_sec: int, count: int) -> List[Candle]:
        resp = await self._call({"ticks_history": symbol, "style": "candles", "granularity": int(timeframe_sec),
                                 "count": int(count), "end": "latest"})
        out = []
        for c in resp.get("candles", []):
            out.append(Candle(time=int(c["epoch"]), open=float(c["open"]), high=float(c["high"]),
                              low=float(c["low"]), close=float(c["close"])))
        return out

    async def quote(self, symbol: str) -> Quote:
        if symbol not in self._tick_subscribed:
            self._tick_events.setdefault(symbol, asyncio.Event())
            await self._call({"ticks": symbol, "subscribe": 1})
            self._tick_subscribed.add(symbol)
        if symbol not in self._ticks:
            await asyncio.wait_for(self._tick_events[symbol].wait(), self.request_timeout)
        return self._ticks[symbol]

    # ------------------------------------------------------------------ positions
    def _to_position(self, poc: Dict[str, Any]) -> Position:
        side = Side.BUY if poc.get("contract_type") == "MULTUP" else Side.SELL
        stake = float(poc.get("buy_price") or 0.0)
        entry = float(poc.get("entry_spot") or poc.get("current_spot") or 0.0)
        mult = float(poc.get("multiplier") or self.multiplier)
        lo = poc.get("limit_order") or {}
        sl = self._level(lo.get("stop_loss"), side, entry, stake, mult, is_sl=True)
        tp = self._level(lo.get("take_profit"), side, entry, stake, mult, is_sl=False)
        cid = str(poc["contract_id"])
        virtual = self._virtual.get(cid) or {}
        if virtual.get("sl"):
            sl = virtual["sl"]
        return Position(id=cid, symbol=poc.get("underlying", ""), side=side, volume=stake,
                        entry_price=entry, stop_loss=sl, take_profit=tp,
                        open_time=float(poc.get("date_start") or time.time()),
                        unrealized_pnl=float(poc.get("profit") or 0.0))

    @staticmethod
    def _level(order: Optional[Dict[str, Any]], side: Side, entry: float, stake: float, mult: float, is_sl: bool) -> float:
        if not order:
            return 0.0
        if order.get("value") not in (None, ""):
            try:
                return float(order["value"])
            except (TypeError, ValueError):
                pass
        amt = abs(float(order.get("order_amount") or 0.0))
        if stake <= 0 or mult <= 0 or entry <= 0:
            return 0.0
        dist = amt * entry / (stake * mult)
        if side is Side.BUY:
            return entry - dist if is_sl else entry + dist
        return entry + dist if is_sl else entry - dist

    def _amount(self, stake: float, entry: float, level: float) -> float:
        """Currency P/L for a multiplier contract when spot moves from entry to level."""
        if entry <= 0 or level <= 0:
            return 0.0
        return round(stake * self.multiplier * abs(level - entry) / entry, 2)

    async def open_positions(self, symbol: str) -> List[Position]:
        out = []
        for poc in list(self._contracts.values()):
            if poc.get("underlying") != symbol:
                continue
            if poc.get("is_sold") or poc.get("status") not in (None, "open"):
                continue
            if str(poc.get("contract_id")) in self._selling:
                continue
            pos = self._to_position(poc)
            if await self._virtual_stop_hit(pos, poc):
                continue
            out.append(pos)
        return out

    async def _virtual_stop_hit(self, pos: Position, poc: Dict[str, Any]) -> bool:
        """Sell the contract when price crosses a stop level Deriv itself cannot hold (stop in profit)."""
        virtual = self._virtual.get(pos.id)
        if not virtual or not virtual.get("sl"):
            return False
        spot = float(poc.get("current_spot") or 0.0)
        if spot <= 0:
            return False
        hit = spot <= virtual["sl"] if pos.side is Side.BUY else spot >= virtual["sl"]
        if not hit:
            return False
        log.info("Deriv virtual stop hit on %s: spot=%s sl=%s -> selling", pos.id, spot, virtual["sl"])
        try:
            await self._call({"sell": int(pos.id), "price": 0})
        except DerivError as e:
            log.warning("virtual stop sell of %s failed: %s", pos.id, e)
            return False
        self._virtual.pop(pos.id, None)
        self._selling.add(pos.id)
        return True

    async def loss_per_unit(self, symbol: str, side: Side, entry: float, stop_loss: float) -> float:
        if entry <= 0:
            return 0.0
        return self.multiplier * abs(entry - stop_loss) / entry

    async def open_market(self, symbol: str, side: Side, volume: float, stop_loss: float,
                          take_profit: float, comment: str = "") -> Position:
        stake = round(max(self.min_stake, min(volume, self.max_stake)), 2)
        quote = await self.quote(symbol)
        entry = quote.ask if side is Side.BUY else quote.bid
        limit_order: Dict[str, float] = {}
        sl_amt = self._amount(stake, entry, stop_loss)
        tp_amt = self._amount(stake, entry, take_profit)
        if sl_amt > 0:
            # Deriv rejects a stop-loss larger than the stake (minus commission).
            limit_order["stop_loss"] = max(0.01, min(sl_amt, round(stake * 0.95, 2)))
        if tp_amt > 0:
            limit_order["take_profit"] = max(0.01, tp_amt)
        params = {
            "amount": stake, "basis": "stake", "currency": self._currency,
            "contract_type": "MULTUP" if side is Side.BUY else "MULTDOWN",
            "symbol": symbol, "multiplier": self.multiplier,
        }
        if limit_order:
            params["limit_order"] = limit_order
        resp = await self._call({"buy": 1, "price": stake, "parameters": params})
        cid = str(resp["buy"]["contract_id"])
        log.info("Deriv bought %s %s stake=%.2f x%d contract=%s sl=%s tp=%s", params["contract_type"], symbol,
                 stake, self.multiplier, cid, limit_order.get("stop_loss"), limit_order.get("take_profit"))
        poc = await self._track(cid)
        pos = self._to_position(poc)
        if pos.entry_price <= 0:
            pos.entry_price = entry
        if pos.stop_loss <= 0:
            pos.stop_loss = stop_loss
        if pos.take_profit <= 0:
            pos.take_profit = take_profit
        pos.comment = comment
        return pos

    async def modify_position(self, pos: Position, stop_loss: float, take_profit: float) -> Position:
        limit_order: Dict[str, Any] = {}
        sl_amt = self._amount(pos.volume, pos.entry_price, stop_loss)
        tp_amt = self._amount(pos.volume, pos.entry_price, take_profit)
        # Deriv's stop_loss is a *loss* amount, so a stop that already sits in profit (breakeven /
        # trailing) cannot be held by the server. We send the smallest legal stop (≈ breakeven) as a
        # hard floor and track the real level as a virtual stop that sells the contract when hit.
        is_buy = pos.side is Side.BUY
        sl_in_profit = (is_buy and stop_loss >= pos.entry_price) or (not is_buy and stop_loss <= pos.entry_price)
        if stop_loss > 0:
            if sl_in_profit:
                limit_order["stop_loss"] = 0.01
                self._virtual.setdefault(pos.id, {})["sl"] = stop_loss
            else:
                limit_order["stop_loss"] = max(0.01, min(sl_amt, round(pos.volume * 0.95, 2)))
                self._virtual.pop(pos.id, None)
        if take_profit > 0:
            limit_order["take_profit"] = max(0.01, tp_amt)
        if not limit_order:
            return pos
        if self._last_limit.get(pos.id) != limit_order:
            await self._call({"contract_update": 1, "contract_id": int(pos.id), "limit_order": limit_order})
            self._last_limit[pos.id] = dict(limit_order)
        poc = self._contracts.get(pos.id)
        if poc is not None:
            lo = poc.setdefault("limit_order", {})
            if "stop_loss" in limit_order:
                lo["stop_loss"] = {"order_amount": -limit_order["stop_loss"], "value": stop_loss}
            if "take_profit" in limit_order:
                lo["take_profit"] = {"order_amount": limit_order["take_profit"], "value": take_profit}
        pos.stop_loss = stop_loss
        pos.take_profit = take_profit
        return pos

    async def close_position(self, pos: Position, reason: str = "") -> ClosedTrade:
        await self._call({"sell": int(pos.id), "price": 0})
        self._selling.add(pos.id)
        for _ in range(20):
            ct = await self.closed_trade(pos.id)
            if ct is not None:
                ct.reason = reason
                return ct
            await asyncio.sleep(0.25)
        raise BrokerError(f"Deriv contract {pos.id} sold but result not available yet")

    async def closed_trade(self, position_id: str) -> Optional[ClosedTrade]:
        poc = self._contracts.get(position_id)
        if poc is None or not poc.get("is_sold"):
            resp = await self._call({"proposal_open_contract": 1, "contract_id": int(position_id)})
            poc = resp.get("proposal_open_contract") or {}
            if poc.get("contract_id") is not None:
                self._contracts[position_id] = poc
        if not poc or not poc.get("is_sold"):
            return None
        side = Side.BUY if poc.get("contract_type") == "MULTUP" else Side.SELL
        exit_price = poc.get("exit_tick") or poc.get("sell_spot") or poc.get("current_spot") or 0.0
        trade = ClosedTrade(
            id=position_id, symbol=poc.get("underlying", ""), side=side, volume=float(poc.get("buy_price") or 0.0),
            entry_price=float(poc.get("entry_spot") or 0.0), exit_price=float(exit_price),
            open_time=float(poc.get("date_start") or 0.0),
            close_time=float(poc.get("sell_time") or poc.get("exit_tick_time") or time.time()),
            pnl=float(poc.get("profit") or 0.0), reason=str(poc.get("status", "")),
        )
        self._contracts.pop(position_id, None)
        self._virtual.pop(position_id, None)
        self._last_limit.pop(position_id, None)
        self._selling.discard(position_id)
        return trade
