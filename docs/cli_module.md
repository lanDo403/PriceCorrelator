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
- `--initial-bankroll-usd` (shared bankroll for dual-timeframe mode)
- `--log-file-path`
- `--log-file-path-5m`
- `--log-file-path-15m`
- `--result-log-file-path`
- `--result-jsonl-file-path`
- `--log-jsonl-file-path-5m`
- `--log-jsonl-file-path-15m`
- `--alerts-file-path`
- `--disable-alerts`
- `--no-console-output`

Log behavior in dual-timeframe mode:

- `--log-file-path-5m` and `--log-file-path-15m` are truncated on startup.
- `--log-jsonl-file-path-5m` and `--log-jsonl-file-path-15m` are truncated on startup.
- timeframe logs now use the same key-value style as result log (`result_*`, `bankroll_*`), plus warning lines.
- `--result-log-file-path` stays append-only.
- `--result-jsonl-file-path` stays append-only and contains structured JSONL.
- cumulative totals are persisted in `result_total_cumulative`.
- dual mode logs bankroll lifecycle:
  - `bankroll_start`
  - `bankroll_update`
  - `bankroll_final`

Code file:

- `src/price_correlator/cli.py`
