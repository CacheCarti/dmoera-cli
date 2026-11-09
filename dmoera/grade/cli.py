"""Command line: dmoera-grade trades.csv [--fee 12] [--trials 20] [--json]
                dmoera-grade key        (checks a Binance API key's permissions; secret is read without echo)
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone

from . import __version__, grade_text
from .parsers import ParseError

SITE = "https://dmoera.xyz/tools/lie-detector"


def _pct(x, signed=True):
    return "n/a" if x is None else (f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%")


def _date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def render(r: dict) -> str:
    s, sel = r["summary"], r["selected"]
    out = []
    w = 62
    out.append("+" + "-" * w + "+")
    out.append("|" + f" dMoERA GRADE   {r['grade']}   {int(r['score'])}/100   at {r['fee_bps']} bps fees".ljust(w) + "|")
    out.append("|" + f" {r['verdict']}"[:w].ljust(w) + "|")
    out.append("+" + "-" * w + "+")
    out.append(f"  {s['trades']} trades, {', '.join(s['symbols'][:4])}, {_date(s['start'])} to {_date(s['end'])} ({s['span_days']:.0f} days)")
    out.append(f"  Read as: {r.get('format', 'trade list')}")
    out.append("")
    out.append(f"  Net return        {_pct(sel['total_return'])}")
    out.append(f"  Win rate          {_pct(sel['win_rate'], False)}")
    out.append(f"  Avg trade         {sel['avg_trade_bps']:+.1f} bps")
    out.append(f"  Max drawdown      {_pct(sel['max_drawdown'], False)}")
    out.append(f"  Sharpe (annual)   {sel['sharpe']:.2f}")
    out.append(f"  Beats random bots {sel['placebo_pct']:.0f}% of {s['placebo_bots']}")
    out.append(f"  Deflated Sharpe   {sel['deflated_sharpe'] * 100:.0f}%  (assuming {r['trials']} tries)")
    be = s["break_even_fee_bps"]
    out.append(f"  Edge dies at      {be:.1f} bps round trip" if be > 0 else "  Edge dies at      0 bps (no edge even before fees)")
    if sel["caps"]:
        out.append("")
        out.append("  Why not higher:")
        for c in sel["caps"]:
            out.append(f"   - {c['reason']} (max {c['max_grade']})")
    out.append("")
    out.append("  Regime          trades   net return   win rate")
    for g in sel["regimes"]:
        wr = "-" if g["win_rate"] is None else f"{g['win_rate'] * 100:.0f}%"
        out.append(f"  {g['regime']:<14}{g['trades']:>7}   {_pct(g['net_return']) if g['trades'] else '-':>10}   {wr:>8}")
    out.append("")
    out.append("  Fees (bps)  " + "  ".join(f"{lv['fee_bps']:>3}" for lv in r["levels"] if lv["fee_bps"] % 4 == 0))
    out.append("  Grade       " + "  ".join(f"{lv['grade']:>3}" for lv in r["levels"] if lv["fee_bps"] % 4 == 0))
    for wmsg in r.get("warnings") or []:
        out.append(f"  Note: {wmsg}")
    out.append("")
    out.append(f"  Full report, fee slider and a README badge: {SITE}")
    return "\n".join(out)


def _cmd_key(argv):
    from .keycheck import check_binance_key
    ap = argparse.ArgumentParser(prog="dmoera-grade key", description="Grade a Binance API key's safety S to F.")
    ap.add_argument("--api-key", default=os.environ.get("BINANCE_API_KEY"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    key = a.api_key or input("API key: ").strip()
    secret = os.environ.get("BINANCE_API_SECRET") or getpass.getpass("API secret (hidden): ").strip()
    res = check_binance_key(key, secret)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        print(f"Could not check the key: {res.get('error')}")
        return 1
    print(f"Key safety grade: {res['grade']}  ({res['headline']})")
    for c in res["checks"]:
        print(f"  [{'ok' if c['ok'] else '!!'}] {c['label']}")
    for f in res["fixes"]:
        print(f"  Fix: {f}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "key":
        return _cmd_key(argv[1:])
    ap = argparse.ArgumentParser(prog="dmoera-grade",
                                 description="Grade a trade log S to F after real fees, market regimes and luck.")
    ap.add_argument("file", help="TradingView CSV, Freqtrade JSON, Binance CSV, or timestamp,side,entry_price,exit_price,size")
    ap.add_argument("--fee", type=float, default=12.0, help="round-trip fee in basis points (default 12)")
    ap.add_argument("--trials", type=int, default=20, help="how many versions you tried before this one (default 20)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--version", action="version", version=f"dmoera-grade {__version__}")
    a = ap.parse_args(argv)
    try:
        with open(a.file, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        r = grade_text(text, os.path.basename(a.file), fee_bps=a.fee, trials=a.trials)
    except (OSError, ParseError, ValueError) as e:
        print(f"dmoera-grade: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(r, indent=2, default=float))
    else:
        print(render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
