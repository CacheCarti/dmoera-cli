"""dmoera.engine — the exact simulation engine from the dMoERA validation
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


BANNED_IMPORTS = {
    "os", "sys", "subprocess", "socket", "http", "urllib",
    "requests", "shutil", "pathlib", "io", "pickle", "shelve",
    "ctypes", "multiprocessing", "threading", "asyncio",
    "importlib", "__import__", "builtins", "gc",
}


BANNED_BUILTINS_ATTRS = {
    "__import__", "__subclasses__", "__bases__", "__class__",
    "__globals__", "__builtins__", "__code__", "__func__",
    # numpy/pandas I/O surface — blocked so enabling those libraries for
    # compute doesn't also hand uploaded code a filesystem/network escape
    # hatch (np.load/save, np.memmap, pd.read_csv accepting a URL, etc.).
    "load", "save", "loads", "dump", "dumps", "fromfile", "tofile",
    "memmap", "loadtxt", "savetxt", "genfromtxt", "savez", "savez_compressed",
    # np.DataSource: confirmed live exploit (2026-08-08) — DataSource().open()
    # does real network fetches AND local file reads, using neither a banned
    # name nor a banned import. It also survives the runtime I/O lockdown in
    # sandbox/strategy_worker.py, because numpy/lib/_datasource.py captures
    # `_open = io.open` as a direct reference at numpy's own import time —
    # patching the io.open ATTRIBUTE afterward doesn't touch that already-
    # captured pointer. This compile-time block is the one that actually
    # holds for this specific class, since it runs before any code executes.
    "DataSource",
    # scipy.io: same shape of hole, enumerated 2026-08-08 — none of these
    # were in any denylist before. loadmat/savemat etc. are direct
    # filesystem I/O; none require a banned import (`import scipy.io`
    # passes since "scipy" is an allowed top-level import).
    "loadmat", "savemat", "mmread", "mmwrite", "hb_read", "hb_write",
    "readsav", "FortranFile", "netcdf_file",
    # pandas I/O, enumerated from the installed version (`dir(pd)` filtered to
    # read_*/to_*) rather than guessed — pandas is compute-enabled 2026-08-08.
    # read_csv/read_json/read_html accept a bare URL, making this the same
    # class of hole as np.DataSource: real network egress via a name nobody
    # would think to ban by hand. Also closes clipboard access (OS-level,
    # not file/socket — the audit hook backstop below doesn't cover it).
    "read_csv", "read_json", "read_html", "read_excel", "read_sql",
    "read_sql_query", "read_sql_table", "read_pickle", "read_parquet",
    "read_feather", "read_hdf", "read_stata", "read_sas", "read_spss",
    "read_orc", "read_gbq", "read_clipboard", "read_fwf", "read_table",
    "read_xml", "to_csv", "to_json", "to_html", "to_excel", "to_sql",
    "to_pickle", "to_parquet", "to_feather", "to_hdf", "to_stata",
    "to_clipboard", "to_gbq", "to_xml",
}


def static_check(code: str) -> Tuple[bool, Optional[str]]:
    """
    Static analysis: syntax, banned imports, dunder traversal, METADATA validation.
    Returns (passed, error_message).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BANNED_IMPORTS:
                    return False, f"Banned import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module in BANNED_IMPORTS:
                return False, f"Banned import: {node.module}"
            # `from scipy.io import FortranFile` names FortranFile as an
            # ImportFrom alias, never as an ast.Attribute — the Attribute
            # check below only catches `scipy.io.FortranFile` (dotted access),
            # not the `from X import Y` form. Found live via static_check
            # testing (2026-08-08): FortranFile passed while the identical
            # capability via dotted access was correctly blocked.
            for alias in node.names:
                if alias.name in BANNED_BUILTINS_ATTRS:
                    return False, f"Banned attribute access: {alias.name}"
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_BUILTINS_ATTRS:
                return False, f"Banned attribute access: {node.attr}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                return False, "Direct __import__ call is banned"

    return True, None


