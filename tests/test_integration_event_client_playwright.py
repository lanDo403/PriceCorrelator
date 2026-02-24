from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import price_correlator.event_client as event_module
from price_correlator.event_client import GammaEventsClient


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


@pytest.fixture(scope="module")
def playwright_ready() -> None:
    if os.getenv("ENABLE_PLAYWRIGHT_INTEGRATION") != "1":
        pytest.skip("Playwright integration tests are disabled. Set ENABLE_PLAYWRIGHT_INTEGRATION=1 to enable.")

    sync_api = pytest.importorskip("playwright.sync_api")
    sync_playwright = sync_api.sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright browser is not available: {exc}")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_client_fetches_price_to_beat_from_playwright_rendered_dom(
    monkeypatch: pytest.MonkeyPatch,
    playwright_ready: None,  # noqa: ARG001
) -> None:
    slug = "btc-updown-5m-1771162200"

    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/events":
            query_slug = parse_qs(parsed.query).get("slug", [""])[0]
            body = json.dumps([{"slug": query_slug, "question": "BTC 5 Minute Up or Down", "description": ""}]).encode(
                "utf-8"
            )
            return 200, {"Content-Type": "application/json"}, body

        if parsed.path == "/markets":
            query_slug = parse_qs(parsed.query).get("slug", [""])[0]
            body = json.dumps(
                [
                    {
                        "slug": query_slug,
                        "question": "BTC 5 Minute Up or Down",
                        "startTimestamp": 1771161900,
                        "endTimestamp": 1771162200,
                        "outcomes": ["Up", "Down"],
                        "clobTokenIds": ["up-1", "down-1"],
                    }
                ]
            ).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, body

        if parsed.path == "/ru":
            html = f'<a href="/ru/event/{slug}">BTC 5 Minute Up or Down</a>'.encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html

        if parsed.path == f"/ru/event/{slug}":
            html = """
                <html><body>
                <script>
                setTimeout(() => {
                    const s = document.createElement('span');
                    s.className = 'text-heading-2xl mt-1 tracking-wide font-[620] text-text-secondary';
                    s.textContent = '$68,267.37';
                    document.body.appendChild(s);
                }, 30);
                </script>
                </body></html>
            """.encode("utf-8")
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html

        return 404, {"Content-Type": "text/plain"}, b"not found"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(event_module, "GAMMA_EVENTS_URL", f"{base_url}/events")
    monkeypatch.setattr(event_module, "GAMMA_MARKETS_URL", f"{base_url}/markets")
    monkeypatch.setattr(event_module, "POLYMARKET_HOME_RU_URL", f"{base_url}/ru")
    monkeypatch.setattr(event_module, "POLYMARKET_EVENT_RU_URL_TEMPLATE", f"{base_url}/ru/event/{{slug}}")

    try:
        client = GammaEventsClient()
        market = await client.fetch_event_market_info(slug)
        discovered = await client.discover_latest_btc_updown_5m_slug()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert discovered == slug
    assert market.slug == slug
    assert market.price_to_beat == 68_267.37
    assert market.up_token_id == "up-1"
    assert market.down_token_id == "down-1"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_client_handles_missing_price_to_beat_in_dom(
    monkeypatch: pytest.MonkeyPatch,
    playwright_ready: None,  # noqa: ARG001
) -> None:
    slug = "btc-updown-5m-1771162500"

    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/markets":
            query_slug = parse_qs(parsed.query).get("slug", [""])[0]
            body = json.dumps(
                [
                    {
                        "slug": query_slug,
                        "question": "BTC 5 Minute Up or Down",
                        "startTimestamp": 1771162200,
                        "endTimestamp": 1771162500,
                        "outcomes": ["Up", "Down"],
                        "clobTokenIds": ["up-2", "down-2"],
                    }
                ]
            ).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, body

        if parsed.path == f"/ru/event/{slug}":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<html><body>No span</body></html>"

        if parsed.path == "/events":
            return 200, {"Content-Type": "application/json"}, b"[]"

        if parsed.path == "/ru":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, b""

        return 404, {"Content-Type": "text/plain"}, b"not found"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(event_module, "GAMMA_EVENTS_URL", f"{base_url}/events")
    monkeypatch.setattr(event_module, "GAMMA_MARKETS_URL", f"{base_url}/markets")
    monkeypatch.setattr(event_module, "POLYMARKET_HOME_RU_URL", f"{base_url}/ru")
    monkeypatch.setattr(event_module, "POLYMARKET_EVENT_RU_URL_TEMPLATE", f"{base_url}/ru/event/{{slug}}")

    try:
        client = GammaEventsClient()
        market = await client.fetch_event_market_info(slug)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert market.price_to_beat is None
