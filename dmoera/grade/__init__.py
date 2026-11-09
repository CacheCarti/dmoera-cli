"""dmoera-grade: grade a trading bot's trade log S to F after real fees, regimes and luck."""
from .audit import audit, FEE_LEVELS, DEFAULT_FEE_BPS, DEFAULT_TRIALS  # noqa: F401
from .parsers import parse_any, ParseError  # noqa: F401
from .scale import GRADE_TIERS, score_to_grade  # noqa: F401

__version__ = "0.1.0"


def grade_text(text: str, filename: str = "", fee_bps: float = DEFAULT_FEE_BPS, trials: int = DEFAULT_TRIALS, **kw):
    """Parse a trade log and audit it in one call."""
    trades, fmt, warnings = parse_any(text, filename)
    out = audit(trades, fee_bps=fee_bps, trials=trials, **kw)
    out["format"] = fmt
    out["warnings"] = warnings
    return out
