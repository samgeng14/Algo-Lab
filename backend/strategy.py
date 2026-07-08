"""Forecast-vs-market edge model.

The daily high is modeled as Normal(forecast, sigma), with sigma growing the
further out the settlement date is. Each Kalshi temperature bucket then gets a
model probability, which is compared against the market's ask price (plus
fees) to find positive-expected-value trades.
"""

import datetime as dt
import math
from dataclasses import dataclass

from .kalshi import event_date_from_ticker, trading_fee_cents


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mean) / (sigma * math.sqrt(2))))


def bucket_probability(market: dict, forecast_high: float, sigma: float) -> float | None:
    """P(daily high lands in this market's bucket) under the model.

    Temperature buckets use half-degree-shifted bounds so integer highs fall
    cleanly inside one bucket (e.g. "34-35" means 33.5 <= T < 35.5).
    """
    strike_type = market.get("strike_type")
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")

    if strike_type == "between" and floor_s is not None and cap_s is not None:
        lo, hi = floor_s - 0.5, cap_s + 0.5
        return normal_cdf(hi, forecast_high, sigma) - normal_cdf(lo, forecast_high, sigma)
    if strike_type in ("greater", "greater_or_equal") and floor_s is not None:
        return 1 - normal_cdf(floor_s - 0.5, forecast_high, sigma)
    if strike_type in ("less", "less_or_equal") and cap_s is not None:
        return normal_cdf(cap_s + 0.5, forecast_high, sigma)
    return None


def sigma_for(days_out: int, sigma_by_days_out: list) -> float:
    idx = min(max(days_out, 0), len(sigma_by_days_out) - 1)
    return float(sigma_by_days_out[idx])


def kelly_fraction(p: float, price_dollars: float) -> float:
    """Optimal bankroll fraction for a binary contract bought at `price_dollars`."""
    c = price_dollars
    if c <= 0 or c >= 1:
        return 0.0
    return max(0.0, p - (1 - p) * c / (1 - c))


@dataclass
class Signal:
    ticker: str
    event_ticker: str
    city: str
    title: str
    event_date: str
    side: str  # 'yes' or 'no'
    price_cents: int  # ask we'd pay
    model_prob: float  # model P(side wins)
    edge: float  # model_prob - price - fee, per contract, in dollars
    contracts: int
    stake_cents: int
    fee_cents: int
    forecast_high: float
    sigma: float


def evaluate_market(
    market: dict,
    city_name: str,
    forecast_high: float,
    today_local: dt.date,
    cash_cents: int,
    cfg: dict,
) -> tuple[dict | None, Signal | None]:
    """Score one market. Returns (row for the dashboard, tradable signal or None)."""
    strat = cfg["strategy"]
    event_date = event_date_from_ticker(market.get("event_ticker", ""))
    if event_date is None:
        return None, None
    days_out = (event_date - today_local).days
    if days_out < 0:
        return None, None
    sigma = sigma_for(days_out, strat["sigma_by_days_out"])

    p_yes = bucket_probability(market, forecast_high, sigma)
    if p_yes is None:
        return None, None

    yes_bid, yes_ask = market.get("yes_bid") or 0, market.get("yes_ask") or 0
    no_ask = 100 - yes_bid if yes_bid else 0  # buying NO fills against the YES bid

    def edge_after_fees(prob: float, ask_cents: int) -> float:
        fee = trading_fee_cents(1, ask_cents) / 100.0
        return prob - ask_cents / 100.0 - fee

    candidates = []
    if strat["min_price_cents"] <= yes_ask <= strat["max_price_cents"]:
        candidates.append(("yes", yes_ask, p_yes, edge_after_fees(p_yes, yes_ask)))
    if strat["min_price_cents"] <= no_ask <= strat["max_price_cents"]:
        candidates.append(("no", no_ask, 1 - p_yes, edge_after_fees(1 - p_yes, no_ask)))

    row = {
        "ticker": market["ticker"],
        "city": city_name,
        "title": market.get("yes_sub_title") or market.get("subtitle") or market["ticker"],
        "event_date": event_date.isoformat(),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "model_prob_yes": round(p_yes, 4),
        "forecast_high": forecast_high,
        "sigma": sigma,
        "best_edge": round(max((c[3] for c in candidates), default=0.0), 4),
        "volume": market.get("volume", 0),
    }

    best = max(candidates, key=lambda c: c[3], default=None)
    if not best or best[3] < strat["edge_threshold"]:
        return row, None

    side, ask, prob, edge = best
    price_dollars = ask / 100.0
    kelly = kelly_fraction(prob, price_dollars) * strat["kelly_fraction"]
    stake_cents = int(min(kelly, strat["max_stake_fraction"]) * cash_cents)
    contracts = min(stake_cents // ask, strat["max_contracts"])
    if contracts < 1:
        return row, None

    fee = trading_fee_cents(contracts, ask)
    if contracts * ask + fee > cash_cents:
        contracts -= 1
        if contracts < 1:
            return row, None
        fee = trading_fee_cents(contracts, ask)

    return row, Signal(
        ticker=market["ticker"],
        event_ticker=market.get("event_ticker", ""),
        city=city_name,
        title=row["title"],
        event_date=event_date.isoformat(),
        side=side,
        price_cents=ask,
        model_prob=prob,
        edge=edge,
        contracts=contracts,
        stake_cents=contracts * ask,
        fee_cents=fee,
        forecast_high=forecast_high,
        sigma=sigma,
    )
