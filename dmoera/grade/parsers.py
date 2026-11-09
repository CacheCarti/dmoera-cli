"""Turn trade logs from common tools into round-trip trades.

Supported:
  * TradingView Strategy Tester "List of trades" CSV export
  * Freqtrade backtest result JSON
  * Binance Futures trade history CSV (fills are rebuilt into positions)
  * Binance Futures position history CSV
  * Simple CSV/JSON rows: timestamp, side, entry_price, exit_price, size (optional exit_time, symbol)

Every trade becomes a dict:
  {"symbol", "side" (+1 long / -1 short), "entry_time", "exit_time" (unix seconds),
   "entry_price", "exit_price", "size"}
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone

MAX_TRADES = 20000


class ParseError(ValueError):
    pass


# ── small helpers ──────────────────────────────────────────────────────────

def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("−", "-")
    s = re.sub(r"[^0-9eE.+\-]", "", s)
    if s in ("", "-", ".", "+"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%b %d, %Y %H:%M", "%b %d, %Y, %H:%M", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
)


def _ts(v):
    """Parse a timestamp into unix seconds (UTC when no zone is given)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
    else:
        s = str(v).strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            x = float(s)
        else:
            s2 = s.replace("Z", "+00:00").replace(" UTC", "")
            try:
                d = datetime.fromisoformat(s2)
            except ValueError:
                d = None
                for f in _DT_FORMATS:
                    try:
                        d = datetime.strptime(s, f)
                        break
                    except ValueError:
                        continue
                if d is None:
                    return None
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
    if x > 1e14:      # microseconds
        return x / 1e6
    if x > 1e11:      # milliseconds
        return x / 1e3
    if x > 1e8:       # seconds
        return x
    return None


