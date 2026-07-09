"""NWS (api.weather.gov) forecast fetcher.

For each city we take the hourly forecast and compute the max temperature over
each local calendar day — a proxy for the daily climate-report high that
settles Kalshi's temperature markets.
"""

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

USER_AGENT = "algo-lab-weather-trader (github.com/samgeng14/Algo-Lab)"
_CACHE_TTL = 15 * 60  # NWS hourly forecasts update roughly hourly


class WeatherService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._grid_urls: dict[tuple, str] = {}
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def _hourly_url(self, lat: float, lon: float) -> str:
        key = (lat, lon)
        if key not in self._grid_urls:
            r = self.session.get(
                f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", timeout=20
            )
            r.raise_for_status()
            self._grid_urls[key] = r.json()["properties"]["forecastHourly"]
        return self._grid_urls[key]

    def daily_highs(self, lat: float, lon: float, tz: str) -> dict[dt.date, int]:
        """Forecast max temperature (deg F) for each upcoming local date."""
        key = (lat, lon)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1]

        r = self.session.get(self._hourly_url(lat, lon), timeout=20)
        r.raise_for_status()
        zone = ZoneInfo(tz)
        highs: dict[dt.date, int] = {}
        for period in r.json()["properties"]["periods"]:
            when = dt.datetime.fromisoformat(period["startTime"]).astimezone(zone)
            temp = period.get("temperature")
            if temp is None:
                continue
            day = when.date()
            if day not in highs or temp > highs[day]:
                highs[day] = temp

        self._cache[key] = (time.time(), highs)
        return highs
