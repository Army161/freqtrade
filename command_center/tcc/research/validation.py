"""Overfitting defences for mass strategy search.

When you test thousands of strategies, the best in-sample result is mostly luck.
We therefore:
  * rank on in-sample *folds* (walk-forward consistency), never a single backtest,
  * hold out an out-of-sample (OOS) window that the search never sees,
  * deflate the OOS Sharpe by the number of trials (Bailey & López de Prado, 2014).
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

EULER = 0.5772156649


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected maximum Sharpe of ``n_trials`` independent zero-skill strategies."""
    n = max(2, n_trials)
    sd = math.sqrt(max(var_sr, 1e-12))
    return sd * ((1 - EULER) * norm.ppf(1 - 1 / n) + EULER * norm.ppf(1 - 1 / (n * math.e)))


def probabilistic_sharpe(sr: float, sr_bench: float, n_obs: int, skew: float, kurt: float) -> float:
    """P(true SR > sr_bench) given observed (non-annualised) SR. Bailey & López de Prado."""
    if n_obs < 3:
        return 0.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    return float(norm.cdf((sr - sr_bench) * math.sqrt(n_obs - 1) / denom))


def deflated_sharpe(sr_annual: float, n_days: int, skew: float, kurt: float,
                    n_trials: int, trial_srs_annual: list[float]) -> float:
    """Deflated Sharpe Ratio (probability the strategy has real skill after multiple testing)."""
    k = math.sqrt(365)
    sr = sr_annual / k
    var = float(np.var(np.asarray(trial_srs_annual) / k)) if len(trial_srs_annual) > 1 else 0.01
    bench = expected_max_sharpe(n_trials, var)
    return probabilistic_sharpe(sr, bench, n_days, skew, kurt)


def fitness(fold_metrics: list[dict], min_trades: int = 20) -> float:
    """Robust in-sample score: rewards consistency across folds, punishes drawdown/few trades."""
    trades = sum(m["trades"] for m in fold_metrics)
    if trades < min_trades:
        return -10.0 + trades / max(1, min_trades)
    sharpes = np.array([m["sharpe"] for m in fold_metrics])
    pos_frac = float((np.array([m["ret"] for m in fold_metrics]) > 0).mean())
    worst_dd = min(m["max_dd"] for m in fold_metrics)  # negative number
    # median (not mean) Sharpe so a single lucky fold can't carry the score, blended with the
    # WORST fold so strategies that blow up in one regime (tiny TP / huge SL) don't win
    robust = 0.5 * float(np.median(sharpes)) + 0.5 * float(np.min(sharpes))
    score = robust * (0.5 + pos_frac) + 2.0 * worst_dd
    score -= 0.5 * float(np.std(sharpes)) / (1 + abs(float(np.median(sharpes))))
    return score


def time_folds(start, end, n_folds: int, oos_frac: float):
    """Split [start,end) into n in-sample folds followed by one held-out OOS window."""
    total = end - start
    oos_start = start + total * (1 - oos_frac)
    step = (oos_start - start) / n_folds
    folds = [(start + step * i, start + step * (i + 1)) for i in range(n_folds)]
    return folds, (oos_start, end)
