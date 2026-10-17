# scalper-bot

A multi-chart scalping bot for **Deriv** and **HFM (HotForex)** with one strategy core and two execution paths:

| Platform | How it connects | Volume unit |
|---|---|---|
| Deriv (native, synthetic indices + forex) | Deriv WebSocket API, multiplier contracts (MULTUP / MULTDOWN) with SL/TP | stake in account currency |
| HFM MT5, Deriv MT5, any MT5 broker | `MetaTrader5` Python package (Windows) **or** the included `mql5/ScalperEA.mq5` Expert Advisor | lots |
| Paper | real prices from either platform, simulated fills | same as data source |

The Python bot and the MQL5 EA implement the same rules, so you can run the EA inside the HFM / Deriv MT5 terminal
and the Python engine against Deriv's API with identical behaviour.

## Strategy (M1 scalping by default)

* **Entry** (evaluated once per closed bar)
  * `cross` mode: EMA(9) crosses EMA(21), close on the right side of EMA(50), RSI(14) inside the momentum band (50–75 long, 25–50 short).
  * `momentum` mode: trend aligned (fast > slow > trend), price pulled back to the fast EMA on the previous bar and closed back in trend direction.
  * Filters: spread ≤ 25 % of ATR, optional absolute spread cap, optional minimum ATR.
