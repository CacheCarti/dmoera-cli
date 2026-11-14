"""dmoera - local backtesting, grading and validation for dMoERA strategies.

Runs the exact simulation engine the dMoERA validation harness uses, on your
own machine, against Binance data you cache locally (or any OHLCV CSV you
bring). Every bot in the Crypto-Strategies corpus runs unmodified.
"""
__version__ = "0.4.1"

import sys as _sys
import types as _types

from dmoera.strategy_contract import (  # noqa: F401
    Strategy, BarContext, Signal, SignalDirection, Bar,
    validate_metadata, CONTRACT_VERSION,
)
from dmoera.engine import simulate_strategy, SimTrade, load_strategy  # noqa: F401

# Back-compat alias: corpus bots do `from domains.strategy_contract import
# ...`. The real contract lives at dmoera.strategy_contract; registering the
# alias here keeps that import path working for anything that imported
# dmoera first (the engine additionally injects it for sandboxed strategy
# code, see engine.build_safe_globals).
if "domains" not in _sys.modules:
    _domains = _types.ModuleType("domains")
    _domains.__path__ = []
    _sys.modules["domains"] = _domains
_domains_mod = _sys.modules["domains"]
_contract_mod = _sys.modules[__name__ + ".strategy_contract"]
if "domains.strategy_contract" not in _sys.modules:
    _sys.modules["domains.strategy_contract"] = _contract_mod
_domains_mod.strategy_contract = _contract_mod
