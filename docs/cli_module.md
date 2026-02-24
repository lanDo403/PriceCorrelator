# CLI Module

Purpose: run strategy mode from command line and map CLI arguments to `StrategyConfig`.

Key arguments:

- `--market-timeframe-minutes` (`5` or `15`)
- `--run-both-timeframes` (runs `5m` and `15m` in parallel)
- `--duration-seconds`
- `--entry-seconds-before-end`
- `--final-price-delay-seconds`
- `--price-threshold-30s-usd` (threshold for 30..6 seconds remaining)
- `--price-threshold-usd` (threshold for 5 seconds remaining)
- `--price-threshold-4s-usd` (threshold for 4 seconds remaining)
- `--price-threshold-near-end-usd` (threshold for 3/2/1 seconds remaining)
- `--stake-usd`
- `--log-file-path`
- `--log-file-path-5m`
- `--log-file-path-15m`
- `--result-log-file-path`
- `--alerts-file-path`
- `--disable-alerts`
- `--no-console-output`

Log behavior in dual-timeframe mode:

- `--log-file-path-5m` and `--log-file-path-15m` are truncated on startup.
- `--result-log-file-path` stays append-only.
- cumulative totals are persisted in `result_total_cumulative`.

Code file:

- `src/price_correlator/cli.py`
