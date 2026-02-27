# Operations

## Monitoring

- Main runtime log: `logs/price_correlator.log` (or `--log-file-path`).
- Alert log: `logs/alerts.log` (or `--alerts-file-path`).
- Warning-level events are automatically duplicated to alert log.
- In dual-timeframe mode:
  - `logs/strategy_test_5.log` and `logs/strategy_test_15.log` are reset each start.
  - `logs/strategy_test_result.log` is append-only and stores cumulative totals.
  - bankroll lines are written to `logs/strategy_test_result.log`:
    - `bankroll_start`
    - `bankroll_update`
    - `bankroll_final`

## Alerts

An alert entry is created for every message that starts with:

- `warning:`
- `Warning:`

This includes:

- RTDS reconnect scheduling warnings.
- Event switching failures.
- Price refresh and CLOB access failures.
- Fatal strategy exceptions (process exits with code `1`).

## Recommended External Alerting

Use your process supervisor / log collector to trigger notifications on new lines in `logs/alerts.log`:

- Windows Task Scheduler + PowerShell watcher
- Fluent Bit / Filebeat
- SIEM/monitoring pipeline

## Health Checks

- Run `.\.venv\Scripts\python.exe -m pytest -q` before deployment.
- Run `.\.venv\Scripts\python.exe -m pip check` after dependency install.
- Confirm both log files are writable at startup.
- If running on Linux/systemd, also verify:
  - `systemctl status price-correlator`
  - fresh lines appear in `logs/strategy_test_result.log`
