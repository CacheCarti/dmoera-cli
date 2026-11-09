"""dmoera.data - candle loading for local backtests.

Sources, in order of preference:
  1. Local cache (~/.dmoera/cache) - instant, offline
  2. data.binance.vision bulk dumps - Binance's own public OHLCV archive,
     a separate CDN from the API with far fewer geo restrictions
  3. data-api.binance.vision klines mirror - keyless, geo-friendly
     (override with DMOERA_API_BASE, e.g. https://api.binance.us)
  4. BYO CSV - any OHLCV file with timestamp,open,high,low,close[,volume]

All loaders return candles oldest-first as dicts with keys:
  timestamp (datetime, UTC-naive), open, high, low, close, volume
which is exactly what dmoera.engine.simulate_strategy expects.

Cache semantics: the cache is a growing window per (symbol, interval).
load_candles backfills BOTH the missing head (older than the cache's
first bar) and the missing tail (newer than its last bar). If network
fetches fail but the cache covers part of the requested window, the
cached portion is used with a warning instead of crashing.
"""
from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

CACHE_DIR = Path(os.environ.get("DMOERA_CACHE", Path.home() / ".dmoera" / "cache"))

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}

_UA = {"User-Agent": "dmoera/0.2 (+https://github.com/CacheCarti/dmoera-cli)"}


