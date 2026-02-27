$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"

& $python -m price_correlator.cli `
  --run-both-timeframes `
  --no-console-output `
  --duration-seconds 7200 `
  --entry-seconds-before-end 5 `
  --final-price-delay-seconds 2 `
  --price-threshold-30s-usd 150 `
  --price-threshold-usd 50 `
  --price-threshold-4s-usd 40 `
  --price-threshold-near-end-usd 30 `
  --initial-bankroll-usd 100 `
  --stake-usd 100 `
  --log-file-path-5m ".\logs\strategy_test_5.log" `
  --log-file-path-15m ".\logs\strategy_test_15.log" `
  --result-log-file-path ".\logs\strategy_test_result.log" `
  --result-jsonl-file-path ".\logs\strategy_test_result.jsonl" `
  --log-jsonl-file-path-5m ".\logs\strategy_test_5.jsonl" `
  --log-jsonl-file-path-15m ".\logs\strategy_test_15.jsonl"
