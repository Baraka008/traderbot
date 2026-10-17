# Installation guide

Works on Windows, macOS and Linux for Deriv's API and paper trading. The MetaTrader 5 Python
adapter needs **Windows** (the `MetaTrader5` package only exists there); the MQL5 Expert Advisor runs
inside any MT5 terminal on any OS.

## 1. Prerequisites

| Need | Where |
|---|---|
| Python 3.9 or newer | https://www.python.org/downloads/ (tick *Add python.exe to PATH* on Windows) |
| Git (optional) | https://git-scm.com |
| Deriv account + API token | https://app.deriv.com → Settings → API token → scopes **Read + Trade** (start with a **demo** account) |
| HFM MT5 account + terminal | https://www.hfm.com → MetaTrader 5 download; note your login, password and server (e.g. `HFMarketsGlobal-Demo`) |
| Deriv MT5 (optional) | Deriv app → Trader's Hub → Deriv MT5 → create account; server e.g. `Deriv-Demo` / `Deriv-Server` |

## 2. Get the code

```bash
git clone <your-repo-url> scalper-bot
cd scalper-bot
```
(or unzip the folder and `cd` into it).

## 3. Create a virtual environment and install

**Windows (PowerShell):**
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[mt5,dev]"
```
If PowerShell refuses to run the activate script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Verify:
```bash
scalper --version
pytest -q          # 42 tests should pass
```

## 4. Configure

```bash
cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml
```
Credentials come from environment variables so they never live in the YAML:

**Windows (PowerShell, current session):**
```powershell
$env:DERIV_TOKEN  = "your-token"
$env:DERIV_APP_ID = "1089"          # or your own app id from https://api.deriv.com
$env:MT5_LOGIN    = "12345678"
$env:MT5_PASSWORD = "your-password"
$env:MT5_SERVER   = "HFMarketsGlobal-Demo"
```
Persist them with `setx DERIV_TOKEN "your-token"` (new terminals only).

**macOS / Linux:**
```bash
export DERIV_TOKEN=your-token DERIV_APP_ID=1089
```
(or copy `.env.example` to `.env` and load it with `set -a; . ./.env; set +a`).

Edit `config.yaml`:

| Setting | Deriv API | HFM / Deriv MT5 |
|---|---|---|
| `broker` | `deriv` (or `paper` with `paper.data_source: deriv`) | `mt5` (or `paper` with `paper.data_source: mt5`) |
| `symbol` | `R_75`, `R_100`, `1HZ75V`, `frxEURUSD` … | `EURUSD`, `XAUUSD`, `Volatility 75 Index` … exactly as shown in Market Watch |
| `deriv.multiplier` | one of the values from `scalper symbols --check-multipliers` | – |
| `backtest.instrument` | `multiplier` | `cfd` (+ `tick_value` of the symbol) |
| `strategy.trade_hours` | UTC | MT5 **server** time |

## 5. Pre-flight check

```bash
scalper check -c config.yaml
```
This validates the config, connects, prints symbol specs, balance, spread vs ATR, whether the
market is open, and the lot/stake the risk model would use. Fix every `WARN` / `FAIL` before going on.

## 6. Build history, backtest, optimise

```bash
scalper fetch -c config.yaml --count 5000      # saves candles into data/<symbol>_<tf>.csv (re-run to extend)
scalper history -c config.yaml                 # what is stored, gaps, date range
scalper backtest -c config.yaml                # uses the stored history automatically
scalper optimize -c config.yaml --write config.tuned.yaml   # grid search with out-of-sample check
```
Only trade a parameter set that the optimiser marks **ROBUST** (profitable on bars it never saw).

## 6b. Optional: Claude-assisted review

```bash
pip install -e ".[ai]"
export ANTHROPIC_API_KEY=sk-ant-...        # Windows: $env:ANTHROPIC_API_KEY = "sk-ant-..."
scalper analyze -c config.yaml --out review.md --write config.suggested.yaml
```
Claude reads the journal, history and backtest and writes a review with parameter suggestions. Backtest
`config.suggested.yaml` before using it. Claude never trades.

## 7. Paper trade, then go live

```bash
scalper run -c config.yaml                     # broker: paper -> real prices, simulated fills
scalper run -c config.yaml --broker deriv      # live on Deriv
scalper run -c config.yaml --broker mt5        # live on HFM / Deriv MT5 (Windows, terminal running & logged in)
```
Stop with `Ctrl-C`. Logs go to the console (`--log-file bot.log` to keep them), closed trades to
`trades.csv`, candles and indicator snapshots to `data/`.

## 8. MQL5 Expert Advisor (HFM / Deriv MT5, no Python needed)

1. In MT5: *File → Open Data Folder* → `MQL5/Experts/` → copy `mql5/ScalperEA.mq5` there.
2. *Tools → MetaQuotes Language Editor* → open the file → **Compile** (no errors expected).
3. Back in MT5: *View → Navigator → Expert Advisors → ScalperEA* → drag onto an **M1** chart.
4. Tick *Allow Algo Trading* in the dialog and on the toolbar. Inputs mirror `config.yaml`.
5. Test in *View → Strategy Tester* (model: *Every tick based on real ticks*) before a live account.

## 9. Run it unattended (optional)

* **Windows:** Task Scheduler → run `C:\path\scalper-bot\.venv\Scripts\scalper.exe run -c C:\path\config.yaml --log-file bot.log` at logon.
* **Linux (systemd):**
  ```ini
  [Service]
  WorkingDirectory=/opt/scalper-bot
  EnvironmentFile=/opt/scalper-bot/.env
  ExecStart=/opt/scalper-bot/.venv/bin/scalper run -c config.yaml --log-file bot.log
  Restart=always
  ```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `broker error: Deriv API token is required` | export `DERIV_TOKEN` in the same terminal before running |
| `multiplier 100 not available for X` | run `scalper symbols -c config.yaml --check-multipliers`, pick a listed value |
| `MetaTrader5 package not available` | Windows only; `pip install MetaTrader5`; MT5 terminal must be installed and logged in |
| `MT5 symbol 'EURUSD' not found` | the symbol name must match Market Watch exactly (some brokers add suffixes like `EURUSD.a`) |
| `entries blocked: spread_too_wide` | spread > 25 % of ATR; trade a tighter-spread symbol/time or raise `max_spread_atr_ratio` |
| `entries blocked: market_closed` | forex closed (weekend) or symbol suspended; synthetics trade 24/7 |
| `signal skipped: computed volume is 0` | balance too small for the minimum lot/stake at your risk %; see `scalper check` |
| `quote feed stale` | terminal disconnected or the API stopped ticking; the bot reconnects automatically |