* **Exits**
  * Stop-loss 1.0 × ATR, take-profit 1.5 × ATR (both respect the broker's minimum stop distance).
  * Breakeven after 0.8 × ATR of favourable movement, ATR trailing stop after 1.0 × ATR.
  * Time stop after 15 bars, exit on opposite EMA cross.
* **Risk** (`scalper/risk.py`, same in the EA)
  * Position size from `% balance at risk` and the real loss between entry and stop (MT5: `order_calc_profit`; Deriv: stake × multiplier × distance).
  * Daily loss cap (default 3 %), optional daily profit target, max trades per day, max open positions,
    cooldown after a loss, pause after N consecutive losses. Stats survive restarts (MT5 reads deal history, Deriv recovers open contracts).

* **Filters that cut bad trades**: trading session (`trade_hours`, `trade_days`), volatility ceiling (`max_atr_points`),
  higher-timeframe trend confirmation (`htf_multiplier`, e.g. M5 EMA when scalping M1).

Every parameter is in `config.example.yaml`. Step-by-step setup is in [INSTALL.md](INSTALL.md).

## Trading several charts at once

List the charts under `symbols:`; the bot runs one strategy loop per chart over a single broker connection
and takes a position wherever a setup passes the filters:

```yaml
symbols:
  - R_75
  - R_100
  - symbol: frxEURUSD          # per-chart overrides
    multiplier: 500
    strategy: {trade_hours: "07:00-20:00", trade_days: "mon,tue,wed,thu,fri"}
risk:
  max_open_positions: 3        # portfolio-wide cap on simultaneous positions
  max_positions_per_symbol: 1
  max_daily_loss_pct: 3.0      # portfolio-wide: one cap for the whole account
```

The risk manager is shared, so the daily loss cap, trade count, cooldowns and `max_open_positions` apply across
all charts; `max_positions_per_symbol` limits each one. `scalper check` warns when
`max_open_positions × risk_per_trade_pct` exceeds the daily cap. Every command (`fetch`, `backtest`, `optimize`,
`analyze`, `check`) runs over the whole list, or one chart with `--symbol R_75`; `optimize --write` and
`analyze --write` store tuned parameters as per-symbol overrides. For the MQL5 EA, attach it to each chart with
the same magic number: `InpPortfolioLimits` makes the daily limits account-wide and `InpMaxTotalPositions` caps
open positions across charts.

## Chart history, tracking and optimisation

* `scalper fetch` and the live engine both write candles to `data/<symbol>_<tf>.csv` (de-duplicated, gap-checked),
  plus a per-bar indicator snapshot (`*_indicators.csv`: close, EMAs, RSI, ATR, spread) so every decision can be reviewed.
* `scalper history` shows what is stored; `scalper backtest` uses it automatically.
* `scalper optimize` grid-searches the strategy parameters on the first 70 % of the history and re-tests the best sets on
  the remaining 30 % they never saw. Only rows marked **ROBUST** (profitable out-of-sample) are worth trading;
  `--write config.tuned.yaml` saves the best one.
* `scalper check` is a pre-flight: config validation, connectivity, symbol specs, spread vs ATR, market open, position sizing.

## Claude-assisted analysis (optional)

`scalper analyze` compiles everything the bot has recorded (trade journal broken down by exit reason, side, hour
and weekday; indicator snapshot distributions; stored-history quality; a fresh backtest) and asks Claude to review
it: where the losses come from, what is working, which parameters to change and why, and whether the symbol is
worth trading at all. Claude never places trades and is never in the tick-to-order path; it reads statistics after
the fact.

```bash
pip install -e ".[ai]"                       # installs the anthropic SDK
export ANTHROPIC_API_KEY=sk-ant-...          # https://platform.claude.com
scalper analyze -c config.yaml               # streams the review to the terminal
scalper analyze -c config.yaml --out review.md --write config.suggested.yaml
scalper backtest -c config.suggested.yaml    # always verify suggestions before trading them
scalper analyze -c config.yaml --stats-only  # just print the statistics pack, no API call
```

Default model is `claude-opus-5-5` with server-side refusal fallback enabled; `--model`, `--effort` and
`--max-tokens` override. One review costs roughly a few cents of API usage; the token count is printed afterwards.

## Reliability

* Candle data is validated on every bar (duplicates, malformed bars, gaps are logged); a frozen price feed is detected
  and blocks entries; a closed market blocks entries.
* Transient broker errors are logged and retried; after `max_consecutive_errors` the connection is rebuilt with back-off.
  Deriv rate limits are retried automatically; open positions are re-synced from the broker after every reconnect.

## Install

```bash
git clone <this repo> && cd scalper-bot
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .            # adds the `scalper` command
pip install -e ".[mt5]"     # Windows only, for the MT5 adapter
pip install -e ".[dev]" && pytest   # run the test-suite
cp config.example.yaml config.yaml
cp .env.example .env        # fill in tokens, then export them (or use direnv / python-dotenv)
```

## 1. Backtest first

```bash
scalper check -c config.yaml                              # pre-flight
scalper fetch -c config.yaml --count 5000                 # last 5000 M1 candles into data/ (or --broker mt5)
scalper backtest -c config.yaml --trades-out trades.csv   # on the stored history
scalper optimize -c config.yaml --write config.tuned.yaml # find robust parameters for this symbol
```

The backtester shares the live strategy / risk code and evaluates on the same rolling window the live engine sees. It fills at bar close ± spread, checks SL/TP on the following
bars' highs/lows (stop first when both are touched) and reports win rate, profit factor, expectancy and max drawdown.
Set `backtest.instrument` to `multiplier` for Deriv stakes or `cfd` (lots + `tick_value`) for HFM/MT5 symbols.

## 2. Paper trade on live prices

```bash
export DERIV_TOKEN=...          # read-only scope is enough for paper mode
scalper run -c config.yaml      # broker: paper, paper.data_source: deriv
```

Paper mode uses the real tick stream and candles, simulates fills with slippage, and writes every closed trade to
`trades.csv`. Run it for at least a few sessions before going live.

## 3. Live on Deriv

1. Create an API token with **Read + Trade** scope at https://app.deriv.com/account/api-token (demo account first).
2. Optionally register your own app id at https://api.deriv.com and set `DERIV_APP_ID`.
3. Pick a symbol and multiplier that Deriv offers for it:
   `scalper symbols -c config.yaml --market synthetic_index --check-multipliers`
4. Set `broker: deriv`, `symbol: R_75` (or `1HZ75V`, `frxEURUSD`, …), `deriv.multiplier`, then `scalper run -c config.yaml`.

Deriv specifics: `volume` is the stake; the bot converts ATR stop/target distances into the currency amounts
Deriv's `limit_order` expects, caps the stop-loss at the stake and updates SL/TP with `contract_update`. Deriv cannot
hold a stop that sits in profit, so breakeven/trailing levels are kept as a *virtual stop* (hard floor 0.01 on the
server) and the contract is sold the moment the cached spot crosses them. Time-stop / opposite-cross exits also sell
the contract. Open contracts are recovered after a restart.

## 4. Live on HFM (or Deriv MT5)

**Option A – Python engine (Windows + MT5 terminal installed):**

```bash
set MT5_LOGIN=12345678
set MT5_PASSWORD=...
set MT5_SERVER=HFMarketsGlobal-Demo      # Deriv MT5: Deriv-Demo
scalper run -c config.yaml --broker mt5   # symbol: EURUSD, XAUUSD, "Volatility 75 Index", ...
```

The adapter selects the symbol, sizes lots with `order_calc_profit`, tries FOK → IOC → RETURN filling, tags orders with
`mt5.magic`, and books SL/TP hits from the deal history (profit + commission + swap).

**Option B – Expert Advisor (any OS, runs inside the terminal):**

1. Copy `mql5/ScalperEA.mq5` to `<MT5 data folder>/MQL5/Experts/`, open it in MetaEditor and press **Compile**.
2. Drag the EA onto an M1 chart (HFM: EURUSD/GBPUSD/XAUUSD; Deriv MT5: Volatility 75 Index etc.), tick *Allow algo trading*.
3. Inputs mirror `config.example.yaml`. Test it in the Strategy Tester (every tick) before running it on a live account.

## Operations

* Stop with Ctrl-C; set `close_positions_on_exit: true` to flatten on shutdown.
* Connection drops reconnect with exponential back-off; open positions are re-synced from the broker.
* Logs go to stdout (`--log-file` to also write a file); closed trades to `journal_csv`.
* Trading is blocked and the reason logged (`daily_loss_limit`, `cooldown`, `max_trades_per_day`, …) when a risk gate trips.

## Layout

```
scalper/
  strategy.py     entries, exits, breakeven/trailing      risk.py       sizing + daily limits
  engine.py       live loop (poll, reconcile, manage)     backtest.py   simulator + metrics + CSV/synthetic data
  brokers/deriv.py  Deriv WebSocket API (multipliers)     brokers/mt5.py  MetaTrader5 (HFM, Deriv MT5)
  brokers/paper.py  simulated fills on real data          cli.py        run / backtest / fetch / symbols
mql5/ScalperEA.mq5  the same strategy as an MT5 Expert Advisor
scalper/history.py  candle store + indicator snapshots        optimize.py   grid search with out-of-sample check
scalper/analysis.py statistics pack + Claude review (analyze)  engine.MultiEngine  one loop per chart, shared risk
tests/              47 tests: indicators, strategy, risk, backtest, engine+paper, history, optimizer, Deriv protocol
```

## Important

* **No strategy is profitable in all conditions, and nothing here guarantees profit.** Scalping is highly sensitive
  to spread, commission, latency and slippage. The way to make this bot profitable *for your symbol* is the workflow
  above: fetch real history, optimise, keep only ROBUST parameters, paper trade, then size small.
* Deriv multipliers charge a commission on entry and have a *stop-out* level; HFM charges spread/commission per lot.
  Put those into `backtest.commission_per_unit` / `spread_points` so the backtest is honest.
* The Deriv adapter was written against the public API documentation and tested against a scripted fake server;
  the MT5 adapter against the documented `MetaTrader5` package API. Run them on **demo accounts** first and watch the logs.
* Keep API tokens in environment variables, never in the YAML.
