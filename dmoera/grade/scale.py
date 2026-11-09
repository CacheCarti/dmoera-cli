"""The grade scale: S, A, B, C, D, F plus a 0-100 score. Mirrors validation/grading.py in dMoERA."""
from __future__ import annotations

GRADE_TIERS = [
    ("S", 80.0, "Verified edge: beats random entries, survives fees and holds up across market regimes."),
    ("A", 65.0, "Strong: real edge after fees, with some weak spots."),
    ("B", 50.0, "Marginal: some edge, but not enough evidence to trust it."),
    ("C", 35.0, "Weak: the results could easily be luck."),
    ("D", 20.0, "Fragile: fees or losses eat the edge."),
    ("F", 0.0, "Fail: loses money after realistic fees."),
]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_to_grade(score: float) -> str:
    s = clamp(float(score), 0.0, 100.0)
    for g, lo, _ in GRADE_TIERS:
        if s >= lo:
            return g
    return "F"


def grade_verdict(grade: str) -> str:
    for g, _, v in GRADE_TIERS:
        if g == grade:
            return v
    return ""
