#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"

exec "$PYTHON_BIN" -m price_correlator.cli \
  --run-both-timeframes \
  --no-console-output \
  --duration-seconds 315360000 \
  --entry-seconds-before-end 5 \
  --final-price-delay-seconds 2 \
  --price-threshold-30s-usd 150 \
  --price-threshold-usd 50 \
  --price-threshold-4s-usd 40 \
  --price-threshold-near-end-usd 30 \
  --initial-bankroll-usd 100 \
  --stake-usd 100 \
  --log-file-path-5m "./logs/strategy_test_5.log" \
  --log-file-path-15m "./logs/strategy_test_15.log" \
  --result-log-file-path "./logs/strategy_test_result.log"
