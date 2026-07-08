"""Kalshi Trade API v2 client.

Public market data needs no auth. Trading endpoints (live mode) sign each
request with the account's RSA key per Kalshi's API-key scheme.
"""

import base64
import datetime as dt
import logging
import time
import uuid

import requests

log = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self, api_base: str, api_key_id: str = "", private_key_path: str = ""):
        self.api_base = api_base.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = None
        if api_key_id and private_key_path:
            from cryptography.hazmat.primitives import serialization

            with open(private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        self.session = requests.Session()

    # ---- auth ----

    def _auth_headers(self, method: str, path: str) -> dict:
        if self._private_key is None:
            raise RuntimeError("Kalshi API credentials not configured (needed for live trading)")
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _request(self, method: str, path: str, *, params=None, json_body=None, auth=False):
        url = self.api_base + path
        headers = {"Accept": "application/json"}
        if auth:
            # Signature covers the path without the query string.
            sign_path = "/trade-api/v2" + path
            headers.update(self._auth_headers(method, sign_path))
        resp = self.session.request(
            method, url, params=params, json=json_body, headers=headers, timeout=20
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---- public market data ----

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> list:
        """All markets in a series, following pagination."""
        markets, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                return markets

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")["market"]

    # ---- authed portfolio endpoints (live mode) ----

    def get_balance_cents(self) -> int:
        return self._request("GET", "/portfolio/balance", auth=True)["balance"]

    def create_order(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        """Place a limit buy order. side is 'yes' or 'no'."""
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
        }
        log.info("LIVE ORDER: %s", body)
        return self._request("POST", "/portfolio/orders", json_body=body, auth=True)


def event_date_from_ticker(event_ticker: str) -> dt.date | None:
    """Parse the settlement date out of tickers like KXHIGHNY-25JUL08."""
    months = {m: i + 1 for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    )}
    try:
        datepart = event_ticker.split("-")[1]
        year, mon, day = 2000 + int(datepart[:2]), months[datepart[2:5]], int(datepart[5:7])
        return dt.date(year, mon, day)
    except (IndexError, KeyError, ValueError):
        return None


def trading_fee_cents(count: int, price_cents: int) -> int:
    """Kalshi trading fee: ceil(0.07 * count * p * (1-p)), in cents."""
    import math

    p = price_cents / 100.0
    return math.ceil(7 * count * p * (1 - p))
