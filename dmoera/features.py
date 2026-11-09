"""dmoera.features - local cross-asset feature plumbing.

Reproduces the production feature pipeline (validation/upload_service.py
_feature_series + features/provider.py) for local backtests:

  1. used_feature_keys(code) - which ctx.features keys the strategy reads
     (same regexes and fail-safe semantics as production).
  2. Reference-candle momentum keys: btc_return_pct, btc_return_1d_pct,
     btc_return_3d_pct, btc_return_7d_pct, eth_*/sol_* - computed from the
     reference asset's own candles (fetched with the same data layer).
  3. Positioning/flow keys from the Binance Vision futures metrics archive
     (daily zips, 5-minute prints, cached locally): oi_usd_<sym>, oi_<sym>,
     toptrader_ls_<sym>, toptrader_pos_ls_<sym>, accounts_ls_<sym>,
     taker_ls_<sym>.

Keys the strategy needs that we cannot serve locally are reported as
warnings - never fabricated.
"""
from __future__ import annotations

import bisect
import csv
import io
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from .data import CACHE_DIR, INTERVAL_MS, load_candles, _UA

# ── which keys does the strategy read? (verbatim production semantics) ──────

_FEATURE_GET_LITERAL_RE = re.compile(r"ctx\.features\.get\(\s*(['\"])([A-Za-z0-9_]+)\1")
_FEATURE_GET_ANY_RE = re.compile(r"ctx\.features\.get\(\s*([^,)\s])")
_FEATURE_ATTR_RE = re.compile(r"ctx\.features\.(\w+)")


def used_feature_keys(code: str) -> Optional[Set[str]]:
    """The feature keys a strategy actually reads, or None when we can't tell.

    Same rules as production: literal .get("key") calls are collected; any
    dynamic keying (non-literal .get, subscript, other attribute access)
    returns None -> ship everything we can. Empty set = featureless bot."""
    if not code:
        return None
    for m in _FEATURE_GET_ANY_RE.finditer(code):
        if not m.group(1).startswith(("'", '"')):
            return None
    if re.search(r"ctx\.features\[", code):
        return None
    for m in _FEATURE_ATTR_RE.finditer(code):
        if m.group(1) != "get":
            return None
    return {m.group(2) for m in _FEATURE_GET_LITERAL_RE.finditer(code)}


# ── reference-candle momentum keys (CrossAssetSource equivalent) ────────────

# asset tag -> Binance spot symbol (mirrors HistoricalMetricsSource.COIN_KEYS)
_ASSET_SYMBOLS = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "trx": "TRXUSDT",
    "btcdom": "BTCDOMUSDT", "xau": "XAUUSDT", "paxg": "PAXGUSDT",
    "cl": "CLUSDT", "bz": "BZUSDT",
}
_RETURN_KEY_RE = re.compile(r"^([a-z]+)_return_(?:(\d+d)_)?pct$")
_LOOKBACKS = {"1d": 288, "3d": 864, "7d": 2016}  # bars at the 5m base interval
_DEFAULT_LOOKBACK = 12


class _RefReturnSource:
    """Trailing % return of a reference asset's closes, looked up per timestamp."""

    def __init__(self, key: str, ref_candles: List[dict], lookback_bars: int):
        self.key = key
        self.lb = lookback_bars
        rows = sorted((c["timestamp"], float(c["close"])) for c in ref_candles)
        self._ts = [r[0] for r in rows]
        self._close = [r[1] for r in rows]

    def value_at(self, ts: datetime) -> Optional[float]:
        idx = bisect.bisect_right(self._ts, ts) - 1
        if idx < self.lb or idx >= len(self._close):
            return None
        past = self._close[idx - self.lb]
        if not past:
            return None
        return round((self._close[idx] - past) / past * 100.0, 4)


class _FnSource:
    """A feature served by a plain lookup function."""

    def __init__(self, key: str, fn):
        self.key = key
        self._fn = fn

    def value_at(self, ts: datetime) -> Optional[float]:
        return self._fn(ts)


