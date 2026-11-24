"""Stage 1: vectorized + numba-compiled strategy screener.

Evaluates thousands of genomes per hour on a laptop-class CPU. It is deliberately a
*screen*: it approximates fills (next-bar open entry, stop-before-target on the same bar,
fees + slippage on both sides) so that only survivors get the slower, exact
Freqtrade event-driven backtest in Stage 2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from .indicators import compute_signals

TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


@njit(cache=True)
def _simulate(o, h, l, c, entry, exit_sig, lo_idx, hi_idx, tp, sl, hold, trail, cost):
    n = len(c)
    out_e = np.empty(n, np.int64)
    out_x = np.empty(n, np.int64)
    out_r = np.empty(n, np.float64)
    k = 0
    i = lo_idx
    while i < hi_idx - 1:
        if not entry[i]:
            i += 1
            continue
        ei = i + 1  # fill at next bar open (Freqtrade semantics)
        ep = o[ei]
        if ep <= 0:
            i += 1
            continue
        peak = ep
        xp = c[min(ei + hold, n - 1)]
        xi = min(ei + hold, n - 1)
        last = min(ei + hold, hi_idx - 1, n - 1)
        for j in range(ei, last + 1):
            stop = (peak if trail else ep) * (1.0 - sl)
            if l[j] <= stop:
                xp = min(stop, o[j]) if j > ei else stop
                xi = j
                break
            if h[j] >= ep * (1.0 + tp):
                xp = max(ep * (1.0 + tp), o[j]) if j > ei else ep * (1.0 + tp)
                xi = j
                break
            if exit_sig[j] and j > ei:
                xp = c[j]
                xi = j
                break
            if h[j] > peak:
                peak = h[j]
            if j == last:
                xp = c[j]
                xi = j
        out_e[k] = ei
        out_x[k] = xi
        out_r[k] = xp / ep - 1.0 - cost
        k += 1
        i = xi + 1
    return out_e[:k], out_x[:k], out_r[:k]


@dataclass(eq=False)
class DataStore:
    datadir: Path
    pairs: list[str]

    def __post_init__(self):
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._ns: dict[tuple[str, str], np.ndarray] = {}

    def dates(self, pair: str, tf: str) -> np.ndarray:
        self.load(pair, tf)
        return self._ns[(pair, tf)]

    def load(self, pair: str, tf: str) -> pd.DataFrame:
        key = (pair, tf)
        if key not in self._cache:
            f = Path(self.datadir) / f"{pair.replace('/', '_')}-{tf}.feather"
            df = pd.read_feather(f)
            df["date"] = pd.to_datetime(df["date"], utc=True)
            df = df.reset_index(drop=True)
            self._ns[key] = df["date"].dt.tz_convert(None).to_numpy().astype("datetime64[ns]")
            self._cache[key] = df
        return self._cache[key]

    def span(self, tf: str = "1h") -> tuple[pd.Timestamp, pd.Timestamp]:
        starts, ends = [], []
        for p in self.pairs:
            d = self.load(p, tf)["date"]
            starts.append(d.iloc[0])
            ends.append(d.iloc[-1])
        return max(starts), min(ends)


_SIG_CACHE: dict = {}


def _signals(store: DataStore, pair: str, g: dict):
    key = (pair, g["timeframe"], g["family"], tuple(sorted(g["p"].items())),
           tuple(sorted(g["filters"].items())))
    if key not in _SIG_CACHE:
        if len(_SIG_CACHE) > 4000:
            _SIG_CACHE.clear()
        df = store.load(pair, g["timeframe"])
        e, x = compute_signals(df, g)
        _SIG_CACHE[key] = (e.to_numpy(), x.to_numpy())
    return _SIG_CACHE[key]


def run_genome(store: DataStore, g: dict, start: pd.Timestamp, end: pd.Timestamp,
               fee: float = 0.001, slippage: float = 0.0005, slots: int = 4) -> dict:
    """Simulate genome ``g`` on all pairs within [start, end). Returns metrics dict."""
    tf = g["timeframe"]
    tfm = TF_MINUTES[tf]
    hold = int(g["exit"]["hold"])  # bars of the genome's own timeframe
    cost = 2 * fee + 2 * slippage
    rows = []
    for pair in store.pairs:
        df = store.load(pair, tf)
        dates = store.dates(pair, tf)
        lo = int(np.searchsorted(dates, np.datetime64(start.tz_convert(None).to_datetime64(), "ns")))
        hi = int(np.searchsorted(dates, np.datetime64(end.tz_convert(None).to_datetime64(), "ns")))
        if hi - lo < 50:
            continue
        ent, ex = _signals(store, pair, g)
        e_idx, x_idx, r = _simulate(
            df["open"].to_numpy(np.float64), df["high"].to_numpy(np.float64),
            df["low"].to_numpy(np.float64), df["close"].to_numpy(np.float64),
            ent, ex, lo, hi, g["exit"]["tp"], g["exit"]["sl"], hold, g["exit"]["trail"], cost,
        )
        if len(r):
            rows.append(pd.DataFrame({
                "pair": pair, "exit_date": dates[x_idx], "ret": r,
                "bars": x_idx - e_idx + 1,
            }))
    days = max(1.0, (end - start).total_seconds() / 86400)
    if not rows:
        return empty_metrics(days)
    trades = pd.concat(rows, ignore_index=True)
    return metrics_from_trades(trades, start, end, slots, tfm)


def empty_metrics(days: float) -> dict:
    return dict(trades=0, ret=0.0, sharpe=0.0, sortino=0.0, max_dd=0.0, win_rate=0.0,
                profit_factor=0.0, avg_trade=0.0, skew=0.0, kurt=3.0, days=days,
                exposure=0.0, n_days=int(days))


def metrics_from_trades(trades: pd.DataFrame, start, end, slots: int, tfm: int) -> dict:
    trades = trades.sort_values("exit_date")
    idx = pd.date_range(start.normalize(), end.normalize(), freq="1D", tz="UTC")
    daily = (trades.assign(d=pd.to_datetime(trades["exit_date"], utc=True).dt.normalize())
             .groupby("d")["ret"].sum() / slots).reindex(idx, fill_value=0.0)
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min())
    mu, sd = daily.mean(), daily.std(ddof=1)
    neg = daily[daily < 0].std(ddof=1)
    wins = trades["ret"][trades["ret"] > 0].sum()
    losses = -trades["ret"][trades["ret"] < 0].sum()
    days = max(1.0, (end - start).total_seconds() / 86400)
    total_bars = days * 1440 / tfm * len(trades["pair"].unique())
    return dict(
        trades=int(len(trades)),
        ret=float(eq.iloc[-1] - 1),
        sharpe=float(mu / sd * math.sqrt(365)) if sd > 0 else 0.0,
        sortino=float(mu / neg * math.sqrt(365)) if neg and neg > 0 else 0.0,
        max_dd=dd,
        win_rate=float((trades["ret"] > 0).mean()),
        profit_factor=float(wins / losses) if losses > 0 else (99.0 if wins > 0 else 0.0),
        avg_trade=float(trades["ret"].mean()),
        skew=float(daily.skew()) if len(daily) > 3 else 0.0,
        kurt=float(daily.kurt() + 3) if len(daily) > 3 else 3.0,
        days=days,
        exposure=float(trades["bars"].sum() / max(1, total_bars)),
        n_days=len(daily),
    )