def build_safe_globals() -> dict:
    """Build restricted globals for strategy execution."""
    import builtins
    import math
    import statistics

    allowed_builtins = [
        "abs", "all", "any", "bool", "complex", "dict", "divmod",
        "enumerate", "filter", "float", "format", "frozenset", "hasattr",
        "hash", "hex", "id", "int", "isinstance", "issubclass", "iter",
        "len", "list", "map", "max", "min", "next", "oct", "ord",
        "pow", "range", "repr", "reversed", "round", "set", "slice", "sum",
        "sorted", "str", "tuple", "type", "zip",
        "True", "False", "None",
        "__build_class__", "__name__",
        "Exception", "ValueError", "TypeError", "IndexError", "KeyError",
        "ArithmeticError", "ZeroDivisionError", "StopIteration",
    ]

    safe_builtins = {}
    for name in allowed_builtins:
        try:
            safe_builtins[name] = getattr(builtins, name)
        except AttributeError:
            pass

    real_import = builtins.__import__
    def _safe_import(name, globals=None, locals=None, fromlist=None, level=0):
        # Pure, deterministic, no-I/O stdlib modules only. Deliberately EXCLUDED:
        #   time  -> wall-clock reads break backtest determinism
        #   json / io / os   -> I/O or filesystem access breaks isolation
        #
        # datetime is ALLOWED in the sandbox for fun/meme strategies (moon cycle
        # bots, day-of-week patterns, etc). It's pure Python with no I/O — safe
        # alongside the _lockdown.py audit hook backstop. However, datetime
        # imports are BLOCKED at submission time (see upload_service.py
        # check_submission_safety) — strategies using datetime can run in the
        # sandbox but can't enter the marketplace, because time-based logic
        # can't be deterministically validated against historical data.
        #
        # numpy / ta / scipy / statsmodels / pandas: compute-only quant
        # libraries, enabled because creators need the same tools our own
        # bots use (domains/ml_bots*.py already import numpy and ta). Safe
        # alongside the BANNED_BUILTINS_ATTRS block above, which strips the
        # I/O methods each library exposes (np.load/save, np.memmap,
        # pd.read_csv/to_pickle/etc.) — see that block's comments for how the
        # pandas list was derived. Also backstopped at runtime by the audit
        # hook in sandbox/_lockdown.py, which blocks the underlying file/socket
        # open regardless of which attribute name reached it — belt and
        # suspenders, not reliance on either alone.
        #
        # scikit-learn is deliberately still excluded. Unlike the I/O-method
        # bans above, its risk isn't enumerable the same way: APIs like
        # FunctionTransformer(func=...) accept an arbitrary callable by
        # design, which is a different SHAPE of hole than "this named method
        # touches disk" and needs its own dedicated review, not a name added
        # to this set under time pressure.
        allowed = {
            "domains", "domains.strategy_contract", "typing",
            "math", "statistics", "random",
            "collections", "itertools", "functools", "dataclasses",
            "decimal", "fractions", "enum", "heapq", "bisect",
            "datetime",
            "numpy", "ta", "scipy", "statsmodels", "pandas",
        }
        root = name.split(".")[0]
        if name not in allowed and root not in allowed:
            raise ImportError(f"Import of '{name}' is not allowed in the sandbox")
        return real_import(name, globals, locals, fromlist, level)
    safe_builtins["__import__"] = _safe_import

    import typing
    safe_globals = {
        "__builtins__": safe_builtins,
        "math": math,
        "statistics": statistics,
        "random": random,
        "typing": typing,
        "Optional": typing.Optional,
        "Dict": typing.Dict,
        "List": typing.List,
        "Any": typing.Any,
        "Tuple": typing.Tuple,
        "Optional": typing.Optional,
    }

    safe_globals["Strategy"] = Strategy
    safe_globals["Signal"] = Signal
    safe_globals["SignalDirection"] = SignalDirection
    safe_globals["BarContext"] = BarContext
    safe_globals["Bar"] = Bar

    import sys
    import types as _types
    if "domains" not in sys.modules:
        domains_mod = _types.ModuleType("domains")
        contract_mod = _types.ModuleType("domains.strategy_contract")
        contract_mod.Strategy = Strategy
        contract_mod.Signal = Signal
        contract_mod.SignalDirection = SignalDirection
        contract_mod.BarContext = BarContext
        contract_mod.Bar = Bar
        contract_mod.validate_metadata = validate_metadata
        contract_mod.CONTRACT_VERSION = CONTRACT_VERSION
        domains_mod.strategy_contract = contract_mod
        sys.modules["domains"] = domains_mod
        sys.modules["domains.strategy_contract"] = contract_mod

    return safe_globals


def load_strategy(code: str) -> Tuple[Optional[type], Optional[str]]:
    """
    Compile and extract the Strategy subclass from uploaded code.
    Returns (strategy_class, error_message).

    static_check() runs here, not just at the caller — found (2026-08-08) that
    sandbox/live_worker.py, which runs a GRADUATED bot's code on every live
    bar forever, called load_strategy() directly with no static_check()
    anywhere in the file. Relying on every caller to remember to check first
    is exactly the kind of thing that stays fine for months and then isn't.
    This makes it structural: nothing can reach exec() through this function
    without passing the check, regardless of which of the 5 call sites (2
    validation stages, sandbox backtest, walk-forward, live) got here."""
    ok, err = static_check(code)
    if not ok:
        return None, err

    safe_globals = build_safe_globals()

    try:
        compiled = compile(code, "<strategy_upload>", "exec")
        # A single dict for both globals and locals (not two separate dicts)
        # is required here: with separate dicts, top-level statements like
        # `import numpy as np` bind `np` into the *locals* dict, but a
        # method's __globals__ is fixed to the *globals* dict at def time —
        # so on_bar()/initialize() could never see a module-level import,
        # and any strategy using numpy/ta/scipy would crash with
        # NameError the first time on_bar() actually ran, despite loading
        # and even compiling without error. Using one dict for both makes
        # top-level imports visible exactly like a normal module.
        exec(compiled, safe_globals)
    except Exception as e:
        return None, f"Compilation error: {type(e).__name__}: {e}"

    for obj in safe_globals.values():
        if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy:
            return obj, None

    return None, "No Strategy subclass found in uploaded code"


@dataclass
class SimTrade:
    """A single simulated trade."""
    bar_index: int
    timestamp: str
    direction: str
    entry_price: float
    exit_price: float
    return_bps: float
    confidence: float
    hold_bars: int
    exit_reason: str = "signal_flip"
    regime: str = "normal"
    position_size_usd: float = 0.0
    equity_after: float = 0.0
    entry_reason: str = ""

    def __post_init__(self):
        # Alias for backward compat: upload_service reads "realized_bps"
        self.realized_bps = self.return_bps