def _side(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return -1 if v else 1          # is_short
    if isinstance(v, (int, float)):
        return 1 if v > 0 else -1 if v < 0 else None
    s = str(v).strip().lower()
    if not s:
        return None
    if "short" in s or s in ("sell", "s", "-1", "bear"):
        return -1
    if "long" in s or s in ("buy", "b", "l", "1", "+1", "bull"):
        return 1
    return None


def norm_symbol(v) -> str:
    s = str(v or "").strip().upper()
    if not s:
        return "ASSET"
    s = s.split(":")[-1] if ":" in s and not s.endswith(":USDT") else s.split(":")[0]
    s = s.replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
    s = re.sub(r"(PERP|\.P)$", "", s)
    return s or "ASSET"


def _pick(d: dict, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _lower_keys(d: dict) -> dict:
    return {str(k).strip().lower().lstrip("﻿"): v for k, v in d.items()}


ENTRY_T = ("entry_time", "open_time", "open_date", "opened", "entry time", "open time", "entry_date",
           "open_timestamp", "timestamp", "time", "date", "datetime", "date/time", "date(utc)")
EXIT_T = ("exit_time", "close_time", "close_date", "closed", "exit time", "close time", "exit_date",
          "close_timestamp", "exit_timestamp")
ENTRY_P = ("entry_price", "entry price", "open_price", "open price", "open_rate", "entry", "avg entry price",
           "avg. entry price", "entryprice")
EXIT_P = ("exit_price", "exit price", "close_price", "close price", "close_rate", "exit", "avg close price",
          "avg. close price", "exitprice")
SIZE = ("size", "qty", "quantity", "amount", "contracts", "closed vol", "closed volume", "volume", "max open interest")
SYMBOL = ("symbol", "pair", "ticker", "market", "instrument", "coin", "contract")
SIDE = ("side", "direction", "type", "position", "position side", "is_short")


def _row_to_trade(d: dict):
    d = _lower_keys(d)
    ep, xp = _num(_pick(d, *ENTRY_P)), _num(_pick(d, *EXIT_P))
    if not ep or not xp or ep <= 0 or xp <= 0:
        return None
    side = _side(d["is_short"]) if "is_short" in d else _side(_pick(d, *SIDE))
    if side is None:
        side = 1
    et = _ts(_pick(d, *ENTRY_T))
    xt = _ts(_pick(d, *EXIT_T))
    if et is None and xt is None:
        return None
    if et is None:
        et = xt
    if xt is None or xt < et:
        xt = None  # filled in later from the next trade
    size = _num(_pick(d, *SIZE))
    return {"symbol": norm_symbol(_pick(d, *SYMBOL)), "side": side, "entry_time": et, "exit_time": xt,
            "entry_price": ep, "exit_price": xp, "size": abs(size) if size else 1.0}


def _fill_exit_times(trades):
    trades.sort(key=lambda t: t["entry_time"])
    gaps = [b["entry_time"] - a["entry_time"] for a, b in zip(trades, trades[1:]) if b["entry_time"] > a["entry_time"]]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 3600.0
    for i, t in enumerate(trades):
        if t["exit_time"] is None:
            nxt = trades[i + 1]["entry_time"] if i + 1 < len(trades) else None
            t["exit_time"] = nxt if nxt and nxt > t["entry_time"] else t["entry_time"] + typical
    return trades


# ── format readers ────────────────────────────────────────────────────────

def _read_csv(text: str):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    if not rows:
        raise ParseError("The file has no rows.")
    return [_lower_keys(r) for r in rows]


def _tradingview(rows):
    cols = list(rows[0].keys())
    num_c = next(c for c in cols if c.startswith("trade #") or c == "trade")
    type_c = next(c for c in cols if c == "type")
    date_c = next((c for c in cols if "date" in c or c == "time"), None)
    price_c = next((c for c in cols if c.startswith("price")), None)
    size_c = next((c for c in cols if c.startswith("contracts") or c.startswith("quantity") or c.startswith("size")), None)
    sig_c = next((c for c in cols if c == "signal"), None)
    if not date_c or not price_c:
        raise ParseError("TradingView export is missing the date or price column.")
    groups = {}
    for r in rows:
        groups.setdefault(str(r.get(num_c)).strip(), []).append(r)
    trades = []
    for rs in groups.values():
        entry = next((r for r in rs if str(r.get(type_c, "")).lower().startswith("entry")), None)
        exit_ = next((r for r in rs if str(r.get(type_c, "")).lower().startswith("exit")), None)
        if not entry or not exit_:
            continue
        if sig_c and str(exit_.get(sig_c, "")).strip().lower() == "open":
            continue  # still-open trade
        t = _row_to_trade({"entry_price": entry.get(price_c), "exit_price": exit_.get(price_c),
                           "side": entry.get(type_c), "entry_time": entry.get(date_c),
                           "exit_time": exit_.get(date_c), "size": entry.get(size_c) if size_c else 1})
        if t:
            trades.append(t)
    return trades


def _binance_fills(rows):
    """Rebuild round trips from Binance Futures fills (average-cost, one trade per position episode)."""
    fills = []
    for r in rows:
        t = _ts(_pick(r, "date(utc)", "time", "date", "time(utc)", "timestamp"))
        px = _num(_pick(r, "price", "avg price", "average price"))
        q = _num(_pick(r, "quantity", "executed", "qty", "amount"))
        side = _side(_pick(r, "side"))
        if t is None or not px or not q or side is None:
            continue
        fills.append((t, norm_symbol(_pick(r, "symbol", "pair")), side * abs(q), px))
    fills.sort(key=lambda f: f[0])
    state = {}
    trades = []
    for t, sym, sq, px in fills:
        st = state.setdefault(sym, {"pos": 0.0, "avg": 0.0, "open": None, "cq": 0.0, "cv": 0.0})
        pos = st["pos"]
        if pos == 0 or (pos > 0) == (sq > 0):
            new = abs(pos) + abs(sq)
            st["avg"] = (st["avg"] * abs(pos) + px * abs(sq)) / new
            if pos == 0:
                st["open"] = t
            st["pos"] = pos + sq
            continue
        close_q = min(abs(sq), abs(pos))
        st["cq"] += close_q
        st["cv"] += close_q * px
        rest = abs(sq) - close_q
        st["pos"] = pos + (close_q if sq > 0 else -close_q)
        if abs(st["pos"]) < 1e-12:
            trades.append({"symbol": sym, "side": 1 if pos > 0 else -1, "entry_time": st["open"], "exit_time": t,
                           "entry_price": st["avg"], "exit_price": st["cv"] / st["cq"], "size": st["cq"]})
            st.update(pos=0.0, avg=0.0, open=None, cq=0.0, cv=0.0)
            if rest > 1e-12:
                st.update(pos=rest if sq > 0 else -rest, avg=px, open=t)
    return trades


def _freqtrade(obj):
    trades_src = None
    if isinstance(obj, dict) and isinstance(obj.get("strategy"), dict):
        best = max(obj["strategy"].values(), key=lambda s: len((s or {}).get("trades") or []), default=None)
        trades_src = (best or {}).get("trades")
    elif isinstance(obj, dict) and isinstance(obj.get("trades"), list):
        trades_src = obj["trades"]
    if not trades_src:
        return None
    out = []
    for tr in trades_src:
        if not isinstance(tr, dict):
            continue
        d = dict(tr)
        if "open_timestamp" in d:
            d["entry_time"] = d["open_timestamp"]
        if "close_timestamp" in d:
            d["exit_time"] = d["close_timestamp"]
        t = _row_to_trade(d)
        if t:
            out.append(t)
    return out


def parse_any(text: str, filename: str = ""):
    """Return (trades, format_name, warnings)."""
    if not text or not text.strip():
        raise ParseError("The file is empty.")
    text = text.lstrip("﻿")
    warnings = []
    stripped = text.lstrip()
    fmt = None
    if stripped[:1] in "{[":
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ParseError(f"Could not read the JSON: {e.msg} (line {e.lineno}).")
        trades = _freqtrade(obj)
        if trades is not None:
            fmt = "Freqtrade backtest JSON"
        else:
            rows = obj if isinstance(obj, list) else obj.get("trades") if isinstance(obj, dict) else None
            if not isinstance(rows, list):
                raise ParseError("JSON must be a Freqtrade backtest result or a list of trades.")
            trades = [t for t in (_row_to_trade(r) for r in rows if isinstance(r, dict)) if t]
            fmt = "JSON trade list"
    else:
        rows = _read_csv(text)
        cols = set(rows[0].keys())
        if any(c.startswith("trade #") for c in cols) and "type" in cols:
            trades, fmt = _tradingview(rows), "TradingView Strategy Tester CSV"
        elif cols & set(ENTRY_P) and cols & set(EXIT_P):
            trades = [t for t in (_row_to_trade(r) for r in rows) if t]
            fmt = "Binance position history CSV" if "closed vol" in cols or "opened" in cols else "Trade list CSV"
        elif "side" in cols and "price" in cols and cols & {"quantity", "executed", "qty", "amount"}:
            trades, fmt = _binance_fills(rows), "Binance Futures trade history CSV"
            warnings.append("Fills were rebuilt into positions; positions still open at the end are ignored.")
        else:
            raise ParseError(
                "Unrecognized columns. Expected a TradingView or Binance export, a Freqtrade JSON, "
                "or columns: timestamp, side, entry_price, exit_price, size.")
    if not trades:
        raise ParseError(f"Read the file as {fmt}, but found no complete trades.")
    if len(trades) > MAX_TRADES:
        warnings.append(f"Only the first {MAX_TRADES:,} trades were graded.")
        trades = sorted(trades, key=lambda t: t["entry_time"])[:MAX_TRADES]
    trades = _fill_exit_times(trades)
    return trades, fmt, warnings
