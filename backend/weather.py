"""NWS (api.weather.gov) forecast and observation fetcher.

For each city we take the hourly forecast and compute the max temperature over
each local calendar day — a proxy for the daily climate-report high that
settles Kalshi's temperature markets.

We also track the running high actually observed today at each market's
settlement station. Those observations come from the same ASOS instrument
that produces the climate-report high, so the settled daily high can never
come in below the running observed high — buckets under it are dead.
"""

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

USER_AGENT = "algo-lab-weather-trader (github.com/samgeng14/Algo-Lab)"
_CACHE_TTL = 15 * 60  # NWS hourly forecasts update roughly hourly
_OBS_CACHE_TTL = 5 * 60  # station observations land every ~5-20 minutes


class WeatherService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._grid_urls: dict[tuple, str] = {}
        self._cache: dict[tuple, tuple[float, object]] = {}

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

    def running_high(self, station: str, tz: str) -> float | None:
        """Max temperature (deg F) observed at `station` so far today, local time.

        Returns None when the station has no usable observations yet (e.g.
        shortly after local midnight).
        """
        key = ("obs", station)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < _OBS_CACHE_TTL:
            return cached[1]

        midnight = dt.datetime.now(ZoneInfo(tz)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        r = self.session.get(
            f"https://api.weather.gov/stations/{station}/observations",
            params={"start": midnight.isoformat(timespec="seconds"), "limit": 200},
            timeout=20,
        )
        r.raise_for_status()
        temps_c = [
            t["value"]
            for f in r.json().get("features", [])
            if (t := f.get("properties", {}).get("temperature")) and t.get("value") is not None
        ]
        high = round(max(temps_c) * 9 / 5 + 32, 1) if temps_c else None
        self._cache[key] = (time.time(), high)
        return high
