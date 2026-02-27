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

    @staticmethod
    def _require_token_id(token_id: str) -> str:
        if not token_id or not token_id.strip():
            raise ValueError("token_id must not be empty.")
        return token_id

    @staticmethod
    def _parse_rate(value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        if parsed <= 1:
            return parsed
        # Some endpoints expose basis points, for example 156 -> 0.0156.
        if parsed <= 10_000:
            return parsed / 10_000
        return None

    @classmethod
    def _extract_taker_fee_rate(cls, payload: dict) -> float | None:
        direct_keys = (
            "takerRate",
            "taker_rate",
            "takerFeeRate",
            "taker_fee_rate",
            "feeRate",
            "fee_rate",
            "takerRateBps",
            "taker_rate_bps",
            "feeRateBps",
            "fee_rate_bps",
        )
        for key in direct_keys:
            if key not in payload:
                continue
            rate = cls._parse_rate(payload.get(key))
            if rate is not None:
                return rate

        nested = payload.get("rates")
        if isinstance(nested, dict):
            for key in ("taker", "takerRate", "taker_rate", "feeRate", "fee_rate"):
                if key not in nested:
                    continue
                rate = cls._parse_rate(nested.get(key))
                if rate is not None:
                    return rate
        return None

    def get_best_ask(self, token_id: str) -> float | None:
        token_id = self._require_token_id(token_id)

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

    def get_taker_fee_rate(self, token_id: str) -> float | None:
        token_id = self._require_token_id(token_id)

        query = urlencode({"token_id": token_id})
        request = Request(f"{CLOB_BASE_URL}/fee-rate?{query}", method="GET", headers=_DEFAULT_HTTP_HEADERS)
        with _urlopen_without_proxy(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"CLOB /fee-rate returned status {response.status}.")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return self._extract_taker_fee_rate(payload)
