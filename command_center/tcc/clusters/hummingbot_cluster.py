"""Hummingbot cluster: V2-controller research (backtests) + supervised headless paper bots.

Research: random search over controller parameter grids, executed by
``workers/hb_backtest.py`` inside the Hummingbot venv (separate Cython build).
Ranking uses mark-to-market return (realised + unrealised) and Sharpe, NOT the
"accuracy" (win-rate) figure, which market-making controllers inflate trivially.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import time
from pathlib import Path

from ..store import Store

GRIDS = {
    "pmm_mister": {
        "buy_spreads": ["0.0002", "0.0005", "0.001", "0.002"],
        "take_profit": ["0.0002", "0.0005", "0.001", "0.002"],
        "executor_refresh_time": [15, 30, 60, 120],
        "target_base_pct": ["0.3", "0.5"],
    },
    "bollinger_v1": {
        "interval": ["1m", "5m", "15m"], "bb_length": [20, 50, 100], "bb_std": [1.5, 2.0, 2.5, 3.0],
        "bb_long_threshold": [-0.1, 0.0, 0.1], "stop_loss": ["0.01", "0.02", "0.03"],
        "take_profit": ["0.005", "0.01", "0.02", "0.04"], "time_limit": [1800, 3600, 4 * 3600],
    },
    "macd_bb_v1": {
        "interval": ["5m", "15m"], "bb_length": [20, 50, 100], "bb_std": [2.0, 2.5],
        "bb_long_threshold": [-0.1, 0.0, 0.1], "macd_fast": [12, 21], "macd_slow": [26, 42],
        "macd_signal": [9, 12], "stop_loss": ["0.01", "0.02", "0.03"],
        "take_profit": ["0.01", "0.02", "0.04"], "time_limit": [3600, 4 * 3600],
    },
}


def _base(controller: str, connector: str, pair: str, amount: float) -> dict:
    if controller == "pmm_mister":
        return {"id": "", "controller_name": "pmm_mister", "controller_type": "generic",
                "connector_name": connector, "trading_pair": pair, "total_amount_quote": amount,
                "leverage": 1, "portfolio_allocation": "0.02", "min_base_pct": "0.2",
                "max_base_pct": "0.8", "sell_spreads": "0.0005", "buy_amounts_pct": "1",
                "sell_amounts_pct": "1", "buy_cooldown_time": 30, "sell_cooldown_time": 30,
                "buy_position_effectivization_time": 1800, "sell_position_effectivization_time": 1800,
                "price_distance_tolerance": 0.0002, "max_active_executors_by_level": 10,
                "position_profit_protection": True}
    return {"id": "", "controller_name": controller, "controller_type": "directional_trading",
            "connector_name": connector, "trading_pair": pair, "candles_connector": connector,
            "candles_trading_pair": pair, "total_amount_quote": amount, "max_executors_per_side": 2,
            "cooldown_time": 300, "leverage": 1,
            "bb_short_threshold": 99.0}  # spot: long-only (short signal unreachable)


class HummingbotCluster:
    engine = "hummingbot"

    def __init__(self, store: Store, cfg: dict, state_dir: Path, worker: Path):
        self.store = store
        self.cfg = cfg["clusters"]["hummingbot"]
        self.station = cfg["station"]
        self.home = Path(self.cfg["home"]).expanduser().resolve()
        self.python = Path(self.cfg.get("python") or self.home / ".venv/bin/python")
        self.worker = worker
        self.state_dir = state_dir

    def available(self) -> bool:
        return self.python.exists() and (self.home / "controllers").is_dir()

    def sample_jobs(self, n: int, rng: random.Random, controllers: list[str] | None = None) -> list[dict]:
        ctrls = controllers or list(GRIDS)
        pairs = self.cfg.get("pairs") or ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        jobs = []
        for _ in range(n):
            c = rng.choice(ctrls)
            conf = _base(c, self.cfg.get("connector", "okx"), rng.choice(pairs),
                         self.cfg.get("research_amount_quote", 1000))
            conf.update({k: rng.choice(v) for k, v in GRIDS[c].items()})
            if c == "pmm_mister":
                conf["sell_spreads"] = conf["buy_spreads"]
            jid = "hb-" + hashlib.sha1(json.dumps(conf, sort_keys=True).encode()).hexdigest()[:10]
            conf["id"] = jid
            jobs.append({"id": jid, "config": conf})
        return jobs

    def run_research(self, n_jobs: int = 12, days: float = 7, seed: int | None = None, log=print) -> dict:
        if not self.available():
            msg = f"Hummingbot not installed at {self.home} (run scripts/install.sh)"
            self.store.event("warning", "hummingbot", msg)
            return {"error": msg}
        rng = random.Random(seed)
        jobs = self.sample_jobs(n_jobs, rng)
        req = {"hb_home": str(self.home), "days": days, "resolution": self.cfg.get("resolution", "1m"),
               "trade_cost": self.cfg.get("trade_cost", 0.001), "jobs": jobs}
        t0 = time.time()
        p = subprocess.run([str(self.python), str(self.worker)], input=json.dumps(req),
                           capture_output=True, text=True, timeout=3600, cwd=self.home)
        results, errors = [], 0
        conf_by_id = {j["id"]: j["config"] for j in jobs}
        for line in p.stdout.splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in r:
                errors += 1
                continue
            m = r["metrics"]
            # robust score: mark-to-market return, penalised by drawdown; win-rate ignored
            score = m["mtm_return"] * 100 + 0.2 * m["sharpe_ratio"] + 2 * m["max_drawdown_pct"]
            conf = conf_by_id[r["id"]]
            desc = (f"HB {conf['controller_name']} {conf['trading_pair']} " +
                    " ".join(f"{k}={conf[k]}" for k in GRIDS[conf["controller_name"]]))
            ok = m["mtm_return"] > 0 and m["sharpe_ratio"] > 0 and m["max_drawdown_pct"] > -0.1
            self.store.execute(
                "INSERT OR REPLACE INTO genomes(id,genome,description,created,generation,run_id,"
                "is_fitness,is_metrics,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (r["id"], json.dumps({"engine": "hummingbot", **conf}), desc, time.time(), 0,
                 f"hb-{int(t0)}", score, json.dumps(m), "hb_candidate" if ok else "rejected"))
            results.append((score, desc, m))
            log(f"  {'+' if ok else '-'} {desc[:90]} | mtm={m['mtm_return']:+.2%} "
                f"sharpe={m['sharpe_ratio']:.2f} dd={m['max_drawdown_pct']:.2%} "
                f"winrate={m['accuracy']:.0%}")
        summary = {"jobs": len(jobs), "ok": len(results), "errors": errors,
                   "candidates": sum(1 for s, _, m in results if m["mtm_return"] > 0),
                   "seconds": round(time.time() - t0, 1)}
        if p.returncode != 0 and not results:
            summary["stderr"] = p.stderr[-800:]
        self.store.event("info", "hummingbot", f"HB research: {summary['ok']}/{len(jobs)} backtests, "
                         f"{summary['candidates']} profitable mark-to-market", **summary)
        return summary


# ---- live / paper nodes ---------------------------------------------------------------
import os  # noqa: E402
import signal  # noqa: E402

import yaml  # noqa: E402

from .freqtrade_cluster import pid_matches  # noqa: E402

PW_SETUP = (
    "import sys; sys.path.insert(0, '.');"
    "from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, store_password_verification;"
    "from hummingbot.client.config.security import Security;"
    "import os;"
    "Security.new_password_required() and store_password_verification(ETHKeyFileSecretManger(os.environ['HB_PW']))"
)


class HummingbotNodes:
    """Headless Hummingbot processes running V2 controllers (paper connector by default)."""

    def __init__(self, hb: HummingbotCluster):
        self.hb = hb
        self.store = hb.store
        self.procs: dict[str, subprocess.Popen] = {}
        self.password = os.environ.get("TCC_HB_PASSWORD", "tcc-paper-only")
        self._next_restart: dict[str, float] = {}

    def _ensure_password(self) -> None:
        subprocess.run([str(self.hb.python), "-c", PW_SETUP], cwd=self.hb.home, check=True,
                       env={**os.environ, "HB_PW": self.password}, capture_output=True, timeout=120)

    def deploy(self, candidate_id: str, label: str = "") -> str:
        row = self.store.genome(candidate_id)
        if not row:
            raise KeyError(candidate_id)
        conf = {k: v for k, v in row["genome"].items() if k != "engine"}
        live = self.hb.station.get("mode") == "live" and os.environ.get("TCC_LIVE") == "1"
        if not live:  # route orders to Hummingbot's built-in paper exchange
            paper = self.hb.cfg.get("paper_connector") or conf["connector_name"]
            conf["connector_name"] = f"{paper}_paper_trade"
        name = f"hb-{candidate_id[3:11]}"
        ctrl_file = f"tcc_{name}.yml"
        script_file = f"tcc_{name}_script.yml"
        (self.hb.home / "conf/controllers" / ctrl_file).write_text(yaml.safe_dump({**conf, "id": name}))
        (self.hb.home / "conf/scripts" / script_file).write_text(yaml.safe_dump({
            "script_file_name": "v2_with_controllers.py",
            "controllers_config": [ctrl_file],
            "max_global_drawdown_quote": self.hb.cfg.get("max_global_drawdown_quote", 50),
            "max_controller_drawdown_quote": self.hb.cfg.get("max_controller_drawdown_quote", 25)}))
        self._ensure_password()
        self.store.execute(
            "INSERT OR REPLACE INTO bots(name,engine,genome_id,port,status,config_path,started,meta) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name, "hummingbot", candidate_id, None, "starting", script_file, time.time(),
             json.dumps({"label": label, "live": live, "connector": conf["connector_name"]})))
        self.store.set_genome_status(candidate_id, "live" if live else "incubating")
        self._spawn(name, script_file)
        self.store.event("info", "cluster", f"deployed Hummingbot node {name} ({conf['connector_name']})")
        return name

    def _spawn(self, name: str, script_file: str) -> None:
        logdir = self.hb.state_dir / "bots" / name
        logdir.mkdir(parents=True, exist_ok=True)
        log = open(logdir / "stdout.log", "ab")
        launcher = self.hb.worker.parent / "hb_launch.py"  # quickstart + paper-connector shim
        p = subprocess.Popen([str(self.hb.python), str(launcher), "--headless",
                              "-p", self.password, "--v2", script_file],
                             cwd=self.hb.home, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, env={**os.environ, "HEADLESS_MODE": "true"})
        self.procs[name] = p
        self.store.execute("UPDATE bots SET pid=?, status='running', started=? WHERE name=?",
                           (p.pid, time.time(), name))

    def bots(self) -> list[dict]:
        return self.store.query("SELECT * FROM bots WHERE engine='hummingbot' ORDER BY name")

    def stop(self, name: str, final_status: str = "stopped") -> None:
        p = self.procs.pop(name, None)
        pid = p.pid if p else (self.store.query("SELECT pid FROM bots WHERE name=?", (name,)) or [{}])[0].get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        self.store.execute("UPDATE bots SET status=?, pid=NULL WHERE name=?", (final_status, name))
        if final_status == "retired":
            self.store.execute("UPDATE genomes SET status='retired' WHERE id=(SELECT genome_id FROM bots "
                               "WHERE name=?)", (name,))

    def supervise(self) -> None:
        for b in self.bots():
            if b["status"] != "running":
                continue
            p = self.procs.get(b["name"])
            if p is not None and p.poll() is None:
                continue
            if p is None and b["pid"] and pid_matches(b["pid"], b["config_path"]):
                continue
            if b["restarts"] >= 5:
                self.stop(b["name"], "failed")
                self.store.event("error", "supervisor", f"{b['name']} failed 5 restarts; giving up "
                                 "(see state/bots/<name>/stdout.log)")
                continue
            now = time.time()
            if now < self._next_restart.get(b["name"], 0):
                continue
            self._next_restart[b["name"]] = now + min(600, 15 * 2 ** b["restarts"])
            self.store.execute("UPDATE bots SET restarts=restarts+1 WHERE name=?", (b["name"],))
            self.store.event("warning", "supervisor", f"restarting Hummingbot node {b['name']}")
            self._spawn(b["name"], b["config_path"])

    def tail(self, name: str, n: int = 40) -> str:
        f = self.hb.state_dir / "bots" / name / "stdout.log"
        if not f.exists():
            return ""
        return "\n".join(f.read_text(errors="replace").splitlines()[-n:])

    def shutdown(self) -> None:
        for name in list(self.procs):
            self.stop(name)
