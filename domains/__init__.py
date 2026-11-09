"""Compatibility shim: repo bots do `from domains.strategy_contract import ...`.

Installing `dmoera` provides this exact import path so corpus bots run
unmodified. The classes are the same objects as dmoera.strategy_contract.
"""
from dmoera.strategy_contract import (  # noqa: F401
    Signal, SignalDirection, Bar, BarContext, Strategy,
    validate_metadata, CONTRACT_VERSION,
)
