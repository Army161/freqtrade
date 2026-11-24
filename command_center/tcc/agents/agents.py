"""The agent crew. Each agent: gather facts -> ask LLM (or rules) -> validate -> guarded apply.

ResearchDirector : steers where the genetic search looks (family weights). Bounded [0.2, 3].
RiskOfficer      : may TIGHTEN risk limits and pause bots. Can never loosen or add risk.
PortfolioManager : promotes confirmed genomes into the paper cluster and retires laggards,
                   within max_bots. Promotion to LIVE money is never automatic.
Every proposal + whether it was applied is written to agent_notes (audit trail).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

from ..research.genome import FAMILY_SPACE
from ..store import Store
from .providers import ProviderError

SYSTEM_BASE = (
    "You are part of an automated crypto trading research station. You see only aggregated "
    "statistics. Be skeptical: backtest results across many trials are mostly noise, win-rate "
    "alone is meaningless, and capital preservation beats returns. Respond with ONE JSON object "
    "only, matching the requested schema exactly."
)


class Agent:
    name = "agent"

    def __init__(self, store: Store, providers: list):
        self.store = store
        self.providers = providers

    def ask(self, system: str, user: str) -> tuple[dict, str]:
        for p in self.providers:
            try:
                return p.complete_json(system, user), p.name
            except (ProviderError, KeyError, ValueError) as e:
                self.store.event("warning", f"agent:{self.name}", f"{p.name} failed: {e}")
        return {"_rules": True}, "rules"

    def note(self, provider: str, proposal: dict, applied: bool, reason: str):
        self.store.execute("INSERT INTO agent_notes(ts,agent,provider,proposal,applied,reason) "
                           "VALUES(?,?,?,?,?,?)",
                           (time.time(), self.name, provider, json.dumps(proposal, default=str),
                            int(applied), reason))


class ResearchDirector(Agent):
    name = "research_director"

    def family_stats(self) -> dict:
        rows = self.store.query("SELECT genome, status, is_fitness, dsr FROM genomes "
                                "WHERE genome NOT LIKE '%hummingbot%' ORDER BY created DESC LIMIT 5000")
        st = defaultdict(lambda: {"trials": 0, "passed": 0, "fit_sum": 0.0})
        for r in rows:
            fam = json.loads(r["genome"])["family"]
            s = st[fam]
            s["trials"] += 1
            s["fit_sum"] += max(-10.0, r["is_fitness"] or -10.0)
            s["passed"] += r["status"] in ("oos_pass", "confirmed", "incubating", "live")
        return {f: {"trials": s["trials"], "passed": s["passed"],
                    "avg_fitness": round(s["fit_sum"] / max(1, s["trials"]), 3)} for f, s in st.items()}

    def run(self) -> dict:
        stats = self.family_stats()
        current = self.store.kv_get("family_weights", {f: 1.0 for f in FAMILY_SPACE})
        out, provider = self.ask(
            SYSTEM_BASE,
            "Strategy families and their research outcomes so far:\n" + json.dumps(stats) +
            "\nCurrent sampling weights: " + json.dumps(current) +
            '\nReturn {"family_weights": {family: weight 0.2-3.0 for each of ' +
            json.dumps(list(FAMILY_SPACE)) + '}, "rationale": "<one paragraph>"}. Keep exploring '
            "every family (minimum 0.2) because regimes change.")
        if out.get("_rules"):
            # rules: weight ∝ pass-rate with Laplace smoothing, plus exploration floor
            w = {f: 0.5 + 2.5 * (stats.get(f, {}).get("passed", 0) + 1) /
                 (stats.get(f, {}).get("trials", 0) + 10) * 5 for f in FAMILY_SPACE}
            out = {"family_weights": w, "rationale": "pass-rate weighting (rules)"}
        w = out.get("family_weights", {})
        clean = {f: float(min(3.0, max(0.2, float(w.get(f, 1.0))))) for f in FAMILY_SPACE}
        self.store.kv_set("family_weights", clean)
        self.note(provider, {"family_weights": clean, "rationale": out.get("rationale", "")}, True,
                  "bounded to [0.2, 3.0]")
        return clean


class RiskOfficer(Agent):
    name = "risk_officer"

    def run(self, risk_engine) -> dict:
        events = self.store.query("SELECT level, source, message FROM events WHERE ts > ? AND "
                                  "level IN ('warning','error','critical') ORDER BY ts DESC LIMIT 40",
                                  (time.time() - 86400,))
        facts = {"risk_state": risk_engine.state.last, "limits": risk_engine.limits,
                 "recent_events": events}
        out, provider = self.ask(
            SYSTEM_BASE + " You are the risk officer. You may only make limits stricter.",
            "Current risk picture:\n" + json.dumps(facts, default=str) +
            '\nReturn {"tighten": {limit_name: new_value}, "pause_bots": [bot names], '
            '"rationale": "..."}. Valid limit names: ' + json.dumps(list(risk_engine.limits)) +
            ". Return empty objects if no change is warranted.")
        if out.get("_rules"):
            out = {"tighten": {}, "pause_bots": [], "rationale": "rules: no change"}
            last = risk_engine.state.last or {}
            if last.get("drawdown", 0) < -0.5 * risk_engine.limits["max_portfolio_drawdown"]:
                out["tighten"] = {"max_exposure": round(risk_engine.limits["max_exposure"] * 0.8, 3)}
                out["rationale"] = "rules: drawdown past half the limit -> cut exposure 20%"
        res = risk_engine.propose_limits(out.get("tighten") or {}, f"agent:{provider}")
        self.note(provider, out, bool(res["applied"]), json.dumps(res))
        return {"proposal": out, "result": res}


class PortfolioManager(Agent):
    name = "portfolio_manager"

    def run(self, ft_cluster, max_bots: int, min_paper_trades: int = 15,
            hb_nodes=None, max_hb_nodes: int = 0) -> dict:
        bots = [b for b in ft_cluster.bots() if b["status"] == "running"]
        last = self.store.kv_get("risk_state", {}).get("last", {})
        actions = {"deployed": [], "retired": []}
        # 1) retire paper bots that have had a fair sample and are losing
        snaps = self.store.kv_get("last_bot_snaps", {})
        for b in bots:
            s = snaps.get(b["name"], {})
            if s.get("closed", 0) >= min_paper_trades and s.get("dd", 0) < 0 and \
                    s.get("realized", 0) < 0 and b["meta"].get("label") != "baseline":
                ft_cluster.retire(b["name"], f"paper underperformance after {s['closed']} trades")
                actions["retired"].append(b["name"])
        # 2) fill free slots with the best confirmed genomes (never while killed/halted)
        if last.get("killed") or last.get("halted"):
            self.note("rules", actions, False, "risk halt active; no deployments")
            return actions
        running = {b["genome_id"] for b in ft_cluster.bots() if b["status"] == "running"}
        free = max_bots - len(running)
        for r in self.store.leaderboard("confirmed", 20):
            if free <= 0:
                break
            if r["id"] in running:
                continue
            ft_cluster.deploy(r["id"], r["genome"], label="promoted")
            actions["deployed"].append(r["id"])
            free -= 1
        # 3) Hummingbot cluster: keep up to max_hb_nodes paper nodes on the best HB candidates
        if hb_nodes is not None and max_hb_nodes > 0:
            hb_running = {b["genome_id"] for b in hb_nodes.bots() if b["status"] == "running"}
            hb_free = max_hb_nodes - len(hb_running)
            for r in self.store.query("SELECT id FROM genomes WHERE status='hb_candidate' "
                                      "ORDER BY is_fitness DESC LIMIT 10"):
                if hb_free <= 0:
                    break
                if r["id"] not in hb_running:
                    hb_nodes.deploy(r["id"], label="promoted")
                    actions["deployed"].append(r["id"])
                    hb_free -= 1
        self.note("rules", actions, bool(actions["deployed"] or actions["retired"]),
                  f"max_bots={max_bots}, max_hb_nodes={max_hb_nodes}")
        return actions
