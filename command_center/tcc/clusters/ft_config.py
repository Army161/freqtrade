"""Generate Freqtrade bot configs from genomes (shared by backtests and the live cluster)."""

from __future__ import annotations

import copy
import os


def exchange_block(name: str, pairs: list[str], key: str = "", secret: str = "",
                   password: str = "") -> dict:
    ex = {"name": name, "key": key, "secret": secret, "pair_whitelist": pairs,
          "pair_blacklist": [], "enable_ws": False, "ccxt_config": {}, "ccxt_async_config": {}}
    if password:
        ex["password"] = password
    proxy = os.environ.get("TCC_EXCHANGE_PROXY")  # only needed behind an egress proxy
    if proxy:
        ex["ccxt_config"]["httpsProxy"] = proxy
        ex["ccxt_async_config"]["aiohttp_trust_env"] = True
    return ex


def protections(risk: dict) -> list[dict]:
    """Freqtrade-native protections: the per-bot first line of defence."""
    return [
        {"method": "CooldownPeriod", "stop_duration_candles": 3},
        {"method": "StoplossGuard", "lookback_period_candles": 48, "trade_limit": 3,
         "stop_duration_candles": 24, "only_per_pair": False},
        {"method": "MaxDrawdown", "lookback_period_candles": 288, "trade_limit": 5,
         "stop_duration_candles": 96, "max_allowed_drawdown": risk.get("bot_max_drawdown", 0.10),
         "calculation_mode": "equity"},
        {"method": "LowProfitPairs", "lookback_period_candles": 288, "trade_limit": 4,
         "stop_duration_candles": 144, "required_profit": -0.01},
    ]


def build_config(genome: dict, *, exchange: str, pairs: list[str], stake_currency: str = "USDT",
                 wallet: float = 100.0, max_open_trades: int = 3, dry_run: bool = True,
                 risk: dict | None = None, api: dict | None = None, bot_name: str = "tcc",
                 keys: dict | None = None, fee: float | None = None) -> dict:
    g = copy.deepcopy(genome)
    cfg = {
        "bot_name": bot_name,
        "strategy": "TCCGenome",
        "tcc_genome": g,
        "timeframe": g["timeframe"],
        "minimal_roi": {"0": g["exit"]["tp"]},
        "stoploss": -abs(g["exit"]["sl"]),
        "trailing_stop": bool(g["exit"]["trail"]),
        "max_open_trades": max_open_trades,
        "stake_currency": stake_currency,
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.98,
        "dry_run": dry_run,
        "dry_run_wallet": wallet,
        "cancel_open_orders_on_exit": True,
        "trading_mode": "spot",
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "order_types": {"entry": "limit", "exit": "limit", "emergency_exit": "market",
                        "force_exit": "market", "force_entry": "market",
                        "stoploss": "market", "stoploss_on_exchange": not dry_run},
        "exchange": exchange_block(exchange, pairs, **(keys or {})),
        "pairlists": [{"method": "StaticPairList"}],
        "tcc_protections": protections(risk or {}),
        "internals": {"process_throttle_secs": 5, "heartbeat_interval": 60},
        "dataformat_ohlcv": "feather",
        "initial_state": "running",
    }
    if fee is not None:
        cfg["fee"] = fee
    if api:
        cfg["api_server"] = {
            "enabled": True, "listen_ip_address": "127.0.0.1", "listen_port": api["port"],
            "verbosity": "error", "enable_openapi": False, "jwt_secret_key": api["jwt"],
            "CORS_origins": [], "username": api["username"], "password": api["password"],
        }
    return cfg
