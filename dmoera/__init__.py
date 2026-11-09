"""dmoera - local backtesting, grading and validation for dMoERA strategies.

Runs the exact simulation engine the dMoERA validation harness uses, on your
own machine, against Binance data you cache locally (or any OHLCV CSV you
bring). Every bot in the Crypto-Strategies corpus runs unmodified.
"""
__version__ = "0.2.0"

from dmoera.strategy_contract import (  # noqa: F401
    Strategy, BarContext, Signal, SignalDirection, Bar,
    validate_metadata, CONTRACT_VERSION,
)
from dmoera.engine import simulate_strategy, SimTrade, load_strategy  # noqa: F401