def compute_alpha_metrics(trades: List[Any], candles: List[dict]) -> Dict[str, Any]:
    """Market-neutralised performance: what the strategy added beyond the move
    it was sitting in.

    WHY THIS EXISTS. The out-of-sample gate used to require absolute profit.
    Measured over a year of hourly BTC/ETH/SOL, **47-54% of OOS-sized windows
    are down-markets**, so roughly half of all submissions were being asked to
    make money in a falling market to pass — and a long-biased strategy that
    lost 3% while the market lost 15% was rejected as unprofitable despite
    having obvious skill. That gate was scoring the calendar as much as the bot.

    The benchmark for each trade is what a position of that DURATION and
    DIRECTION would have earned from the window's average drift — not the
    market's realised move over the trade's own bars:

        alpha = realized - side * drift_per_bar * hold_bars   (side = +1 / -1)

    Subtracting the trade's OWN realised market move (the obvious first
    attempt, and one this function briefly did) is degenerate: for a horizon
    exit the strategy's return IS that move minus friction, so alpha collapses
    to the constant -friction for every trade, variance goes to zero and the
    t-statistic explodes. The drift benchmark instead asks the question that
    matters — "did you pick better-than-average moments to hold that side?" —
    which is market-timing skill and has real variance.

    Sanity check on the definition: a permanently-long strategy with no timing
    skill earns roughly drift*hold - friction per trade, so its alpha is about
    -friction. It correctly scores as adding nothing. A strategy that shorted
    only during the dips of a rising market earns positive alpha even though
    it fought the trend, which is also correct — it timed.

    Returns {} when it cannot be computed (fewer than 2 usable trades, or
    candles too short to cover the holds). Callers must treat that as
    "unknown", never as zero: silently reading a missing alpha as 0.0 would
    fail every strategy on a data problem while blaming the strategy.
    """
    if not trades or len(candles) < 2:
        return {}
    closes = [float(c["close"]) for c in candles]
    n = len(closes)
    if closes[0] <= 0 or n < 2:
        return {}
    drift_per_bar = ((closes[-1] - closes[0]) / closes[0] * 10000.0) / (n - 1)

    alphas: List[float] = []
    for t in trades:
        entry_i = int(getattr(t, "bar_index", None) if not isinstance(t, dict)
                      else t.get("bar_index", -1))
        hold = int(getattr(t, "hold_bars", 0) if not isinstance(t, dict)
                   else t.get("hold_bars", 0) or 0)
        realized = float(getattr(t, "return_bps", 0.0) if not isinstance(t, dict)
                         else t.get("return_bps", 0.0) or 0.0)
        direction = (getattr(t, "direction", "long") if not isinstance(t, dict)
                     else t.get("direction", "long"))
        if entry_i < 0 or entry_i >= n or hold < 0:
            continue
        side = 1.0 if direction == "long" else -1.0
        alphas.append(realized - side * drift_per_bar * hold)

    if len(alphas) < 2:
        return {}
    mean_a = sum(alphas) / len(alphas)
    var = sum((a - mean_a) ** 2 for a in alphas) / len(alphas)
    std = math.sqrt(var)
    # Same t-statistic form as metrics["sharpe"], so MIN_OOS_SHARPE means the
    # same thing whichever series it is applied to.
    #
    # The degenerate guard is RELATIVE, not a fixed 1e-12. Identical trades
    # (a strategy with a fixed hold on a smooth stretch) leave a std of ~1e-14
    # that is pure floating-point residue but still clears an absolute epsilon,
    # producing a t-statistic in the tens of thousands and a gate decided by
    # rounding error. If the spread is negligible next to the level, there is
    # no distribution here to test.
    scale = max(abs(mean_a), 1e-9)
    t_stat = (mean_a / std * math.sqrt(len(alphas))) if std > scale * 1e-6 else 0.0
    return {
        "alpha_total_bps": round(sum(alphas), 2),
        "alpha_mean_bps": round(mean_a, 3),
        "alpha_sharpe": round(t_stat, 4),
        "alpha_trades": len(alphas),
        "alpha_win_rate": round(sum(1 for a in alphas if a > 0) / len(alphas), 4),
        "benchmark_drift_bps_per_bar": round(drift_per_bar, 5),
    }


def _infer_bar_seconds(candles: List[dict]) -> int:
    """Infer bar duration in seconds from candle timestamps."""
    if len(candles) < 2:
        return 300
    t0 = candles[0].get("timestamp")
    t1 = candles[1].get("timestamp")
    if isinstance(t0, datetime) and isinstance(t1, datetime):
        return max(1, int((t1 - t0).total_seconds()))
    if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
        return max(1, int(float(t1) - float(t0)))
    return 300


def _infer_domain_key(symbol: str) -> str:
    """Map symbol to domain key for friction lookup."""
    s = symbol.lower().replace("usdt", "").replace("usd", "")
    mapping = {"eth": "eth_usdc", "btc": "btc_usdc", "sol": "sol_usdc"}
    return mapping.get(s, "eth_usdc")


def _default_fee_bps() -> float:
    """Per-side exchange fee, derived from live config instead of a hardcoded
    guess, so validation cannot silently drift from what a deployed bot
    actually pays (this is exactly what happened before: a hardcoded 8.0
    here vs. the real 4.0 bps taker fee in ExecutionConfig — validation
    charged strategies ~2.2x live cost round-trip, rejecting bots that
    would have been profitable).

    Uses TAKER (not maker) fee: ExecutionConfig.limit_order_enabled defaults
    to False, i.e. live execution is market orders by default, so taker is
    the honest default here — matching what most bots will actually pay,
    not the more optimistic maker rate. Evaluated once at import time (a
    plain module-level constant, not a live per-call config read) because
    this only needs to track the deployed default, not update mid-backtest.
    """
    try:
        from config_manager import get_config
        return get_config().execution.fee_taker_pct * 10000.0
    except Exception:
        return 4.0  # last-known real taker fee, if config is unavailable


_DEFAULT_FEE_BPS = _default_fee_bps()


_VALIDATION_LENIENCY = 0.85


_CURVE_MAX_POINTS = 120


def _bucket_series(values: List[float], max_points: int = _CURVE_MAX_POINTS) -> List[float]:
    """Downsample a numeric series to <= max_points, preserving the first and
    last points and the overall shape. Even-stride sampling; no timestamps
    or prices are attached, so the curve shows the PnL path only."""
    n = len(values)
    if n <= max_points:
        return list(values)
    stride = n / max_points
    out = []
    for k in range(max_points):
        idx = int(k * stride)
        out.append(values[idx])
    # Always include the true final point
    if out[-1] != values[-1]:
        out[-1] = values[-1]
    return out


