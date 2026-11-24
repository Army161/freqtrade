"""FastAPI control plane + dashboard. Binds to 127.0.0.1 by default.

Mutating endpoints require ``Authorization: Bearer $TCC_API_TOKEN`` when that env var is set
(always set it if you expose the port beyond localhost, e.g. via Tailscale).
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from ..config import load_config
from ..orchestrator import Orchestrator
from ..risk.sizing import small_account_plan

DASH = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def create_app(config_path: str | None = None, start_loops: bool = True) -> FastAPI:
    cfg = load_config(config_path)
    orch = Orchestrator(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_loops:
            await orch.start()
        yield
        await orch.stop(stop_bots=os.environ.get("TCC_STOP_BOTS_ON_EXIT") == "1")

    app = FastAPI(title="Trading Command Center", version="0.1.0", lifespan=lifespan)
    app.state.orch = orch
    S = orch.store

    def auth(authorization: str | None = Header(default=None)):
        token = os.environ.get("TCC_API_TOKEN")
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "missing/invalid bearer token")

    @app.get("/")
    def dashboard():
        return FileResponse(DASH)

    @app.get("/api/overview")
    def overview():
        bots = S.query("SELECT engine, status, COUNT(*) n FROM bots GROUP BY engine, status")
        g = S.query("SELECT status, COUNT(*) n FROM genomes GROUP BY status")
        runs = S.query("SELECT COUNT(*) n, COALESCE(SUM(n_trials),0) trials FROM runs")[0]
        st = cfg["station"]
        live = st["mode"] == "live" and os.environ.get("TCC_LIVE") == "1"
        return {"station": st["name"], "mode": "LIVE" if live else "PAPER",
                "exchange": st["exchange"], "pairs": st["pairs"], "risk": orch.risk.state.last,
                "limits": orch.risk.limits, "bots": bots, "genomes": g, "runs": runs,
                "status": orch.status, "uptime_s": time.time() - orch.status["started"],
                "plan": small_account_plan(cfg["capital"]["live_capital"],
                                           cfg["capital"]["min_order_usd"], 2 * cfg["research"]["fee"])}

    @app.get("/api/bots")
    def bots():
        snaps = S.kv_get("last_bot_snaps", {})
        rows = S.query("SELECT b.*, g.description FROM bots b LEFT JOIN genomes g ON g.id=b.genome_id "
                       "ORDER BY b.status, b.name")
        for r in rows:
            r.pop("meta", None)
            r["snap"] = snaps.get(r["name"], {})
        return rows

    @app.get("/api/leaderboard")
    def leaderboard(status: str | None = None, limit: int = 50):
        return S.leaderboard(status, limit)

    @app.get("/api/runs")
    def runs(limit: int = 20):
        return S.query("SELECT id, started, finished, n_trials, summary FROM runs ORDER BY started DESC "
                       "LIMIT ?", (limit,))

    @app.get("/api/events")
    def events(limit: int = 100):
        return S.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/equity")
    def equity(hours: float = 48):
        return S.query("SELECT ts, total, open_trades, realized, unrealized FROM equity WHERE ts > ? "
                       "ORDER BY ts", (time.time() - hours * 3600,))

    @app.get("/api/agents")
    def agents(limit: int = 30):
        return S.query("SELECT * FROM agent_notes ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/bots/{name}/log")
    def bot_log(name: str, n: int = 60):
        if name.startswith("hb-"):
            return {"log": orch.hb_nodes.tail(name, n)}
        f = orch.state_dir / "bots" / name / "bot.log"
        if not f.exists() or "/" in name or ".." in name:
            raise HTTPException(404)
        return {"log": "\n".join(f.read_text(errors="replace").splitlines()[-n:])}

    # ---- actions ------------------------------------------------------------------------
    @app.post("/api/kill", dependencies=[Depends(auth)])
    async def kill(reason: str = "manual kill switch"):
        await orch.risk.kill(reason)
        return {"ok": True}

    @app.post("/api/reset-kill", dependencies=[Depends(auth)])
    def reset_kill():
        orch.risk.reset_kill()
        # killed bots come back: the supervisor respawns anything marked running
        S.execute("UPDATE bots SET status='running', restarts=0 WHERE status='killed'")
        return {"ok": True}

    @app.post("/api/research/run", dependencies=[Depends(auth)])
    async def research_run():
        import asyncio
        if orch.status["research"] != "idle":
            return {"busy": True, "status": orch.status["research"]}
        app.state.research_task = asyncio.create_task(orch.research())  # keep a reference
        return {"started": True}

    @app.post("/api/agents/run", dependencies=[Depends(auth)])
    async def agents_run():
        return await orch.agents()

    @app.post("/api/deploy/{gid}", dependencies=[Depends(auth)])
    def deploy(gid: str):
        if orch.risk.state.killed:
            raise HTTPException(409, "kill switch active")
        g = S.genome(gid)
        if not g:
            raise HTTPException(404)
        if g["genome"].get("engine") == "hummingbot":
            return {"name": orch.hb_nodes.deploy(gid, label="manual")}
        # the ONLY path that can create a real-money bot (station live mode + TCC_LIVE=1 + keys)
        return {"name": orch.ft.deploy(gid, g["genome"], label="manual", allow_live=True)}

    @app.post("/api/bots/{name}/{action}", dependencies=[Depends(auth)])
    async def bot_action(name: str, action: str):
        if name.startswith("hb-"):
            if action in ("stop", "retire"):
                orch.hb_nodes.stop(name, "retired" if action == "retire" else "stopped")
                return {"ok": True}
            raise HTTPException(400, "hummingbot nodes support stop|retire")
        bot = next((b for b in orch.ft.bots() if b["name"] == name), None)
        if not bot:
            raise HTTPException(404)
        c = orch.ft.client(bot)
        if action == "pause":
            return await c.stop_entries()
        if action == "resume":
            return await c.start()
        if action == "exit-all":
            return await c.force_exit_all()
        if action == "retire":
            orch.ft.retire(name, "operator")
            return {"ok": True}
        if action == "stop":
            orch.ft.stop(name)
            return {"ok": True}
        raise HTTPException(400, "action must be pause|resume|exit-all|retire|stop")

    return app
