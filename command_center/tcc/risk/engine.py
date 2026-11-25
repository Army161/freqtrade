"""Portfolio risk engine: the layer every bot and every AI agent sits underneath.

Layers of defence (outermost first):
  1. KILL SWITCH        manual or automatic; stop entries + force-exit everything + stop bots
  2. Portfolio drawdown equity below peak by > max_portfolio_drawdown  -> kill switch
  3. Daily loss limit   loss since UTC midnight > daily_loss_limit      -> halt entries until tomorrow
  4. Per-bot drawdown   bot equity below its start by > bot_max_drawdown -> retire bot
  5. Losing streak      N consecutive losing trades                    -> pause bot entries
  6. Exposure cap       open stake / equity > max_exposure              -> pause new entries
  7. Heartbeat          bot API unreachable > timeout                   -> supervisor restart
Plus, inside every bot: Freqtrade protections (cooldown, stoploss guard, max DD, low-profit pairs).

Agents may only TIGHTEN these limits automatically; loosening requires a human edit of station.yaml.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..store import Store

TIGHTEN_ONLY_KEYS = {
    # key: direction in which the value is "tighter"
    "max_portfolio_drawdown": "lower", "daily_loss_limit": "lower", "bot_max_drawdown": "lower",
    "max_consecutive_losses": "lower", "max_exposure": "lower",
}


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted_until_day: str = ""
    killed: bool = False
    kill_reason: str = ""
    last: dict = field(default_factory=dict)


class RiskEngine:
    def __init__(self, store: Store, limits: dict, cluster):
        self.store = store
        self.limits = dict(limits)
        self.cluster = cluster
        self.state = RiskState(**store.kv_get("risk_state", {}))
        self.last_seen: dict[str, float] = {}
        self.on_kill: list = []  # extra sync callables run by the kill switch (e.g. flatten stocks)

    def save(self):
        self.store.kv_set("risk_state", self.state.__dict__)

    # ---- agent interface ------------------------------------------------------------
    def propose_limits(self, changes: dict, source: str) -> dict:
        applied, refused = {}, {}
        for k, v in changes.items():
            if k not in TIGHTEN_ONLY_KEYS or not isinstance(v, (int, float)):
                refused[k] = "unknown key"
                continue
            cur = self.limits.get(k)
            tighter = v < cur if TIGHTEN_ONLY_KEYS[k] == "lower" else v > cur
            floor_ok = v > 0
            if tighter and floor_ok:
                self.limits[k] = v
                applied[k] = v
            else:
                refused[k] = "would loosen limit (human approval required)"
        if applied:
            self.store.event("warning", "risk", f"limits tightened by {source}", **applied)
        self.store.kv_set("risk_limits", self.limits)
        return {"applied": applied, "refused": refused}

    # ---- main loop --------------------------------------------------------------------
    async def kill(self, reason: str) -> None:
        self.state.killed, self.state.kill_reason = True, reason
        self.save()
        self.store.event("critical", "risk", f"KILL SWITCH: {reason}")
        for b in self.cluster.bots():
            if b["status"] != "running":
                continue
            c = self.cluster.client(b)
            for fn in (c.stop_entries, c.force_exit_all):
                try:
                    await fn()
                except Exception as e:  # noqa: BLE001
                    self.store.event("error", "risk", f"kill step failed on {b['name']}: {e}")
            await asyncio.sleep(2)
            await asyncio.to_thread(self.cluster.stop, b["name"], "killed")
        for cb in self.on_kill:
            try:
                await asyncio.to_thread(cb)
            except Exception as e:  # noqa: BLE001
                self.store.event("error", "risk", f"kill hook {getattr(cb, '__name__', cb)} failed: {e}")

    def reset_kill(self) -> None:
        self.state.killed, self.state.kill_reason = False, ""
        self.state.peak_equity = 0.0
        self.save()
        self.store.event("warning", "risk", "kill switch reset by operator")

    async def tick(self) -> dict:
        L = self.limits
        bots = [b for b in self.cluster.bots() if b["status"] == "running"]
        snaps, total, open_stake, realized, unrealized, n_open = {}, 0.0, 0.0, 0.0, 0.0, 0
        for b in bots:
            try:
                s = await self.cluster.client(b).snapshot()
            except Exception as e:  # noqa: BLE001
                age = time.time() - self.last_seen.get(b["name"], b["started"] or time.time())
                snaps[b["name"]] = {"error": str(e)[:200], "unreachable_s": round(age)}
                continue
            self.last_seen[b["name"]] = time.time()
            # bot-scoped figures (total_bot vs starting_capital) so tradable_balance_ratio cancels out
            eq = float(s["balance"].get("total_bot", s["balance"].get("total", 0.0)))
            start_cap = float(s["balance"].get("starting_capital", eq) or eq)
            opens = s["open"]
            stake = sum(float(t.get("stake_amount", 0)) for t in opens)
            upnl = sum(float(t.get("profit_abs", 0) or 0) for t in opens)
            closed = [t for t in s["recent"] if not t.get("is_open")]
            closed.sort(key=lambda t: t.get("close_timestamp") or 0, reverse=True)
            streak = 0
            for t in closed:
                if (t.get("profit_ratio") or 0) < 0:
                    streak += 1
                else:
                    break
            p = s["profit"]
            snaps[b["name"]] = {
                "equity": eq, "start": start_cap, "dd": eq / start_cap - 1 if start_cap else 0.0,
                "open_trades": len(opens), "stake": stake, "unrealized": upnl,
                "realized": float(p.get("profit_closed_coin", 0)), "trades": p.get("trade_count", 0),
                "closed": p.get("closed_trade_count", 0), "wins": p.get("winning_trades", 0),
                "losses": p.get("losing_trades", 0), "streak": streak, "state": s["state"],
                "genome_id": b["genome_id"],
            }
            total += eq
            open_stake += stake
            realized += snaps[b["name"]]["realized"]
            unrealized += upnl
            n_open += len(opens)

            # layer 4 + 5: per-bot
            if snaps[b["name"]]["dd"] <= -L["bot_max_drawdown"]:
                await asyncio.to_thread(self.cluster.retire, b["name"], f"bot drawdown {snaps[b['name']]['dd']:.1%}")
            elif streak >= L["max_consecutive_losses"] and s["state"] == "running":
                await self.cluster.client(b).stop_entries()
                self.store.event("warning", "risk", f"{b['name']} paused: {streak} losses in a row")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        st = self.state
        if total > 0:
            if st.day != today:
                st.day, st.day_start_equity = today, total
            st.peak_equity = max(st.peak_equity, total)
            port_dd = total / st.peak_equity - 1
            day_pnl = total / st.day_start_equity - 1 if st.day_start_equity else 0.0
            exposure = open_stake / total
            if not st.killed and port_dd <= -L["max_portfolio_drawdown"]:
                await self.kill(f"portfolio drawdown {port_dd:.1%} from peak")
            elif day_pnl <= -L["daily_loss_limit"] and st.halted_until_day != today:
                st.halted_until_day = today
                self.store.event("critical", "risk", f"daily loss limit hit ({day_pnl:.1%}); "
                                 "entries halted until next UTC day")
                for b in bots:
                    try:
                        await self.cluster.client(b).stop_entries()
                    except Exception:  # noqa: BLE001
                        pass
            elif st.halted_until_day and st.halted_until_day != today:
                st.halted_until_day = ""
                for b in bots:
                    try:
                        await self.cluster.client(b).start()
                    except Exception:  # noqa: BLE001
                        pass
                self.store.event("info", "risk", "new UTC day: entry halt lifted")
            if exposure > L["max_exposure"]:
                self.store.event("warning", "risk", f"exposure {exposure:.0%} above cap")
        else:
            port_dd = day_pnl = exposure = 0.0

        summary = {"ts": time.time(), "equity": total, "peak": st.peak_equity, "drawdown": port_dd,
                   "day_pnl": day_pnl, "exposure": exposure, "open_trades": n_open,
                   "realized": realized, "unrealized": unrealized, "killed": st.killed,
                   "kill_reason": st.kill_reason, "halted": bool(st.halted_until_day),
                   "limits": L, "bots": snaps}
        st.last = {k: v for k, v in summary.items() if k != "bots"}
        self.save()
        if total > 0:
            self.store.execute(
                "INSERT INTO equity(ts,total,open_trades,realized,unrealized,detail) VALUES(?,?,?,?,?,?)",
                (time.time(), total, n_open, realized, unrealized,
                 json.dumps({k: v.get("equity") for k, v in snaps.items()})))
        return summary
