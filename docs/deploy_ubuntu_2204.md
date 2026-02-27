# Ubuntu 22.04 VPS Deployment

This guide runs the strategy 24/7 on Ubuntu `22.04` with Python `3.10.12`,
then adds Grafana + Loki + Promtail for remote log monitoring.

Replace `<user>` and `<host>` with your real Linux username and VPS host.

## 1. Copy Project to VPS

Option A (recommended): clone on VPS.

```bash
ssh <user>@<host>
mkdir -p ~/apps
cd ~/apps
git clone <your-repo-url> PriceCorrelator
cd PriceCorrelator
```

Option B: copy from Windows via `scp`.

```powershell
scp -r .\ <user>@<host>:~/apps/PriceCorrelator
```

## 2. Install Python Runtime

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
  --initial-bankroll-usd 100 \
  --log-file-path-5m ./logs/strategy_test_5.log \
  --log-file-path-15m ./logs/strategy_test_15.log \
  --result-log-file-path ./logs/strategy_test_result.log \
  --result-jsonl-file-path ./logs/strategy_test_result.jsonl \
  --log-jsonl-file-path-5m ./logs/strategy_test_5.jsonl \
  --log-jsonl-file-path-15m ./logs/strategy_test_15.jsonl
```

Check logs:

```bash
tail -n 50 logs/strategy_test_5.log
tail -n 50 logs/strategy_test_15.log
tail -n 50 logs/strategy_test_result.log
tail -n 20 logs/strategy_test_5.jsonl
tail -n 20 logs/strategy_test_15.jsonl
tail -n 20 logs/strategy_test_result.jsonl
```

## 4. Run 24/7 via systemd

1. Copy service file:

```bash
sudo cp scripts/price-correlator.service /etc/systemd/system/price-correlator.service
```

2. If needed, edit `User`, `WorkingDirectory`, and `ExecStart` in
   `/etc/systemd/system/price-correlator.service`.

3. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable price-correlator
sudo systemctl restart price-correlator
sudo systemctl status price-correlator
```

The service automatically restarts on failure.

## 5. Log Semantics

- `logs/strategy_test_result.log` appends and keeps cumulative totals.
- In dual mode it is updated during runtime with:
  - `result_event`,
  - `result_running`,
  - `result_total_running`,
  - `result_total_cumulative_running`.
- `logs/strategy_test_5.log` and `logs/strategy_test_15.log` are reset on each start.
- `--no-console-output` disables stdout log echo (file logging only).

## 6. Install Docker Engine (Official Repository)

Use the official Docker repo. This avoids `docker-compose-plugin` not found errors.

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
docker --version
docker compose version
```

## 7. Deploy Grafana + Loki + Promtail

Create observability folder:

```bash
mkdir -p ~/observability
cd ~/observability
```

Create `docker-compose.yml`:

```yaml
services:
  loki:
    image: grafana/loki:2.9.8
    command: -config.file=/etc/loki/local-config.yaml
    volumes:
      - ./loki-config.yml:/etc/loki/local-config.yaml:ro
      - loki-data:/loki
    restart: unless-stopped

  promtail:
    image: grafana/promtail:2.9.8
    command: -config.file=/etc/promtail/config.yml
    volumes:
      - ./promtail-config.yml:/etc/promtail/config.yml:ro
      - /root/apps/PriceCorrelator/logs:/var/log/price_correlator:ro
      - promtail-data:/tmp
    depends_on:
      - loki
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.1.4
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - grafana-data:/var/lib/grafana
    depends_on:
      - loki
    restart: unless-stopped

volumes:
  loki-data:
  promtail-data:
  grafana-data:
```

Create `loki-config.yml`:

```yaml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

ruler:
  alertmanager_url: http://localhost:9093
```

Create `promtail-config.yml`:

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: price-correlator
    static_configs:
      - targets:
          - localhost
        labels:
          job: price-correlator
          app: price-correlator
          __path__: /var/log/price_correlator/*.log
      - targets:
          - localhost
        labels:
          job: price-correlator
          app: price-correlator
          __path__: /var/log/price_correlator/*.jsonl
```

Start stack:

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=50 promtail
```

## 8. Access Grafana Securely (SSH Tunnel)

Do not expose Grafana publicly. Use SSH tunnel from your PC:

```powershell
ssh -L 3000:127.0.0.1:3000 <user>@<host>
```

Then open `http://localhost:3000`.

- Login: `admin` / `admin`
- Change password at first login.

## 9. Configure Loki Datasource and Dashboard

In Grafana:

1. `Connections -> Data sources -> Add data source -> Loki`
2. URL: `http://loki:3100`
3. Save & test.

Example LogQL queries:

- All strategy logs:
  - `{job="price-correlator"}`
- Only warnings:
  - `{job="price-correlator"} |= "warning:"`
- Cumulative totals:
  - `{job="price-correlator"} |= "result_total_cumulative"`

## 10. Optional Alerting

Basic approach:

1. Create alert rule in Grafana from query `{job="price-correlator"} |= "warning:"`.
2. Condition: count over last `5m` is `> 0`.
3. Add contact point (Telegram, email, webhook).

## 11. Operations and Troubleshooting

Service and app:

```bash
sudo systemctl status price-correlator
sudo journalctl -u price-correlator -n 100 --no-pager
tail -n 50 ~/apps/PriceCorrelator/logs/strategy_test_result.log
```

Observability stack:

```bash
cd ~/observability
docker compose ps
docker compose logs --tail=100 loki
docker compose logs --tail=100 promtail
docker compose logs --tail=100 grafana
```

Upgrade images:

```bash
cd ~/observability
docker compose pull
docker compose up -d
```
