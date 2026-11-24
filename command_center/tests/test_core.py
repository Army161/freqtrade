import asyncio
import random
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tcc.research.genome import FAMILY_SPACE, crossover, genome_id, mutate, random_genome
from tcc.research.indicators import compute_signals
from tcc.research.screener import DataStore, _simulate, run_genome
from tcc.research.validation import deflated_sharpe, fitness, time_folds
from tcc.risk.engine import RiskEngine
from tcc.risk.sizing import fractional_kelly, small_account_plan
from tcc.store import Store


# ---- genome -------------------------------------------------------------------------------
def test_genomes_valid_and_mutable():
    rng = random.Random(0)
    for _ in range(300):
        g = mutate(crossover(random_genome(rng), random_genome(rng), rng), rng)
        assert g["family"] in FAMILY_SPACE
        if g["family"] in ("ema_cross", "macd_trend"):
            assert g["p"]["fast"] < g["p"]["slow"]
        if g["family"] == "rsi_revert":
            assert g["p"]["lo"] < g["p"]["hi"]
    a = random_genome(random.Random(1))
    assert genome_id(a) == genome_id(dict(a))


def _ohlcv(n=3000, seed=0, drift=0.0):
    r = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(r.normal(drift, 0.004, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + np.abs(r.normal(0, 0.002, n)))
    low = np.minimum(open_, close) * (1 - np.abs(r.normal(0, 0.002, n)))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1000.0)})


def test_signals_are_causal():
    """Appending future bars must not change past signals (no lookahead)."""
    df = _ohlcv()
    rng = random.Random(3)
    for _ in range(25):
        g = random_genome(rng)
        e1, x1 = compute_signals(df.iloc[:2000].copy(), g)
        e2, x2 = compute_signals(df.copy(), g)
        assert (e1.values == e2.values[:2000]).all()
        assert (x1.values == x2.values[:2000]).all()


# ---- simulator ----------------------------------------------------------------------------
def test_simulator_take_profit_and_stop():
    n = 10
    o = np.full(n, 100.0)
    h = np.full(n, 100.5)
    l = np.full(n, 99.5)
    c = np.full(n, 100.0)
    entry = np.zeros(n, bool)
    entry[1] = True
    ex = np.zeros(n, bool)
    h[4] = 103.0  # TP 2% hit on bar 4
    e, x, r = _simulate(o, h, l, c, entry, ex, 0, n, 0.02, 0.05, 50, False, 0.0)
    assert list(e) == [2] and list(x) == [4] and r[0] == pytest.approx(0.02)
    l[3] = 90.0  # now the stop (5%) is hit first, on bar 3
    e, x, r = _simulate(o, h, l, c, entry, ex, 0, n, 0.02, 0.05, 50, False, 0.001)
    assert list(x) == [3] and r[0] == pytest.approx(-0.05 - 0.001)


def test_screener_runs_on_feather(tmp_path: Path):
    for pair, seed in (("AAA/USDT", 1), ("BBB/USDT", 2)):
        df = _ohlcv(seed=seed)
        df.to_feather(tmp_path / f"{pair.replace('/', '_')}-1h.feather")
    ds = DataStore(tmp_path, ["AAA/USDT", "BBB/USDT"])
    s, e = ds.span("1h")
    g = {"family": "rsi_revert", "p": {"len": 14, "lo": 30, "hi": 60},
         "filters": {"trend_ema": 0, "adx_min": 0, "vol_max": 0},
         "exit": {"tp": 0.01, "sl": 0.02, "hold": 24, "trail": False}, "timeframe": "1h"}
    m = run_genome(ds, g, s, e)
    assert m["trades"] > 10 and -1 < m["ret"] < 5 and m["max_dd"] <= 0


# ---- validation ---------------------------------------------------------------------------
def test_dsr_penalises_more_trials():
    srs = list(np.random.default_rng(0).normal(0, 1, 500))
    few = deflated_sharpe(2.0, 120, 0.0, 3.0, 10, srs)
    many = deflated_sharpe(2.0, 120, 0.0, 3.0, 5000, srs)
    assert 0 <= many < few <= 1


