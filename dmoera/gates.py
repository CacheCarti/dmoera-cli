"""The 6-gate deep battery — local, open, pure math (stdlib only).

This is the OPEN METHOD behind dMoERA's deep certification lane: the same
gates the production battery runs on the 6.25-year research window
(2020-01-01 -> 2026-04-01, the sealed-slice boundary):

  min_trades, net_tstat, deflated Sharpe, regime_split, staleness,
  plateau/PBO.

Local runs have no trial ledger (n_trials=1 -> the DSR correction term is
exactly 0), so the local battery is the METHOD, not the enforcement. The
sealed server-side exam (`dmoera certify`) is where the enforced ledger N,
the sealed holdout and the hash-bound verdict live.

Honesty rule: with no variant grid supplied, plateau and PBO record N/A
and never block — a single bot cannot be judged on a grid it doesn't have.
"""
import math
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Dict, List, Optional, Tuple

REGIME_PERIODS: List[Tuple[str, int, int]] = [
    ("2020-21_bull", 2020, 2022),
    ("2022_crash", 2022, 2023),
    ("2023-24_recovery", 2023, 2025),
    ("2025-26_bear", 2025, 2026),
]

MIN_TRADES = 50
MIN_TSTAT = 2.0
MIN_REGIMES_POSITIVE = 3
SPECIALIST_MIN_N = 15
SPECIALIST_MIN_T = 2.0
RESEARCH_START = datetime(2020, 1, 1)
RESEARCH_END = datetime(2026, 4, 1)   # the sealed final-exam slice starts here


