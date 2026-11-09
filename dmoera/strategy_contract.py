"""
DMoEn Strategy Contract v2

The frozen interface for creator-uploaded strategies. This is the foundation
of the creator platform — nothing downstream is trustworthy without it.

Key design decisions:
  - on_bar(ctx: BarContext) is the primary entry point (not on_tick)
  - BarContext exposes ONLY past-and-present data — no forward indexing
  - Determinism is enforced: seeded RNG via ctx.rng, no wall clock, no network
  - METADATA declares SL/TP/hold time, validated against realized behavior
  - Contract version is pinned so future changes don't silently break old bots

Backward compatibility:
  - Legacy Strategy (v1) with on_tick(ctx: StrategyContext) still works
  - The sandbox runner auto-detects which interface a bot implements
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum
import random


CONTRACT_VERSION = 2


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"


@dataclass
class Signal:
    """A trading signal returned by a strategy."""
    direction: SignalDirection
    confidence: float = 0.5
    stop_loss_bps: Optional[float] = None
    take_profit_bps: Optional[float] = None
    horizon_seconds: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.direction, SignalDirection):
            self.direction = SignalDirection(str(self.direction).lower())
        self.confidence = float(max(0.0, min(1.0, self.confidence)))

    def is_active(self) -> bool:
        return self.direction not in (SignalDirection.FLAT, SignalDirection.HOLD)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "confidence": self.confidence,
            "stop_loss_bps": self.stop_loss_bps,
            "take_profit_bps": self.take_profit_bps,
            "horizon_seconds": self.horizon_seconds,
            "metadata": self.metadata,
        }


@dataclass
class Bar:
    """A single OHLCV bar — the atomic unit of market data in the contract."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class BarContext:
    """
    Point-in-time context passed to Strategy.on_bar().

    GUARANTEES:
      - bars list contains ONLY bars up to and including the current bar
      - No access to future data, full series handles, or wall clock
      - rng is a seeded Random — same seed + same data = same output
      - position reflects the strategy's current open position (if any)

    This is the ONLY data a strategy receives. No engine internals leak.
    """
    bot_id: str
    symbol: str
    bar: Bar
    bars: List[Bar] = field(default_factory=list)
    position: Optional[Dict[str, Any]] = None
    rng: random.Random = field(default_factory=lambda: random.Random(42))
    bar_index: int = 0
    regime: str = "normal"
    features: Dict[str, Any] = field(default_factory=dict)
    market: Dict[str, Any] = field(default_factory=dict)
    # Seconds per bar of the engine feeding this context (60 = 1-minute sim,
    # 3600 = 1h fast-research engine, 86400 = daily). Strategies that want
    # their lookbacks to mean the same THING at any granularity should
    # compute bar counts as `horizon_seconds // ctx.bar_seconds` instead of
    # hardcoding bar counts. Default 60 preserves v2 semantics exactly.
    bar_seconds: int = 60

    def closes(self, lookback: int = 100) -> List[float]:
        """Recent closing prices, oldest first."""
        b = self.bars[-lookback:] if len(self.bars) > lookback else self.bars
        return [b.close for b in b]

    def opens(self, lookback: int = 100) -> List[float]:
        b = self.bars[-lookback:] if len(self.bars) > lookback else self.bars
        return [b.open for b in b]

    def highs(self, lookback: int = 100) -> List[float]:
        b = self.bars[-lookback:] if len(self.bars) > lookback else self.bars
        return [b.high for b in b]

    def lows(self, lookback: int = 100) -> List[float]:
        b = self.bars[-lookback:] if len(self.bars) > lookback else self.bars
        return [b.low for b in b]

    def volumes(self, lookback: int = 100) -> List[float]:
        b = self.bars[-lookback:] if len(self.bars) > lookback else self.bars
        return [b.volume for b in b]

    def has_position(self) -> bool:
        return self.position is not None

    def position_direction(self) -> Optional[str]:
        if self.position:
            return self.position.get("direction")
        return None

    def signal(self, direction: str, confidence: float = 0.5,
               stop_loss_bps: Optional[float] = None,
               take_profit_bps: Optional[float] = None,
               horizon_seconds: Optional[int] = None,
               metadata: Optional[Dict[str, Any]] = None) -> Signal:
        """Factory method to create a Signal."""
        return Signal(
            direction=direction,
            confidence=confidence,
            stop_loss_bps=stop_loss_bps,
            take_profit_bps=take_profit_bps,
            horizon_seconds=horizon_seconds,
            metadata=metadata or {},
        )

    def __repr__(self) -> str:
        return f"BarContext(bot_id={self.bot_id}, symbol={self.symbol}, bar_index={self.bar_index}, close={self.bar.close})"