def _ts_to_datetime(ts_ms: float) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def _naive_to_ms(dt: datetime) -> int:
    """Interpret a naive datetime as UTC and return epoch ms.

    datetime.timestamp() on a naive datetime assumes LOCAL time, which on a
    non-UTC machine silently shifts every cached/filtered timestamp by the
    timezone offset. All ms conversions in this module go through here.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _norm_ts(raw: float) -> float:
    """Normalize a Binance timestamp to milliseconds. Vision spot archives
    use microseconds; the API uses milliseconds."""
    if raw > 1e14:  # microseconds
        return raw / 1000.0
    return raw


def _cache_path(symbol: str, interval: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{symbol.upper()}_{interval}.csv"


def _write_cache(path: Path, candles: List[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([
                _naive_to_ms(c["timestamp"]),
                c["open"], c["high"], c["low"], c["close"], c.get("volume", 0.0),
            ])


def _read_cache(path: Path) -> Optional[List[dict]]:
    if not path.exists():
        return None
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = float(row["timestamp"])
            if ts < 1e11:  # seconds (hand-edited cache) - treat as UTC
                ts *= 1000.0
            out.append({
                "timestamp": _ts_to_datetime(ts),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            })
    return out or None


def _merge(old: List[dict], new: List[dict]) -> List[dict]:
    """Merge two oldest-first candle lists, dropping duplicates by timestamp."""
    seen = {c["timestamp"] for c in old}
    return sorted(old + [c for c in new if c["timestamp"] not in seen],
                  key=lambda c: c["timestamp"])


def _window(candles: List[dict], start_ms: int, end_ms: int) -> List[dict]:
    return [c for c in candles if start_ms <= _naive_to_ms(c["timestamp"]) <= end_ms]


def _parse_kline_row(parts) -> Optional[dict]:
    if len(parts) < 6:
        return None
    ts = _norm_ts(float(parts[0]))
    return {
        "timestamp": _ts_to_datetime(ts),
        "open": float(parts[1]), "high": float(parts[2]),
        "low": float(parts[3]), "close": float(parts[4]),
        "volume": float(parts[5]),
    }


def _fetch_klines_api(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
    """Binance public klines API (keyless). ~1000 bars per request.
    Defaults to the data-api.binance.vision mirror, which serves the same
    market-data endpoints from the Vision CDN with far fewer geo blocks."""
    import urllib.request
    import json
    out: List[dict] = []
    cursor = start_ms
    base = os.environ.get("DMOERA_API_BASE", "https://data-api.binance.vision")
    step = INTERVAL_MS.get(interval, 300_000)
    while cursor < end_ms:
        url = (f"{base}/api/v3/klines?symbol={symbol.upper()}&interval={interval}"
               f"&startTime={cursor}&endTime={end_ms}&limit=1000")
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.load(r)
        if not rows:
            break
        for row in rows:
            out.append({
                "timestamp": _ts_to_datetime(row[0]),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            })
        cursor = rows[-1][0] + step
        if len(rows) == 1000:
            time.sleep(0.15)  # stay polite on long paginated fetches
    return out


def _fetch_binance_vision(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
    """data.binance.vision monthly/daily zips. Fewer geo restrictions than the API."""
    import urllib.request
    sym = symbol.upper()
    out: List[dict] = []
    months = []
    d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    y, m = d.year, d.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    for (yy, mm) in months:
        fname = f"{sym}-{interval}-{yy}-{mm:02d}.zip"
        url = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/{interval}/{fname}"
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                blob = r.read()
        except Exception:
            continue  # month not published yet / not available - skip
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = z.namelist()[0]
            raw = z.read(name).decode()
        for line in raw.splitlines():
            if not line or line.startswith("open_time"):
                continue
            row = _parse_kline_row(line.split(","))
            if row is None:
                continue
            ts = _naive_to_ms(row["timestamp"])
            if start_ms <= ts <= end_ms:
                out.append(row)
    return out


def _fetch_vision_daily_tail(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
    """Daily zips cover the recent tail that monthly zips don't (the current
    month is only published as daily files until the month closes)."""
    import urllib.request
    sym = symbol.upper()
    out: List[dict] = []
    d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    day = d.date()
    while day <= end.date():
        fname = f"{sym}-{interval}-{day.isoformat()}.zip"
        url = f"https://data.binance.vision/data/spot/daily/klines/{sym}/{interval}/{fname}"
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                blob = r.read()
        except Exception:
            day += timedelta(days=1)
            continue
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            raw = z.read(z.namelist()[0]).decode()
        for line in raw.splitlines():
            if not line or line.startswith("open_time"):
                continue
            row = _parse_kline_row(line.split(","))
            if row is None:
                continue
            ts = _naive_to_ms(row["timestamp"])
            if start_ms <= ts <= end_ms:
                out.append(row)
        day += timedelta(days=1)
    return out


def _fetch_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
    """Best-effort fetch of one range: Vision monthly -> Vision daily -> API.
    Returns whatever it got; empty list means nothing was reachable."""
    new: List[dict] = []
    try:
        new = _fetch_binance_vision(symbol, interval, start_ms, end_ms)
        if new:
            # Monthly zips stop at the last closed month; fill the tail with dailies.
            last_new_ms = _naive_to_ms(new[-1]["timestamp"])
            if last_new_ms < end_ms - INTERVAL_MS.get(interval, 300_000):
                new = _merge(new, _fetch_vision_daily_tail(symbol, interval, last_new_ms, end_ms))
        else:
            new = _fetch_vision_daily_tail(symbol, interval, start_ms, end_ms)
    except Exception:
        pass
    if not new:
        try:
            new = _fetch_klines_api(symbol, interval, start_ms, end_ms)
        except Exception:
            pass
    return new


def _coverage(candles: List[dict], interval: str) -> Tuple[int, int, int]:
    """Return (first_ms, last_ms, gap_count) for a candle list."""
    if not candles:
        return 0, 0, 0
    step = INTERVAL_MS.get(interval, 300_000)
    mss = [_naive_to_ms(c["timestamp"]) for c in candles]
    gaps = sum(1 for a, b in zip(mss, mss[1:]) if b - a > step * 1.5)
    return mss[0], mss[-1], gaps


def load_candles(symbol: str, interval: str = "5m",
                 days: int = 180,
                 csv_path: Optional[str] = None,
                 quiet: bool = False) -> List[dict]:
    """Load candles for a symbol, cache-first with head+tail backfill.

    csv_path: use this OHLCV file instead of any network source.
    days: how much history to load. The cache grows to cover whatever has
    been requested before; missing head or tail ranges are fetched.
    """
    if csv_path:
        return load_csv(csv_path)
    if days <= 0:
        raise ValueError("days must be positive")

    def warn(msg: str) -> None:
        if not quiet:
            print(f"[dmoera] {msg}")

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    step = INTERVAL_MS.get(interval, 300_000)
    path = _cache_path(symbol, interval)
    cached = _read_cache(path) or []

    cached_ms = [_naive_to_ms(c["timestamp"]) for c in cached]
    have_first = min(cached_ms) if cached_ms else None
    have_last = max(cached_ms) if cached_ms else None

    # Ranges we still need: head (before cache) and tail (after cache).
    head_range = (start_ms, have_first - step) if (have_first and have_first > start_ms) else None
    tail_range = (have_last + step, end_ms) if (have_last and have_last < end_ms - step // 2) else None
    if not cached:
        head_range = None
        tail_range = (start_ms, end_ms)

    fetched: List[dict] = []
    failed = False
    for rng in (head_range, tail_range):
        if rng is None:
            continue
        got = _fetch_range(symbol, interval, rng[0], rng[1])
        if got:
            fetched = _merge(fetched, got)
        else:
            failed = True

    merged = _merge(cached, fetched)
    merged = _window(merged, start_ms, end_ms)

    if merged:
        _write_cache(path, merged)
    elif failed:
        raise RuntimeError(
            f"Could not fetch candles for {symbol} {interval}. "
            f"Binance data endpoints are geo-blocked or unreachable from this network. "
            f"Options: use --csv with any OHLCV file, set DMOERA_API_BASE to a "
            f"reachable mirror (e.g. https://api.binance.us), or retry from a "
            f"different network.")

    # Honest coverage report instead of silent short windows.
    if merged and not quiet:
        first_ms, last_ms, gaps = _coverage(merged, interval)
        want_first, want_last = start_ms, end_ms
        if first_ms > want_first + step * 2:
            warn(f"data starts {merged[0]['timestamp'].date()} - earlier history "
                 f"unavailable from this network (requested {days}d)")
        if failed and last_ms < want_last - step * 2:
            warn(f"tail fetch failed; data ends {merged[-1]['timestamp'].date()} "
                 f"(latest bars unavailable)")
        if gaps:
            warn(f"{gaps} gap(s) in the candle series (exchange downtime or "
                 f"missing archive months) - results may be affected")
    return merged


def load_csv(path: str) -> List[dict]:
    """Load any OHLCV CSV: timestamp,open,high,low,close[,volume].

    Timestamps may be unix ms/s or ISO strings. Oldest-first output.
    """
    out: List[dict] = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return out
    header = [h.strip().lower() for h in rows[0]]
    has_header = "open" in header and "close" in header
    cols = {name: i for i, name in enumerate(header)} if has_header else {
        "timestamp": 0, "open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
    data = rows[1:] if has_header else rows
    for row in data:
        if len(row) < 5:
            continue
        raw_ts = row[cols["timestamp"]].strip()
        try:
            ts = float(raw_ts)
            dt = (_ts_to_datetime(ts) if ts > 1e11
                  else _ts_to_datetime(ts * 1000))
        except ValueError:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).replace(tzinfo=None)
        vol = float(row[cols["volume"]]) if "volume" in cols and len(row) > cols["volume"] else 0.0
        out.append({
            "timestamp": dt,
            "open": float(row[cols["open"]]), "high": float(row[cols["high"]]),
            "low": float(row[cols["low"]]), "close": float(row[cols["close"]]),
            "volume": vol,
        })
    return sorted(out, key=lambda c: c["timestamp"])
