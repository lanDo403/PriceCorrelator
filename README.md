# Price Correlator

Runs a strategy for Polymarket `BTC 5 Minute Up or Down` and `BTC 15 Minute Up or Down` markets, logging per-event outcomes.

## Strategy Logic

- Event timer comes from active Polymarket market metadata.
- `price_to_beat` source is the Playwright-rendered event HTML page (dynamic DOM).
- Entry checks run on every Chainlink tick during the final 5 seconds.
- Distance thresholds to `price_to_beat`:
  - `30..6s` left: `150 USD`,
  - `5s` left: `50 USD`,
  - `4s` left: `40 USD`,
  - `3/2/1s` left: `30 USD`.
- If liquidity is missing, checks continue until entry window closes.
- `final_price` is taken from Chainlink `2s` after event end.

Per-event log fields:

- `price_to_beat`
- `entry_price`
- `final_price`
- `side` (`up/down`)
- `result` (`win/lose/skip`)
- `profit_usd`
- `reason`

## Log Behavior

- `strategy_test_result.log` is append-only and keeps cumulative totals across restarts.
- During `--run-both-timeframes`, `strategy_test_result.log` is updated live:
  - `result_event` per closed event,
  - `result_running` per timeframe,
  - `result_total_running` and `result_total_cumulative_running` across both timeframes.
- `strategy_test_5.log` and `strategy_test_15.log` are truncated at each new dual-timeframe run.
- Use `--no-console-output` for file-only logging (no stdout echo).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.lock
.\.venv\Scripts\python.exe -m pip install -e .[dev]
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pip check
```

## Run

```powershell
.\.venv\Scripts\python.exe -m price_correlator.cli `
  --run-both-timeframes `
  --no-console-output `
  --duration-seconds 7200 `
  --entry-seconds-before-end 5 `
  --final-price-delay-seconds 2 `
  --price-threshold-30s-usd 150 `
  --price-threshold-usd 50 `
  --price-threshold-4s-usd 40 `
  --price-threshold-near-end-usd 30 `
  --stake-usd 100 `
  --log-file-path-5m .\logs\strategy_test_5.log `
  --log-file-path-15m .\logs\strategy_test_15.log `
  --result-log-file-path .\logs\strategy_test_result.log `
  --alerts-file-path .\logs\alerts.log
```

Single timeframe mode is still available via `--market-timeframe-minutes 5` or `15` + `--log-file-path`.

Or:

```powershell
.\scripts\run_strategy.ps1
```

## Ubuntu 22.04 VPS

See full step-by-step deployment guide:

- `docs/deploy_ubuntu_2204.md`

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Playwright integration tests are opt-in:

```powershell
$env:ENABLE_PLAYWRIGHT_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_integration_event_client_playwright.py
```

## Production Notes

- Ops runbook: `docs/operations.md`
- Rollback plan: `docs/rollback_plan.md`
- Rollback script: `scripts/rollback_to_lock.ps1`
- Assumptions and limits: `docs/assumptions.md`