class Strategy(ABC):
    """
    Base class for all DMoEn strategies (Contract v2).

    Creators subclass this and implement on_bar(). The METADATA dict declares
    the strategy's intended parameters — these are validated against realized
    behavior during the validation pipeline.

    Required METADATA fields:
        name: str           — human-readable strategy name
        domain: str         — trading domain (e.g. "eth_usdc")
        declared_sl_bps: float   — intended stop-loss in bps
        declared_tp_bps: float   — intended take-profit in bps
        declared_hold_seconds: int — intended average hold time
        warmup_bars: int    — bars needed before first signal

    Optional lifecycle hooks:
        initialize(ctx)     — one-time setup
        on_bar(ctx)         — called once per closed bar (PRIMARY)
        on_tick(ctx)        — legacy v1 compat, called on each price tick
        on_position_close(ctx, position) — called when a position closes
    """
    METADATA: Dict[str, Any] = {}

    def initialize(self, ctx: BarContext) -> None:
        """One-time setup before the strategy starts receiving bars."""
        pass

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        """Called once per closed bar. Must return a Signal or None."""
        pass

    def on_position_close(self, ctx: BarContext, position: Dict[str, Any]) -> None:
        """Called when a position opened by this strategy is closed."""
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(metadata={self.METADATA})"


def validate_metadata(metadata: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """Validate that a strategy's METADATA dict has all required fields."""
    # Hard ceiling on declared SL: no sane strategy risks more than 20% of
    # principal on a single trade. At any leverage >= 1x, a SL wider than
    # 10000 bps (100%) is beyond liquidation — the exchange liquidates
    # before the SL can fire, making the bot's risk plan meaningless.
    # 2000 bps (20%) is the absolute ceiling; even at 5x leverage the
    # effective SL is 400 bps (4% move), well inside liquidation distance.
    MAX_DECLARED_SL_BPS = 2000.0
    MAX_DECLARED_TP_BPS = 5000.0  # 50% of principal — generous but bounded

    required = ["name", "domain", "declared_sl_bps", "declared_tp_bps",
                "declared_hold_seconds", "warmup_bars"]
    for key in required:
        if key not in metadata:
            return False, f"Missing required METADATA field: {key}"
    if not isinstance(metadata["name"], str) or not metadata["name"]:
        return False, "METADATA.name must be a non-empty string"
    if not isinstance(metadata["domain"], str) or not metadata["domain"]:
        return False, "METADATA.domain must be a non-empty string"
    if not isinstance(metadata["declared_sl_bps"], (int, float)) or metadata["declared_sl_bps"] < 0:
        return False, "METADATA.declared_sl_bps must be a non-negative number"
    if not isinstance(metadata["declared_tp_bps"], (int, float)) or metadata["declared_tp_bps"] < 0:
        return False, "METADATA.declared_tp_bps must be a non-negative number"
    if metadata["declared_sl_bps"] > MAX_DECLARED_SL_BPS:
        return False, (f"METADATA.declared_sl_bps {metadata['declared_sl_bps']} exceeds "
                       f"max {MAX_DECLARED_SL_BPS} bps (20% of principal). "
                       f"A stop this wide would sit beyond the liquidation price at any leverage.")
    if metadata["declared_tp_bps"] > MAX_DECLARED_TP_BPS:
        return False, (f"METADATA.declared_tp_bps {metadata['declared_tp_bps']} exceeds "
                       f"max {MAX_DECLARED_TP_BPS} bps (50% of principal). "
                       f"A target this wide is unreachable within a reasonable hold time.")
    if not isinstance(metadata["declared_hold_seconds"], int) or metadata["declared_hold_seconds"] < 0:
        return False, "METADATA.declared_hold_seconds must be a non-negative integer"
    if not isinstance(metadata["warmup_bars"], int) or metadata["warmup_bars"] < 0:
        return False, "METADATA.warmup_bars must be a non-negative integer"
    return True, None
