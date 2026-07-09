#!/usr/bin/env python3
"""Backtest the weather strategy on real historical data.

Replays the exact live strategy (backend/strategy.py) day by day:

- market prices: Kalshi's public hourly candlesticks for each settled
  daily-high market (no API key needed)
- forecasts: Open-Meteo's historical-forecast archive, which stores the
  forecast *as it was issued* that day — no lookahead
- settlement: the market's actual result from Kalshi

Each simulated day the strategy sees the morning forecast and the market
prices at the decision hour, buys whatever clears the edge threshold (same
Kelly sizing, fee model, and per-event caps as live), holds to settlement,
and compounds the bankroll.

Usage:
    python backtest.py --start 2026-06-01 --end 2026-06-30 --bankroll 1000

Needs network access to api.elections.kalshi.com and
historical-forecast-api.open-meteo.com.
"""

import argparse
import datetime as dt
import statistics
import sys
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

import requests

from backend.config import load_config
from backend.kalshi import event_date_from_ticker
from backend.strategy import evaluate_market

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO = "https://historical-forecast-api.open-meteo.com/v1/forecast"


# ---------------- data fetchers ----------------

def fetch_settled_markets(session, series: str, start: dt.date, end: dt.date) -> list:
    """All settled markets in a series whose event date falls in [start, end]."""
    min_ts = int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp())
    max_ts = int(dt.datetime.combine(end + dt.timedelta(days=3), dt.time(), dt.timezone.utc).timestamp())
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200,
                  "min_close_ts": min_ts, "max_close_ts": max_ts}
        if cursor:
            params["cursor"] = cursor
        r = session.get(f"{KALSHI}/markets", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return [m for m in markets
            if (d := event_date_from_ticker(m.get("event_ticker", ""))) and start <= d <= end]


def fetch_candles(session, series: str, ticker: str, start_ts: int, end_ts: int) -> list:
    r = session.get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
                    params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60},
                    timeout=30)
    r.raise_for_status()
    return r.json().get("candlesticks", [])


def fetch_forecasts(session, lat: float, lon: float, tz: str,
                    start: dt.date, end: dt.date) -> dict:
    """Day-0 max-temperature forecast for each date, as issued that day."""
    r = session.get(OPEN_METEO, params={
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "timezone": tz, "start_date": start.isoformat(), "end_date": end.isoformat(),
    }, timeout=30)
    r.raise_for_status()
    daily = r.json()["daily"]
    return {dt.date.fromisoformat(d): t
            for d, t in zip(daily["time"], daily["temperature_2m_max"]) if t is not None}


