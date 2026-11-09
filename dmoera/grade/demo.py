"""An invented example for the demo button: a '90% win rate' scalper.

It takes a small profit often and a big loss rarely, on a made-up market. The win rate
looks great; the edge is tiny and real exchange fees wipe it out. Not a real trader or product.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

DEMO_NAME = "Invented example: 90% win-rate scalper"
_STEP = 300  # 5-minute bars


def _market(seed: int):
    rng = random.Random(seed)
    t0 = datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp()
    n = 400 * 288
    prices, p, r = [], 94000.0, 0.0
    for _ in range(n):
        r = 0.12 * r + rng.gauss(0, 0.0011)
        p *= 1 + r
        prices.append(p)
    return t0, prices


def _simulate(seed: int):
    t0, px = _market(seed)
    rng = random.Random(seed + 1)
    trades, i = [], 300
    tp, sl, max_hold = 0.0030, 0.0240, 3 * 288
    while i < len(px) - max_hold - 1:
        i += int(rng.uniform(4, 12) * 12)
        mom = px[i] / px[i - 12] - 1
        side = 1 if mom > 0 else -1
        entry = px[i]
        j = i + 1
        while j < i + max_hold:
            move = side * (px[j] / entry - 1)
            if move >= tp or move <= -sl:
                break
            j += 1
        trades.append((t0 + i * _STEP, t0 + j * _STEP, side, entry, px[j]))
        i = j
    return t0, px, trades


def demo_csv(seed: int = 11) -> str:
    _, _, trades = _simulate(seed)
    fmt = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    rows = ["Trade #,Type,Signal,Date/Time,Price USDT,Contracts"]
    for k, (te, tx, side, pe, pxit) in enumerate(trades, 1):
        kind = "long" if side > 0 else "short"
        rows.append(f"{k},Exit {kind},Exit,{fmt(tx)},{pxit:.2f},0.1")
        rows.append(f"{k},Entry {kind},Scalp,{fmt(te)},{pe:.2f},0.1")
    return "\n".join(rows) + "\n"


def demo_price_points(seed: int = 11, every: int = 6):
    """The invented market at 30-minute resolution, as (unix_seconds, price) points."""
    t0, px, _ = _simulate(seed)
    return [(t0 + k * _STEP, px[k]) for k in range(0, len(px), every)]
