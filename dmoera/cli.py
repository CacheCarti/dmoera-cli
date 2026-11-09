"""dmoera command line interface.

Commands:
  dmoera backtest bot.py [--symbol ETHUSDT] [--interval 5m] [--days 180]
                         [--csv FILE] [--friction 6.0] [--export-trades F]
  dmoera grade trades.csv [--fee 12] [--trials 20] [--json]
  dmoera validate bot.py [--days 180]   (7 open stages, public thresholds)
  dmoera certify bot.py               (official verdict; needs DMOERA_API_KEY)
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def _load_bot_file(path: str):
    from dmoera.engine import load_strategy
    code = Path(path).read_text(encoding="utf-8")
    strategy_class, error = load_strategy(code)
    if error:
        print(f"[dmoera] failed to load {path}: {error}")
        sys.exit(1)
    return strategy_class


def _write_trades_csv(path: str, trades, symbol: str, interval: str) -> None:
    """Export SimTrades in the simple format dmoera grade parses:
    timestamp,side,entry_price,exit_price,size[,exit_time,symbol]"""
    import csv as _csv
    from datetime import timedelta
    step = timedelta(seconds={"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                              "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}.get(interval, 300))
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["timestamp", "side", "entry_price", "exit_price", "size",
                    "exit_time", "symbol"])
        for t in trades:
            try:
                entry_dt = datetime.fromisoformat(str(t.timestamp).replace("Z", ""))
            except ValueError:
                continue
            exit_dt = entry_dt + step * max(1, t.hold_bars)
            w.writerow([entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "long" if t.direction == "long" else "short",
                        t.entry_price, t.exit_price, 1,
                        exit_dt.strftime("%Y-%m-%d %H:%M:%S"), symbol])


def _resolve_symbol(args) -> str:
    meta = getattr(getattr(args, "_strategy_class", None), "METADATA", {}) or {}
    if args.symbol:
        return args.symbol.upper()
    parts = meta.get("domain", "eth_usdc").split("_")
    quote = "USDC" if len(parts) > 1 and parts[1].lower() == "usdc" else "USDT"
    return parts[0].upper() + quote


def _maybe_features(strategy_class, code: str, symbol: str, interval: str,
                    candles, days: int) -> Optional[list]:
    """Build the local feature series if the bot reads ctx.features."""
    from dmoera.features import build_feature_series, used_feature_keys
    keys = used_feature_keys(code)
    if keys is not None and not keys:
        return None
    print("[dmoera] bot reads ctx.features - building local feature series ...")
    return build_feature_series(candles, code, symbol, interval)


def cmd_backtest(args) -> int:
    from dmoera import data as d
    from dmoera.engine import simulate_strategy

    strategy_class = _load_bot_file(args.file)
    meta = getattr(strategy_class, "METADATA", {}) or {}
    if args.symbol:
        symbol = args.symbol.upper()
    else:
        # domain like "eth_usdc" / "btc_usdt" -> ETHUSDC / BTCUSDT
        parts = meta.get("domain", "eth_usdc").split("_")
        quote = "USDC" if len(parts) > 1 and parts[1].lower() == "usdc" else "USDT"
        symbol = parts[0].upper() + quote
    warmup = int(meta.get("warmup_bars", 0) or 0)

    print(f"[dmoera] loading candles: {symbol} {args.interval} last {args.days}d ...")
    candles = d.load_candles(symbol, interval=args.interval, days=args.days, csv_path=args.csv)
    if len(candles) < warmup + 10:
        print(f"[dmoera] not enough candles ({len(candles)}) for warmup {warmup}. "
              f"Use --days or --csv to provide more data.")
        return 1
    print(f"[dmoera] {len(candles)} candles "
          f"({candles[0]['timestamp'].date()} -> {candles[-1]['timestamp'].date()})")

    code = Path(args.file).read_text(encoding="utf-8")
    feats = _maybe_features(strategy_class, code, symbol, args.interval, candles, args.days)

    strategy = strategy_class()
    trades, metrics = simulate_strategy(
        strategy, candles, symbol=symbol,
        friction_bps=args.friction, warmup_bars=warmup,
        features_series=feats,
    )
    if "error" in metrics:
        print(f"[dmoera] backtest error: {metrics['error']}")
        return 1

    if args.export_trades:
        _write_trades_csv(args.export_trades, trades, symbol, args.interval)
        print(f"[dmoera] {len(trades)} trades exported -> {args.export_trades} "
              f"(grade them: dmoera grade {args.export_trades})")

    print()
    print(f"  strategy:     {meta.get('name', strategy_class.__name__)}")
    print(f"  domain:       {meta.get('domain', '?')}  ({symbol} {args.interval})")
    print(f"  trades:       {metrics.get('total_trades', 0)}")
    print(f"  net return:   {metrics.get('total_return_bps', 0.0):+.1f} bps "
          f"(after ~{args.friction * 2:.0f} bps round-trip friction)")
    print(f"  win rate:     {metrics.get('win_rate', 0.0) * 100:.1f}%")
    print(f"  significance: {metrics.get('sharpe', 0.0):.2f} "
          f"(t-stat on {metrics.get('total_trades', 0)} trades)")
    print(f"  max drawdown: {metrics.get('max_drawdown_bps', 0.0):.1f} bps")
    print(f"  final equity: {metrics.get('final_equity', 100.0):.2f} "
          f"(start {100.0})")
    print()
    print("  Same engine, same friction, same 1000-bar cap as the dMoERA")
    print("  validation harness. A number here means the same thing it")
    print("  means there. Run `dmoera validate` for the 7-stage battery,")
    print("  or `dmoera certify` for the official sealed verdict.")
    return 0


def cmd_grade(args) -> int:
    from dmoera.grade import cli as grade_cli
    argv = [args.file, "--fee", str(args.fee), "--trials", str(args.trials)]
    if args.json:
        argv.append("--json")
    return grade_cli.main(argv)


# ── validate: the 7 open stages, public default thresholds ────────────────
# These thresholds are PUBLISHED approximations, not the sealed exam's bar.
# Passing here means "not obviously broken", nothing more.

STAGE_SPLIT = 0.6      # in-sample = first 60%, OOS = last 40%
HOLDOUT_FRAC = 0.15    # last 15% is the holdout slice
WF_WINDOWS = 5         # rolling walk-forward windows
RANDOM_STARTS = 5      # randomized-start runs
STRESS_MULT = 2.0      # slippage stress: friction x2


def _sim_slice(strategy_class, candles, symbol, warmup, lo, hi, friction, feats=None):
    from dmoera.engine import simulate_strategy
    start = max(0, lo - warmup)
    s = strategy_class()
    f = feats[start:hi] if feats is not None else None
    return simulate_strategy(s, candles[start:hi], symbol=symbol,
                             friction_bps=friction, warmup_bars=warmup,
                             features_series=f)


def _fmt_bps(x):
    return f"{x:+.1f} bps" if x else "0.0 bps"


def cmd_validate(args) -> int:
    from dmoera import data as d
    from dmoera.engine import load_strategy, simulate_strategy

    strategy_class = _load_bot_file(args.file)
    meta = getattr(strategy_class, "METADATA", {}) or {}
    if args.symbol:
        symbol = args.symbol.upper()
    else:
        parts = meta.get("domain", "eth_usdc").split("_")
        quote = "USDC" if len(parts) > 1 and parts[1].lower() == "usdc" else "USDT"
        symbol = parts[0].upper() + quote
    warmup = int(meta.get("warmup_bars", 0) or 0)
    friction = args.friction

    print(f"[dmoera] loading candles: {symbol} {args.interval} last {args.days}d ...")
    candles = d.load_candles(symbol, interval=args.interval, days=args.days, csv_path=args.csv)
    n = len(candles)
    if n < warmup + 200:
        print(f"[dmoera] not enough candles ({n}) for the battery. Need ~200+ after warmup.")
        return 1
    print(f"[dmoera] {n} candles ({candles[0]['timestamp'].date()} -> {candles[-1]['timestamp'].date()})")
    print(f"[dmoera] running the 7 open stages (public thresholds, friction {friction} bps/side) ...\n")

    code = Path(args.file).read_text(encoding="utf-8")
    feats = _maybe_features(strategy_class, code, symbol, args.interval, candles, args.days)

    holdout_start = int(n * (1 - HOLDOUT_FRAC))
    oos_end = holdout_start
    is_end = int(oos_end * STAGE_SPLIT)
    results = []

    def stage(name, ok, detail):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # 1. code safety - load_strategy's static checks already ran in _load_bot_file
    stage("1 code safety", True, "static checks passed (no network, no clock, no file IO)")

    # 2. in-sample
    _, m_is = _sim_slice(strategy_class, candles, symbol, warmup, 0, is_end, friction, feats)
    is_bps = m_is.get("total_return_bps", 0.0)
    stage("2 in-sample", m_is.get("total_trades", 0) > 0,
          f"{m_is.get('total_trades', 0)} trades, {_fmt_bps(is_bps)}")

    # 3. out-of-sample
    _, m_oos = _sim_slice(strategy_class, candles, symbol, warmup, is_end, oos_end, friction, feats)
    oos_bps = m_oos.get("total_return_bps", 0.0)
    stage("3 out-of-sample", oos_bps > 0,
          f"{m_oos.get('total_trades', 0)} trades, {_fmt_bps(oos_bps)} (bar: positive after friction)")

    # 4. rolling walk-forward: WF_WINDOWS consecutive slices over the IS+OOS span
    wf_rets = []
    span = oos_end
    wsize = max(warmup + 50, int(span * 0.25))
    for k in range(WF_WINDOWS):
        lo = int(k * (span - wsize) / max(1, WF_WINDOWS - 1))
        _, m_w = _sim_slice(strategy_class, candles, symbol, warmup, lo, lo + wsize, friction, feats)
        wf_rets.append(m_w.get("total_return_bps", 0.0))
    wf_pos = sum(1 for r in wf_rets if r > 0)
    stage("4 walk-forward", wf_pos >= WF_WINDOWS // 2 + 1,
          f"{wf_pos}/{WF_WINDOWS} windows positive (bar: majority)")

    # 5. randomized starts: short runs from random offsets (seeded, deterministic)
    import random as _random
    rng = _random.Random(20261109)
    rs_rets = []
    rsize = max(warmup + 50, int(n * 0.2))
    for _ in range(RANDOM_STARTS):
        lo = rng.randint(0, max(0, n - rsize - 1))
        _, m_r = _sim_slice(strategy_class, candles, symbol, warmup, lo, lo + rsize, friction, feats)
        rs_rets.append(m_r.get("total_return_bps", 0.0))
    rs_med = sorted(rs_rets)[len(rs_rets) // 2]
    stage("5 randomized starts", rs_med > 0,
          f"median of {RANDOM_STARTS} random windows: {_fmt_bps(rs_med)}")

    # 6. slippage stress: OOS again at 2x friction
    _, m_st = _sim_slice(strategy_class, candles, symbol, warmup, is_end, oos_end, friction * STRESS_MULT, feats)
    st_bps = m_st.get("total_return_bps", 0.0)
    stage("6 slippage stress", st_bps > 0,
          f"OOS at {friction * STRESS_MULT:.0f} bps/side: {_fmt_bps(st_bps)}")

    # 7. holdout - reported, never tuned against
    _, m_h = _sim_slice(strategy_class, candles, symbol, warmup, holdout_start, n, friction, feats)
    h_bps = m_h.get("total_return_bps", 0.0)
    stage("7 holdout", h_bps > 0,
          f"last {n - holdout_start} bars: {_fmt_bps(h_bps)} (never used above)")

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print(f"  VALIDATION: {passed}/7 stages passed (public thresholds)")
    if passed == 7:
        print("  Not obviously broken. The REAL exam is sealed and server-side:")
        print("  `dmoera certify` for the official verdict.")
    else:
        fails = [name for name, ok, _ in results if not ok]
        print(f"  Failed: {', '.join(fails)}")
        print("  Fix these locally for free - certify counts attempts, so")
        print("  don't burn server attempts on a bot that fails here.")
    return 0 if passed == 7 else 1


def cmd_certify(args) -> int:
    import hashlib
    import json
    import os
    import urllib.request

    api_key = args.api_key or os.environ.get("DMOERA_API_KEY")
    if not api_key:
        print("[dmoera] certify needs an API key (it runs the sealed exam on our servers,")
        print("         which costs compute). Get one free at https://dmoera.xyz ->")
        print("         Settings -> API keys, then:  set DMOERA_API_KEY=dmo_pat_...")
        return 1
    server = (args.server or os.environ.get("DMOERA_SERVER") or "https://dmoera.xyz").rstrip("/")

    code = Path(args.file).read_text(encoding="utf-8")
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    print(f"[dmoera] code hash: {code_hash[:16]}...")
    print(f"[dmoera] uploading to {server} (sealed exam: real thresholds, holdout, attempt ledger) ...")

    body = json.dumps({"code": code, "code_hash": code_hash}).encode()
    req = urllib.request.Request(
        f"{server}/api/package/certify", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}",
                 "User-Agent": "dmoera-cli/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except Exception:
            err = {}
        if e.code == 401:
            print("[dmoera] unauthorized - that API key is not valid. Check DMOERA_API_KEY.")
        elif e.code == 402:
            print(f"[dmoera] {err.get('error', 'not enough credits')}")
        elif e.code == 429:
            print(f"[dmoera] rate limited: {err.get('error', 'try again later')}")
        else:
            print(f"[dmoera] certify failed (HTTP {e.code}): {err.get('error', e.reason)}")
        return 1
    except Exception as e:
        print(f"[dmoera] could not reach {server}: {e}")
        return 1

    if not res.get("success"):
        print(f"[dmoera] certify failed: {res.get('error', 'unknown error')}")
        return 1
    print()
    print("+" + "-" * 62 + "+")
    print("|" + f" OFFICIAL VERDICT   {res.get('verdict', '?')}   attempt #{res.get('attempt', '?')}".ljust(62) + "|")
    print("+" + "-" * 62 + "+")
    for k, label in (("oos_return_bps", "OOS return"), ("sharpe", "Sharpe"),
                     ("max_drawdown_bps", "Max drawdown"), ("total_trades", "Trades")):
        if k in res:
            print(f"  {label:16s} {res[k]}")
    if res.get("cert_id"):
        print(f"  cert:            {res['cert_id']}")
        print(f"  verify:          {server}/api/package/certify/verify/{res.get('code_hash', '')}")
        print(f"  Share the cert line + hash in your README - anyone can verify it.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dmoera", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="backtest a strategy file locally")
    bt.add_argument("file", help="path to the strategy .py file")
    bt.add_argument("--symbol", default=None, help="e.g. ETHUSDT (default: from METADATA.domain)")
    bt.add_argument("--interval", default="5m",
                    choices=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"])
    bt.add_argument("--days", type=int, default=180, help="history length (default 180)")
    bt.add_argument("--csv", default=None, help="use this OHLCV CSV instead of fetching")
    bt.add_argument("--friction", type=float, default=6.0,
                    help="per-side friction in bps (default 6.0)")
    bt.add_argument("--export-trades", default=None,
                    help="write the trade list to this CSV (then: dmoera grade FILE)")
    bt.set_defaults(func=cmd_backtest)

    gr = sub.add_parser("grade", help="grade a trade log S-F after fees, regimes and luck")
    gr.add_argument("file", help="trade log: our CSV, TradingView, Freqtrade JSON, Binance CSV")
    gr.add_argument("--fee", type=float, default=12.0, help="round-trip fee bps (default 12)")
    gr.add_argument("--trials", type=int, default=20,
                    help="how many versions you tried before this one (default 20)")
    gr.add_argument("--json", action="store_true", help="print full JSON result")
    gr.set_defaults(func=cmd_grade)

    va = sub.add_parser("validate", help="run the 7 open validation stages on a bot (public thresholds)")
    va.add_argument("file", help="path to the strategy .py file")
    va.add_argument("--symbol", default=None)
    va.add_argument("--interval", default="5m",
                    choices=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"])
    va.add_argument("--days", type=int, default=180)
    va.add_argument("--csv", default=None, help="use this OHLCV CSV instead of fetching")
    va.add_argument("--friction", type=float, default=6.0)
    va.set_defaults(func=cmd_validate)

    ce = sub.add_parser("certify", help="official verdict from the dMoERA sealed exam (needs DMOERA_API_KEY)")
    ce.add_argument("file", help="path to the strategy .py file")
    ce.add_argument("--api-key", default=None, help="or set DMOERA_API_KEY env var")
    ce.add_argument("--server", default=None, help="dMoERA API base (default https://dmoera.xyz)")
    ce.set_defaults(func=cmd_certify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
