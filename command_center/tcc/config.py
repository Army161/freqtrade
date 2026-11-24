"""Station config loading (station.yaml) with safe defaults."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # command_center/

DEFAULTS = {
    "station": {
        "name": "TCC-01", "mode": "paper", "exchange": "okx", "stake_currency": "USDT",
        "pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT",
                  "LINK/USDT", "AVAX/USDT"],
        "state_dir": "state", "freqtrade_root": "..", "freqtrade_bin": "../.venv/bin/freqtrade",
    },
    "capital": {"live_capital": 20.0, "paper_wallet_per_bot": 100.0, "min_order_usd": 5.0},
    "research": {"enabled": True, "interval_minutes": 240, "population": 300, "generations": 10,
                 "n_folds": 4, "oos_frac": 0.25, "top_k_oos": 40, "dsr_threshold": 0.9,
                 "min_trades": 30, "workers": 0, "fee": 0.001, "slippage": 0.0005,
                 "confirm_parallel": 2, "download_days": 730},
    "clusters": {
        "freqtrade": {"max_bots": 6, "base_port": 8101, "max_open_trades": 3, "seed_genomes": []},
        "hummingbot": {"enabled": True, "home": "../../army161/hummingbot", "connector": "okx",
                       "paper_connector": "kraken", "pairs": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
                       "research_jobs": 12, "research_days": 7, "max_nodes": 0},
    },
    "risk": {"tick_seconds": 10, "max_portfolio_drawdown": 0.15, "daily_loss_limit": 0.05,
             "bot_max_drawdown": 0.10, "max_consecutive_losses": 5, "max_exposure": 0.9,
             "heartbeat_timeout_s": 120},
    "agents": {"enabled": True, "interval_minutes": 60, "providers": ["anthropic", "xai"],
               "anthropic_model": "claude-opus-5-5", "anthropic_effort": "medium",
               "xai_model": "grok-4", "min_paper_trades": 15},
    "api": {"host": "127.0.0.1", "port": 8800},
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "station.yaml"
    user = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = _merge(DEFAULTS, user or {})
    st = cfg["station"]
    for k in ("state_dir", "freqtrade_root", "freqtrade_bin"):
        p = Path(st[k])
        st[k] = str(p if p.is_absolute() else (ROOT / p).resolve())
    hb = cfg["clusters"]["hummingbot"]
    p = Path(hb["home"]).expanduser()
    hb["home"] = str(p if p.is_absolute() else (ROOT / p).resolve())
    if st["mode"] not in ("paper", "live"):
        raise ValueError("station.mode must be 'paper' or 'live'")
    return cfg
