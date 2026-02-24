from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import websockets

import price_correlator.event_client as event_module
from price_correlator.config import MonitorConfig
from price_correlator.event_client import GammaEventsClient
from price_correlator.lag_analyzer import LagAnalyzer
from price_correlator.monitor import MonitorService
from price_correlator.rtds_client import RtdsClient, TOPIC_CHAINLINK, TOPIC_POLYMARKET


def _start_http_server(
    handle_request,
) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, headers, body = handle_request(self.path)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}", thread


@pytest.mark.asyncio
@pytest.mark.integration
async def test_monitor_service_e2e_with_real_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = "btc-updown-5m-300"

    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/ru":
            html = f'<a href="/ru/event/{slug}">BTC 5 Minute Up or Down</a>'.encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html

        if parsed.path == "/events":
            query_slug = parse_qs(parsed.query).get("slug", [""])[0]
            payload = [
                {
                    "slug": query_slug,
                    "question": "BTC 5 Minute Up or Down",
                    "description": "",
                    "resolutionSource": "https://data.chain.link/streams/btc-usd",
                }
            ]
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        if parsed.path == "/markets":
            query_slug = parse_qs(parsed.query).get("slug", [""])[0]
            payload = [
                {
                    "slug": query_slug,
                    "question": "BTC 5 Minute Up or Down",
                    "startTimestamp": 0,
                    "endTimestamp": 300,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-1", "down-1"],
                }
            ]
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        return 404, {"Content-Type": "text/plain"}, b"not found"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(event_module, "GAMMA_EVENTS_URL", f"{base_url}/events")
    monkeypatch.setattr(event_module, "GAMMA_MARKETS_URL", f"{base_url}/markets")
    monkeypatch.setattr(event_module, "POLYMARKET_HOME_RU_URL", f"{base_url}/ru")

    async def ws_handler(websocket):  # noqa: ANN001
        _ = await websocket.recv()
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_POLYMARKET,
                    "payload": {"symbol": "btcusdt", "value": 100_100.0, "timestamp": 1_000},
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_CHAINLINK,
                    "payload": {"symbol": "btc/usd", "value": 100_090.0, "timestamp": 1_010},
                }
            )
        )
        await websocket.close()

    logs: list[str] = []
    try:
        async with websockets.serve(ws_handler, "127.0.0.1", 0) as ws_server:
            ws_port = ws_server.sockets[0].getsockname()[1]
            monitor = MonitorService(
                event_client=GammaEventsClient(),
                rtds_client=RtdsClient(websocket_url=f"ws://127.0.0.1:{ws_port}"),
                lag_analyzer=LagAnalyzer(),
                logger=logs.append,
            )
            summary = await monitor.run(
                config=MonitorConfig(
                    event_url="auto",
                    symbol_pair="BTC/USD",
                    duration_seconds=10,
                    report_interval_seconds=1,
                    stale_threshold_ms=0,
                    summary_json_path=None,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert summary.sample_count == 1
    assert any("Auto-selected event" in log for log in logs)
    assert any(log.startswith("| observed_utc |") for log in logs)
    assert any("Summary:" in log for log in logs)