# ── Binance Vision futures metrics archive (positioning/flow) ───────────────

_METRIC_COLS = ("oi", "oi_usd", "toptrader_ls", "toptrader_pos_ls",
                "accounts_ls", "taker_ls")
_METRIC_KEY_RE = re.compile(r"^(" + "|".join(_METRIC_COLS) + r")_([a-z]+usdt)$")
_METRICS_BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
_COIN_OF_SYMBOL = {v.lower(): k for k, v in _ASSET_SYMBOLS.items()}


def _metrics_cache_path(coin: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"metrics_{coin}.csv"


def _read_metrics_cache(coin: str) -> Dict[datetime, dict]:
    path = _metrics_cache_path(coin)
    out: Dict[datetime, dict] = {}
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = datetime.utcfromtimestamp(int(row["time_ms"]) / 1000.0)
            out[ts] = {k: float(row[k]) for k in _METRIC_COLS if row.get(k) not in (None, "")}
    return out


def _write_metrics_cache(coin: str, rows: Dict[datetime, dict]) -> None:
    path = _metrics_cache_path(coin)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ms"] + list(_METRIC_COLS))
        for ts in sorted(rows):
            d = rows[ts]
            w.writerow([int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000)] +
                       [d.get(c, "") for c in _METRIC_COLS])


def _fetch_metrics_day(symbol: str, day) -> Dict[datetime, dict]:
    url = f"{_METRICS_BASE}/{symbol}/{symbol}-metrics-{day.year}-{day.month:02d}-{day.day:02d}.zip"
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
    except Exception:
        return {}
    out: Dict[datetime, dict] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            raw = z.read(z.namelist()[0]).decode()
    except Exception:
        return {}
    for line in raw.splitlines():
        if not line or line.startswith("create_time"):
            continue
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            # create_time like "2026-11-07 00:15:00" (UTC); parts[1] is the symbol
            ts = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
            vals = {"oi": parts[2], "oi_usd": parts[3], "toptrader_ls": parts[4],
                    "toptrader_pos_ls": parts[5], "accounts_ls": parts[6],
                    "taker_ls": parts[7]}
            out[ts] = {c: float(v) for c, v in vals.items() if v not in ("", None)}
        except (ValueError, IndexError):
            continue
    return out


def _load_metrics(symbol: str, coin: str, start: datetime, end: datetime) -> Dict[datetime, dict]:
    """Daily metrics zips for [start, end], cached per coin. Threaded like prod."""
    cached = _read_metrics_cache(coin)
    have = set(cached)
    days = []
    cur = datetime(start.year, start.month, start.day)
    while cur <= end:
        if cur not in have:
            days.append(cur)
        cur += timedelta(days=1)
    if days:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for day_rows in pool.map(lambda d: _fetch_metrics_day(symbol, d), days):
                cached.update(day_rows)
        if any(cached.values()):
            _write_metrics_cache(coin, cached)
    return cached


# ── the builder ──────────────────────────────────────────────────────────────

