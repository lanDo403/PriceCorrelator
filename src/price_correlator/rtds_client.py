from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

import websockets

from price_correlator.models import PriceSource, PriceTick

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

TOPIC_POLYMARKET = "crypto_prices"
TOPIC_CHAINLINK = "crypto_prices_chainlink"
_SYMBOL_PART_RE = re.compile(r"^[A-Z0-9]+$")


class RtdsMessageError(ValueError):
    """Raised when an RTDS message cannot be parsed."""


def build_subscribe_message(symbol_pair: str = "BTC/USD") -> dict[str, Any]:
    """Build a single RTDS subscribe payload for both sources of one symbol pair."""

    normalized = symbol_pair.strip().upper()
    parts = normalized.split("/")
    if len(parts) != 2:
        raise ValueError("symbol_pair must be in BASE/QUOTE format, for example BTC/USD.")
    if any(part != part.strip() for part in parts):
        raise ValueError("symbol_pair must not contain spaces around '/'.")
    base, quote = parts
    if not base or not quote:
        raise ValueError("symbol_pair must include non-empty BASE and QUOTE parts.")
    if _SYMBOL_PART_RE.fullmatch(base) is None or _SYMBOL_PART_RE.fullmatch(quote) is None:
        raise ValueError("symbol_pair BASE/QUOTE must contain only letters and digits.")

    # Topic crypto_prices expects Binance-like spot symbol.
    spot_symbol = f"{base.lower()}{'usdt' if quote == 'USD' else quote.lower()}"
    chainlink_symbol = f"{base.lower()}/{quote.lower()}"

    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": TOPIC_POLYMARKET,
                "type": "update",
                "filters": json.dumps({"symbol": spot_symbol}, separators=(",", ":")),
            },
            {
                "topic": TOPIC_CHAINLINK,
                "type": "*",
                "filters": json.dumps({"symbol": chainlink_symbol}, separators=(",", ":")),
            },
        ],
    }


def parse_rtds_message(payload: Mapping[str, Any], received_timestamp_ms: int) -> PriceTick | None:
    """Convert RTDS payload into a domain price tick."""

    topic = payload.get("topic")
    if topic not in {TOPIC_POLYMARKET, TOPIC_CHAINLINK}:
        return None

    inner_payload = payload.get("payload")
    if not isinstance(inner_payload, Mapping):
        raise RtdsMessageError("Expected object payload.")

    symbol = inner_payload.get("symbol")
    price = inner_payload.get("value")
    source_ts = inner_payload.get("timestamp", payload.get("timestamp"))

    # Subscribe responses may include payload.data snapshots.
    if price is None or source_ts is None:
        data = inner_payload.get("data")
        if isinstance(data, list) and data:
            last_point = data[-1]
            if isinstance(last_point, Mapping):
                price = last_point.get("value")
                source_ts = last_point.get("timestamp")

    if symbol is None or price is None or source_ts is None:
        raise RtdsMessageError("Message is missing symbol/value/timestamp.")

    source = PriceSource.POLYMARKET if topic == TOPIC_POLYMARKET else PriceSource.CHAINLINK

    try:
        parsed_price = float(price)
        parsed_source_ts = int(source_ts)
    except (TypeError, ValueError) as exc:
        raise RtdsMessageError("Message contains non-numeric value/timestamp.") from exc

    return PriceTick(
        source=source,
        symbol=str(symbol),
        price=parsed_price,
        source_timestamp_ms=parsed_source_ts,
        received_timestamp_ms=received_timestamp_ms,
    )


class RtdsClient:
    """Polymarket RTDS client that streams ticks."""

    def __init__(
        self,
        websocket_url: str = RTDS_WS_URL,
        ping_interval_s: float = 20.0,
        ping_timeout_s: float = 60.0,
    ) -> None:
        self.websocket_url = websocket_url
        self.ping_interval_s = ping_interval_s
        self.ping_timeout_s = ping_timeout_s

    async def stream_ticks(self, symbol_pair: str = "BTC/USD") -> AsyncIterator[PriceTick]:
        subscribe_message = build_subscribe_message(symbol_pair)
        # Disable system proxy to avoid broken local proxy entries in some environments.
        async with websockets.connect(
            self.websocket_url,
            ping_interval=self.ping_interval_s,
            ping_timeout=self.ping_timeout_s,
            proxy=None,
        ) as websocket:
            await websocket.send(json.dumps(subscribe_message))

            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    continue

                received_timestamp_ms = time.time_ns() // 1_000_000
                try:
                    parsed = json.loads(raw_message)
                except json.JSONDecodeError:
                    # Service heartbeat payloads can be non-JSON.
                    continue
                if not isinstance(parsed, Mapping):
                    continue

                try:
                    tick = parse_rtds_message(parsed, received_timestamp_ms=received_timestamp_ms)
                except RtdsMessageError:
                    # Some service messages differ from expected market payload schema.
                    continue
                if tick is not None:
                    yield tick
