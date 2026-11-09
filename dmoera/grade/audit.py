"""The audit: friction haircut, regime split, random-entry placebo test, deflated Sharpe, grade.

Pure Python. Each trade is counted at equal size (1x, no leverage); costs are a round-trip
fee in basis points of notional applied to every trade.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import random
from statistics import mean, pstdev

from .scale import clamp, grade_verdict, score_to_grade

FEE_LEVELS = list(range(0, 21))          # 0..20 bps round trip, for the fee slider
DEFAULT_FEE_BPS = 12
DEFAULT_TRIALS = 20
CURVE_POINTS = 240
REGIMES = ("Bull", "Bear", "Chop", "Quiet")
EULER = 0.5772156649


# ── maths helpers ─────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's approximation, |error| < 1.2e-9)."""
    p = clamp(p, 1e-12, 1 - 1e-12)
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(returns, trials: int) -> dict:
    """Probability that the true per-trade Sharpe is above the best you'd expect from `trials` tries of pure luck."""
    n = len(returns)
    if n < 3:
        return {"dsr": 0.0, "sharpe_per_trade": 0.0, "benchmark": 0.0}
    m = mean(returns)
    sd = pstdev(returns)
    if sd <= 0:
        return {"dsr": 1.0 if m > 0 else 0.0, "sharpe_per_trade": 0.0, "benchmark": 0.0}
    sr = m / sd
    skew = sum(((r - m) / sd) ** 3 for r in returns) / n
    kurt = sum(((r - m) / sd) ** 4 for r in returns) / n
    trials = max(1, int(trials))
    if trials > 1:
        sr0 = math.sqrt(1.0 / (n - 1)) * ((1 - EULER) * norm_ppf(1 - 1.0 / trials)
                                          + EULER * norm_ppf(1 - 1.0 / (trials * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    denom = math.sqrt(denom) if denom > 1e-9 else 1e-4
    dsr = norm_cdf((sr - sr0) * math.sqrt(n - 1) / denom)
    return {"dsr": dsr, "sharpe_per_trade": sr, "benchmark": sr0}


# ── price paths ───────────────────────────────────────────────────────────

class PricePath:
    """Piecewise-linear price over time."""

    def __init__(self, points):
        pts = sorted((float(t), float(p)) for t, p in points if p and p > 0)
        dedup = {}
        for t, p in pts:
            dedup[t] = p
        self.t = sorted(dedup)
        self.p = [dedup[t] for t in self.t]

    @property
    def start(self):
        return self.t[0]

    @property
    def end(self):
        return self.t[-1]

    def at(self, t: float) -> float:
        ts = self.t
        if t <= ts[0]:
            return self.p[0]
        if t >= ts[-1]:
            return self.p[-1]
        i = bisect.bisect_right(ts, t)
        t0, t1 = ts[i - 1], ts[i]
        p0, p1 = self.p[i - 1], self.p[i]
        return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p1

    def window_range(self, t0: float, t1: float) -> float:
        lo_i, hi_i = bisect.bisect_left(self.t, t0), bisect.bisect_right(self.t, t1)
        vals = [self.at(t0), self.at(t1)] + self.p[lo_i:hi_i]
        m = sum(vals) / len(vals)
        return (max(vals) - min(vals)) / m if m > 0 else 0.0


def paths_from_trades(trades):
    pts = {}
    for t in trades:
        pts.setdefault(t["symbol"], []).extend([(t["entry_time"], t["entry_price"]), (t["exit_time"], t["exit_price"])])
    return {s: PricePath(p) for s, p in pts.items()}


# ── per-fee metrics ───────────────────────────────────────────────────────

def _equity(returns):
    eq, out = 1.0, [1.0]
    for r in returns:
        eq *= max(1e-9, 1 + r)
        out.append(eq)
    return out


def _max_dd(curve):
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak if peak > 0 else 0)
    return dd


def _downsample(curve, n=CURVE_POINTS):
    if len(curve) <= n:
        return [round(v, 6) for v in curve]
    step = (len(curve) - 1) / (n - 1)
    return [round(curve[round(i * step)], 6) for i in range(n)]


def _metrics(net, span_days):
    n = len(net)
    curve = _equity(net)
    total = curve[-1] - 1
    wins = [r for r in net if r > 0]
    losses = [r for r in net if r <= 0]
    sd = pstdev(net) if n > 1 else 0.0
    tpy = n / max(span_days / 365.0, 1 / 365.0)
    sharpe = (mean(net) / sd) * math.sqrt(tpy) if sd > 0 else 0.0
    gl = -sum(losses)
    years = span_days / 365.0
    cagr = (curve[-1] ** (1 / years) - 1) if years >= 1 and curve[-1] > 0 else None
    return {
        "total_return": total,
        "cagr": cagr,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_trade_bps": mean(net) * 1e4 if n else 0.0,
        "profit_factor": (sum(wins) / gl) if gl > 0 else None,
        "max_drawdown": _max_dd(curve),
        "sharpe": sharpe,
        "curve": curve,
    }


# ── regimes ───────────────────────────────────────────────────────────────

def _label_regimes(trades, paths, span_days):
    win_days = clamp(span_days / 6.0, 3.0, 30.0)
    w = win_days * 86400.0
    k = 0.08 * math.sqrt(win_days / 30.0)
    raw = []
    for t in trades:
        p = paths[t["symbol"]]
        t1 = t["entry_time"]
        t0 = max(p.start, t1 - w)
        p0, p1 = p.at(t0), p.at(t1)
        ret = p1 / p0 - 1 if p0 > 0 else 0.0
        raw.append((ret, p.window_range(t0, t1)))
    ranges = sorted(r for _, r in raw)
    med = ranges[len(ranges) // 2] if ranges else 0.0
    labels = []
    for ret, rng in raw:
        if ret > k:
            labels.append("Bull")
        elif ret < -k:
            labels.append("Bear")
        elif rng < med:
            labels.append("Quiet")
        else:
            labels.append("Chop")
    return labels, win_days


def _regime_table(labels, net):
    n = len(net)
    rows = []
    for reg in REGIMES:
        rs = [r for lab, r in zip(labels, net) if lab == reg]
        if not rs:
            rows.append({"regime": reg, "trades": 0, "net_return": 0.0, "win_rate": None, "avg_trade_bps": None, "assessed": False})
            continue
        eq = 1.0
        for r in rs:
            eq *= max(1e-9, 1 + r)
        rows.append({"regime": reg, "trades": len(rs), "net_return": eq - 1,
                     "win_rate": sum(1 for r in rs if r > 0) / len(rs), "avg_trade_bps": mean(rs) * 1e4,
                     "assessed": len(rs) >= max(5, 0.05 * n)})
    assessed = [r for r in rows if r["assessed"]]
    profitable = [r for r in assessed if r["net_return"] > 0]
    gains = [max(0.0, sum(r for lab, r in zip(labels, net) if lab == reg["regime"])) for reg in assessed]
    best_share = (max(gains) / sum(gains)) if sum(gains) > 0 else 1.0
    best = assessed[gains.index(max(gains))]["regime"] if assessed and sum(gains) > 0 else None
    return rows, len(assessed), len(profitable), best_share, best


# ── placebo ───────────────────────────────────────────────────────────────

def _placebo_gross(trades, paths, n_placebo, rng):
    """For each placebo bot: the same trades (symbol, side, hold time), entered at random times."""
    sims = []
    for _ in range(n_placebo):
        g = []
        for t in trades:
            p = paths[t["symbol"]]
            hold = max(60.0, t["exit_time"] - t["entry_time"])
            hi = p.end - hold
            t0 = p.start if hi <= p.start else rng.uniform(p.start, hi)
            a, b = p.at(t0), p.at(t0 + hold)
            g.append(t["side"] * (b / a - 1) if a > 0 else 0.0)
        sims.append(g)
    return sims


def _log_total(rs, fee):
    return sum(math.log(max(1e-9, 1 + r - fee)) for r in rs)


# ── grade ─────────────────────────────────────────────────────────────────

def _score(m, n, span_days, placebo_pct, dsr, n_assessed, n_profitable, best_share, best_regime, fee_bps, trials, n_placebo=500):
    if m["total_return"] > 0:
        edge = clamp(35 + m["sharpe"] * 20, 0, 100)
    else:
        edge = clamp(15 + m["sharpe"] * 10, 0, 30)
    significance = 0.5 * placebo_pct + 50.0 * dsr
    if n_assessed:
        robustness = (n_profitable / n_assessed) * 100 * (1 - max(0.0, best_share - 0.6))
    else:
        robustness = 30.0
    risk = clamp(100 - m["max_drawdown"] * 160, 0, 100)
    sample = min(70.0, n / 200 * 70) + min(1.0, span_days / 365) * 30
    pillars = {"edge": edge, "significance": significance, "robustness": robustness, "risk": risk, "sample": sample}
    score = 0.30 * edge + 0.20 * significance + 0.20 * robustness + 0.15 * risk + 0.15 * sample

    caps = []

    def cap(limit, why):
        caps.append({"max_score": limit, "max_grade": score_to_grade(limit), "reason": why})

    if m["total_return"] <= -0.2:
        cap(19.9, f"Loses {-m['total_return']:.0%} after {fee_bps} bps round-trip fees.")
    elif m["total_return"] <= 0:
        cap(34.9, f"Loses money after {fee_bps} bps round-trip fees.")
    if n < 30:
        cap(49.9, f"Only {n} trades. Too few to tell skill from luck.")
    if m["max_drawdown"] > 0.8:
        cap(34.9, f"Worst drawdown was {m['max_drawdown']:.0%}.")
    elif m["max_drawdown"] > 0.5:
        cap(49.9, f"Worst drawdown was {m['max_drawdown']:.0%}.")
    if dsr < 0.5:
        cap(49.9, f"Deflated Sharpe is {dsr:.0%}: little evidence the edge is real after {trials} tries.")
    elif dsr < 0.95:
        cap(79.9, f"Deflated Sharpe is {dsr:.0%}, short of the 95% needed for S.")
    if placebo_pct < 80:
        cap(64.9, f"Beats only {placebo_pct:.0f}% of {n_placebo} random-entry bots making the same trades.")
    elif placebo_pct < 95:
        cap(79.9, f"Beats {placebo_pct:.0f}% of random-entry bots; S needs 95%.")
    if n_assessed >= 2 and n_profitable == 0:
        cap(64.9, "No market regime was profitable.")
    elif n_assessed >= 2 and (n_profitable == 1 or best_share > 0.8):
        cap(64.9, f"Most of the profit came from one market regime ({best_regime}).")
    if span_days < 30:
        cap(64.9, f"Covers only {span_days:.0f} days of trading.")

    final = score
    for c in caps:
        final = min(final, c["max_score"])
    final = round(clamp(final, 0, 100), 1)
    for c in caps:
        c["binding"] = c["max_score"] < score
    caps.sort(key=lambda c: c["max_score"])
    return final, {k: round(v, 1) for k, v in pillars.items()}, round(score, 1), caps


def audit(trades, fee_bps: float = DEFAULT_FEE_BPS, trials: int = DEFAULT_TRIALS, placebo_n: int = 500,
          price_paths=None, price_source: str | None = None, seed=None, fee_levels=None):
    """Grade a list of round-trip trades. Returns a JSON-safe dict."""
    trades = sorted((t for t in trades if t["entry_price"] > 0 and t["exit_price"] > 0), key=lambda t: t["entry_time"])
    n = len(trades)
    if n == 0:
        raise ValueError("No usable trades.")
    fee_levels = list(fee_levels or FEE_LEVELS)
    t_start = min(t["entry_time"] for t in trades)
    t_end = max(t["exit_time"] for t in trades)
    span_days = max(1.0, (t_end - t_start) / 86400.0)
    gross = [t["side"] * (t["exit_price"] / t["entry_price"] - 1) for t in trades]

    paths = paths_from_trades(trades)
    if price_paths:
        for sym, pp in price_paths.items():
            if sym in paths and pp is not None and pp.start <= paths[sym].start and pp.end >= paths[sym].end:
                paths[sym] = pp
            elif sym in paths:
                price_source = None
    source = price_source or "your trades' own entry and exit prices"

    labels, win_days = _label_regimes(trades, paths, span_days)

    if seed is None:
        h = hashlib.sha256(repr([(round(t["entry_time"]), round(t["entry_price"], 8)) for t in trades[:500]]).encode()).hexdigest()
        seed = int(h[:12], 16)
    rng = random.Random(seed)
    n_placebo = int(max(100, min(placebo_n, 400_000 // max(1, n))))
    sims = _placebo_gross(trades, paths, n_placebo, rng)

    levels = []
    for f in fee_levels:
        fee = f / 1e4
        net = [g - fee for g in gross]
        m = _metrics(net, span_days)
        real = _log_total(gross, fee)
        totals = sorted(_log_total(s, fee) for s in sims)
        below = bisect.bisect_left(totals, real)
        ties = bisect.bisect_right(totals, real) - below
        placebo_pct = 100.0 * (below + 0.5 * ties) / len(totals)
        q = lambda x: math.exp(totals[min(len(totals) - 1, int(x * len(totals)))]) - 1  # noqa: E731
        ds = deflated_sharpe(net, trials)
        reg_rows, n_assessed, n_profitable, best_share, best = _regime_table(labels, net)
        score, pillars, raw, caps = _score(m, n, span_days, placebo_pct, ds["dsr"], n_assessed, n_profitable,
                                           best_share, best, f, trials, n_placebo)
        grade = score_to_grade(score)
        levels.append({
            "fee_bps": f, "score": score, "raw_score": raw, "grade": grade, "caps": caps, "pillars": pillars,
            "total_return": m["total_return"], "cagr": m["cagr"], "win_rate": m["win_rate"],
            "avg_trade_bps": m["avg_trade_bps"], "profit_factor": m["profit_factor"],
            "max_drawdown": m["max_drawdown"], "sharpe": m["sharpe"],
            "placebo_pct": placebo_pct,
            "placebo_quantiles": {"p5": q(0.05), "p50": q(0.5), "p95": q(0.95)},
            "deflated_sharpe": ds["dsr"], "sharpe_per_trade": ds["sharpe_per_trade"],
            "sharpe_benchmark": ds["benchmark"],
            "regimes": reg_rows, "regimes_profitable": n_profitable, "regimes_assessed": n_assessed,
            "best_regime_share": best_share,
            "curve": _downsample(m["curve"]),
        })

    sel = min(levels, key=lambda lv: abs(lv["fee_bps"] - fee_bps))
    mean_gross_bps = mean(gross) * 1e4
    symbols = sorted({t["symbol"] for t in trades})
    regime_counts = {r: labels.count(r) for r in REGIMES}
    return {
        "grade": sel["grade"], "score": sel["score"], "verdict": grade_verdict(sel["grade"]),
        "fee_bps": sel["fee_bps"], "trials": int(trials),
        "selected": sel, "levels": levels,
        "summary": {
            "trades": n, "symbols": symbols, "start": t_start, "end": t_end, "span_days": round(span_days, 1),
            "long_share": sum(1 for t in trades if t["side"] > 0) / n,
            "median_hold_hours": sorted((t["exit_time"] - t["entry_time"]) / 3600 for t in trades)[n // 2],
            "gross_avg_trade_bps": mean_gross_bps,
            "break_even_fee_bps": mean_gross_bps if mean_gross_bps > 0 else 0.0,
            "placebo_bots": n_placebo, "regime_window_days": round(win_days, 1),
            "regime_counts": regime_counts, "price_source": source,
        },
        "method": [
            "Every trade is counted at equal size (1x). Fees are a round-trip cost in basis points of notional.",
            f"Regimes use the trailing {win_days:.0f}-day move before each entry: Bull and Bear when the move is large, "
            "Chop or Quiet otherwise depending on how wide the price range was.",
            f"Placebo: {n_placebo} bots make the same trades (symbol, side, hold time) at random entry times. "
            f"Prices come from {source}.",
            f"Deflated Sharpe assumes you tried {int(trials)} versions before this one.",
        ],
    }