def build_feature_series(candles: List[dict], code: str, symbol: str,
                         interval: str = "5m", quiet: bool = False) -> Optional[List[dict]]:
    """Per-bar feature dicts aligned 1:1 with candles, trimmed to the keys the
    strategy reads. Returns None when the strategy reads no features at all
    (same as production: a featureless bot ships no features)."""
    def warn(msg: str) -> None:
        if not quiet:
            print(f"[dmoera] {msg}")

    keys = used_feature_keys(code)
    if keys is not None and not keys:
        return None  # featureless bot
    if not candles:
        return None
    if keys is None:
        # Fail-safe, same as production: unresolvable keying ships ALL
        # servable features rather than risking silent starvation.
        keys = set()
        for asset in _ASSET_SYMBOLS:
            keys.add(f"{asset}_return_pct")
            for tag in _LOOKBACKS:
                keys.add(f"{asset}_return_{tag}_pct")
        for sym in _ASSET_SYMBOLS.values():
            sym_l = sym.lower()
            for col in _METRIC_COLS:
                keys.add(f"{col}_{sym_l}")

    stamps = [c["timestamp"] for c in candles]
    window = (min(stamps), max(stamps))

    # Warmup margin: reference returns need lookback history before the window.
    max_lb_bars = max(_LOOKBACKS.values()) * INTERVAL_MS.get("5m", 300_000) // INTERVAL_MS.get(interval, 300_000)
    warmup_days = max(10, int(max_lb_bars * INTERVAL_MS.get(interval, 300_000) / 86_400_000) + 2)
    fetch_days = max(30, (window[1] - window[0]).days + warmup_days)

    sources: List = []          # (key -> callable(ts) -> value)
    served: Set[str] = set()
    failed_refs: Set[str] = set()
    failed_metrics: Set[str] = set()

    # 1. reference-candle momentum keys
    ref_assets: Dict[str, List[dict]] = {}
    for key in sorted(keys or []):
        m = _RETURN_KEY_RE.match(key)
        if not m:
            continue
        asset, tag = m.group(1), m.group(2)
        sym = _ASSET_SYMBOLS.get(asset)
        if not sym:
            continue
        if sym not in ref_assets:
            try:
                ref_assets[sym] = load_candles(sym, interval=interval, days=fetch_days, quiet=True)
            except Exception:
                ref_assets[sym] = []
        ref = ref_assets[sym]
        if not ref:
            failed_refs.add(sym)
            continue
        lb = _LOOKBACKS.get(tag or "", _DEFAULT_LOOKBACK)
        # lookbacks are defined on the 5m grid; scale to the actual interval
        lb = max(2, int(lb * INTERVAL_MS.get("5m", 300_000) / INTERVAL_MS.get(interval, 300_000)))
        sources.append(_RefReturnSource(key, ref, lb))
        served.add(key)

    # 2. positioning/flow keys from the metrics archive
    metric_coins: Dict[str, str] = {}   # binance perp symbol -> coin tag
    for key in sorted(keys or []):
        m = _METRIC_KEY_RE.match(key)
        if not m:
            continue
        sym = m.group(2).upper()
        coin = _COIN_OF_SYMBOL.get(sym.lower())
        if not coin:
            continue
        metric_coins[sym] = coin
    for sym, coin in metric_coins.items():
        rows = _load_metrics(sym, coin, window[0] - timedelta(days=2), window[1])
        if not rows:
            failed_metrics.add(sym)
            continue
        ts_sorted = sorted(rows)
        for col in _METRIC_COLS:
            key = f"{col}_{sym.lower()}"
            if key in (keys or ()):
                vals = [rows[t].get(col) for t in ts_sorted]

                def make_lookup(col=col, ts_sorted=ts_sorted, vals=vals):
                    def lookup(ts: datetime) -> Optional[float]:
                        i = bisect.bisect_right(ts_sorted, ts.replace(second=0, microsecond=0)) - 1
                        if i < 0:
                            return None
                        return vals[i]
                    return lookup
                sources.append(_FnSource(key, make_lookup()))
                served.add(key)

    unserved = sorted((keys or set()) - served)
    if failed_refs:
        warn(f"reference candles unavailable for: {', '.join(sorted(failed_refs))} "
             f"(their return keys read as the strategy's default)")
    if failed_metrics:
        warn(f"metrics archive unavailable for: {', '.join(sorted(failed_metrics))} "
             f"(their positioning keys read as the strategy's default)")
    if unserved:
        warn(f"{len(unserved)} feature key(s) not available locally - "
             f"they read as the strategy's default (usually 0.0)")
    if not sources:
        return None

    # merge per bar (later sources don't overlap keys - each key has one source)
    series: List[dict] = []
    for ts in stamps:
        d: dict = {}
        for src in sources:
            v = src.value_at(ts)
            if v is not None:
                d[src.key] = v
        series.append(d)
    return series
