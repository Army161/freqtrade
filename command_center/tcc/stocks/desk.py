"""Stock dip desk: daily scan -> risk-sized Alpaca PAPER bracket orders -> exit management.

Runs once per trading day after the close (orders queue for the next open), or on demand.
Every order is a bracket (stop-loss + take-profit attached), so no position is ever unprotected.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from zoneinfo import ZoneInfo

import numpy as np

from ..store import Store
from .alpaca import AlpacaError, AlpacaPaper
from .data import load_bars
from .signals import DipParams, backtest, features, scan

ET = ZoneInfo("America/New_York")
SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_trades (
  client_order_id TEXT PRIMARY KEY, symbol TEXT, qty INTEGER, entry_ref REAL, stop REAL, target REAL,
  signal_date TEXT, submitted REAL, status TEXT, note TEXT
);
"""


class DipDesk:
    def __init__(self, store: Store, cfg: dict, killed=lambda: False):
        self.store = store
        self.cfg = cfg
        self.killed = killed
        self.p = DipParams(**{k: v for k, v in cfg.get("params", {}).items() if hasattr(DipParams, k)})
        store.execute(SCHEMA)  # executescript not needed for a single statement

    # ---- helpers -----------------------------------------------------------------------
    def universe(self) -> list[str]:
        u = []
        for group in self.cfg["universe"].values():
            u.extend(group)
        return list(dict.fromkeys(s.upper() for s in u))

    def broker(self) -> AlpacaPaper | None:
        return AlpacaPaper() if AlpacaPaper.configured() else None

    def due(self) -> bool:
        """True once per weekday, after 16:10 ET, when the market is closed (orders queue for next open)."""
        now = dt.datetime.now(ET)
        if self.store.kv_get("stocks_last_scan_day") == now.date().isoformat():
            return False
        if now.weekday() >= 5 or (now.hour, now.minute) < (16, 10):
            return False
        try:
            b = self.broker()
            if b and b.clock().get("is_open"):
                return False
        except AlpacaError:
            pass
        return True

    # ---- main entry points ---------------------------------------------------------------
    def run_scan(self) -> dict:
        t0 = time.time()
        syms = self.universe()
        frames, errors = load_bars(syms, years=2)
        # use completed sessions only: drop today's still-forming bar while the market is open
        now = dt.datetime.now(ET)
        if now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0):
            today = now.date()
            frames = {k: v[v["date"].dt.date < today] for k, v in frames.items()}
        rows = scan(frames, self.p)
        spy = frames.get("SPY")
        regime = None
        if spy is not None and len(spy) > 200:
            f = features(spy).iloc[-1]
            regime = {"spy_close": round(float(f["close"]), 2), "spy_sma200": round(float(f["sma200"]), 2),
                      "risk_on": bool(f["close"] > f["sma200"])}
        out = {"ts": time.time(), "date": rows[0]["date"] if rows else None, "rows": rows, "errors": errors,
               "regime": regime, "seconds": round(time.time() - t0, 1),
               "counts": {s: sum(r["status"] == s for r in rows)
                          for s in ("BUY", "stabilizing", "falling_knife", "no_dip", "excluded")}}
        self.store.kv_set("stocks_last_scan", out)
        self.store.kv_set("stocks_last_scan_day", dt.datetime.now(ET).date().isoformat())
        c = out["counts"]
        self.store.event("info", "stocks", f"dip scan: {len(rows)} symbols | BUY {c['BUY']} | stabilizing "
                         f"{c['stabilizing']} | falling knives {c['falling_knife']}",
                         buys=[r["symbol"] for r in rows if r["status"] == "BUY"])
        return out

    def run_backtest(self, years: int = 5) -> dict:
        frames, errors = load_bars(self.universe(), years=years)
        res = backtest(frames, self.p)
        if "SPY" in frames:
            s = frames["SPY"]
            first = res.get("first")
            if first:
                s = s[s["date"] >= first]
            res["spy_buy_hold"] = round(float(s["close"].iloc[-1] / s["close"].iloc[0] - 1), 4)
        res["data_errors"] = errors
        self.store.kv_set("stocks_backtest", {"ts": time.time(), **res})
        return res

    def trade(self, scan_out: dict | None = None) -> dict:
        """Manage open positions, then place bracket orders for today's BUY signals (paper)."""
        b = self.broker()
        if not b:
            return {"skipped": "no Alpaca paper keys configured"}
        if not self.cfg.get("auto_trade", True):
            return {"skipped": "auto_trade disabled"}
        scan_out = scan_out or self.store.kv_get("stocks_last_scan")
        acct = b.account()
        equity = float(acct["equity"])
        positions = {p["symbol"]: p for p in b.positions()}
        open_syms = {o["symbol"] for o in b.open_orders()}
        actions = {"closed": [], "submitted": [], "skipped": []}

        # 1) bookkeeping + time stop
        for t in self.store.query("SELECT * FROM stock_trades WHERE status IN ('submitted','open')"):
            sym = t["symbol"]
            if sym in positions:
                if t["status"] == "submitted":
                    self.store.execute("UPDATE stock_trades SET status='open' WHERE client_order_id=?",
                                       (t["client_order_id"],))
                held = int(np.busday_count(t["signal_date"], dt.date.today().isoformat()))
                if held > self.p.time_stop:
                    b.close_position(sym)
                    self.store.execute("UPDATE stock_trades SET status='closed', note=? WHERE client_order_id=?",
                                       (f"time stop after {held} sessions", t["client_order_id"]))
                    actions["closed"].append(sym)
            elif sym not in open_syms and t["status"] == "open":
                self.store.execute("UPDATE stock_trades SET status='closed', note='stop or target filled' "
                                   "WHERE client_order_id=?", (t["client_order_id"],))

        # 2) guards
        peak = max(float(self.store.kv_get("stocks_peak_equity", equity)), equity)
        self.store.kv_set("stocks_peak_equity", peak)
        if self.killed():
            return {**actions, "blocked": "kill switch active"}
        mdd = self.cfg.get("max_drawdown", 0.10)
        if equity < peak * (1 - mdd):
            self.store.event("critical", "stocks", f"paper equity {equity:,.0f} is >{mdd:.0%} below peak "
                             f"{peak:,.0f}: no new stock entries")
            return {**actions, "blocked": "drawdown guard"}
        if not scan_out:
            return {**actions, "blocked": "no scan yet"}

        # 3) entries
        risk_frac = self.cfg.get("risk_per_trade", 0.01)
        regime = scan_out.get("regime") or {}
        if regime and not regime.get("risk_on", True):
            risk_frac /= 2  # SPY below its 200-day average: half size
        slots = self.cfg.get("max_positions", 8) - len(positions) - len(open_syms - set(positions))
        bp = float(acct.get("buying_power", equity))
        for r in [r for r in scan_out["rows"] if r["status"] == "BUY"][: self.cfg.get("max_new_per_day", 3)]:
            sym = r["symbol"]
            if slots <= 0:
                actions["skipped"].append(f"{sym}: max positions")
                continue
            if sym in positions or sym in open_syms:
                actions["skipped"].append(f"{sym}: already held/ordered")
                continue
            per_share = r["close"] - r["stop"]
            qty = math.floor(min(equity * risk_frac / per_share,
                                 equity * self.cfg.get("max_position_pct", 0.10) / r["close"],
                                 bp * 0.95 / r["close"]))
            if qty < 1:
                actions["skipped"].append(f"{sym}: size < 1 share")
                continue
            coid = f"tcc-dip-{sym}-{r['date']}"
            try:
                b.submit_bracket(sym, qty, r["target"], r["stop"], coid)
            except AlpacaError as e:
                actions["skipped"].append(f"{sym}: {e}")
                self.store.event("warning", "stocks", f"order rejected {sym}: {e}")
                continue
            self.store.execute("INSERT OR REPLACE INTO stock_trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (coid, sym, qty, r["close"], r["stop"], r["target"], r["date"], time.time(),
                                "submitted", "paper bracket order"))
            self.store.event("info", "stocks", f"PAPER buy {qty} {sym} ~{r['close']} stop {r['stop']} "
                             f"target {r['target']}")
            actions["submitted"].append({"symbol": sym, "qty": qty, "stop": r["stop"], "target": r["target"]})
            slots -= 1
            bp -= qty * r["close"]
        return actions

    def status(self) -> dict:
        out = {"configured": AlpacaPaper.configured(), "scan": self.store.kv_get("stocks_last_scan"),
               "backtest": self.store.kv_get("stocks_backtest"),
               "trades": self.store.query("SELECT * FROM stock_trades ORDER BY submitted DESC LIMIT 50")}
        b = None
        try:
            b = self.broker()
        except AlpacaError:
            pass
        if b:
            try:
                a = b.account()
                out["account"] = {k: a.get(k) for k in ("equity", "cash", "buying_power", "last_equity", "status")}
                out["positions"] = [{k: p.get(k) for k in ("symbol", "qty", "avg_entry_price", "current_price",
                                                           "unrealized_pl", "unrealized_plpc")} for p in b.positions()]
            except AlpacaError as e:
                out["account_error"] = str(e)
        return out

    def flatten(self) -> list:
        b = self.broker()
        if not b:
            return []
        res = b.close_all()
        self.store.execute("UPDATE stock_trades SET status='closed', note='kill switch' "
                           "WHERE status IN ('submitted','open')")
        self.store.event("critical", "stocks", "kill switch: all paper stock positions closed")
        return res


def dumps(x) -> str:
    return json.dumps(x, default=str, indent=2)
