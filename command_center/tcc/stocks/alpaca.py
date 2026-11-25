"""Minimal Alpaca Trading API client — PAPER ONLY.

The base URL is fixed to paper-api.alpaca.markets. There is deliberately no switch for the
live endpoint in this module: real-money stock trading would be a separate, reviewed change.
Keys come from ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (paper keys from the Alpaca dashboard).
"""

from __future__ import annotations

import os

import httpx

PAPER_BASE = "https://paper-api.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


class AlpacaPaper:
    def __init__(self, key: str | None = None, secret: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        key = key or os.environ.get("ALPACA_API_KEY_ID", "")
        secret = secret or os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not key or not secret:
            raise AlpacaError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set (use PAPER keys)")
        self.c = httpx.Client(base_url=PAPER_BASE, timeout=20, transport=transport,
                              headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"))

    def _req(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise AlpacaError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def clock(self) -> dict:
        return self._req("GET", "/v2/clock")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions")

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"status": "open", "nested": "true", "limit": 500}
        if symbol:
            params["symbols"] = symbol
        return self._req("GET", "/v2/orders", params=params)

    def submit_bracket(self, symbol: str, qty: int, take_profit: float, stop_loss: float,
                       client_order_id: str) -> dict:
        """Market entry + take-profit limit + stop-loss stop, all GTC so the exits survive overnight."""
        if qty < 1:
            raise AlpacaError("qty must be >= 1 (bracket orders need whole shares)")
        return self._req("POST", "/v2/orders", json={
            "symbol": symbol, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "gtc",
            "order_class": "bracket", "client_order_id": client_order_id,
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
        })

    def cancel_order(self, order_id: str) -> None:
        self._req("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol: str) -> dict:
        for o in self.open_orders(symbol):  # release shares held by bracket legs first
            self.cancel_order(o["id"])
        return self._req("DELETE", f"/v2/positions/{symbol}")

    def close_all(self) -> list:
        return self._req("DELETE", "/v2/positions", params={"cancel_orders": "true"})
