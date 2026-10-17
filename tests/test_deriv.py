"""Deriv adapter against a fake websocket (protocol/routing test, no network)."""
import asyncio
import json

import pytest

from scalper.brokers.deriv import DerivBroker, DerivError
from scalper.models import Side


class FakeWS:
    def __init__(self):
        self.sent = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.contract_sold = False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        rid = msg["req_id"]
        if "authorize" in msg:
            await self.inbox.put({"msg_type": "authorize", "req_id": rid,
                                  "authorize": {"loginid": "CR1", "currency": "USD", "balance": 1000}})
        elif "balance" in msg:
            await self.inbox.put({"msg_type": "balance", "req_id": rid, "balance": {"balance": 1000.0}})
        elif "portfolio" in msg:
            await self.inbox.put({"msg_type": "portfolio", "req_id": rid, "portfolio": {"contracts": []}})
        elif "active_symbols" in msg:
            await self.inbox.put({"msg_type": "active_symbols", "req_id": rid, "active_symbols": [
                {"symbol": "R_75", "pip": 0.0001, "market": "synthetic_index", "display_name": "Volatility 75 Index",
                 "exchange_is_open": 1}]})
        elif "contracts_for" in msg:
            await self.inbox.put({"msg_type": "contracts_for", "req_id": rid, "contracts_for": {"available": [
                {"contract_category": "multiplier", "multiplier_range": [10, 20, 30, 50, 100]}]}})
        elif "ticks_history" in msg:
            await self.inbox.put({"msg_type": "candles", "req_id": rid, "candles": [
                {"epoch": 60 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5} for i in range(5)]})
        elif "ticks" in msg:
            await self.inbox.put({"msg_type": "tick", "req_id": rid,
                                  "tick": {"symbol": "R_75", "quote": 1234.5, "epoch": 1700000000}})
        elif "buy" in msg:
            assert msg["parameters"]["contract_type"] == "MULTUP"
            assert msg["parameters"]["limit_order"]["stop_loss"] == pytest.approx(5.0)
            assert msg["parameters"]["limit_order"]["take_profit"] == pytest.approx(7.5)
            await self.inbox.put({"msg_type": "buy", "req_id": rid, "buy": {"contract_id": 42, "buy_price": 10}})
        elif "proposal_open_contract" in msg:
            poc = {"contract_id": 42, "contract_type": "MULTUP", "underlying": "R_75", "entry_spot": 1234.5,
                   "current_spot": 1235.0, "buy_price": 10, "multiplier": 100, "profit": 0.4, "date_start": 1700000000,
                   "status": "open", "is_sold": 0,
                   "limit_order": {"stop_loss": {"order_amount": -5, "value": 1228.3275},
                                   "take_profit": {"order_amount": 7.5, "value": 1243.75875}}}
            if self.contract_sold:
                poc.update(is_sold=1, status="sold", sell_spot=1240.0, sell_time=1700000100, profit=4.45)
            await self.inbox.put({"msg_type": "proposal_open_contract", "req_id": rid, "proposal_open_contract": poc})
        elif "sell" in msg:
            self.contract_sold = True
            await self.inbox.put({"msg_type": "sell", "req_id": rid, "sell": {"sold_for": 14.45}})
        elif "contract_update" in msg:
            await self.inbox.put({"msg_type": "contract_update", "req_id": rid, "contract_update": {}})
        elif "forget_all" in msg:
            await self.inbox.put({"msg_type": "forget_all", "req_id": rid, "forget_all": []})
        else:
            await self.inbox.put({"req_id": rid, "error": {"code": "Unknown", "message": "unexpected"}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self):
        self.closed = True
        await self.inbox.put(None)


class FakeDeriv(DerivBroker):
    def __init__(self, ws, **kw):
        super().__init__(token="tok", **kw)
        self._fake = ws

    async def _open_ws(self):
        return self._fake


def test_deriv_flow():
    async def go():
        ws = FakeWS()
        b = FakeDeriv(ws, multiplier=100)
        await b.connect()
        assert await b.balance() == 1000.0
        info = await b.symbol_info("R_75")
        assert info.point == 0.0001 and info.digits == 4 and info.min_volume == 1.0
        cs = await b.candles("R_75", 60, 5)
        assert len(cs) == 5 and cs[-1].close == 1.5
        q = await b.quote("R_75")
        assert q.bid == q.ask == 1234.5
        # loss per 1 USD stake with multiplier 100 and 0.5% adverse move = 0.5
        assert await b.loss_per_unit("R_75", Side.BUY, 1234.5, 1228.3275) == pytest.approx(0.5)
        pos = await b.open_market("R_75", Side.BUY, 10.0, 1228.3275, 1243.75875)
        assert pos.id == "42" and pos.side is Side.BUY and pos.entry_price == 1234.5
        assert pos.stop_loss == pytest.approx(1228.3275) and pos.take_profit == pytest.approx(1243.75875)
        assert [p.id for p in await b.open_positions("R_75")] == ["42"]
        assert await b.closed_trade("42") is None
        await b.modify_position(pos, 1230.0, 1243.75875)   # tighten SL below entry -> amount recalculated
        upd = [m for m in ws.sent if "contract_update" in m][-1]
        assert upd["limit_order"]["stop_loss"] == pytest.approx(10 * 100 * 4.5 / 1234.5, abs=0.01)
        assert pos.stop_loss == 1230.0
        ct = await b.close_position(pos, "manual")
        assert ct.pnl == pytest.approx(4.45) and ct.exit_price == 1240.0 and ct.reason == "manual"
        assert await b.open_positions("R_75") == []
        await b.close()
        assert ws.closed

    asyncio.run(go())


def test_deriv_error_surfaces():
    async def go():
        ws = FakeWS()
        b = FakeDeriv(ws)
        await b.connect()
        with pytest.raises(DerivError):
            await b._call({"bogus": 1})
        await b.close()

    asyncio.run(go())


def test_wrong_multiplier_rejected():
    async def go():
        b = FakeDeriv(FakeWS(), multiplier=77)
        await b.connect()
        with pytest.raises(Exception, match="multiplier 77"):
            await b.symbol_info("R_75")
        await b.close()

    asyncio.run(go())


def test_virtual_stop_sells_when_price_crosses_trailed_level():
    async def go():
        ws = FakeWS()
        b = FakeDeriv(ws, multiplier=100)
        await b.connect()
        await b.symbol_info("R_75")
        pos = await b.open_market("R_75", Side.BUY, 10.0, 1228.3275, 1243.75875)
        await b.modify_position(pos, 1236.0, 1243.75875)   # SL above entry: Deriv gets 0.01 floor, we track 1236
        upd = [m for m in ws.sent if "contract_update" in m][-1]
        assert upd["limit_order"]["stop_loss"] == 0.01
        await b.modify_position(pos, 1236.0, 1243.75875)   # identical update is not re-sent
        assert len([m for m in ws.sent if "contract_update" in m]) == 1
        assert b._to_position(b._contracts["42"]).stop_loss == 1236.0   # reported level is the virtual one
        # cached spot is 1235.0 < 1236.0 -> polling positions must sell the contract
        assert await b.open_positions("R_75") == []
        assert ws.contract_sold
        assert await b.open_positions("R_75") == []
        ct = await b.closed_trade("42")
        assert ct is not None and ct.pnl == pytest.approx(4.45)
        await b.close()

    asyncio.run(go())
