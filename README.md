# dmoera

This is the engine behind [Crypto-Strategies](https://github.com/CacheCarti/Crypto-Strategies),
packaged so you can run it yourself. Every bot in that corpus, the winners
and the 700-odd corpses, was tested with the same simulation code you get
when you install this. Same bar loop, same exit logic, same friction model,
same 1000-bar context cap.

```
pip install dmoera
dmoera backtest strategies/weekend_season_eth_v6.py
```

The first run downloads candles once from Binance's public data and caches
them in `~/.dmoera/cache`. After that everything runs offline and instant.
If Binance is unreachable from your network for any reason, point `--csv`
at any OHLCV file and you're still in business.

## The commands

**`dmoera backtest bot.py`** runs a bot over real candles and tells you what
it did: trades, net return after friction, win rate, significance, drawdown.
Add `--export-trades trades.csv` and you get the full trade list as a file.

**`dmoera battery bot.py`** is the deep battery: the full 6.25-year window
on 1h bars, six gates - trade count, net t-stat, deflated Sharpe, regime
split (bull/crash/recovery/bear), staleness, and plateau/PBO when you bring
a variant grid. This is the same battery that killed 363 hand-built bots of
which 211 were net-positive. Locally it runs with no trial ledger
(n_trials=1), so it is the method, not the enforcement - the sealed exam
(`dmoera certify`) is where your real attempt count gets charged.

**`dmoera grade trades.csv`** is the lie detector. It takes any trade log,
yours or one of ours, and grades it S through F after realistic fees. It
splits your results by market regime, runs 500 random-entry placebo bots
against yours, and computes a deflated Sharpe so a lucky streak can't
pretend to be skill. It reads TradingView, Freqtrade and Binance exports
too, so you can bring results from other tools and still get a straight
answer.

**`dmoera validate bot.py`** runs the 7 open validation stages on a bot:
code safety, in-sample, out-of-sample, rolling walk-forward, randomized
starts, slippage stress, and a holdout slice nothing is allowed to touch.
These use public default thresholds. Passing here means your bot isn't
obviously broken. It is not the real exam.

**`dmoera certify bot.py`** is the real exam. It uploads your code to
dMoERA's servers, where the sealed version runs: the actual thresholds, the
sealed holdout data, the attempt ledger that catches brute-forcing. You
need an API key from dmoera.xyz Settings for this one, because it costs
real compute (15 CR, enough to iterate on).

**`dmoera certify bot.py --deep`** is the certification people can check.
Same sealed exam plus the 6.25-year deep battery on 1h bars, for 500 CR.
The result becomes a public certificate at dmoera.xyz/verify/<hash> with a
QR code, bound to your code's exact hash, the data fingerprint and the
test config. Anyone can open it without an account and confirm the exact
code that was tested. That is the thing to put in your README.

The short version: `backtest` tells you what a bot did. `grade` tells you
whether to believe it. `validate` tells you whether it holds up across
windows it didn't pick. `certify` is the only result that counts.

## Reproducing the corpus numbers

Every report card in the Crypto-Strategies repo came out of this engine.
To check a card's claim yourself:

```
dmoera backtest strategies/<bot>.py --interval 5m --days 180
dmoera backtest strategies/<bot>.py --interval 5m --days 180 --export-trades t.csv
dmoera grade t.csv
```

Cross-asset strategies that read `ctx.features` work locally too. Open
interest and positioning come from Binance's public metrics archive, and
reference-asset returns (the `btc_return_*` family) are computed from the
reference asset's own candles. If a feature can't be served locally, the
run tells you which keys fell back to defaults instead of failing silently.

## Why the numbers here mean something

Most local backtesters drift from whatever runs in production. Different
fee handling, different exit logic, different context windows, and one day
you notice your local results and the platform's results disagree. This
package doesn't have that problem because the simulation engine was
extracted verbatim from the dMoERA validation harness, the same code that
produced every report card in the corpus. A number here is the same number
there.

What the package deliberately leaves out is the sealed exam. The real
thresholds, the holdout data, the seeds and the attempt ledger stay on
dMoERA's servers. That is what makes `certify` worth something. The public
stages tell you your bot isn't obviously broken. Only the sealed exam can
tell you it's real.

## For AI agents

The [dmoera-mcp](https://github.com/CacheCarti/dmoera-mcp) server exposes
the platform to AI agents, and it uses the same API key this package uses
for `certify`. One key, both tools.

## License

MIT
