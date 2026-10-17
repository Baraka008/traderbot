import json
from contextlib import contextmanager
from types import SimpleNamespace

from scalper.analysis import (SYSTEM_PROMPT, analyze_with_claude, build_statistics_pack, extract_json_block,
                              journal_stats, snapshot_stats)
from scalper.backtest import BacktestConfig, generate_candles
from scalper.engine import Journal
from scalper.history import HistoryStore
from scalper.models import ClosedTrade, Side
from scalper.risk import RiskConfig
from scalper.strategy import StrategyConfig


def _trades():
    out = []
    t0 = 1_700_000_000
    for i in range(30):
        pnl = 12.0 if i % 3 else -10.0
        out.append({"id": str(i), "symbol": "R_75", "side": "buy" if i % 2 else "sell", "volume": 1.0,
                    "entry_price": 100.0, "exit_price": 101.0, "open_time": t0 + i * 600,
                    "close_time": t0 + i * 600 + 180, "pnl": pnl, "reason": "tp" if pnl > 0 else "sl"})
    return out


def test_journal_stats_buckets_and_streaks():
    s = journal_stats(_trades(), 60)
    assert s["trades"] == 30 and s["win_rate"] == 0.667 and s["profit_factor"] == 2.4
    assert s["by_exit_reason"]["sl"]["trades"] == 10 and s["by_exit_reason"]["tp"]["win_rate"] == 1.0
    assert set(s["by_side"]) == {"buy", "sell"} and s["max_consecutive_losses"] == 1
    assert s["avg_bars_held"] == 3.0 and len(s["last_20"]) == 20
    assert journal_stats([], 60) == {"trades": 0}


def test_extract_json_block_takes_last_valid_object():
    text = "intro\n```json\n{\"a\": 1}\n```\nmore\n```\n{\"verdict\": \"paper_only\", \"confidence\": 0.4}\n```\n"
    assert extract_json_block(text) == {"verdict": "paper_only", "confidence": 0.4}
    assert extract_json_block("no json here") is None
    assert extract_json_block("```json\n{broken\n```") is None


def test_statistics_pack_from_files(tmp_path):
    hist = tmp_path / "data"
    store = HistoryStore(str(hist))
    candles = generate_candles(1500, seed=4)
    store.append("R_75", 60, candles)
    for i in range(50):
        store.snapshot("R_75", 60, {"time": candles[i].time, "close": candles[i].close, "ema_fast": 1.0, "ema_slow": 0.9,
                                    "ema_trend": 1.0, "rsi": 50 + i % 10, "atr": 0.5, "spread": 0.05, "bid": 1, "ask": 1})
    journal = tmp_path / "trades.csv"
    j = Journal(str(journal))
    for t in _trades():
        j.write(ClosedTrade(t["id"], t["symbol"], Side(t["side"]), t["volume"], t["entry_price"], t["exit_price"],
                            t["open_time"], t["close_time"], t["pnl"], t["reason"]))
    pack = build_statistics_pack("R_75", 60, StrategyConfig(), RiskConfig(), BacktestConfig(instrument="multiplier"),
                                 str(journal), str(hist))
    assert pack["journal"]["trades"] == 30
    assert pack["history"]["bars"] == 1500
    assert pack["indicator_snapshots"]["rows"] == 50 and pack["indicator_snapshots"]["spread_to_atr"]["median"] == 0.1
    assert pack["backtest_on_history"]["trades"] > 0 and "exit_reasons" in pack["backtest_on_history"]
    assert "sl_atr_mult" in pack["strategy_config"] and "risk_per_trade_pct" in pack["risk_config"]
    assert snapshot_stats(str(tmp_path / "nope.csv")) == {"rows": 0}


class FakeStream:
    def __init__(self, text, stop_reason="end_turn"):
        self.text = text
        self.msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                                   stop_details=SimpleNamespace(category="x", explanation="nope"),
                                   usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0),
                                   model="claude-opus-5-5")

    @property
    def text_stream(self):
        yield self.text[: len(self.text) // 2]
        yield self.text[len(self.text) // 2:]

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.calls = []
        self._text, self._stop = text, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        yield FakeStream(self._text, self._stop)


def test_analyze_with_claude_parses_suggestions_and_sends_expected_request():
    report = ("## Verdict\npaper only.\n\n```json\n" + json.dumps({
        "verdict": "paper_only", "confidence": 0.55,
        "suggested_strategy_changes": {"tp_atr_mult": 2.0, "trade_hours": "07:00-20:00"},
        "suggested_risk_changes": {}, "summary": "losses cluster at night"}) + "\n```")
    client = FakeClient(report)
    pack = {"symbol": "R_75", "journal": {"trades": 30}}
    chunks = []
    text, sugg, usage = analyze_with_claude(pack, client=client, on_text=chunks.append)
    assert text == report and "".join(chunks) == report
    assert sugg["suggested_strategy_changes"]["tp_atr_mult"] == 2.0 and sugg["verdict"] == "paper_only"
    assert usage["output_tokens"] == 200 and usage["model"] == "claude-opus-5-5"
    req = client.calls[0]
    assert req["model"] == "claude-opus-5-5" and req["system"] == SYSTEM_PROMPT
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["output_config"] == {"effort": "high"} and "thinking" not in req
    assert '"R_75"' in req["messages"][0]["content"]


def test_analyze_with_claude_refusal_raises():
    import pytest
    client = FakeClient("", stop_reason="refusal")
    with pytest.raises(RuntimeError, match="declined"):
        analyze_with_claude({"symbol": "X"}, client=client)