def _compute_friction_bps(domain_key: str, position_size_usd: float,
                          fallback_bps: float = 5.0,
                          fee_bps: float = _DEFAULT_FEE_BPS) -> float:
    """Compute per-side friction using size-proportional impact * leniency.

    Matches live paper-trading cost accounting: MarketImpactModel for
    spread/gas/impact (scales with order size) + taker fee per side, all
    multiplied by a leniency factor so the creator studio is slightly
    easier to pass than live. Falls back to flat domain friction if the
    impact model is unavailable.
    """
    try:
        from config_manager import get_config
        get_config()
        try:
            from platform_capacity import MarketImpactModel
            impact_bps = MarketImpactModel().estimate_total_friction_bps(
                domain_key, max(0.0, float(position_size_usd))
            )
            return (impact_bps + fee_bps) * _VALIDATION_LENIENCY
        except Exception:
            cfg = get_config()
            live_per_side = cfg.position.domain_entry_friction_bps.get(
                domain_key, cfg.position.entry_friction_bps
            )
            return live_per_side * _VALIDATION_LENIENCY
    except Exception:
        return fallback_bps + fee_bps


def _compute_bar_regimes(candles: List[dict], vol_window: int = 24) -> List[Dict[str, Any]]:
    """Compute per-bar market regime data from candle history.

    Returns a list aligned 1:1 with candles, each entry being a market
    context dict with 'regime', 'crisis_score', and 'fear_greed_index'.
    Uses adaptive percentile-based thresholds on returns and volatility.
    """
    import numpy as _np

    log_returns = []
    for i, c in enumerate(candles):
        if i == 0:
            log_returns.append(0.0)
        else:
            prev_close = float(candles[i - 1]["close"])
            close = float(c["close"])
            log_returns.append(math.log(close / prev_close) if prev_close > 0 else 0.0)

    vols = []
    for i in range(len(candles)):
        window = log_returns[max(0, i - vol_window + 1):i + 1]
        mean_ret = sum(window) / len(window) if window else 0.0
        variance = sum((r - mean_ret) ** 2 for r in window) / len(window) if window else 0.0
        vols.append((variance ** 0.5) * (vol_window ** 0.5))

    rolling_means = []
    for i in range(len(candles)):
        window = log_returns[max(0, i - vol_window + 1):i + 1]
        rolling_means.append(sum(window) / len(window) if window else 0.0)

    ret_arr = _np.array(log_returns[1:]) if len(log_returns) > 1 else _np.array([0.0])
    vol_arr = _np.array(vols[vol_window:]) if len(vols) > vol_window else _np.array(vols)
    vol_p67 = float(_np.percentile(vol_arr, 67)) if len(vol_arr) > 0 else 0.0
    vol_p33 = float(_np.percentile(vol_arr, 33)) if len(vol_arr) > 0 else 0.0

    # Dead-zone threshold for neutral trend: 25th-75th percentile of |mean_ret|
    abs_means = [abs(m) for m in rolling_means[vol_window:]] if len(rolling_means) > vol_window else [abs(m) for m in rolling_means]
    neutral_threshold = float(_np.percentile(abs_means, 50)) if abs_means else 0.0

    regimes = []
    for i in range(len(candles)):
        vol = vols[i]
        mean_ret = rolling_means[i]

        if vol > vol_p67:
            vol_label = "volatile"
        elif vol < vol_p33:
            vol_label = "calm"
        else:
            vol_label = "normal"

        # Trend classification with neutral dead-zone
        if abs(mean_ret) < neutral_threshold:
            trend = "neutral"
        elif mean_ret < 0:
            trend = "bear"
        else:
            trend = "bull"

        mc_regime = "NORMAL"
        crisis_score = 0.0
        if vol_label == "volatile":
            mc_regime = "HIGH_VOL"
            crisis_score = 0.3
            if trend == "bear":
                mc_regime = "CRISIS"
                crisis_score = 0.6
        elif vol_label == "calm" and trend == "bear":
            mc_regime = "HIGH_VOL"
            crisis_score = 0.25

        regimes.append({
            "regime": mc_regime,
            "crisis_score": crisis_score,
            "fear_greed_index": 50,
            "trend_regime": trend,
        })
    return regimes


