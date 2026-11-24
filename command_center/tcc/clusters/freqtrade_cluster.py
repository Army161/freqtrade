"""Freqtrade cluster: one supervised `freqtrade trade` process per deployed genome.

Each bot gets its own config, SQLite DB, log file and localhost REST API port. The
command center talks to bots only through the official Freqtrade REST API.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx

from ..store import Store
from .ft_config import build_config


def pid_matches(pid: int, marker: str) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        try:  # no /proc (macOS): fall back to existence check
            os.kill(pid, 0)
            return not Path("/proc").exists()
        except OSError:
            return False
    return marker in cmd


class FtClient:
    def __init__(self, port: int, user: str, pw: str):
        self.base = f"http://127.0.0.1:{port}/api/v1"
        self.auth = (user, pw)

    async def _req(self, method: str, path: str, **kw):
        async with httpx.AsyncClient(timeout=8, trust_env=False) as c:
            r = await c.request(method, self.base + path, auth=self.auth, **kw)
            r.raise_for_status()
            return r.json()

    async def snapshot(self) -> dict:
        profit = await self._req("GET", "/profit")
        balance = await self._req("GET", "/balance")
        status = await self._req("GET", "/status")
        trades = await self._req("GET", "/trades", params={"limit": 20})
        state = await self._req("GET", "/show_config")
        return {"profit": profit, "balance": balance, "open": status,
                "recent": trades.get("trades", []), "state": state.get("state")}

    async def stop_entries(self):
        return await self._req("POST", "/stopentry")

    async def force_exit_all(self):
        return await self._req("POST", "/forceexit", json={"tradeid": "all"})

    async def start(self):
        return await self._req("POST", "/start")

    async def stop(self):
        return await self._req("POST", "/stop")


class FreqtradeCluster:
    engine = "freqtrade"

    def __init__(self, store: Store, cfg: dict, state_dir: Path):
        self.store = store
        self.cfg = cfg
        self.st = cfg["station"]
        self.cl = cfg["clusters"]["freqtrade"]
        self.dir = state_dir / "bots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self._next_restart: dict[str, float] = {}

    # ---- lifecycle --------------------------------------------------------------
    def bots(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM bots WHERE engine='freqtrade' ORDER BY name")
        for r in rows:
            r["meta"] = json.loads(r["meta"] or "{}")
        return rows

    def client(self, bot: dict) -> FtClient:
        return FtClient(bot["port"], bot["meta"]["api_user"], bot["meta"]["api_pass"])

    def _free_port(self) -> int:
        used = {b["port"] for b in self.bots()}
        p = self.cl.get("base_port", 8101)
        while p in used:
            p += 1
        return p

    def _write_config(self, name: str, genome: dict, port: int, meta: dict) -> Path:
        """(Re)generate a bot's config from the CURRENT environment.

        Called on every (re)start so new keys / network settings in .env take effect after a
        restart. A bot's paper/live status is fixed at deploy time (meta["live"]): switching the
        station to live never turns existing paper bots into real-money bots.
        """
        live = bool(meta.get("live"))
        keys = None
        if live:
            keys = {"key": os.environ.get("TCC_EXCHANGE_KEY", ""),
                    "secret": os.environ.get("TCC_EXCHANGE_SECRET", ""),
                    "password": os.environ.get("TCC_EXCHANGE_PASSWORD", "")}
        conf = build_config(
            genome, exchange=self.st["exchange"], pairs=self.st["pairs"],
            stake_currency=self.st.get("stake_currency", "USDT"),
            wallet=self.cfg["capital"]["paper_wallet_per_bot"],
            max_open_trades=self.cl.get("max_open_trades", 3), dry_run=not live,
            risk=self.cfg["risk"], bot_name=name, keys=keys,
            api={"port": port, "username": meta["api_user"], "password": meta["api_pass"],
                 "jwt": meta["jwt"]})
        conf["enable_protections"] = True
        bdir = self.dir / name
        bdir.mkdir(parents=True, exist_ok=True)
        f = bdir / "config.json"
        f.touch(mode=0o600, exist_ok=True)
        os.chmod(f, 0o600)
        f.write_text(json.dumps(conf, indent=1))
        return f

    def deploy(self, genome_id: str, genome: dict, label: str = "", allow_live: bool = False) -> str:
        """Deploy a genome. Paper unless allow_live=True (only the operator's manual deploy passes
        that) AND the station is in live mode. Seeds and agent promotions are always paper."""
        name = f"ft-{genome_id[:8]}"
        if any(b["name"] == name for b in self.bots()):
            return name
        live = allow_live and self.st.get("mode") == "live" and os.environ.get("TCC_LIVE") == "1"
        if live and not (os.environ.get("TCC_EXCHANGE_KEY") and os.environ.get("TCC_EXCHANGE_SECRET")):
            raise RuntimeError("live mode requested but TCC_EXCHANGE_KEY/SECRET are not set")
        port = self._free_port()
        meta = {"api_user": "tcc", "api_pass": secrets.token_urlsafe(16), "jwt": secrets.token_hex(24),
                "label": label, "live": live}
        cfg_path = self._write_config(name, genome, port, meta)
        self.store.execute(
            "INSERT INTO bots(name,engine,genome_id,port,status,config_path,started,meta) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name, "freqtrade", genome_id, port, "starting", str(cfg_path), time.time(), json.dumps(meta)))
        self.store.set_genome_status(genome_id, "live" if live else "incubating")
        self.store.event("info", "cluster", f"deployed {name} on :{port} ({'LIVE' if live else 'paper'})",
                         genome=genome_id, label=label)
        self._spawn(name)
        return name

    def _spawn(self, name: str) -> None:
        bot = next(b for b in self.bots() if b["name"] == name)
        meta = bot["meta"]
        meta.setdefault("jwt", secrets.token_hex(24))  # bots deployed by older versions
        g = self.store.genome(bot["genome_id"])
        bdir = self.dir / name
        if g:
            self._write_config(name, g["genome"], bot["port"], meta)
            self.store.execute("UPDATE bots SET meta=? WHERE name=?", (json.dumps(meta), name))
        root = Path(self.st["freqtrade_root"]).resolve()
        cmd = [self.st["freqtrade_bin"], "trade", "-c", str(bdir / "config.json"),
               "--userdir", str(root / "user_data"),
               "--db-url", f"sqlite:///{bdir / 'trades.sqlite'}",
               "--logfile", str(bdir / "bot.log")]
        log = open(bdir / "stdout.log", "ab")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=root,
                             start_new_session=True)
        self.procs[name] = p
        self.store.execute("UPDATE bots SET pid=?, status='running', started=? WHERE name=?",
                           (p.pid, time.time(), name))

    def stop(self, name: str, final_status: str = "stopped") -> None:
        p = self.procs.pop(name, None)
        pid = p.pid if p else (self.store.query("SELECT pid FROM bots WHERE name=?", (name,)) or [{}])[0].get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            if p:
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(pid, signal.SIGKILL)
        self.store.execute("UPDATE bots SET status=?, pid=NULL WHERE name=?", (final_status, name))
        self.store.event("info", "cluster", f"{name} -> {final_status}")

    def retire(self, name: str, reason: str) -> None:
        bot = next((b for b in self.bots() if b["name"] == name), None)
        self.stop(name, "retired")
        if bot:
            self.store.set_genome_status(bot["genome_id"], "retired")
        self.store.event("warning", "cluster", f"retired {name}: {reason}")

    def supervise(self) -> None:
        """Restart crashed bots with exponential backoff (24/7 operation)."""
        for b in self.bots():
            if b["status"] not in ("running", "starting"):
                continue
            p = self.procs.get(b["name"])
            alive = p is not None and p.poll() is None
            if not alive and p is None and b["pid"]:
                # adopt a bot left running by a previous orchestrator, but only if that PID really
                # is this bot (after a container/host restart PIDs get reused by other processes)
                alive = pid_matches(b["pid"], b["name"])
            if alive:
                if b["restarts"] and time.time() - (b["started"] or 0) > 600:  # healthy 10 min: reset backoff
                    self.store.execute("UPDATE bots SET restarts=0 WHERE name=?", (b["name"],))
                continue
            now = time.time()
            if now < self._next_restart.get(b["name"], 0):
                continue
            delay = min(600, 10 * 2 ** min(6, b["restarts"]))
            self._next_restart[b["name"]] = now + delay
            self.store.execute("UPDATE bots SET restarts=restarts+1 WHERE name=?", (b["name"],))
            self.store.event("warning", "supervisor", f"{b['name']} not running; restarting "
                             f"(attempt {b['restarts'] + 1}, next backoff {delay}s)")
            self._spawn(b["name"])

    def shutdown(self) -> None:
        for name in list(self.procs):
            self.stop(name, "stopped")
