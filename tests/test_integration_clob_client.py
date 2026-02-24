from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

import price_correlator.clob_client as clob_module
from price_correlator.clob_client import ClobClient


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


@pytest.mark.integration
def test_clob_client_returns_best_ask_from_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path != "/book":
            return 404, {"Content-Type": "application/json"}, b"{}"
        token = parse_qs(parsed.query).get("token_id", [""])[0]
        if token != "token-1":
            return 404, {"Content-Type": "application/json"}, b"{}"
        body = json.dumps({"asks": [{"price": "0.42"}, {"price": "0.50"}]}).encode("utf-8")
        return 200, {"Content-Type": "application/json"}, body

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(clob_module, "CLOB_BASE_URL", base_url)
    try:
        client = ClobClient()
        best_ask = client.get_best_ask("token-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert best_ask == 0.42


@pytest.mark.integration
def test_clob_client_returns_none_for_empty_orderbook_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/book":
            body = json.dumps({"asks": []}).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, body
        return 404, {"Content-Type": "application/json"}, b"{}"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(clob_module, "CLOB_BASE_URL", base_url)
    try:
        client = ClobClient()
        best_ask = client.get_best_ask("token-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert best_ask is None


@pytest.mark.integration
def test_clob_client_raises_for_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle_request(path: str):
        return 503, {"Content-Type": "application/json"}, b'{"error":"unavailable"}'

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(clob_module, "CLOB_BASE_URL", base_url)
    try:
        client = ClobClient()
        with pytest.raises(HTTPError, match="503"):
            client.get_best_ask("token-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.integration
def test_clob_client_rejects_empty_token_id() -> None:
    client = ClobClient()
    with pytest.raises(ValueError, match="token_id"):
        client.get_best_ask("")


@pytest.mark.integration
def test_clob_client_returns_none_for_non_numeric_ask_price(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle_request(path: str):
        parsed = urlparse(path)
        if parsed.path == "/book":
            body = json.dumps({"asks": [{"price": "bad-number"}]}).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, body
        return 404, {"Content-Type": "application/json"}, b"{}"

    server, base_url, thread = _start_http_server(handle_request)
    monkeypatch.setattr(clob_module, "CLOB_BASE_URL", base_url)
    try:
        client = ClobClient()
        best_ask = client.get_best_ask("token-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert best_ask is None