def test_fitness_prefers_consistency():
    good = [dict(trades=20, sharpe=1.5, ret=0.03, max_dd=-0.03)] * 4
    lucky = [dict(trades=20, sharpe=s, ret=r, max_dd=-0.1) for s, r in
             ((6, 0.2), (-1, -0.02), (-1, -0.02), (-1, -0.03))]
    assert fitness(good) > fitness(lucky)
    assert fitness([dict(trades=1, sharpe=9, ret=1, max_dd=0)]) < 0  # too few trades


def test_time_folds_hold_out_oos():
    s, e = pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC")
    folds, (os_, oe) = time_folds(s, e, 4, 0.25)
    assert folds[-1][1] == os_ and oe == e and len(folds) == 4


# ---- risk ---------------------------------------------------------------------------------
class FakeClient:
    def __init__(self, bot):
        self.bot = bot

    async def snapshot(self):
        b = self.bot
        return {"profit": {"profit_closed_coin": 0, "trade_count": 0}, "state": "running",
                "balance": {"total_bot": b["eq"], "starting_capital": b["start"]},
                "open": [], "recent": []}

    async def stop_entries(self):
        self.bot["entries_stopped"] = True

    async def force_exit_all(self):
        self.bot["exited"] = True

    async def start(self):
        self.bot["entries_stopped"] = False


class FakeCluster:
    def __init__(self, bots):
        self._bots = bots

    def bots(self):
        return self._bots

    def client(self, b):
        return FakeClient(b)

    def stop(self, name, status="stopped"):
        next(b for b in self._bots if b["name"] == name)["status"] = status

    def retire(self, name, reason):
        self.stop(name, "retired")


LIMITS = dict(max_portfolio_drawdown=0.15, daily_loss_limit=0.05, bot_max_drawdown=0.10,
              max_consecutive_losses=5, max_exposure=0.9)


def _bot(name, eq, start=100.0):
    return {"name": name, "status": "running", "eq": eq, "start": start, "started": 0,
            "genome_id": name, "meta": {}}


def test_risk_tighten_only(tmp_path):
    eng = RiskEngine(Store(tmp_path / "s.db"), LIMITS, FakeCluster([]))
    res = eng.propose_limits({"daily_loss_limit": 0.03, "max_portfolio_drawdown": 0.5,
                              "evil": 1}, "test")
    assert res["applied"] == {"daily_loss_limit": 0.03}
    assert "max_portfolio_drawdown" in res["refused"] and "evil" in res["refused"]
    assert eng.limits["max_portfolio_drawdown"] == 0.15


def test_risk_kill_switch_on_portfolio_drawdown(tmp_path):
    bots = [_bot("a", 100), _bot("b", 100)]
    eng = RiskEngine(Store(tmp_path / "s.db"), LIMITS, FakeCluster(bots))
    asyncio.run(eng.tick())  # peak 200
    bots[0]["eq"], bots[1]["eq"] = 92, 76  # total 168 => -16% (bot b alone is -24% -> retired)
    asyncio.run(eng.tick())
    assert eng.state.killed
    assert all(b["status"] in ("killed", "retired") for b in bots)


def test_risk_daily_halt(tmp_path):
    bots = [_bot("a", 100), _bot("b", 100), _bot("c", 100)]
    eng = RiskEngine(Store(tmp_path / "s.db"), LIMITS, FakeCluster(bots))
    asyncio.run(eng.tick())
    for b in bots:
        b["eq"] = 94  # -6% day, below each bot's 10% retire threshold, above 15% kill
    s = asyncio.run(eng.tick())
    assert s["halted"] and not s["killed"]
    assert all(b.get("entries_stopped") for b in bots)


# ---- sizing -------------------------------------------------------------------------------
def test_small_account_plan():
    p = small_account_plan(20, 5)
    assert p["max_concurrent_positions"] == 3 and p["viable"]
    assert small_account_plan(4, 5)["viable"] is False
    assert fractional_kelly(0.55, 0.02, 0.02) == pytest.approx(0.025)
    assert fractional_kelly(0.3, 0.01, 0.02) == 0.0


def test_pid_matches_only_real_bot():
    import os

    from tcc.clusters.freqtrade_cluster import pid_matches
    assert pid_matches(os.getpid(), "python") or pid_matches(os.getpid(), "pytest")
    assert not pid_matches(os.getpid(), "ft-deadbeef")
    assert not pid_matches(999999, "anything")
