from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import websockets

import price_correlator.clob_client as clob_module
import price_correlator.event_client as event_module
from price_correlator.clob_client import ClobClient
from price_correlator.event_client import GammaEventsClient
from price_correlator.rtds_client import RtdsClient, TOPIC_CHAINLINK
from price_correlator.strategy import StrategyConfig, StrategyRunner


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


def _make_local_event_page_fetcher(base_url: str):
    def fetcher(slug: str) -> str:
        request = Request(f"{base_url}/ru/event/{slug}", method="GET")
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return response.read().decode("utf-8", errors="ignore")

    return fetcher


@pytest.mark.asyncio
@pytest.mark.integration
async def test_strategy_runner_e2e_with_real_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    current_slug = "btc-updown-5m-300"
    next_slug = "btc-updown-5m-600"

    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/ru":
            html = (
                f'<a href="/ru/event/{current_slug}">BTC 5 Minute Up or Down</a>'
                f'<a href="/ru/event/{next_slug}">BTC 5 Minute Up or Down</a>'
            ).encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html

        if parsed.path == "/markets":
            slug = parse_qs(parsed.query).get("slug", [""])[0]
            if slug == current_slug:
                payload = [
                    {
                        "slug": current_slug,
                        "question": "BTC 5 Minute Up or Down",
                        "startTimestamp": 0,
                        "endTimestamp": 300,
                        "outcomes": ["Up", "Down"],
                        "clobTokenIds": ["up-1", "down-1"],
                    }
                ]
            elif slug == next_slug:
                payload = [
                    {
                        "slug": next_slug,
                        "question": "BTC 5 Minute Up or Down",
                        "startTimestamp": 300,
                        "endTimestamp": 600,
                        "outcomes": ["Up", "Down"],
                        "clobTokenIds": ["up-2", "down-2"],
                    }
                ]
            else:
                payload = []
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        if parsed.path == "/events":
            slug = parse_qs(parsed.query).get("slug", [""])[0]
            payload = [{"slug": slug, "question": "BTC 5 Minute Up or Down", "description": ""}]
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        if parsed.path in {f"/ru/event/{current_slug}", f"/ru/event/{next_slug}"}:
            html = (
                '<span class="text-heading-2xl mt-1 tracking-wide font-[620] text-text-secondary">'
                "$100,000.00"
                "</span>"
            ).encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html

        if parsed.path == "/book":
            token = parse_qs(parsed.query).get("token_id", [""])[0]
            if token in {"up-1", "down-1"}:
                payload = {"asks": [{"price": "0.5"}]}
            else:
                payload = {"asks": []}
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        return 404, {"Content-Type": "text/plain"}, b"not found"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(event_module, "GAMMA_EVENTS_URL", f"{base_url}/events")
    monkeypatch.setattr(event_module, "GAMMA_MARKETS_URL", f"{base_url}/markets")
    monkeypatch.setattr(event_module, "POLYMARKET_HOME_RU_URL", f"{base_url}/ru")
    monkeypatch.setattr(event_module, "POLYMARKET_EVENT_RU_URL_TEMPLATE", f"{base_url}/ru/event/{{slug}}")
    monkeypatch.setattr(clob_module, "CLOB_BASE_URL", base_url)

    async def ws_handler(websocket):  # noqa: ANN001
        _ = await websocket.recv()
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_CHAINLINK,
                    "payload": {"symbol": "btc/usd", "value": 100_050.0, "timestamp": 295_000},
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_CHAINLINK,
                    "payload": {"symbol": "btc/usd", "value": 100_060.0, "timestamp": 302_000},
                }
            )
        )
        await websocket.close()

    logs: list[str] = []
    try:
        async with websockets.serve(ws_handler, "127.0.0.1", 0) as ws_server:
            ws_port = ws_server.sockets[0].getsockname()[1]
            event_client = GammaEventsClient(
                event_page_fetcher=_make_local_event_page_fetcher(base_url),
            )
            runner = StrategyRunner(
                event_client=event_client,
                rtds_client=RtdsClient(websocket_url=f"ws://127.0.0.1:{ws_port}"),
                clob_client=ClobClient(),
                logger=logs.append,
                now_seconds=lambda: 100,
            )
            summary = await runner.run(
                StrategyConfig(
                    symbol_pair="BTC/USD",
                    duration_seconds=20,
                    entry_seconds_before_end=5,
                    final_price_delay_seconds=2,
                    threshold_usd=50.0,
                    threshold_4s_usd=40.0,
                    threshold_near_end_usd=30.0,
                    stake_usd=100.0,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.total_profit_usd == 100.0
    assert any(log.startswith("| event_slug |") for log in logs)
    assert any("| up | 0.500000 | win | 100.00 | filled |" in log for log in logs)