def _compute_metrics(
    returns: List[float],
    trades: List[SimTrade],
    equity_curve: Optional[List[float]] = None,
    initial_equity: float = 100.0,
) -> Dict[str, Any]:
    """Compute performance metrics from trade returns and equity curve.

    Returns a comprehensive metrics dict including:
    - Basic: total_trades, total_return_bps, win_rate, wins, losses
    - Risk: sharpe, sortino, calmar, profit_factor, max_drawdown_bps
    - Behavioral: avg_hold_bars, avg_confidence
    - Equity: equity_curve, final_equity
    - Breakdown: regime_breakdown, exit_reason_breakdown
    """
    if not returns:
        return {
            "total_trades": 0,
            "total_return_bps": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "sharpe_per_trade": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_bps": 0.0,
            "avg_hold_bars": 0.0,
            "avg_confidence": 0.0,
            "wins": 0,
            "losses": 0,
            "equity_curve": equity_curve or [initial_equity],
            "final_equity": (equity_curve or [initial_equity])[-1],
            "regime_breakdown": {},
            "exit_reason_breakdown": {},
            "avg_win_bps": 0.0,
            "avg_loss_bps": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "expectancy_bps": 0.0,
            "payoff_ratio": 0.0,
            "largest_win_bps": 0.0,
            "largest_loss_bps": 0.0,
            "drawdown_duration_bars": 0,
            "exposure_pct": 0.0,
            "var_95_bps": 0.0,
            "cvar_95_bps": 0.0,
            "skewness": 0.0,
            "kurtosis": 0.0,
            "r_value": 0.0,
        }

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    total_return = sum(returns)

    # "sharpe" here is (mean/std) * sqrt(n) -- a t-STATISTIC (is the average
    # return distinguishable from zero), not an annualized Sharpe ratio. Kept
    # under this key because deflated_sharpe_ratio() and every downstream
    # comparison already expect it in this exact form; always described to
    # USERS as "significance" (see _stage_out_of_sample's diagnosis text).
    # sharpe_per_trade is the un-scaled mean/std -- closer to what a creator
    # intuitively means by "Sharpe-like", provided as a separate, honestly
    # named field rather than overloading the gate's own metric.
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = variance ** 0.5
        sharpe = (mean_r / std_r * math.sqrt(len(returns))) if std_r > 0 else 0.0
        sharpe_per_trade = (mean_r / std_r) if std_r > 0 else 0.0
    else:
        sharpe = 0.0
        sharpe_per_trade = 0.0

    # Sortino ratio (downside-deviation only)
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if downside:
            dd_var = sum(r ** 2 for r in downside) / len(downside)
            dd_std = dd_var ** 0.5
            sortino = (mean_r / dd_std * math.sqrt(len(returns))) if dd_std > 0 else 0.0
        else:
            sortino = sharpe if sharpe > 0 else 0.0
    else:
        sortino = 0.0

    # Max drawdown from equity curve (compounding-aware)
    if equity_curve and len(equity_curve) > 1:
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 10000 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
    else:
        peak = 0.0
        max_dd = 0.0
        cumulative = 0.0
        for r in returns:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

    # Calmar: total return / max drawdown
    calmar = abs(total_return / max_dd) if max_dd > 0 else 0.0

    # Profit factor: gross profit / gross loss
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r <= 0))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0

    avg_hold = sum(t.hold_bars for t in trades) / len(trades) if trades else 0.0
    avg_confidence = sum(t.confidence for t in trades) / len(trades) if trades else 0.0

    # ── Confidence normalization statistics ──────────────
    # Collect confidence distribution for normalization of community bots.
    # Uses 5th/95th percentile to be robust to outlier confidence values.
    confidence_values = sorted(t.confidence for t in trades) if trades else []
    if confidence_values:
        n = len(confidence_values)
        p5_idx = max(0, int(n * 0.05) - 1) if n > 1 else 0
        p95_idx = min(n - 1, int(n * 0.95)) if n > 1 else 0
        conf_p5 = confidence_values[p5_idx]
        conf_p95 = confidence_values[p95_idx]
        conf_mean = avg_confidence
        conf_std = (sum((c - conf_mean) ** 2 for c in confidence_values) / n) ** 0.5 if n > 1 else 0.0
    else:
        conf_p5 = 0.0
        conf_p95 = 1.0
        conf_mean = 0.5
        conf_std = 0.0
    confidence_stats = {
        "p5": round(conf_p5, 6),
        "p95": round(conf_p95, 6),
        "mean": round(conf_mean, 6),
        "std": round(conf_std, 6),
    }

    # Per-regime breakdown
    regime_stats: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        r = t.regime or "UNKNOWN"
        if r not in regime_stats:
            regime_stats[r] = {"trades": 0, "return_bps": 0.0, "wins": 0}
        regime_stats[r]["trades"] += 1
        regime_stats[r]["return_bps"] += t.return_bps
        if t.return_bps > 0:
            regime_stats[r]["wins"] += 1
    for r, s in regime_stats.items():
        s["win_rate"] = round(s["wins"] / s["trades"], 4) if s["trades"] > 0 else 0.0
        s["return_bps"] = round(s["return_bps"], 2)

    # Exit reason breakdown
    exit_stats: Dict[str, int] = {}
    for t in trades:
        reason = t.exit_reason or "unknown"
        exit_stats[reason] = exit_stats.get(reason, 0) + 1

    # ── Comprehensive trade-level statistics ──────────────
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    expectancy = (len(wins) / len(returns)) * avg_win + (len(losses) / len(returns)) * avg_loss

    # Consecutive wins / losses
    max_consec_wins = 0
    max_consec_losses = 0
    cur_wins = 0
    cur_losses = 0
    for r in returns:
        if r > 0:
            cur_wins += 1
            cur_losses = 0
            max_consec_wins = max(max_consec_wins, cur_wins)
        else:
            cur_losses += 1
            cur_wins = 0
            max_consec_losses = max(max_consec_losses, cur_losses)

    # Largest win / loss
    largest_win = max(returns) if returns else 0.0
    largest_loss = min(returns) if returns else 0.0

    # Drawdown duration (bars in drawdown from equity curve)
    dd_duration = 0
    if equity_curve and len(equity_curve) > 1:
        peak = equity_curve[0]
        in_dd = False
        dd_start = 0
        for i, eq in enumerate(equity_curve):
            if eq >= peak:
                if in_dd:
                    dd_duration = max(dd_duration, i - dd_start)
                    in_dd = False
                peak = eq
            else:
                if not in_dd:
                    in_dd = True
                    dd_start = i
        if in_dd:
            dd_duration = max(dd_duration, len(equity_curve) - dd_start)

    # Exposure (% of bars in a position)
    total_hold_bars = sum(t.hold_bars for t in trades)
    last_bar_idx = max((t.bar_index for t in trades), default=0)
    exposure_pct = (total_hold_bars / (last_bar_idx + 1)) if last_bar_idx > 0 else 0.0

    # VaR / CVaR (95th percentile, trade-level)
    sorted_returns = sorted(returns)
    n = len(sorted_returns)
    var_idx = max(0, int(n * 0.05) - 1)
    var_95 = sorted_returns[var_idx] if n > 0 else 0.0
    tail = [r for r in sorted_returns if r <= var_95]
    cvar_95 = sum(tail) / len(tail) if tail else var_95

    # Skewness & Kurtosis (excess)
    if n > 2:
        mean_r = sum(returns) / n
        std_r = (sum((r - mean_r) ** 2 for r in returns) / (n - 1)) ** 0.5
        if std_r > 0:
            skew = (sum((r - mean_r) ** 3 for r in returns) / (n * std_r ** 3))
            kurt = (sum((r - mean_r) ** 4 for r in returns) / (n * std_r ** 4)) - 3.0
        else:
            skew = 0.0
            kurt = 0.0
    else:
        skew = 0.0
        kurt = 0.0

    # R-value (return per unit risk, using avg loss as 1R)
    r_value = abs(total_return / avg_loss) if avg_loss != 0 else 0.0

    return {
        "total_trades": len(returns),
        "total_return_bps": round(total_return, 2),
        "win_rate": round(len(wins) / len(returns), 4),
        "sharpe": round(sharpe, 4),
        "sharpe_per_trade": round(sharpe_per_trade, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown_bps": round(max_dd, 2),
        "avg_hold_bars": round(avg_hold, 1),
        "avg_confidence": round(avg_confidence, 4),
        "confidence_stats": confidence_stats,
        "wins": len(wins),
        "losses": len(losses),
        "equity_curve": [round(e, 6) for e in (equity_curve or [initial_equity])],
        "final_equity": round((equity_curve or [initial_equity])[-1], 6),
        "regime_breakdown": regime_stats,
        "exit_reason_breakdown": exit_stats,
        "avg_win_bps": round(avg_win, 2),
        "avg_loss_bps": round(avg_loss, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "expectancy_bps": round(expectancy, 2),
        "payoff_ratio": round(payoff_ratio, 4),
        "largest_win_bps": round(largest_win, 2),
        "largest_loss_bps": round(largest_loss, 2),
        "drawdown_duration_bars": dd_duration,
        "exposure_pct": round(exposure_pct, 4),
        "var_95_bps": round(var_95, 2),
        "cvar_95_bps": round(cvar_95, 2),
        "skewness": round(skew, 4),
        "kurtosis": round(kurt, 4),
        "r_value": round(r_value, 4),
    }


def simulate_strategy(
    strategy: Strategy,
    candles: List[dict],
    symbol: str = "ETHUSDT",
    seed: int = 42,
    friction_bps: float = 5.0,
    fee_bps: float = _DEFAULT_FEE_BPS,
    warmup_bars: int = 0,
    features_series: Optional[List[dict]] = None,
    market_series: Optional[List[dict]] = None,
    initial_equity: float = 100.0,
    exact_friction_bps: Optional[float] = None,
) -> Tuple[List[SimTrade], Dict[str, Any]]:
    """Run a strategy over historical candles with realistic simulation.

    Implements production-parity exit logic:
      - Stop-loss / take-profit with intrabar high/low checking
      - Horizon (time) stop using real candle timestamps
      - Signal-flip exits at bar close
      - Position sizing via signal.confidence (fraction of equity)
      - Compounding equity curve
      - Size-proportional friction via MarketImpactModel
      - Per-trade regime tagging

    Args:
        strategy: Instantiated Strategy object
        candles: List of OHLCV dicts (oldest first)
        symbol: Trading symbol
        seed: RNG seed for determinism
        friction_bps: Fallback per-side friction if MarketImpactModel unavailable
        fee_bps: Per-side exchange fee in bps
        warmup_bars: Bars to skip before generating signals
        features_series: Optional per-bar feature dicts, aligned 1:1 with candles
        market_series: Optional per-bar market context dicts (regime, crisis_score,
            fear_greed_index), aligned 1:1 with candles. None -> auto-computed.
        initial_equity: Starting equity for compounding (default 100.0)
        exact_friction_bps: When set, charge exactly this per side (Studio's user-chosen
            friction) instead of the size-proportional impact model. None = production model.

    Returns:
        (trades, metrics_dict)
    """
    rng = random.Random(seed)
    # Rolling bar history, capped to the SAME 1000-bar window the live worker
    # exposes (sandbox/live_worker.py MAX_BARS). The uncapped version was an
    # O(N^2) disaster: `list(bars)` copied the full history on EVERY bar, so a
    # 137k-bar run spent ~29s per simulation purely on list copies (measured)
    # — that was the bulk of the old 4m14s certify. Live parity also means
    # bots can't rely on more than 1000 bars of context in production anyway.
    bars: List[Bar] = []
    _BARS_CAP = 1000
    trades: List[SimTrade] = []
    returns: List[float] = []
    equity_curve: List[float] = [initial_equity]

    position: Optional[dict] = None
    entry_bar_idx = 0
    initialized = False
    equity = initial_equity

    bar_seconds = _infer_bar_seconds(candles)
    domain_key = _infer_domain_key(symbol)

    # Production-parity exit management config (trailing stop + allocation
    # SL). These are engine-level exits the engine applies to uploaded bots
    # in _grade_positions, so the harness must model them. Defaults mirror
    # config_manager.PositionConfig so behavior is identical even if config
    # is unavailable.
    try:
        from config_manager import get_config as _get_exit_cfg
        _pcfg = _get_exit_cfg().position
        trailing_enabled = bool(getattr(_pcfg, "trailing_stop_enabled", True))
        trail_trigger_pct = float(getattr(_pcfg, "trailing_stop_trigger_pct", 0.6))
        trail_lock_pct = float(getattr(_pcfg, "trailing_stop_lock_pct", 0.4))
        alloc_sl_enabled = bool(getattr(_pcfg, "allocation_level_sl_enabled", True))
        alloc_sl_frac = float(getattr(_pcfg, "allocation_level_sl_frac", 0.6))
        alloc_sl_cap_bps = float(getattr(_pcfg, "allocation_level_sl_bps", 300.0))
    except Exception:
        trailing_enabled = True
        trail_trigger_pct = 0.6
        trail_lock_pct = 0.4
        alloc_sl_enabled = True
        alloc_sl_frac = 0.6
        alloc_sl_cap_bps = 300.0

    # Extract METADATA defaults for SL/TP/horizon
    metadata = getattr(strategy, "METADATA", {}) or {}
    default_sl_bps = float(metadata.get("declared_sl_bps", 0) or 0)
    default_tp_bps = float(metadata.get("declared_tp_bps", 0) or 0)
    default_hold_seconds = int(metadata.get("declared_hold_seconds", 0) or 0)

    # Compute regimes if not provided
    if market_series is None:
        market_series = _compute_bar_regimes(candles)

    def _close_position(exit_price: float, exit_reason: str,
                        exit_bar_idx: int, bar_ts: datetime,
                        bar_regime: str) -> None:
        """Record a trade and update equity curve."""
        nonlocal equity, position
        entry = position["entry_price"]
        direction = position["direction"]
        direction_mult = 1 if direction == "long" else -1
        gross_bps = ((exit_price - entry) / entry) * 10000 * direction_mult
        pos_size = position["size_usd"]
        if exact_friction_bps is not None:
            rt_friction = float(exact_friction_bps) * 2
        else:
            rt_friction = _compute_friction_bps(domain_key, pos_size, friction_bps, fee_bps) * 2
        net_bps = gross_bps - rt_friction

        # PnL scales with POSITION SIZE, not total equity: a 10%-confidence
        # trade must move equity 10x less than a full-size trade.
        equity += pos_size * net_bps / 10000.0
        equity_curve.append(round(equity, 6))

        trade = SimTrade(
            bar_index=entry_bar_idx,
            timestamp=position["entry_ts"].isoformat(),
            direction=direction,
            entry_price=entry,
            exit_price=exit_price,
            return_bps=round(net_bps, 2),
            confidence=position["confidence"],
            hold_bars=exit_bar_idx - entry_bar_idx,
            exit_reason=exit_reason,
            regime=bar_regime,
            position_size_usd=round(pos_size, 6),
            equity_after=round(equity, 6),
            entry_reason=position.get("entry_reason", ""),
        )
        trades.append(trade)
        returns.append(net_bps)
        position = None

    for i, c in enumerate(candles):
        # Build bar with proper timestamp from candle data
        ts = c.get("timestamp")
        if isinstance(ts, datetime):
            bar_ts = ts
        elif isinstance(ts, (int, float)):
            bar_ts = datetime.utcfromtimestamp(float(ts))
        else:
            bar_ts = datetime.utcnow()

        bar = Bar(
            timestamp=bar_ts,
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=float(c.get("volume", 0.0)),
        )
        bars.append(bar)
        if len(bars) > _BARS_CAP:
            del bars[:-_BARS_CAP]

        bar_market = market_series[i] if (market_series and i < len(market_series)) else {}
        bar_regime = bar_market.get("regime", "NORMAL") if bar_market else "NORMAL"

        # ── Check exits on open position BEFORE signal processing ──
        # Position was opened on a previous bar, so SL/TP/horizon
        # are checked against the current bar's high/low/close.
        if position is not None:
            exit_reason = None
            exit_price = bar.close

            sl_bps = position["sl_bps"]
            tp_bps = position["tp_bps"]
            hold_seconds = position["hold_seconds"]
            entry = position["entry_price"]
            direction = position["direction"]

            # Update running high/low — the engine tracks these per-tick for
            # trailing stop and allocation-level SL (PaperTradeTracker).
            position["high_price"] = max(position.get("high_price", entry), bar.high)
            position["low_price"] = min(position.get("low_price", entry), bar.low)

            # 1. Stop-loss: check intrabar high/low
            if sl_bps > 0:
                if direction == "long":
                    sl_price = entry * (1 - sl_bps / 10000)
                    if bar.low <= sl_price:
                        exit_reason = "stop_loss"
                        exit_price = sl_price
                else:
                    sl_price = entry * (1 + sl_bps / 10000)
                    if bar.high >= sl_price:
                        exit_reason = "stop_loss"
                        exit_price = sl_price

            # 2. Trailing stop: armed once peak profit exceeds trigger pct of
            #    TP; exits on a giveback of lock pct of TP from the peak.
            #    Mirrors live grading of uploaded bots in engine_pro.
            if exit_reason is None and trailing_enabled and tp_bps > 0:
                trigger_bps = tp_bps * trail_trigger_pct
                lock_bps = tp_bps * trail_lock_pct
                if lock_bps > 0:
                    if direction == "long":
                        peak_bps = ((position["high_price"] - entry) / entry) * 10000.0
                        if peak_bps > trigger_bps:
                            trail_price = entry * (1 + (peak_bps - lock_bps) / 10000)
                            if bar.low <= trail_price:
                                exit_reason = "trailing_stop"
                                exit_price = trail_price
                    else:
                        peak_bps = ((entry - position["low_price"]) / entry) * 10000.0
                        if peak_bps > trigger_bps:
                            trail_price = entry * (1 - (peak_bps - lock_bps) / 10000)
                            if bar.high >= trail_price:
                                exit_reason = "trailing_stop"
                                exit_price = trail_price

            # 3. Allocation-level SL: exit when giveback from peak exceeds a
            #    fraction of the position's own SL (capped), before the hard
            #    SL fires. Mirrors cfg.position.allocation_level_sl_*.
            if exit_reason is None and alloc_sl_enabled and sl_bps > 0:
                alloc_level_bps = min(alloc_sl_frac * sl_bps, alloc_sl_cap_bps)
                if direction == "long":
                    peak_bps = ((position["high_price"] - entry) / entry) * 10000.0
                    worst_bps = ((bar.low - entry) / entry) * 10000.0
                else:
                    peak_bps = ((entry - position["low_price"]) / entry) * 10000.0
                    worst_bps = ((entry - bar.high) / entry) * 10000.0
                # No positive-peak guard: live initializes peak to the first
                # observed unrealized, so a straight-down position still exits
                # via the (tighter) alloc SL before the hard SL.
                if (peak_bps - worst_bps) > alloc_level_bps:
                    exit_reason = "allocation_sl"
                    exit_price = bar.close

            # 4. Take-profit: hard-close only when trailing is disabled —
            #    with trailing on, live grading arms the trail at TP instead
            #    of hard-closing, letting winners run past target.
            if exit_reason is None and not trailing_enabled and tp_bps > 0:
                if direction == "long":
                    tp_price = entry * (1 + tp_bps / 10000)
                    if bar.high >= tp_price:
                        exit_reason = "take_profit"
                        exit_price = tp_price
                else:
                    tp_price = entry * (1 - tp_bps / 10000)
                    if bar.low <= tp_price:
                        exit_reason = "take_profit"
                        exit_price = tp_price

            # 5. Horizon: time-based exit at bar close
            if exit_reason is None and hold_seconds > 0:
                elapsed = (bar_ts - position["entry_ts"]).total_seconds()
                if elapsed >= hold_seconds:
                    exit_reason = "horizon"
                    exit_price = bar.close

            if exit_reason:
                _close_position(exit_price, exit_reason, i, bar_ts, bar_regime)

        # ── Build context and get signal ──
        bar_features = features_series[i] if (features_series and i < len(features_series)) else {}
        ctx = BarContext(
            bot_id="validation",
            symbol=symbol,
            bar=bar,
            bars=bars,
            position=position,
            rng=rng,
            bar_index=i,
            regime=bar_regime.lower() if bar_regime else "normal",
            features=dict(bar_features) if bar_features else {},
            market=dict(bar_market) if bar_market else {},
        )

        if not initialized:
            try:
                strategy.initialize(ctx)
            except Exception as e:
                return trades, {"error": f"Initialize error: {type(e).__name__}: {e}"}
            initialized = True

        if i < warmup_bars:
            continue

        try:
            signal = strategy.on_bar(ctx)
        except Exception as e:
            return trades, {"error": f"Runtime error at bar {i}: {type(e).__name__}: {e}"}

        if signal is None:
            continue
        if signal.direction == SignalDirection.FLAT:
            # Contract v2: "flat" is a real close-any-position signal. The live
            # adapter passes flat to the engine (sandbox/isolated_bot.py), so
            # validation must honor it too — dropping it here made long/flat
            # strategies backtest with exits they don't have live.
            if position is not None:
                _close_position(bar.close, "signal_flat", i, bar_ts, bar_regime)
            continue
        if not signal.is_active():
            continue

        new_dir = signal.direction.value

        # Signal flip: close existing position at bar close
        if position is not None and position["direction"] != new_dir:
            _close_position(bar.close, "signal_flip", i, bar_ts, bar_regime)

        # Open new position
        if position is None:
            sl_bps = signal.stop_loss_bps if signal.stop_loss_bps is not None else default_sl_bps
            tp_bps = signal.take_profit_bps if signal.take_profit_bps is not None else default_tp_bps
            hold_seconds = signal.horizon_seconds if signal.horizon_seconds is not None else default_hold_seconds

            pos_size = equity * signal.confidence

            position = {
                "direction": new_dir,
                "entry_price": bar.close,
                "confidence": signal.confidence,
                "entry_bar": i,
                "entry_ts": bar_ts,
                "sl_bps": sl_bps,
                "tp_bps": tp_bps,
                "hold_seconds": hold_seconds,
                "size_usd": pos_size,
                "entry_reason": (signal.metadata or {}).get("reason", ""),
                "high_price": bar.close,
                "low_price": bar.close,
            }
            entry_bar_idx = i

    # Close any remaining position at the end of data
    last_c = candles[-1] if candles else None
    if last_c is not None:
        _last_ts = last_c.get("timestamp")
        last_bar_ts = _last_ts if isinstance(_last_ts, datetime) else datetime.utcfromtimestamp(float(_last_ts))
    if position is not None:
        last_regime = market_series[-1].get("regime", "NORMAL") if market_series else "NORMAL"
        _close_position(float(last_c["close"]), "end_of_data", len(candles) - 1, last_bar_ts, last_regime)

    metrics = _compute_metrics(returns, trades, equity_curve, initial_equity)
    # Store start timestamp and bar duration for datetime-axis charting
    # (from `candles`, NOT the capped rolling `bars` window)
    if candles:
        first_ts = candles[0].get("timestamp")
        first_bar_ts = first_ts if isinstance(first_ts, datetime) else datetime.utcfromtimestamp(float(first_ts))
        metrics["start_timestamp"] = first_bar_ts.isoformat()
        metrics["end_timestamp"] = last_bar_ts.isoformat()
    metrics["bar_seconds"] = bar_seconds
    # Bucketed equity shape for the report-card PnL chart — no timestamps,
    # no prices, just the path equity took across the run.
    metrics["pnl_curve"] = [round(e, 4) for e in _bucket_series(equity_curve)]

    # Buy-and-hold benchmark: simulate holding the asset for the entire period
    # (computed from the FULL candle series, not the capped bar window)
    if len(candles) >= 2:
        first_close = float(candles[0]["close"])
        last_close = float(candles[-1]["close"])
        bh_return_bps = ((last_close - first_close) / first_close) * 10000.0
        # Build buy-and-hold equity curve (proportional to price)
        bh_equity = [initial_equity * (float(c["close"]) / first_close) for c in candles]
        metrics["benchmark_return_bps"] = round(bh_return_bps, 2)
        metrics["benchmark_equity_curve"] = [round(e, 6) for e in _bucket_series(bh_equity)]
        # Excess return (alpha) over buy-and-hold
        metrics["excess_return_bps"] = round(metrics["total_return_bps"] - bh_return_bps, 2)
    else:
        metrics["benchmark_return_bps"] = 0.0
        metrics["benchmark_equity_curve"] = []
        metrics["excess_return_bps"] = round(metrics.get("total_return_bps", 0.0), 2)

    return trades, metrics
