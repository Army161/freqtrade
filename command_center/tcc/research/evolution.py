"""Stage 1 search loop: parallel genetic evolution of strategy genomes.

generation 0: random genomes (biased by agent-tunable family weights)
each generation: evaluate on K in-sample walk-forward folds -> robust fitness
                 elites survive, rest refilled by crossover + mutation + fresh randoms
end: top unique genomes are evaluated ONCE on the held-out OOS window and given a
     Deflated Sharpe Ratio that accounts for *every* trial run in this search.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from ..store import Store
from .genome import crossover, describe, genome_id, mutate, random_genome
from .screener import DataStore, run_genome
from .validation import deflated_sharpe, fitness, time_folds


@dataclass
class SearchConfig:
    datadir: str
    pairs: list[str]
    population: int = 200
    generations: int = 8
    elite_frac: float = 0.15
    random_frac: float = 0.2
    n_folds: int = 4
    oos_frac: float = 0.25
    top_k_oos: int = 40
    min_trades: int = 30
    dsr_threshold: float = 0.90
    oos_min_sharpe: float = 0.5
    oos_max_dd: float = -0.20
    fee: float = 0.001
    slippage: float = 0.0005
    workers: int = max(1, (os.cpu_count() or 2) - 1)
    seed: int | None = None
    family_weights: dict = field(default_factory=dict)


# ---- worker process state -----------------------------------------------------
_W: dict = {}


def _init_worker(datadir: str, pairs: list[str], folds, fee, slippage):
    _W["store"] = DataStore(Path(datadir), list(pairs))
    _W["folds"] = folds
    _W["fee"], _W["slip"] = fee, slippage


def _eval_is(g: dict, min_trades: int):
    ms = [run_genome(_W["store"], g, s, e, _W["fee"], _W["slip"]) for s, e in _W["folds"]]
    return g, fitness(ms, min_trades), ms


def _eval_window(g: dict, start, end):
    return g, run_genome(_W["store"], g, start, end, _W["fee"], _W["slip"])


def run_search(cfg: SearchConfig, store: Store, log=print) -> dict:
    rng = random.Random(cfg.seed)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    ds = DataStore(Path(cfg.datadir), cfg.pairs)
    start, end = ds.span("1h")
    start = start + pd.Timedelta(days=10)  # indicator warm-up
    folds, (oos_s, oos_e) = time_folds(start, end, cfg.n_folds, cfg.oos_frac)
    store.execute("INSERT INTO runs(id,started,config) VALUES(?,?,?)",
                  (run_id, time.time(), json.dumps(asdict(cfg))))
    store.event("info", "research", f"search {run_id} started",
                population=cfg.population, generations=cfg.generations,
                is_window=[str(start), str(oos_s)], oos_window=[str(oos_s), str(end)])
    log(f"[{run_id}] IS {start.date()}..{oos_s.date()} ({cfg.n_folds} folds) | "
        f"OOS {oos_s.date()}..{end.date()} | {cfg.workers} workers")

    seen: dict[str, tuple[dict, float, list]] = {}
    pop = [random_genome(rng, cfg.family_weights) for _ in range(cfg.population)]
    t0 = time.time()
    with ProcessPoolExecutor(cfg.workers, initializer=_init_worker,
                             initargs=(cfg.datadir, cfg.pairs, folds, cfg.fee, cfg.slippage)) as ex:
        for gen in range(cfg.generations):
            todo = [g for g in {genome_id(g): g for g in pop}.values() if genome_id(g) not in seen]
            for g, fit, ms in ex.map(_eval_is, todo, [cfg.min_trades] * len(todo), chunksize=4):
                seen[genome_id(g)] = (g, fit, ms)
                store.execute(
                    "INSERT OR IGNORE INTO genomes(id,genome,description,created,generation,run_id,"
                    "is_fitness,is_metrics) VALUES(?,?,?,?,?,?,?,?)",
                    (genome_id(g), json.dumps(g), describe(g), time.time(), gen, run_id, fit,
                     json.dumps(ms)))
            ranked = sorted(seen.values(), key=lambda t: t[1], reverse=True)
            best = ranked[0]
            log(f"  gen {gen}: evaluated {len(todo):4d} new | total trials {len(seen):5d} | "
                f"best fitness {best[1]:+.3f} {describe(best[0])} | {time.time() - t0:.0f}s")
            n_elite = max(2, int(cfg.population * cfg.elite_frac))
            elites = [t[0] for t in ranked[:n_elite]]
            nxt = list(elites)
            while len(nxt) < cfg.population:
                r = rng.random()
                if r < cfg.random_frac:
                    nxt.append(random_genome(rng, cfg.family_weights))
                elif r < 0.6:
                    nxt.append(mutate(rng.choice(elites), rng))
                else:
                    nxt.append(mutate(crossover(rng.choice(elites), rng.choice(elites), rng), rng, 0.1))
            pop = nxt

        # ---- held-out OOS + deflated Sharpe over ALL trials ------------------------
        ranked = sorted(seen.values(), key=lambda t: t[1], reverse=True)
        cands = [t[0] for t in ranked[: cfg.top_k_oos] if t[1] > -5]
        # Sharpe dispersion across *viable* trials (enough trades), clipped so broken strategies
        # (Sharpe -12) don't inflate the multiple-testing benchmark; N still counts every trial.
        trial_srs = [max(-4.0, min(4.0, float(sum(m["sharpe"] for m in t[2]) / len(t[2]))))
                     for t in seen.values() if sum(m["trades"] for m in t[2]) >= cfg.min_trades]
        n_trials = len(seen)
        results = list(ex.map(_eval_window, cands, [oos_s] * len(cands), [oos_e] * len(cands)))

    passed = []
    for g, m in results:
        dsr = deflated_sharpe(m["sharpe"], m["n_days"], m["skew"], m["kurt"], n_trials, trial_srs)
        ok = (dsr >= cfg.dsr_threshold and m["sharpe"] >= cfg.oos_min_sharpe
              and m["max_dd"] >= cfg.oos_max_dd and m["trades"] >= cfg.min_trades // 3)
        status = "oos_pass" if ok else "rejected"
        store.execute("UPDATE genomes SET oos_metrics=?, dsr=?, status=? WHERE id=?",
                      (json.dumps(m), dsr, status, genome_id(g)))
        if ok:
            passed.append((g, m, dsr))
    summary = dict(run_id=run_id, trials=n_trials, oos_tested=len(results), oos_passed=len(passed),
                   oos_window=[str(oos_s), str(oos_e)], seconds=round(time.time() - t0, 1),
                   best=[dict(id=genome_id(g), desc=describe(g), oos=m, dsr=d) for g, m, d in
                         sorted(passed, key=lambda x: -x[2])[:10]])
    store.execute("UPDATE runs SET finished=?, n_trials=?, summary=? WHERE id=?",
                  (time.time(), n_trials, json.dumps(summary), run_id))
    store.event("info", "research", f"search {run_id} done: {n_trials} trials, "
                f"{len(passed)}/{len(results)} passed OOS+DSR", **{k: summary[k] for k in
                                                                  ("trials", "oos_passed")})
    log(f"[{run_id}] done: {n_trials} trials in {summary['seconds']}s; "
        f"{len(passed)}/{len(results)} candidates passed OOS + DSR>={cfg.dsr_threshold}")
    return summary
