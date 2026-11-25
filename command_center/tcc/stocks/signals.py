"""Dip scanner: separates *stabilizing* dips from *falling knives*, plus a backtest of the rule.

Rules are fixed up front (not optimised on the data), so the backtest is an honest check
rather than another curve-fit. All signals use only data up to the signal bar's close.

Classification per symbol (daily bars):
  excluded       price < min_price, thin volume, or leveraged/inverse ETF
  no_dip         less than min_drawdown below the 52-week high
  falling_knife  made a new 52-week low in the last 5 sessions, or below a falling 20-day average
  stabilizing    dip + no new low in 5 sessions + a higher low (last 10 days above the prior 20)
  BUY            stabilizing + close back above the 20-day average + RSI recovered from oversold
                 (was < 35 within the last 10 sessions, now > 40)
Trade plan for a BUY: stop = recent swing low - 0.5 ATR, target = entry + 2 x risk, time stop 20 sessions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "MULL", "RAML", "UPRO", "SPXU", "SPXL", "SPXS", "TNA", "TZA",
             "NVDL", "NVDU", "TSLL", "LABU", "LABD", "FNGU", "FNGD", "UVXY", "SVXY", "RGTZ", "CONL",
             "MSTU", "MSTX", "BITX", "ETHU", "AMDL", "PLTU"}


@dataclass
class DipParams:
    min_price: float = 5.0
    min_dollar_volume: float = 20e6     # 20-day average $ volume
    min_drawdown: float = 0.15          # at least 15% below the 52-week high
    rsi_oversold: float = 35.0
    rsi_recovered: float = 40.0
    stop_atr: float = 0.5
    reward_risk: float = 2.0
    time_stop: int = 20                 # trading sessions
    slippage: float = 0.0005


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def features(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    c, h, low = f["close"], f["high"], f["low"]
    f["sma20"] = c.rolling(20).mean()
    f["sma50"] = c.rolling(50).mean()
    f["sma200"] = c.rolling(200).mean()
    f["rsi"] = _rsi(c)
    pc = c.shift(1)
    tr = pd.concat([h - low, (h - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f["hi52"] = h.rolling(252, min_periods=120).max()
    f["lo52"] = low.rolling(252, min_periods=120).min()
    f["dd"] = c / f["hi52"] - 1
    f["new_low5"] = low.rolling(5).min() <= f["lo52"] * 1.0001
    f["low10"] = low.rolling(10).min()
    f["low_prior20"] = low.shift(10).rolling(20).min()
    f["higher_low"] = f["low10"] > f["low_prior20"]
    f["sma20_falling"] = f["sma20"] < f["sma20"].shift(5)
    f["rsi_min10"] = f["rsi"].rolling(10).min()
    f["dollar_vol20"] = (c * f["volume"]).rolling(20).mean()
    return f


def classify_row(r: pd.Series, symbol: str, p: DipParams) -> str:
    if symbol in LEVERAGED:
        return "excluded"
    if r["close"] < p.min_price or not (r["dollar_vol20"] >= p.min_dollar_volume):
        return "excluded"
    if not (r["dd"] <= -p.min_drawdown):
        return "no_dip"
    if r["new_low5"] or (r["close"] < r["sma20"] and r["sma20_falling"]):
        return "falling_knife"
    if not r["higher_low"]:
        return "falling_knife"
    if r["close"] > r["sma20"] and r["rsi_min10"] < p.rsi_oversold and r["rsi"] > p.rsi_recovered:
        return "BUY"
    return "stabilizing"


@dataclass
class ScanRow:
    symbol: str
    name: str
    status: str
    date: str
    close: float
    dd_from_high: float
    hi52: float
    lo52: float
    rsi: float
    vs_sma20: float
    vs_sma200: float | None
    dollar_vol20: float
    stop: float | None
    target: float | None
    risk_pct: float | None
    why: str


def plan(r: pd.Series, p: DipParams) -> tuple[float, float]:
    stop = float(r["low10"] - p.stop_atr * r["atr"])
    entry = float(r["close"])
    stop = min(stop, entry * 0.98)  # at least 2% of room
    return round(stop, 2), round(entry + p.reward_risk * (entry - stop), 2)


def scan_symbol(symbol: str, df: pd.DataFrame, p: DipParams) -> ScanRow | None:
    if len(df) < 130:
        return None
    f = features(df)
    r = f.iloc[-1]
    st = classify_row(r, symbol, p)
    why = {
        "excluded": "leveraged ETF" if symbol in LEVERAGED else
                    f"price < ${p.min_price:.0f} or under ${p.min_dollar_volume / 1e6:.0f}M/day traded",
        "no_dip": f"only {r['dd']:.0%} below 52-week high",
        "falling_knife": "new 52-week low in last 5 sessions" if r["new_low5"] else
                         ("below a falling 20-day average" if r["close"] < r["sma20"] else "no higher low yet"),
        "stabilizing": "higher low in place; waiting for reclaim of 20-day avg + RSI recovery",
        "BUY": "higher low + reclaimed 20-day avg + RSI recovered from oversold",
    }[st]
    stop = target = risk = None
    if st == "BUY":
        stop, target = plan(r, p)
        risk = round(1 - stop / r["close"], 4)
    return ScanRow(symbol=symbol, name=df.attrs.get("name", symbol), status=st, date=str(r["date"].date()),
                   close=round(float(r["close"]), 2), dd_from_high=round(float(r["dd"]), 4),
                   hi52=round(float(r["hi52"]), 2), lo52=round(float(r["lo52"]), 2), rsi=round(float(r["rsi"]), 1),
                   vs_sma20=round(float(r["close"] / r["sma20"] - 1), 4),
                   vs_sma200=round(float(r["close"] / r["sma200"] - 1), 4) if pd.notna(r["sma200"]) else None,
                   dollar_vol20=round(float(r["dollar_vol20"] or 0)), stop=stop, target=target, risk_pct=risk, why=why)


ORDER = {"BUY": 0, "stabilizing": 1, "falling_knife": 2, "no_dip": 3, "excluded": 4}


def scan(frames: dict[str, pd.DataFrame], p: DipParams | None = None) -> list[dict]:
    p = p or DipParams()
    rows = [scan_symbol(s, df, p) for s, df in frames.items()]
    rows = [asdict(r) for r in rows if r]
    rows.sort(key=lambda r: (ORDER[r["status"]], r["dd_from_high"]))
    return rows


def backtest(frames: dict[str, pd.DataFrame], p: DipParams | None = None) -> dict:
    """Event-driven daily backtest of the BUY rule, one position per symbol at a time.

    Entry next open after a BUY close; stop checked before target on the same day (conservative);
    time stop at the close after `time_stop` sessions. Returns trade stats in R (risk) units and %.
    """
    p = p or DipParams()
    trades = []
    for sym, df in frames.items():
        if sym in LEVERAGED or len(df) < 260:
            continue
        f = features(df).reset_index(drop=True)
        o, h, low, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
        i, n = 252, len(f)
        while i < n - 1:
            r = f.iloc[i]
            if classify_row(r, sym, p) != "BUY":
                i += 1
                continue
            stop, _ = plan(r, p)
            entry = o[i + 1] * (1 + p.slippage)
            if entry <= stop:
                i += 1
                continue
            target = entry + p.reward_risk * (entry - stop)
            exit_px, j = None, i + 1
            last = min(i + p.time_stop, n - 1)
            while j <= last:
                if low[j] <= stop:
                    exit_px = min(stop, o[j])
                    break
                if h[j] >= target:
                    exit_px = max(target, o[j])
                    break
                j += 1
            if exit_px is None:
                j = last
                exit_px = c[j]
            exit_px *= (1 - p.slippage)
            ret = exit_px / entry - 1
            trades.append({"symbol": sym, "entry_date": str(f["date"].iloc[i + 1].date()),
                           "exit_date": str(f["date"].iloc[j].date()), "ret": ret,
                           "r_mult": (exit_px - entry) / (entry - stop), "bars": j - i})
            i = j + 1
    if not trades:
        return {"trades": 0}
    t = pd.DataFrame(trades)
    wins, losses = t[t.ret > 0].ret.sum(), -t[t.ret < 0].ret.sum()
    return {
        "trades": len(t), "symbols": int(t.symbol.nunique()),
        "win_rate": round(float((t.ret > 0).mean()), 3),
        "avg_return": round(float(t.ret.mean()), 4), "median_return": round(float(t.ret.median()), 4),
        "avg_r": round(float(t.r_mult.mean()), 3),
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
        "avg_bars_held": round(float(t.bars.mean()), 1),
        "worst": round(float(t.ret.min()), 4), "best": round(float(t.ret.max()), 4),
        "first": t.entry_date.min(), "last": t.exit_date.max(),
        "by_year": {str(k): {"n": int(len(v)), "avg": round(float(v.ret.mean()), 4)}
                    for k, v in t.groupby(t.entry_date.str[:4])},
        "recent": t.sort_values("entry_date").tail(15).to_dict("records"),
    }
