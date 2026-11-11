"""Build script: extracts the exact sim functions from validation/harness.py
into the standalone dmoera package engine. AST-sliced, so the ported code is
byte-identical to production — parity by construction, not by hope."""
import ast, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
HARNESS = ROOT.parent.parent / "validation" / "harness.py"
PKG = ROOT / "dmoera"

src = HARNESS.read_text(encoding="utf-8")
tree = ast.parse(src)

WANTED = [
    "BANNED_IMPORTS", "BANNED_BUILTINS_ATTRS",
    "static_check", "build_safe_globals", "load_strategy",
    "SimTrade", "compute_alpha_metrics",
    "_infer_bar_seconds", "_infer_domain_key", "_default_fee_bps",
    "_DEFAULT_FEE_BPS", "_VALIDATION_LENIENCY", "_CURVE_MAX_POINTS", "_bucket_series",
    "_compute_friction_bps", "_compute_bar_regimes", "_compute_metrics",
    "simulate_strategy", "JUDGE_VERSION",
]

lines = src.splitlines(keepends=True)
segments = {}
for node in tree.body:
    name = None
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        name = node.name
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                name = t.id
    if name in WANTED and name not in segments:
        start = node.lineno
        if getattr(node, "decorator_list", None):
            start = min(start, min(d.lineno for d in node.decorator_list))
        segments[name] = "".join(lines[start - 1:node.end_lineno])

missing = [w for w in WANTED if w not in segments]
if missing:
    print("MISSING:", missing)
    sys.exit(1)

header = '''"""dmoera.engine — the exact simulation engine from the dMoERA validation
harness, extracted verbatim. Same bar loop, same exit logic (SL/TP intrabar,
trailing stop, allocation SL, horizon), same friction model, same 1000-bar
context cap, same metrics. Local results mean what the server's mean.

The two optional platform imports (config_manager, MarketImpactModel) are
wrapped in try/except with baked-in fallbacks, exactly as in production, so
this module runs standalone with no server present."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import ast
import math
import random

from dmoera.strategy_contract import (
    Strategy, BarContext, Signal, SignalDirection, Bar,
    validate_metadata, CONTRACT_VERSION,
)


'''

parts = [header]
order = [
    "BANNED_IMPORTS", "BANNED_BUILTINS_ATTRS",
    "static_check", "build_safe_globals", "load_strategy",
    "SimTrade", "compute_alpha_metrics",
    "_infer_bar_seconds", "_infer_domain_key", "_default_fee_bps",
    "_DEFAULT_FEE_BPS", "_VALIDATION_LENIENCY", "_CURVE_MAX_POINTS", "_bucket_series",
    "_compute_friction_bps", "_compute_bar_regimes", "_compute_metrics",
    "simulate_strategy", "JUDGE_VERSION",
]
for name in order:
    parts.append(segments[name])
    parts.append("\n\n")

out = "".join(parts).rstrip() + "\n"
(PKG / "engine.py").write_text(out, encoding="utf-8")
print(f"engine.py written: {len(out)} chars, {len(WANTED)} segments")
