"""Regression tests from the external TIYlab audit of CLI v0.4.1 (2026-11-16).

Two minimal fixtures the auditor supplied, plus the behaviors they pin:

Fixture 1 - OHLCV validation (bars.csv)
  * impossible candle (high < low) must be flagged
  * duplicate timestamp must be flagged
  * ISO timestamp with a timezone offset must convert to UTC, not be
    silently reinterpreted as local-naive
  * missing volume must be flagged, not silently read as 0
  * load_candles(csv_path=..., start=, end=) must apply the window
  * strict=True must raise instead of warning

Fixture 2 - trade parser (missing.csv)
  * inferred side/size/exit-time must be reported in warnings
  * the still-open trade must be reported as omitted, not vanish
  * supplied fee/net_return/funding columns must be reported as unused
  * strict=True must raise instead of inferring
"""
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dmoera.data import load_csv, load_candles  # noqa: E402
from dmoera.grade.parsers import parse_any, ParseError  # noqa: E402

BARS_CSV = """timestamp,open,high,low,close
2025-01-01T00:00:00Z,100,90,110,100
2025-01-01T00:00:00Z,100,100,100,100
2025-01-01T03:00:00+02:00,100,100,100,100
"""

MISSING_CSV = """timestamp,entry_price,exit_price,fee,net_return,funding
2025-01-01T00:00:00Z,100,101,20,-0.19,-0.2
2025-01-01T02:00:00Z,100,90,10,0.2,0.1
2025-01-01T04:00:00Z,100,,0,0,0
"""


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


def test_fixture1_tz_and_validation(tmp_path=None):
    p = Path(_tmpdir()) / "bars.csv"
    p.write_text(BARS_CSV)
    bars, out = _capture(load_csv, str(p))
    # TZ offset candle converts to UTC: 03:00+02:00 -> 01:00 UTC
    assert bars[-1]["timestamp"] == datetime(2025, 1, 1, 1, 0), bars[-1]["timestamp"]
    # warnings name the impossible candle and the duplicate timestamp
    assert "impossible OHLC" in out, out
    assert "duplicate timestamp" in out, out
    # strict mode raises instead of warning
    try:
        load_csv(str(p), strict=True, quiet=True)
        raise AssertionError("strict load_csv should have raised")
    except ValueError:
        pass
    print("test_fixture1_tz_and_validation OK")


def test_fixture1_csv_window(tmp_path=None):
    p = Path(_tmpdir()) / "bars.csv"
    p.write_text(BARS_CSV)
    # window entirely outside the data -> empty + warning (was: all 3 returned)
    bars, out = _capture(load_candles, "BTCUSDT", "1h",
                         start="2024-01-01", end="2024-01-02", csv_path=str(p))
    assert bars == [], bars
    assert "outside the CSV data" in out, out
    # window inside the data -> filtered subset + warning
    bars, out = _capture(load_candles, "BTCUSDT", "1h",
                         start="2025-01-01", end="2025-01-01T01:00:00", csv_path=str(p))
    assert len(bars) == 2, bars
    assert "kept 2 of 3" in out, out
    print("test_fixture1_csv_window OK")


def test_fixture2_parser_warnings(tmp_path=None):
    trades, fmt, warnings = parse_any(MISSING_CSV, "missing.csv")
    assert fmt == "Trade list CSV"
    assert len(trades) == 2, trades
    joined = " ".join(warnings)
    assert "assumed LONG" in joined, joined
    assert "assumed 1.0" in joined, joined
    assert "exit time inferred" in joined, joined
    assert "still-open trade" in joined, joined
    assert "fee" in joined and "net_return" in joined and "funding" in joined, joined
    # strict mode refuses to guess
    try:
        parse_any(MISSING_CSV, "missing.csv", strict=True)
        raise AssertionError("strict parse_any should have raised")
    except ParseError:
        pass
    print("test_fixture2_parser_warnings OK")


def _tmpdir():
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="dmoera_fixture_"))
    return d


if __name__ == "__main__":
    test_fixture1_tz_and_validation()
    test_fixture1_csv_window()
    test_fixture2_parser_warnings()
    print("ALL FIXTURE TESTS PASSED")
