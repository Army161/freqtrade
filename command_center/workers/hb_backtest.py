"""Hummingbot V2 controller backtest worker.

Runs INSIDE the Hummingbot virtualenv (its Cython build is incompatible with the Freqtrade
env), driven by tcc.clusters.hummingbot_cluster over stdin/stdout JSON:

    stdin : {"hb_home": "...", "days": 14, "resolution": "1m", "trade_cost": 0.001,
             "jobs": [{"id": "...", "config": {...controller config...}}, ...]}
    stdout: one JSON object per line: {"id": "...", "metrics": {...}} or {"id": "...", "error": "..."}
"""

import asyncio
import json
import math
import os
import sys
import time


async def main():
    req = json.loads(sys.stdin.read())
    hb_home = req["hb_home"]
    sys.path.insert(0, hb_home)
    os.chdir(hb_home)
    try:  # same optional-dependency patch the upstream backtest scripts apply
        from pyinjective.proto.injective.stream.v2 import query_pb2
        if not hasattr(query_pb2, "OrderFailuresFilter"):
            query_pb2.OrderFailuresFilter = type("OrderFailuresFilter", (), {})
    except ImportError:
        pass
    from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

    end_ts = int(time.time())
    start_ts = end_ts - int(req["days"] * 86400)
    for job in req["jobs"]:
        try:
            cfg = BacktestingEngineBase.get_controller_config_instance_from_dict(
                job["config"], controllers_module="controllers")
            engine = BacktestingEngineBase()
            res = await engine.run_backtesting(cfg, start_ts, end_ts,
                                               backtesting_resolution=req.get("resolution", "1m"),
                                               trade_cost=req.get("trade_cost", 0.001))
            r = res["results"]

            def f(x):
                x = float(x)
                return x if math.isfinite(x) else 0.0
            m = {k: f(r.get(k, 0)) for k in ("net_pnl", "net_pnl_quote", "unrealized_pnl_quote",
                                               "accuracy", "sharpe_ratio", "max_drawdown_pct",
                                               "profit_factor", "total_volume")}
            m["executors"] = int(r.get("total_executors", 0))
            m["with_position"] = int(r.get("total_executors_with_position", 0))
            m["close_types"] = {str(k): int(v) for k, v in (r.get("close_types") or {}).items()}
            # mark-to-market return: realised + unrealised over capital
            cap = float(job["config"].get("total_amount_quote", 1000))
            m["mtm_return"] = (m["net_pnl_quote"] + m["unrealized_pnl_quote"]) / cap
            print(json.dumps({"id": job["id"], "metrics": m}), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"id": job["id"], "error": f"{type(e).__name__}: {e}"[:500]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
