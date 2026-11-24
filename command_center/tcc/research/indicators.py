"""Indicator + signal library shared by the vectorized screener and the Freqtrade strategy.

Keeping one implementation is what makes Stage-1 screening results comparable with
Stage-2 Freqtrade confirmation: both engines see exactly the same entry/exit signals.
All functions are causal (only use data up to and including the current bar).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, n)
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def compute_signals(df: pd.DataFrame, g: dict) -> tuple[pd.Series, pd.Series]:
    """Return (entry, exit) boolean Series for genome ``g`` on OHLCV ``df``.

    Long-only spot logic. ``g`` keys are documented in ``genome.py``.
    """
    c = df["close"]
    fam = g["family"]
    p = g["p"]
    if fam == "rsi_revert":
        r = rsi(c, p["len"])
        entry = r < p["lo"]
        exit_ = r > p["hi"]
    elif fam == "ema_cross":
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        entry = _cross_up(f, s)
        exit_ = _cross_up(s, f)
    elif fam == "bb_revert":
        mid = c.rolling(p["len"]).mean()
        sd = c.rolling(p["len"]).std()
        entry = c < (mid - p["std"] * sd)
        exit_ = c > mid
    elif fam == "donchian_break":
        hh = df["high"].rolling(p["len"]).max().shift(1)
        ll = df["low"].rolling(max(2, p["len"] // 2)).min().shift(1)
        entry = c > hh
        exit_ = c < ll
    elif fam == "macd_trend":
        m = ema(c, p["fast"]) - ema(c, p["slow"])
        sig = ema(m, p["signal"])
        entry = _cross_up(m, sig)
        exit_ = _cross_up(sig, m)
    else:
        raise ValueError(f"unknown family {fam}")

    f = g.get("filters", {})
    if f.get("trend_ema", 0):
        entry &= c > ema(c, f["trend_ema"])
    if f.get("adx_min", 0):
        entry &= adx(df) > f["adx_min"]
    if f.get("vol_max", 0):  # skip entries in extreme volatility (ATR% above cap)
        entry &= (atr(df, 14) / c) < f["vol_max"]
    entry &= df["volume"] > 0
    return entry.fillna(False).astype(bool), exit_.fillna(False).astype(bool)
