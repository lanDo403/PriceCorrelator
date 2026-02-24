from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

CLOB_BASE_URL = "https://clob.polymarket.com"
_DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PriceCorrelator/0.1",
    "Accept": "application/json,text/plain,*/*",
}


def _urlopen_without_proxy(request: Request, timeout: int = 10):
    opener = build_opener(ProxyHandler({}))
    return opener.open(request, timeout=timeout)


class ClobClient:
    """CLOB API client for reading best ask prices."""

    def get_best_ask(self, token_id: str) -> float | None:
        if not token_id or not token_id.strip():
            raise ValueError("token_id must not be empty.")

        query = urlencode({"token_id": token_id})
        request = Request(f"{CLOB_BASE_URL}/book?{query}", method="GET", headers=_DEFAULT_HTTP_HEADERS)
        with _urlopen_without_proxy(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"CLOB /book returned status {response.status}.")
            payload = json.loads(response.read().decode("utf-8"))

        asks = payload.get("asks")
        if not isinstance(asks, list) or not asks:
            return None
        best = asks[0]
        price = best.get("price")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None