def net_tstat(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    if sd == 0:
        return 0.0
    return mean / (sd / math.sqrt(n))


def expected_max_sharpe_under_null(n_trials: int) -> float:
    """E[max Sharpe] across n_trials zero-skill strategies (Bailey &
    Lopez de Prado 2014), in total-Sharpe units. Exact two-quantile form:
    E[max] = (1-gamma)*Z(1-1/N) + gamma*Z(1-1/(N*e))."""
    if n_trials <= 1:
        return 0.0
    euler = 0.5772156649
    nd = NormalDist()
    z1 = nd.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = nd.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return (1.0 - euler) * z1 + euler * z2


def deflated_sharpe_ratio(observed_sharpe: float, n_trades: int,
                          n_trials: int = 1,
                          skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    observed_sharpe: TOTAL Sharpe (mean/std * sqrt(n)) — not per-trade.
    Returns observed minus the score pure luck expects at n_trials
    attempts, after a non-normality haircut. Positive = beats selection
    bias. Both terms in total-Sharpe units (dimensionally sound)."""
    if n_trades < 2:
        return 0.0
    sr_per_obs = observed_sharpe / math.sqrt(n_trades)
    variance_factor = (1.0 - skewness * sr_per_obs
                       + ((kurtosis - 1.0) / 4.0) * sr_per_obs ** 2)
    if variance_factor <= 0:
        return 0.0
    effective_sharpe = observed_sharpe / math.sqrt(variance_factor)
    return round(effective_sharpe - expected_max_sharpe_under_null(n_trials), 4)


def _year(ts) -> int:
    if isinstance(ts, str):
        return int(ts[:4])
    return ts.year


def regime_split(trades: List[dict]) -> Dict[str, dict]:
    """Coverage-aware regime split: net + positive-flag per regime period.
    A regime with <10 trades can't be positive (same floor as production)."""
    out = {}
    for name, y0, y1 in REGIME_PERIODS:
        sel = [t["net_bps"] for t in trades if y0 <= _year(t["opened_at"]) < y1]
        net = sum(sel)
        out[name] = {
            "n": len(sel),
            "net_bps": round(net, 1),
            "positive": net > 0 and len(sel) >= 10,
            "evaluable": True,
        }
    return out


def specialist_regime_gate(trades: List[dict], regimes: Dict[str, dict],
                           min_n: int = SPECIALIST_MIN_N,
                           min_t: float = SPECIALIST_MIN_T) -> dict:
    """One regime with a statistically real edge (n >= min_n, within-regime
    t >= min_t, net > 0) and not a net loser over the full history."""
    best = {"regime": None, "t": 0.0, "n": 0, "net_bps": 0.0}
    for name, y0, y1 in REGIME_PERIODS:
        r = regimes.get(name)
        if not r or not r.get("evaluable") or r["n"] < min_n:
            continue
        sel = [t["net_bps"] for t in trades if y0 <= _year(t["opened_at"]) < y1]
        t_in = net_tstat(sel)
        if t_in > best["t"]:
            best = {"regime": name, "t": round(t_in, 3), "n": len(sel),
                    "net_bps": round(sum(sel), 1)}
    full_net = sum(t["net_bps"] for t in trades)
    ok = (best["regime"] is not None and best["t"] >= min_t
          and best["net_bps"] > 0 and full_net >= 0)
    return {"ok": ok, "best_regime": best["regime"], "regime_t": best["t"],
            "regime_n": best["n"], "regime_net_bps": best["net_bps"],
            "full_history_net_bps": round(full_net, 1)}


def staleness_gate(trades: List[dict], months: int,
                   end: datetime) -> dict:
    """Decay check: net must be >= 0 over the trailing `months` before the
    window end. A bot that earned everything in 2023-24 and bled since is
    STALE, not proven."""
    if not trades or not months:
        return {"ok": None, "skipped": True}
    end_dt = end if isinstance(end, datetime) else datetime.fromisoformat(str(end))
    cutoff = end_dt - timedelta(days=int(months * 30.44))
    recent = [t["net_bps"] for t in trades
              if cutoff <= _ts(t["closed_at"]) <= end_dt]
    net = sum(recent)
    return {"ok": len(recent) >= 10 and net >= 0,
            "window_months": months, "n_recent": len(recent),
            "recent_net_bps": round(net, 1)}


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def evaluate_battery(trades: List[dict], n_trials: int = 1,
                     min_trades: int = MIN_TRADES,
                     min_tstat: float = MIN_TSTAT,
                     staleness_months: int = 0,
                     research_end: Optional[datetime] = None) -> dict:
    """Run every gate on a trade list of {"net_bps", "opened_at", "closed_at"}.
    Returns the verdict dict — same shape as the production battery."""
    n = len(trades)
    xs = [t["net_bps"] for t in trades]
    tstat = net_tstat(xs)
    net_total = sum(xs)
    wins = sum(1 for x in xs if x > 0)
    wr = wins / n if n else 0.0

    sharpe_total = 0.0
    if n >= 2:
        mean = sum(xs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
        if sd > 0:
            sharpe_total = mean / sd * math.sqrt(n)
    # distribution shape for the DSR haircut
    skew = 0.0
    kurt = 3.0
    if n >= 3:
        m = mean
        s2 = sum((x - m) ** 2 for x in xs) / (n - 1)
        s3 = sum((x - m) ** 3 for x in xs) / (n - 1)
        s4 = sum((x - m) ** 4 for x in xs) / (n - 1)
        if s2 > 0:
            skew = (s3 / s2 ** 1.5) * math.sqrt(n * (n - 1)) / (n - 2) if n > 2 else 0.0
            kurt = s4 / s2 ** 2 if s2 > 0 else 3.0
    dsr = deflated_sharpe_ratio(sharpe_total, n, max(1, n_trials),
                                skewness=skew, kurtosis=kurt)

    regimes = regime_split(trades)
    evaluable = [r for r in regimes.values() if r["evaluable"]]
    n_pos = sum(1 for r in evaluable if r["positive"])
    universal_ok = len(evaluable) >= MIN_REGIMES_POSITIVE and n_pos >= MIN_REGIMES_POSITIVE
    specialist = specialist_regime_gate(trades, regimes)
    regime_ok = universal_ok or specialist["ok"]

    end = research_end or RESEARCH_END
    staleness = (staleness_gate(trades, staleness_months, end)
                 if staleness_months else {"ok": None, "skipped": True})

    gates = {
        "min_trades": {"ok": n >= min_trades, "n": n, "required": min_trades},
        "net_tstat": {"ok": tstat >= min_tstat, "tstat": round(tstat, 3),
                      "required": min_tstat},
        "deflated_sharpe": {"ok": dsr > 0, "dsr": dsr, "n_trials": max(1, n_trials)},
        "regime_split": {"ok": regime_ok, "mode": ("universal" if universal_ok
                                                   else "specialist" if specialist["ok"]
                                                   else "fail"),
                         "positive_regimes": n_pos,
                         "evaluable_regimes": len(evaluable),
                         "specialist": specialist, "detail": regimes},
        "pbo": {"ok": None, "skipped": True,
                "reason": "no variant grid supplied locally"},
        "plateau": {"ok": None, "skipped": True,
                    "reason": "no variant grid supplied locally"},
    }
    if staleness_months:
        gates["staleness"] = staleness

    hard = ["min_trades", "net_tstat", "deflated_sharpe", "regime_split"]
    if staleness_months and staleness.get("ok") is not None:
        hard.append("staleness")
    ok = all(gates[g]["ok"] for g in hard)
    return {
        "admitted": ok,
        "n_trades": n,
        "net_bps_total": round(net_total, 1),
        "win_rate": round(wr, 4),
        "sharpe_total": round(sharpe_total, 3),
        "gates": gates,
    }
