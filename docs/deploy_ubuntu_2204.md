# Ubuntu 22.04 VPS Deployment

This guide runs the strategy 24/7 on Ubuntu 22.04 with Python `3.10.12`.

## 1. Copy Project to VPS

Option A (recommended): use git on VPS.

```bash
ssh <user>@<host>
mkdir -p ~/apps
cd ~/apps
git clone <your-repo-url> PriceCorrelator
cd PriceCorrelator
```

Option B: copy current local folder from Windows via `scp`.

```powershell
scp -r .\ <user>@<host>:~/apps/PriceCorrelator
```

## 2. Install Runtime on VPS

```bash
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-pip
```

Create virtualenv and install dependencies:

```bash
cd ~/apps/PriceCorrelator
python3.10 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.lock
./.venv/bin/python -m pip install -e .
./.venv/bin/python -m playwright install --with-deps chromium
./.venv/bin/python -m pip check
```

## 3. Manual Smoke Run

```bash
./.venv/bin/python -m price_correlator.cli \
  --run-both-timeframes \
  --no-console-output \
  --duration-seconds 120 \
  --log-file-path-5m ./logs/strategy_test_5.log \
  --log-file-path-15m ./logs/strategy_test_15.log \
  --result-log-file-path ./logs/strategy_test_result.log
```

Check logs:

```bash
tail -n 50 logs/strategy_test_5.log
tail -n 50 logs/strategy_test_15.log
tail -n 50 logs/strategy_test_result.log
```

## 4. Run 24/7 via systemd

1) Copy service file:

```bash
sudo cp scripts/price-correlator.service /etc/systemd/system/price-correlator.service
```

2) Edit `ExecStart` and `WorkingDirectory` if your path is not `/home/<user>/apps/PriceCorrelator`.

3) Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable price-correlator
sudo systemctl restart price-correlator
sudo systemctl status price-correlator
```

The service is configured to restart automatically on failure.

## 5. Log Semantics

- `logs/strategy_test_result.log` appends and preserves cumulative totals across restarts.
- `logs/strategy_test_5.log` and `logs/strategy_test_15.log` are reset at each start.
- `--no-console-output` disables stdout log echo; logs are written to files only.

## 6. Grafana (Optional)

Fast path:

1) Keep writing `strategy_test_result.log`.
2) Install Promtail + Loki + Grafana (or Grafana Cloud Loki).
3) Scrape `logs/strategy_test_result.log`.
4) Build a panel from `result_total_cumulative` values (`events`, `win`, `lose`, `skip`, `profit_usd`).

This gives remote visibility without SSH.
