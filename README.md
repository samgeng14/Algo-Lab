# Algo-Lab — Kalshi Weather Trader

An automated trading system for [Kalshi](https://kalshi.com) daily
high-temperature markets, with a web dashboard. It compares National Weather
Service forecasts against market prices and trades when the model sees an edge.

**Paper trading by default.** The paper account simulates fills against real,
live Kalshi market data with a configurable starting bankroll (default **$10**),
so you can watch the strategy run for a while before risking real money.

## How it works

Every cycle (default: every 10 minutes) the engine:

1. **Settles** any open positions whose markets have finalized (win pays $1/contract).
2. **Fetches forecasts** — hourly NWS forecasts for each configured city, reduced
   to a projected daily high per local calendar day.
3. **Scans markets** — all open markets in each city's daily-high series
   (e.g. `KXHIGHNY` for NYC/Central Park).
4. **Models probabilities** — the daily high is treated as
   `Normal(forecast, σ)`, with σ widening for dates further out, giving a model
   probability for every temperature bucket.
5. **Trades edges** — if model probability beats the ask price by more than the
   edge threshold (after Kalshi's fee), it buys YES (or NO when the market looks
   overpriced), sized by fractional Kelly and capped per trade.
6. **Marks to market** and records the equity curve.

Positions are held to settlement (these are same-/next-day markets).

## Quick start (paper trading)

```bash
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8000 — the dashboard shows portfolio value, the equity
curve, open positions, the live market scan with model-vs-market edges, and the
trade log. "Run cycle now" triggers a scan immediately; auto-trading runs in the
background on the configured interval.

The paper ledger lives in `data/portfolio_paper.db`. "Reset paper account"
wipes it back to the starting bankroll.

## Configuration (`config.yaml`)

| Key | Meaning |
|---|---|
| `mode` | `paper` (default) or `live` |
| `starting_bankroll` | paper account starting cash (dollars) |
| `cycle_minutes` | auto-trade interval (0 disables auto) |
| `strategy.edge_threshold` | minimum after-fee edge to trade (0.06 = 6¢/contract) |
| `strategy.kelly_fraction` | fraction of full Kelly to stake |
| `strategy.max_stake_fraction` | max share of cash in one trade |
| `strategy.max_positions_per_event` | max buckets held on one day's temperature (they're correlated) |
| `cities` | which Kalshi series to trade + NWS station coordinates |

## Going live (real money)

> **Warning:** live mode places real orders with real money. Run paper mode
> long enough to trust the strategy first, keep the bankroll small, and expect
> losses — weather markets are competitive and forecasts are public information.

1. Create an API key at kalshi.com → Account → API, save the RSA private key
   file outside the repo (or in `keys/`, which is gitignored).
2. Set credentials via env vars (`KALSHI_API_KEY_ID`,
   `KALSHI_PRIVATE_KEY_PATH`) or in `config.yaml`.
3. Set `mode: live` in `config.yaml` and restart.

Live mode uses the exact same strategy code path; orders are placed as limit
orders at the quoted ask, and the local ledger (`data/portfolio_live.db`)
mirrors them. The server refuses to start in live mode without credentials.

## Project layout

```
backend/
  config.py     load + validate config.yaml
  kalshi.py     Kalshi Trade API v2 client (public data + signed live orders)
  weather.py    NWS hourly forecasts -> projected daily highs
  strategy.py   normal-distribution bucket model, edge + Kelly sizing
  store.py      SQLite ledger: cash, positions, trades, equity curve
  engine.py     the scan/trade/settle cycle
  server.py     Flask API + dashboard host + auto-trade loop
frontend/       dashboard (vanilla JS + SVG)
config.yaml     all knobs
run.py          entry point
```

## Disclaimer

This is an experimental research tool, not financial advice. Trading involves
risk of loss. You are responsible for anything it does with your account.
