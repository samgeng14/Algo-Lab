"""The trading engine: scan markets, trade edges, settle finished positions.

Paper and live mode share every step; the only difference is whether a buy
becomes a simulated fill in SQLite or a real limit order on Kalshi (which is
then mirrored into the same local ledger).
"""

import datetime as dt
import logging
import threading
from zoneinfo import ZoneInfo

from .kalshi import KalshiClient, event_date_from_ticker
from .store import Store
from .strategy import evaluate_market
from .weather import WeatherService

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.kalshi = KalshiClient(
            cfg["kalshi"]["api_base"],
            cfg["kalshi"]["api_key_id"],
            cfg["kalshi"]["private_key_path"],
        )
        self.weather = WeatherService()
        self.store = Store(self.mode, round(cfg["starting_bankroll"] * 100))
        self.auto_enabled = cfg.get("cycle_minutes", 0) > 0
        self.last_cycle: dict = {"at": None, "rows": [], "signals": [], "errors": []}
        self._cycle_lock = threading.Lock()

    # ---- one full scan/trade/settle pass ----

    def run_cycle(self) -> dict:
        with self._cycle_lock:
            rows, fills, errors = [], [], []
            self._settle_finished_positions(errors)
            # Never re-enter a market+side we've already traded, even after it
            # settles — one bet per bucket.
            traded = self.store.traded_keys()
            # Buckets of one event are correlated (same day's temperature), so
            # cap how many positions a single event can accumulate.
            event_counts: dict[str, int] = {}
            for pos in self.store.open_positions():
                event = pos["ticker"].rsplit("-", 1)[0]
                event_counts[event] = event_counts.get(event, 0) + 1

            for city in self.cfg["cities"]:
                try:
                    rows_c, fills_c = self._trade_city(city, traded, event_counts)
                    rows.extend(rows_c)
                    fills.extend(fills_c)
                except Exception as e:
                    log.exception("cycle failed for %s", city["name"])
                    errors.append(f"{city['name']}: {e}")

            self.store.record_equity(self._positions_value_cents(rows))
            self.last_cycle = {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "rows": sorted(rows, key=lambda r: r["best_edge"], reverse=True),
                "signals": fills,
                "errors": errors,
            }
            log.info("cycle done: %d markets, %d fills, %d errors",
                     len(rows), len(fills), len(errors))
            return self.last_cycle

    def _trade_city(self, city: dict, traded: set, event_counts: dict) -> tuple[list, list]:
        highs = self.weather.daily_highs(city["lat"], city["lon"], city["tz"])
        today_local = dt.datetime.now(ZoneInfo(city["tz"])).date()
        markets = self.kalshi.get_markets(city["series"])
        rows, fills = [], []

        for market in markets:
            if market.get("status") not in ("active", "open"):
                continue
            event_date = event_date_from_ticker(market.get("event_ticker", ""))
            if event_date is None or event_date not in highs:
                continue
            row, sig = evaluate_market(
                market, city["name"], highs[event_date], today_local,
                self.store.cash_cents(), self.cfg,
            )
            if row:
                rows.append(row)
            if sig is None or (sig.ticker, sig.side) in traded:
                continue  # one position per market+side, ever
            max_per_event = self.cfg["strategy"].get("max_positions_per_event", 2)
            if event_counts.get(sig.event_ticker, 0) >= max_per_event:
                continue

            try:
                if self.mode == "live":
                    self.kalshi.create_order(sig.ticker, sig.side, sig.contracts, sig.price_cents)
                self.store.record_buy(sig, self.mode)
                traded.add((sig.ticker, sig.side))
                event_counts[sig.event_ticker] = event_counts.get(sig.event_ticker, 0) + 1
                fills.append(vars(sig))
                log.info("FILLED %s %s x%d @ %dc (p=%.2f, edge=%.2f)",
                         sig.side.upper(), sig.ticker, sig.contracts, sig.price_cents,
                         sig.model_prob, sig.edge)
            except Exception as e:
                log.exception("order failed for %s", sig.ticker)
                self.last_cycle.setdefault("errors", []).append(f"{sig.ticker}: {e}")
        return rows, fills

    def _settle_finished_positions(self, errors: list):
        for pos in self.store.open_positions():
            try:
                market = self.kalshi.get_market(pos["ticker"])
            except Exception as e:
                errors.append(f"settle check {pos['ticker']}: {e}")
                continue
            result = market.get("result")
            if market.get("status") in ("settled", "finalized") and result in ("yes", "no"):
                self.store.settle_position(pos["ticker"], pos["side"], result, self.mode)
                log.info("SETTLED %s %s -> %s", pos["ticker"], pos["side"], result)

    # ---- valuation ----

    def _positions_value_cents(self, rows: list) -> int:
        """Mark open positions to market using this cycle's quotes."""
        quotes = {r["ticker"]: r for r in rows}
        total = 0
        for pos in self.store.open_positions():
            total += self._mark_cents(pos, quotes.get(pos["ticker"]))
        return total

    @staticmethod
    def _mark_cents(pos: dict, quote: dict | None) -> int:
        if not (quote and quote["yes_bid"] and quote["yes_ask"]):
            return round(pos["qty"] * pos["avg_price_cents"])  # no quote: mark at cost
        mid = (quote["yes_bid"] + quote["yes_ask"]) / 2
        yes_mark = mid if pos["side"] == "yes" else 100 - mid
        return round(pos["qty"] * yes_mark)

    def positions_with_marks(self) -> list[dict]:
        quotes = {r["ticker"]: r for r in self.last_cycle.get("rows", [])}
        out = []
        for pos in self.store.open_positions():
            mark = self._mark_cents(pos, quotes.get(pos["ticker"]))
            cost = round(pos["qty"] * pos["avg_price_cents"])
            pos.update(mark_cents=mark, cost_cents=cost, unrealized_cents=mark - cost)
            out.append(pos)
        return out