def quote_at(candles: list, decision_ts: int) -> tuple[int, int] | None:
    """(yes_bid, yes_ask) from the last candle at or before decision_ts."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts", 0)
        if ts <= decision_ts:
            best = c
        else:
            break
    if not best:
        return None
    bid = (best.get("yes_bid") or {}).get("close") or 0
    ask = (best.get("yes_ask") or {}).get("close") or 0
    return (bid, ask) if ask else None


# ---------------- backtest core ----------------

def run_backtest(cfg: dict, start: dt.date, end: dt.date, bankroll_cents: int,
                 decision_hour: int, session=None, verbose=True) -> dict:
    session = session or requests.Session()
    strat = cfg["strategy"]
    max_per_event = strat.get("max_positions_per_event", 2)

    # preload all data per city
    city_data = []
    for city in cfg["cities"]:
        try:
            markets = fetch_settled_markets(session, city["series"], start, end)
            forecasts = fetch_forecasts(session, city["lat"], city["lon"], city["tz"], start, end)
        except Exception as e:
            print(f"  ! skipping {city['name']}: {e}", file=sys.stderr)
            continue
        by_date = defaultdict(list)
        for m in markets:
            by_date[event_date_from_ticker(m["event_ticker"])].append(m)
        city_data.append((city, by_date, forecasts))
        if verbose:
            print(f"  {city['name']}: {len(markets)} settled markets, "
                  f"{len(forecasts)} forecast days")

    cash = bankroll_cents
    equity_curve, trades = [], []
    skips: Counter = Counter()
    fetch_errors: Counter = Counter()
    edges_seen: list[float] = []
    day = start
    while day <= end:
        # trade: for each city, evaluate that day's buckets at the decision hour
        signals_today = []
        for city, by_date, forecasts in city_data:
            if day not in forecasts or day not in by_date:
                continue
            zone = ZoneInfo(city["tz"])
            decision_ts = int(dt.datetime.combine(day, dt.time(decision_hour), zone).timestamp())
            for m in by_date[day]:
                try:
                    candles = fetch_candles(session, city["series"], m["ticker"],
                                            decision_ts - 6 * 3600, decision_ts + 60)
                except Exception as e:
                    skips["candle fetch failed"] += 1
                    fetch_errors[f"{type(e).__name__}: {e}"[:120]] += 1
                    continue
                if not candles:
                    skips["no candles in decision window"] += 1
                    continue
                q = quote_at(candles, decision_ts)
                if not q:
                    skips["no ask quote at decision time"] += 1
                    continue
                snapshot = dict(m, yes_bid=q[0], yes_ask=q[1])
                row, sig = evaluate_market(snapshot, city["name"], forecasts[day], day,
                                           cash, cfg)
                if row is None:
                    skips["unparseable market (missing strike/date info)"] += 1
                    continue
                edges_seen.append(row["best_edge"])
                if sig:
                    signals_today.append((sig, m["result"]))
                else:
                    skips["evaluated, edge below threshold"] += 1

        # respect the per-event cap, best edges first, then fill sequentially
        signals_today.sort(key=lambda x: x[0].edge, reverse=True)
        event_counts: dict[str, int] = defaultdict(int)
        for sig, result in signals_today:
            if event_counts[sig.event_ticker] >= max_per_event:
                continue
            cost = sig.stake_cents + sig.fee_cents
            if cost > cash:
                continue
            event_counts[sig.event_ticker] += 1
            won = sig.side == result
            payout = sig.contracts * 100 if won else 0
            cash += payout - cost
            trades.append({"date": day, "ticker": sig.ticker, "city": sig.city,
                           "side": sig.side, "qty": sig.contracts, "price": sig.price_cents,
                           "fee": sig.fee_cents, "edge": sig.edge,
                           "forecast": sig.forecast_high, "model_p": sig.model_prob,
                           "won": won, "pnl": payout - cost})
        equity_curve.append((day, cash))
        day += dt.timedelta(days=1)

    if verbose:
        print("\nmarket funnel:")
        for reason, n in skips.most_common():
            print(f"  {n:5d}  {reason}")
        print(f"  {len(trades):5d}  traded")
        for err, n in fetch_errors.most_common(3):
            print(f"         ({n}x) {err}")
        if edges_seen:
            thr = cfg["strategy"]["edge_threshold"]
            print(f"\nedge distribution over {len(edges_seen)} evaluated markets "
                  f"(threshold {thr:+.2f}):")
            print(f"  max {max(edges_seen):+.3f}   median {statistics.median(edges_seen):+.3f}   "
                  f"above threshold: {sum(e >= thr for e in edges_seen)}")

    return summarize(bankroll_cents, cash, equity_curve, trades, verbose)


def summarize(start_cents: int, end_cents: int, equity_curve, trades, verbose=True) -> dict:
    wins = [t for t in trades if t["won"]]
    fees = sum(t["fee"] for t in trades)
    peak, max_dd = start_cents, 0
    for _, v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    out = {
        "start": start_cents / 100, "end": end_cents / 100,
        "return_pct": 100 * (end_cents - start_cents) / start_cents,
        "trades": len(trades), "wins": len(wins),
        "win_rate": 100 * len(wins) / len(trades) if trades else 0.0,
        "fees": fees / 100, "max_drawdown": max_dd / 100,
        "equity_curve": equity_curve, "trade_log": trades,
    }
    if verbose:
        print(f"\n{'=' * 52}\nBACKTEST RESULT")
        print(f"  bankroll      ${out['start']:.2f} -> ${out['end']:.2f} "
              f"({out['return_pct']:+.1f}%)")
        print(f"  trades        {out['trades']} ({out['wins']} wins, "
              f"{out['win_rate']:.0f}% win rate)")
        print(f"  fees paid     ${out['fees']:.2f}")
        print(f"  max drawdown  ${out['max_drawdown']:.2f}")
        by_city = defaultdict(int)
        for t in trades:
            by_city[t["city"]] += t["pnl"]
        for c, pnl in sorted(by_city.items(), key=lambda x: -x[1]):
            print(f"    {c:28} {pnl / 100:+8.2f}")
        print("=" * 52)
    return out


def main():
    today = dt.date.today()
    first_of_month = today.replace(day=1)
    default_start = (first_of_month - dt.timedelta(days=1)).replace(day=1)
    default_end = first_of_month - dt.timedelta(days=1)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", type=dt.date.fromisoformat, default=default_start,
                    help="first event date (default: start of last month)")
    ap.add_argument("--end", type=dt.date.fromisoformat, default=default_end,
                    help="last event date (default: end of last month)")
    ap.add_argument("--bankroll", type=float, default=1000.0,
                    help="starting bankroll in dollars (default 1000)")
    ap.add_argument("--hour", type=int, default=9,
                    help="local decision hour, 0-23 (default 9am)")
    ap.add_argument("--max-contracts", type=int, default=None,
                    help="override strategy.max_contracts (with a big bankroll the "
                         "config default of 10 binds long before Kelly does)")
    args = ap.parse_args()

    cfg = load_config()
    if args.max_contracts:
        cfg["strategy"]["max_contracts"] = args.max_contracts

    print(f"Backtesting {args.start} .. {args.end}, ${args.bankroll:.2f} bankroll, "
          f"decisions at {args.hour}:00 local\n")
    run_backtest(cfg, args.start, args.end, round(args.bankroll * 100), args.hour)


if __name__ == "__main__":
    main()
