"""Daily OHLCV bars for US stocks/ETFs.

Primary: Alpaca market data (free IEX feed) when ALPACA keys are set.
Fallback: Yahoo Finance chart endpoint (no key; unofficial, fine for daily scans).
"""

from __future__ import annotations

import datetime as dt
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (compatible; tcc-dip-scanner)"}


def yahoo_daily(symbol: str, years: int = 2, client: httpx.Client | None = None) -> pd.DataFrame:
    c = client or httpx.Client(timeout=20, headers=UA)
    r = c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
              params={"range": f"{years}y", "interval": "1d"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"date": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                       "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"], "volume": q["volume"]}).dropna()
    df["date"] = df["date"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    df = df.drop_duplicates("date", keep="last").reset_index(drop=True)
    # Yahoo's daily series can lag the live quote by a session after the close: append the latest
    # session from the quote metadata when it is newer than the last bar.
    m = res["meta"]
    if m.get("regularMarketTime") and m.get("regularMarketPrice"):
        qd = pd.Timestamp(m["regularMarketTime"], unit="s", tz="UTC").tz_convert("America/New_York") \
            .normalize().tz_localize(None)
        if len(df) and qd > df["date"].iloc[-1]:
            px = float(m["regularMarketPrice"])
            df.loc[len(df)] = {"date": qd, "open": px, "high": float(m.get("regularMarketDayHigh") or px),
                               "low": float(m.get("regularMarketDayLow") or px), "close": px,
                               "volume": float(m.get("regularMarketVolume") or 0)}
    df.attrs["name"] = m.get("longName") or m.get("shortName") or symbol
    return df


def alpaca_daily(symbols: list[str], years: int = 2, key: str = "", secret: str = "") -> dict[str, pd.DataFrame]:
    start = (dt.date.today() - dt.timedelta(days=365 * years + 10)).isoformat()
    out: dict[str, list] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 10000,
              "adjustment": "all", "feed": os.environ.get("ALPACA_DATA_FEED", "iex")}
    with httpx.Client(timeout=30, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}) as c:
        while True:
            r = c.get("https://data.alpaca.markets/v2/stocks/bars", params=params)
            r.raise_for_status()
            j = r.json()
            for s, bars in (j.get("bars") or {}).items():
                out[s].extend(bars)
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
    frames = {}
    for s, bars in out.items():
        if not bars:
            continue
        df = pd.DataFrame(bars).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low",
                                                 "c": "close", "v": "volume"})
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("America/New_York").dt.normalize() \
            .dt.tz_localize(None)
        df.attrs["name"] = s
        frames[s] = df[["date", "open", "high", "low", "close", "volume"]]
    return frames


def load_bars(symbols: list[str], years: int = 2, workers: int = 6) -> tuple[dict[str, pd.DataFrame], dict]:
    """Return ({symbol: bars}, {symbol: error}). Uses Alpaca data if keys exist, else Yahoo."""
    key, secret = os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", "")
    errors: dict[str, str] = {}
    if key and secret and os.environ.get("TCC_STOCK_DATA", "alpaca") == "alpaca":
        try:
            frames = alpaca_daily(symbols, years, key, secret)
            missing = [s for s in symbols if s not in frames]
            if not missing:
                return frames, errors
            symbols = missing  # fall through to Yahoo for anything Alpaca's IEX feed lacks
        except httpx.HTTPError as e:
            errors["_alpaca"] = str(e)[:200]
            frames = {}
    else:
        frames = {}
    with httpx.Client(timeout=20, headers=UA) as c:
        def one(s):
            try:
                return s, yahoo_daily(s, years, c), None
            except Exception as e:  # noqa: BLE001
                return s, None, f"{type(e).__name__}: {str(e)[:120]}"
        with ThreadPoolExecutor(workers) as ex:
            for s, df, err in ex.map(one, symbols):
                if df is not None and len(df) > 60:
                    frames[s] = df
                else:
                    errors[s] = err or "not enough history"
    return frames, errors
