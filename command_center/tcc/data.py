"""Market-data download/refresh via `freqtrade download-data` (incremental when data exists)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .clusters.ft_config import exchange_block


def download_cmd(cfg: dict, days: int | None = None) -> list[str]:
    st, rc = cfg["station"], cfg["research"]
    conf = Path(st["state_dir"]) / "download.json"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(json.dumps({"exchange": exchange_block(st["exchange"], []),
                                "stake_currency": st["stake_currency"], "trading_mode": "spot",
                                "dataformat_ohlcv": "feather"}))
    return [st["freqtrade_bin"], "download-data", "-c", str(conf), "--userdir",
            str(Path(st["freqtrade_root"]) / "user_data"), "--pairs", *st["pairs"],
            "--timeframes", "5m", "1h", "--days", str(days or rc["download_days"])]


def has_data(cfg: dict) -> bool:
    st = cfg["station"]
    d = Path(st["freqtrade_root"]) / "user_data" / "data" / st["exchange"]
    return all((d / f"{p.replace('/', '_')}-{tf}.feather").exists() for p in st["pairs"] for tf in ("5m", "1h"))


def refresh(cfg: dict, timeout: int = 3600) -> int:
    """Append the newest candles (freqtrade only fetches what is missing)."""
    return subprocess.run(download_cmd(cfg), capture_output=True, text=True, timeout=timeout).returncode
