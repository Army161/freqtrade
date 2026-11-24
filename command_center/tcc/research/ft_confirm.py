"""Stage 2: confirm screened genomes with Freqtrade's exact event-driven backtester.

Runs ``freqtrade backtesting`` as a subprocess (isolates memory, parallelisable) on the
held-out OOS window using the real TCCGenome strategy, real order semantics, fees,
max_open_trades and the bot's protections.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..clusters.ft_config import build_config
from ..store import Store


def _timerange(start, end) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def ft_backtest(genome: dict, *, freqtrade_bin: str, userdir: Path, exchange: str,
                pairs: list[str], start, end, wallet: float, max_open_trades: int,
                fee: float, risk: dict) -> dict:
    cfg = build_config(genome, exchange=exchange, pairs=pairs, wallet=wallet,
                       max_open_trades=max_open_trades, dry_run=True, risk=risk, fee=fee)
    cfg["enable_protections"] = True
    with tempfile.TemporaryDirectory(prefix="tcc_bt_") as td:
        td = Path(td)
        (td / "config.json").write_text(json.dumps(cfg))
        cmd = [freqtrade_bin, "backtesting", "-c", str(td / "config.json"),
               "--userdir", str(userdir), "--datadir", str(userdir / "data" / exchange),
               "--timerange", _timerange(start, end), "--export", "trades",
               "--backtest-directory", str(td), "--cache", "none", "--enable-protections"]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        from freqtrade.data.btanalysis import get_latest_backtest_filename, load_backtest_stats
        if p.returncode != 0 or not (td / ".last_result.json").exists():
            return {"error": (p.stderr or p.stdout)[-1500:]}
        stats = load_backtest_stats(td / get_latest_backtest_filename(td))
    s = stats["strategy"]["TCCGenome"]
    return {
        "trades": s["total_trades"],
        "profit_total": s["profit_total"],
        "profit_abs": s["profit_total_abs"],
        "win_rate": s.get("winrate", 0.0),
        "profit_factor": s.get("profit_factor", 0.0),
        "sharpe": s.get("sharpe", 0.0),
        "sortino": s.get("sortino", 0.0),
        "calmar": s.get("calmar", 0.0),
        "max_dd": -abs(s.get("max_drawdown_account", 0.0)),
        "market_change": s.get("market_change", 0.0),
        "final_balance": s.get("final_balance", wallet),
        "seconds": round(time.time() - t0, 1),
    }


def confirm_candidates(store: Store, *, freqtrade_bin: str, userdir: Path, exchange: str,
                       pairs: list[str], wallet: float, max_open_trades: int, fee: float,
                       risk: dict, min_sharpe: float = 0.5, max_dd: float = -0.20,
                       min_trades: int = 10, parallel: int = 2, limit: int = 10, log=print) -> list[dict]:
    rows = store.leaderboard("oos_pass", limit)
    out = []

    def work(r):
        oos = json.loads(store.query("SELECT summary FROM runs WHERE id=?", (r["run_id"],))[0]["summary"]
                         or "{}").get("oos_window")
        import pandas as pd
        s, e = pd.Timestamp(oos[0]), pd.Timestamp(oos[1])
        return r, ft_backtest(r["genome"], freqtrade_bin=freqtrade_bin, userdir=userdir,
                              exchange=exchange, pairs=pairs, start=s, end=e, wallet=wallet,
                              max_open_trades=max_open_trades, fee=fee, risk=risk)

    with ThreadPoolExecutor(parallel) as ex:
        for r, m in ex.map(work, rows):
            if "error" in m:
                log(f"  {r['id']} freqtrade error: {m['error'][-300:]}")
                store.event("warning", "research", f"ft backtest failed for {r['id']}", error=m["error"][-500:])
                continue
            ok = m["sharpe"] >= min_sharpe and m["max_dd"] >= max_dd and m["trades"] >= min_trades \
                and m["profit_total"] > 0
            store.execute("UPDATE genomes SET ft_metrics=?, status=? WHERE id=?",
                          (json.dumps(m), "confirmed" if ok else "rejected", r["id"]))
            log(f"  {'CONFIRMED' if ok else 'rejected '} {r['id']} {r['description'][:70]} | "
                f"trades={m['trades']} profit={m['profit_total']:+.2%} sharpe={m['sharpe']:.2f} "
                f"dd={m['max_dd']:.2%} ({m['seconds']}s)")
            out.append({**r, "ft_metrics": m, "confirmed": ok})
    return out
