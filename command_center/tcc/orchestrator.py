"""The always-on orchestrator: four independent async loops sharing one SQLite store.

  risk loop      (every ~10 s) : snapshot every bot, enforce limits, supervise/restart processes
  research loop  (every N min) : Stage-1 evolution -> Stage-2 Freqtrade confirmation -> HB research
  agent loop     (every N min) : research director, risk officer, portfolio manager
  equity history               : persisted each risk tick for the dashboard
A failure in one loop is logged and retried; it never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from pathlib import Path

from .agents.agents import PortfolioManager, ResearchDirector, RiskOfficer
from .agents.providers import make_providers
from .clusters.freqtrade_cluster import FreqtradeCluster
from .clusters.hummingbot_cluster import HummingbotCluster, HummingbotNodes
from .config import ROOT
from .research.evolution import SearchConfig, run_search
from .research.ft_confirm import confirm_candidates
from .research.genome import genome_id
from .risk.engine import RiskEngine
from .store import Store

log = logging.getLogger("tcc")

BASELINE_GENOMES = [
    # Transparent, well-known baselines so the cluster trades (paper) from minute one,
    # before research has confirmed anything. They are benchmarks, not recommendations.
    {"family": "rsi_revert", "p": {"len": 14, "lo": 30, "hi": 65},
     "filters": {"trend_ema": 200, "adx_min": 0, "vol_max": 0},
     "exit": {"tp": 0.02, "sl": 0.03, "hold": 48, "trail": False}, "timeframe": "1h"},
    {"family": "ema_cross", "p": {"fast": 12, "slow": 50},
     "filters": {"trend_ema": 0, "adx_min": 20, "vol_max": 0},
     "exit": {"tp": 0.045, "sl": 0.03, "hold": 96, "trail": True}, "timeframe": "1h"},
    {"family": "bb_revert", "p": {"len": 20, "std": 2.5},
     "filters": {"trend_ema": 100, "adx_min": 0, "vol_max": 0.02},
     "exit": {"tp": 0.01, "sl": 0.02, "hold": 48, "trail": False}, "timeframe": "5m"},
]


class Orchestrator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        st = cfg["station"]
        self.state_dir = Path(st["state_dir"])
        self.store = Store(self.state_dir / "tcc.sqlite")
        self.ft = FreqtradeCluster(self.store, cfg, self.state_dir)
        self.hb = HummingbotCluster(self.store, cfg, self.state_dir, ROOT / "workers" / "hb_backtest.py")
        self.hb_nodes = HummingbotNodes(self.hb)
        limits = {k: v for k, v in cfg["risk"].items() if k not in ("tick_seconds", "heartbeat_timeout_s")}
        saved = self.store.kv_get("risk_limits") or {}
        # saved limits may only be TIGHTER than the config file (agents tighten, humans loosen)
        for k, v in saved.items():
            if k in limits and v < limits[k]:
                limits[k] = v
        self.risk = RiskEngine(self.store, limits, self.ft)
        providers = make_providers(cfg["agents"])
        self.director = ResearchDirector(self.store, providers)
        self.officer = RiskOfficer(self.store, providers)
        self.pm = PortfolioManager(self.store, providers)
        self.status = {"research": "idle", "agents": "idle", "started": time.time(),
                       "providers": [p.name for p in providers]}
        self._tasks: list[asyncio.Task] = []
        self._research_lock = asyncio.Lock()

    # ---- bootstrapping ---------------------------------------------------------------
    def seed(self) -> None:
        if self.store.query("SELECT 1 FROM bots WHERE engine='freqtrade' LIMIT 1"):
            return
        seeds = self.cfg["clusters"]["freqtrade"].get("seed_genomes") or BASELINE_GENOMES
        for g in seeds:
            gid = genome_id(g)
            self.store.execute("INSERT OR IGNORE INTO genomes(id,genome,description,created,status) "
                               "VALUES(?,?,?,?,?)", (gid, __import__("json").dumps(g),
                                                    "baseline", time.time(), "incubating"))
            self.ft.deploy(gid, g, label="baseline")

    # ---- loops -------------------------------------------------------------------------
    async def risk_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.ft.supervise)
                if self.cfg["clusters"]["hummingbot"]["enabled"]:
                    await asyncio.to_thread(self.hb_nodes.supervise)
                summary = await self.risk.tick()
                self.store.kv_set("last_bot_snaps", summary["bots"])
            except Exception as e:  # noqa: BLE001
                log.exception("risk loop")
                self.store.event("error", "orchestrator", f"risk loop error: {e}")
            await asyncio.sleep(self.cfg["risk"]["tick_seconds"])

    def _research_once(self) -> dict:
        rc, st = self.cfg["research"], self.cfg["station"]
        weights = self.store.kv_get("family_weights", {})
        sc = SearchConfig(
            datadir=str(Path(st["freqtrade_root"]) / "user_data" / "data" / st["exchange"]),
            pairs=st["pairs"], population=rc["population"], generations=rc["generations"],
            n_folds=rc["n_folds"], oos_frac=rc["oos_frac"], top_k_oos=rc["top_k_oos"],
            min_trades=rc["min_trades"], dsr_threshold=rc["dsr_threshold"], fee=rc["fee"],
            slippage=rc["slippage"], workers=rc["workers"] or max(1, (os.cpu_count() or 2) - 1),
            family_weights=weights, seed=random.randrange(1 << 30))
        summary = run_search(sc, self.store, log=log.info)
        self.status["research"] = "confirming with freqtrade"
        confirm_candidates(
            self.store, freqtrade_bin=st["freqtrade_bin"],
            userdir=Path(st["freqtrade_root"]) / "user_data", exchange=st["exchange"],
            pairs=st["pairs"], wallet=self.cfg["capital"]["paper_wallet_per_bot"],
            max_open_trades=self.cfg["clusters"]["freqtrade"]["max_open_trades"], fee=rc["fee"],
            risk=self.cfg["risk"], parallel=rc["confirm_parallel"], log=log.info)
        hb = self.cfg["clusters"]["hummingbot"]
        if hb["enabled"] and self.hb.available():
            self.status["research"] = "hummingbot backtests"
            summary["hummingbot"] = self.hb.run_research(hb["research_jobs"], hb["research_days"],
                                                         log=log.info)
        return summary

    async def research(self) -> dict:
        if self._research_lock.locked():
            return {"busy": True}
        async with self._research_lock:
            self.status["research"] = "running evolution"
            try:
                return await asyncio.to_thread(self._research_once)
            except Exception as e:  # noqa: BLE001
                log.exception("research")
                self.store.event("error", "research", f"research cycle failed: {e}")
                return {"error": str(e)}
            finally:
                self.status["research"] = "idle"
                self.status["research_last"] = time.time()

    async def research_loop(self):
        await asyncio.sleep(20)
        while True:
            if self.cfg["research"]["enabled"]:
                await self.research()
            await asyncio.sleep(self.cfg["research"]["interval_minutes"] * 60)

    async def agents(self) -> dict:
        self.status["agents"] = "running"
        try:
            out = {
                "research_director": await asyncio.to_thread(self.director.run),
                "risk_officer": await asyncio.to_thread(self.officer.run, self.risk),
                "portfolio_manager": await asyncio.to_thread(
                    self.pm.run, self.ft, self.cfg["clusters"]["freqtrade"]["max_bots"],
                    self.cfg["agents"]["min_paper_trades"],
                    self.hb_nodes if self.hb.available() and self.cfg["clusters"]["hummingbot"]["enabled"] else None,
                    self.cfg["clusters"]["hummingbot"]["max_nodes"]),
            }
            return out
        except Exception as e:  # noqa: BLE001
            log.exception("agents")
            self.store.event("error", "agents", f"agent cycle failed: {e}")
            return {"error": str(e)}
        finally:
            self.status["agents"] = "idle"
            self.status["agents_last"] = time.time()

    async def agent_loop(self):
        await asyncio.sleep(60)
        while True:
            if self.cfg["agents"]["enabled"]:
                await self.agents()
            await asyncio.sleep(self.cfg["agents"]["interval_minutes"] * 60)

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self):
        mode = self.cfg["station"]["mode"]
        if mode == "live" and os.environ.get("TCC_LIVE") != "1":
            self.store.event("warning", "orchestrator",
                             "station.mode=live but TCC_LIVE!=1 -> running in PAPER mode")
        self.store.event("info", "orchestrator", f"station starting (mode={mode}, "
                         f"agents={self.status['providers']})")
        if not self.risk.state.killed:
            await asyncio.to_thread(self.seed)
        self._tasks = [asyncio.create_task(c()) for c in
                       (self.risk_loop, self.research_loop, self.agent_loop)]

    async def stop(self, stop_bots: bool = False):
        for t in self._tasks:
            t.cancel()
        if stop_bots:
            await asyncio.to_thread(self.ft.shutdown)
            await asyncio.to_thread(self.hb_nodes.shutdown)
        self.store.event("info", "orchestrator", "station stopped")
