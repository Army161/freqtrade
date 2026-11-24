"""tcc command line.

  tcc up                 run the station (orchestrator + API + dashboard) in the foreground
  tcc research           run one research cycle now (Stage 1 + Stage 2 + Hummingbot)
  tcc status             print a status summary
  tcc leaderboard        top strategies
  tcc kill / reset-kill  kill switch
  tcc plan --capital 20  what a small account can realistically do
  tcc download           refresh OHLCV data for the configured pairs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys


def main(argv=None):
    ap = argparse.ArgumentParser("tcc")
    ap.add_argument("-c", "--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    r = sub.add_parser("research")
    r.add_argument("--population", type=int)
    r.add_argument("--generations", type=int)
    r.add_argument("--no-hummingbot", action="store_true")
    sub.add_parser("status")
    lb = sub.add_parser("leaderboard")
    lb.add_argument("--status", default=None)
    lb.add_argument("-n", type=int, default=20)
    sub.add_parser("kill")
    sub.add_parser("reset-kill")
    pl = sub.add_parser("plan")
    pl.add_argument("--capital", type=float, default=20)
    pl.add_argument("--min-order", type=float, default=5)
    sub.add_parser("download")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .config import ROOT, load_config
    env = ROOT / ".env"
    if env.exists():  # simple KEY=VALUE loader; real environment variables win
        import os
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())
    cfg = load_config(a.config)

    if a.cmd == "up":
        import uvicorn

        from .api.server import create_app
        app = create_app(a.config)
        uvicorn.run(app, host=cfg["api"]["host"], port=cfg["api"]["port"], log_level="warning")
        return

    if a.cmd == "plan":
        from .risk.sizing import small_account_plan
        print(json.dumps(small_account_plan(a.capital, a.min_order), indent=2))
        return

    if a.cmd == "download":
        from .data import download_cmd
        sys.exit(subprocess.call(download_cmd(cfg)))

    from .orchestrator import Orchestrator
    orch = Orchestrator(cfg)
    S = orch.store
    if a.cmd == "research":
        if a.population:
            cfg["research"]["population"] = a.population
        if a.generations:
            cfg["research"]["generations"] = a.generations
        if a.no_hummingbot:
            cfg["clusters"]["hummingbot"]["enabled"] = False
        print(json.dumps(asyncio.run(orch.research()), indent=2, default=str))
    elif a.cmd == "status":
        last = orch.risk.state.last
        print(json.dumps({"mode": cfg["station"]["mode"], "risk": last,
                          "bots": S.query("SELECT name, engine, status, port, restarts FROM bots"),
                          "genomes": S.query("SELECT status, COUNT(*) n FROM genomes GROUP BY status")},
                         indent=2, default=str))
    elif a.cmd == "leaderboard":
        for r in S.leaderboard(a.status, a.n):
            oos = r.get("oos_metrics") or {}
            ft = r.get("ft_metrics") or {}
            print(f"{r['status']:11s} {r['id']} dsr={r['dsr'] or 0:.2f} "
                  f"oosSharpe={oos.get('sharpe', 0):+.2f} ftProfit={ft.get('profit_total', 0):+.2%} "
                  f"| {r['description']}")
    elif a.cmd == "kill":
        asyncio.run(orch.risk.kill("manual (cli)"))
    elif a.cmd == "reset-kill":
        orch.risk.reset_kill()


if __name__ == "__main__":
    main()
