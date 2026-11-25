import json

import httpx
import numpy as np
import pandas as pd
import pytest

from tcc.stocks.alpaca import PAPER_BASE, AlpacaError, AlpacaPaper
from tcc.stocks.desk import DipDesk
from tcc.stocks.signals import DipParams, backtest, classify_row, scan
from tcc.store import Store

P = DipParams()


def row(**kw):
    base = dict(close=80.0, dollar_vol20=50e6, dd=-0.30, new_low5=False, sma20=78.0, sma20_falling=False,
                higher_low=True, rsi_min10=30.0, rsi=45.0)
    base.update(kw)
    return pd.Series(base)


def test_classify_rules():
    assert classify_row(row(), "XYZ", P) == "BUY"
    assert classify_row(row(), "SOXL", P) == "excluded"                    # leveraged ETF
    assert classify_row(row(close=3.0), "XYZ", P) == "excluded"            # penny stock
    assert classify_row(row(dollar_vol20=1e6), "XYZ", P) == "excluded"     # illiquid
    assert classify_row(row(dd=-0.05), "XYZ", P) == "no_dip"
    assert classify_row(row(new_low5=True), "XYZ", P) == "falling_knife"
    assert classify_row(row(close=70, sma20_falling=True), "XYZ", P) == "falling_knife"
    assert classify_row(row(higher_low=False), "XYZ", P) == "falling_knife"
    assert classify_row(row(close=77), "XYZ", P) == "stabilizing"          # not back above 20-day avg
    assert classify_row(row(rsi_min10=45), "XYZ", P) == "stabilizing"      # never got oversold


def _bars(n=600, seed=0):
    r = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(r.normal(0, 0.02, n)))
    return pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=n), "open": c * (1 + r.normal(0, .003, n)),
                         "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 1e6)})


def test_scan_and_backtest_run_on_random_walks():
    frames = {f"S{i}": _bars(seed=i) for i in range(8)}
    rows = scan(frames)
    assert len(rows) == 8 and all(r["status"] in ("BUY", "stabilizing", "falling_knife", "no_dip", "excluded")
                                  for r in rows)
    for r in rows:
        if r["status"] == "BUY":
            assert r["stop"] < r["close"] < r["target"]
    res = backtest(frames)
    assert res["trades"] == 0 or (0 <= res["win_rate"] <= 1 and res["worst"] < 1)


# ---- Alpaca client against a mock broker -------------------------------------------------------
def test_alpaca_bracket_payload_and_close():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}"), req.headers))
        if req.method == "GET" and req.url.path == "/v2/orders":
            return httpx.Response(200, json=[{"id": "leg1", "symbol": "AAPL"}])
        return httpx.Response(200, json={"ok": True})

    c = AlpacaPaper("k", "s", transport=httpx.MockTransport(handler))
    assert str(c.c.base_url).rstrip("/") == PAPER_BASE
    c.submit_bracket("AAPL", 3, 210.5, 180.25, "tcc-dip-AAPL-2026-11-24")
    m, path, body, hdr = seen[0]
    assert (m, path) == ("POST", "/v2/orders") and hdr["APCA-API-KEY-ID"] == "k"
    assert body["order_class"] == "bracket" and body["time_in_force"] == "gtc" and body["qty"] == "3"
    assert body["take_profit"]["limit_price"] == "210.50" and body["stop_loss"]["stop_price"] == "180.25"
    seen.clear()
    c.close_position("AAPL")
    assert [(m, p) for m, p, *_ in seen] == [("GET", "/v2/orders"), ("DELETE", "/v2/orders/leg1"),
                                            ("DELETE", "/v2/positions/AAPL")]
    with pytest.raises(AlpacaError):
        c.submit_bracket("AAPL", 0, 1, 1, "x")


# ---- desk sizing / guards with a fake broker ---------------------------------------------------
class FakeBroker:
    def __init__(self, equity=100_000.0, positions=(), orders=()):
        self.equity, self._pos, self._ord, self.submitted, self.closed = equity, list(positions), list(orders), [], []

    def account(self):
        return {"equity": str(self.equity), "buying_power": str(self.equity * 2)}

    def positions(self):
        return [{"symbol": s} for s in self._pos]

    def open_orders(self, symbol=None):
        return [{"symbol": s, "id": s} for s in self._ord]

    def submit_bracket(self, sym, qty, tp, sl, coid):
        self.submitted.append((sym, qty, tp, sl, coid))

    def close_position(self, sym):
        self.closed.append(sym)

    def close_all(self):
        return ["all"]


def _scan(*rows, risk_on=True):
    return {"date": "2026-11-24", "regime": {"risk_on": risk_on},
            "rows": [dict(symbol=s, status=st, close=c, stop=sl, target=tp, date="2026-11-24")
                     for s, st, c, sl, tp in rows]}


CFG = {"universe": {"x": ["AAA"]}, "max_positions": 8, "max_new_per_day": 3, "risk_per_trade": 0.01,
       "max_position_pct": 0.10, "max_drawdown": 0.10, "auto_trade": True}


def desk(tmp_path, broker, killed=False):
    d = DipDesk(Store(tmp_path / "s.db"), CFG, killed=lambda: killed)
    d.broker = lambda: broker
    return d


def test_desk_risk_sizing_and_dedupe(tmp_path):
    b = FakeBroker(positions=["HELD"])
    d = desk(tmp_path, b)
    out = d.trade(_scan(("AAA", "BUY", 100.0, 95.0, 110.0), ("HELD", "BUY", 50.0, 45.0, 60.0),
                        ("CCC", "stabilizing", 10.0, None, None)))
    # 1% of 100k = $1,000 risk / $5 per share = 200 shares, capped at 10% of equity = 100 shares
    assert b.submitted == [("AAA", 100, 110.0, 95.0, "tcc-dip-AAA-2026-11-24")]
    assert any("HELD" in s for s in out["skipped"])


def test_desk_half_size_when_risk_off(tmp_path):
    b = FakeBroker()
    desk(tmp_path, b).trade(_scan(("AAA", "BUY", 100.0, 90.0, 120.0), risk_on=False))
    assert b.submitted[0][1] == 50  # $500 risk / $10 per share


def test_desk_blocked_by_kill_and_drawdown(tmp_path):
    b = FakeBroker()
    assert desk(tmp_path, b, killed=True).trade(_scan(("AAA", "BUY", 100.0, 95.0, 110.0)))["blocked"]
    d = desk(tmp_path, b)
    d.store.kv_set("stocks_peak_equity", 200_000)
    assert d.trade(_scan(("AAA", "BUY", 100.0, 95.0, 110.0)))["blocked"] == "drawdown guard"
    assert b.submitted == []


def test_desk_time_stop_and_flatten(tmp_path):
    b = FakeBroker(positions=["OLD"])
    d = desk(tmp_path, b)
    d.store.execute("INSERT INTO stock_trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("c1", "OLD", 10, 50, 45, 60, "2026-09-01", 0, "open", ""))
    out = d.trade(_scan())
    assert out["closed"] == ["OLD"] and b.closed == ["OLD"]
    assert d.flatten() == ["all"]


def test_no_keys_means_no_trading(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    d = DipDesk(Store(tmp_path / "s.db"), CFG)
    assert "skipped" in d.trade(_scan(("AAA", "BUY", 100.0, 95.0, 110.0)))
