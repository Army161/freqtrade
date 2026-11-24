"""Position sizing helpers, including the hard maths of very small accounts."""

from __future__ import annotations


def fractional_kelly(win_rate: float, avg_win: float, avg_loss: float, fraction: float = 0.25) -> float:
    """Fraction of equity to risk per trade. avg_loss is a positive number. Clamped to [0, 0.25]."""
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    k = win_rate - (1 - win_rate) / b
    return max(0.0, min(0.25, k * fraction))


def small_account_plan(capital: float, min_order: float = 5.0, fee_round_trip: float = 0.002,
                       buffer: float = 1.15) -> dict:
    """How many concurrent positions an account can really hold, and what fees cost it."""
    slot = min_order * buffer
    slots = int(capital // slot)
    return {
        "capital": capital,
        "max_concurrent_positions": slots,
        "stake_per_position": round(capital / slots, 2) if slots else 0.0,
        "fee_per_round_trip_usd": round((capital / slots if slots else 0) * fee_round_trip, 4),
        "viable": slots >= 1,
        "note": ("Below ~$50 the exchange minimum order size limits you to very few positions; "
                 "diversification across many bots is only possible in paper mode."),
    }
