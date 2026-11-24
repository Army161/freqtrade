"""Strategy genome: a compact, mutable description of one trading strategy.

A genome is a plain dict (JSON-serialisable) so it can be stored in SQLite, passed to
Freqtrade through config (``tcc_genome``), and edited by AI agents.

    {
      "family": "rsi_revert" | "ema_cross" | "bb_revert" | "donchian_break" | "macd_trend",
      "p": {...family params...},
      "filters": {"trend_ema": 0|50|100|200, "adx_min": 0..30, "vol_max": 0|0.01..0.05},
      "exit": {"tp": 0.004..0.06, "sl": 0.006..0.08, "hold": bars, "trail": bool},
      "timeframe": "5m" | "1h"
    }
"""

from __future__ import annotations

import copy
import hashlib
import json
import random

FAMILY_SPACE: dict[str, dict[str, list]] = {
    "rsi_revert": {"len": [7, 10, 14, 21], "lo": [15, 20, 25, 30, 35], "hi": [55, 60, 65, 70, 75]},
    "ema_cross": {"fast": [5, 8, 12, 20, 30], "slow": [26, 40, 50, 80, 100, 150]},
    "bb_revert": {"len": [14, 20, 30, 40], "std": [1.5, 2.0, 2.5, 3.0]},
    "donchian_break": {"len": [20, 40, 55, 80, 120]},
    "macd_trend": {"fast": [8, 12, 16], "slow": [21, 26, 34], "signal": [5, 9, 12]},
}
FILTER_SPACE = {
    "trend_ema": [0, 0, 50, 100, 200],
    "adx_min": [0, 0, 15, 20, 25],
    "vol_max": [0, 0, 0.01, 0.02, 0.04],
}
EXIT_SPACE = {
    "tp": [0.004, 0.006, 0.008, 0.01, 0.015, 0.02, 0.03, 0.045, 0.06],
    "sl": [0.006, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08],
    "hold": [12, 24, 48, 96, 144, 288],
    "trail": [False, False, True],
}
TIMEFRAMES = ["5m", "1h"]


def random_genome(rng: random.Random, family_weights: dict[str, float] | None = None) -> dict:
    fams = list(FAMILY_SPACE)
    w = [max(0.01, (family_weights or {}).get(f, 1.0)) for f in fams]
    fam = rng.choices(fams, weights=w)[0]
    g = {
        "family": fam,
        "p": {k: rng.choice(v) for k, v in FAMILY_SPACE[fam].items()},
        "filters": {k: rng.choice(v) for k, v in FILTER_SPACE.items()},
        "exit": {k: rng.choice(v) for k, v in EXIT_SPACE.items()},
        "timeframe": rng.choice(TIMEFRAMES),
    }
    return repair(g)


def repair(g: dict) -> dict:
    p = g["p"]
    if g["family"] in ("ema_cross", "macd_trend") and p["fast"] >= p["slow"]:
        p["fast"], p["slow"] = min(p["fast"], p["slow"]), max(p["fast"], p["slow"]) + 10
    if g["family"] == "rsi_revert" and p["lo"] >= p["hi"]:
        p["hi"] = p["lo"] + 20
    return g


def mutate(g: dict, rng: random.Random, rate: float = 0.3) -> dict:
    g = copy.deepcopy(g)
    for k, v in FAMILY_SPACE[g["family"]].items():
        if rng.random() < rate:
            g["p"][k] = rng.choice(v)
    for k, v in FILTER_SPACE.items():
        if rng.random() < rate:
            g["filters"][k] = rng.choice(v)
    for k, v in EXIT_SPACE.items():
        if rng.random() < rate:
            g["exit"][k] = rng.choice(v)
    if rng.random() < rate / 3:
        g["timeframe"] = rng.choice(TIMEFRAMES)
    return repair(g)


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    child = copy.deepcopy(a if rng.random() < 0.5 else b)
    other = b if child["family"] == a["family"] else a
    if other["family"] == child["family"]:
        for k in child["p"]:
            if rng.random() < 0.5:
                child["p"][k] = other["p"][k]
    for sect in ("filters", "exit"):
        for k in child[sect]:
            if rng.random() < 0.5:
                child[sect][k] = other[sect][k]
    return repair(child)


def genome_id(g: dict) -> str:
    return hashlib.sha1(json.dumps(g, sort_keys=True).encode()).hexdigest()[:12]


def describe(g: dict) -> str:
    p = ",".join(f"{k}={v}" for k, v in g["p"].items())
    f = ",".join(f"{k}={v}" for k, v in g["filters"].items() if v)
    e = g["exit"]
    return (
        f"{g['family']}({p}) {g['timeframe']} [{f or 'nofilter'}] "
        f"tp={e['tp']:.3f} sl={e['sl']:.3f} hold={e['hold']}{' trail' if e['trail'] else ''}"
    )
