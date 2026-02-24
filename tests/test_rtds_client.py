import json
from collections.abc import AsyncIterator

import pytest

from price_correlator.models import PriceSource
from price_correlator.rtds_client import (
    RtdsClient,
    RtdsMessageError,
    TOPIC_CHAINLINK,
    TOPIC_POLYMARKET,
    build_subscribe_message,
    parse_rtds_message,
)


def test_build_subscribe_message_for_btc_usd() -> None:
    message = build_subscribe_message("BTC/USD")
    assert message["action"] == "subscribe"
    assert len(message["subscriptions"]) == 2
    assert message["subscriptions"][0] == {
        "topic": TOPIC_POLYMARKET,
        "type": "update",
        "filters": "{\"symbol\":\"btcusdt\"}",
    }
    assert message["subscriptions"][1]["topic"] == TOPIC_CHAINLINK
    assert message["subscriptions"][1]["type"] == "*"
    assert message["subscriptions"][1]["filters"] == "{\"symbol\":\"btc/usd\"}"


@pytest.mark.parametrize(
    "invalid_symbol_pair",
    ["", "BTCUSD", "BTC/", "/USD", "BTC//USD", "BTC /USD", "BTC/USD!", "BT C/USD"],
)
def test_build_subscribe_message_rejects_invalid_symbol_pair(invalid_symbol_pair: str) -> None:
    with pytest.raises(ValueError):
        build_subscribe_message(invalid_symbol_pair)


def test_parse_rtds_message_for_polymarket() -> None:
    payload = {
        "topic": TOPIC_POLYMARKET,
        "payload": {"symbol": "btcusdt", "value": 100_500.5, "timestamp": 1_000},
    }
    tick = parse_rtds_message(payload, received_timestamp_ms=1_050)
    assert tick is not None
    assert tick.source == PriceSource.POLYMARKET
    assert tick.source_timestamp_ms == 1_000


def test_parse_rtds_message_for_chainlink() -> None:
    payload = {
        "topic": TOPIC_CHAINLINK,
        "payload": {"symbol": "btc/usd", "value": "100000.25", "timestamp": 2_000},
    }
    tick = parse_rtds_message(payload, received_timestamp_ms=2_100)
    assert tick is not None
    assert tick.source == PriceSource.CHAINLINK
    assert tick.price == 100000.25


def test_parse_rtds_message_from_subscribe_snapshot() -> None:
    payload = {
        "topic": TOPIC_POLYMARKET,
        "payload": {
            "symbol": "btc/usd",
            "data": [{"timestamp": 2_001, "value": 100_777.77}],
        },
    }
    tick = parse_rtds_message(payload, received_timestamp_ms=2_100)
    assert tick is not None
    assert tick.source == PriceSource.POLYMARKET
    assert tick.source_timestamp_ms == 2_001
    assert tick.price == 100777.77


def test_parse_rtds_message_ignores_unknown_topic() -> None:
    payload = {"topic": "unknown", "payload": {"symbol": "x", "value": 1, "timestamp": 1}}
    assert parse_rtds_message(payload, received_timestamp_ms=10) is None


def test_parse_rtds_message_raises_on_missing_fields() -> None:
    payload = {"topic": TOPIC_POLYMARKET, "payload": {"symbol": "btcusdt"}}
    with pytest.raises(RtdsMessageError):
        parse_rtds_message(payload, received_timestamp_ms=10)


def test_parse_rtds_message_raises_on_non_numeric_fields() -> None:
    payload = {
        "topic": TOPIC_CHAINLINK,
        "payload": {"symbol": "btc/usd", "value": "not-a-number", "timestamp": "bad-ts"},
    }
    with pytest.raises(RtdsMessageError, match="non-numeric"):
        parse_rtds_message(payload, received_timestamp_ms=10)


@pytest.mark.asyncio
async def test_stream_ticks_subscribes_and_yields(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeWebSocket:
        def __init__(self, messages: list[str]) -> None:
            self._messages = iter(messages)
            self.sent_messages: list[dict] = []

        async def send(self, message: str) -> None:
            self.sent_messages.append(json.loads(message))

        def __aiter__(self) -> AsyncIterator[str]:
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._messages)
            except StopIteration as stop:
                raise StopAsyncIteration from stop

    class FakeConnection:
        def __init__(self, ws: FakeWebSocket) -> None:
            self._ws = ws

        async def __aenter__(self) -> FakeWebSocket:
            return self._ws

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    raw_messages = [
        "PING",
        json.dumps(
            {
                "topic": TOPIC_POLYMARKET,
                "payload": {"symbol": "btcusdt", "value": 101_000, "timestamp": 1_000},
            }
        ),
        json.dumps(
            {
                "topic": TOPIC_CHAINLINK,
                "payload": {"symbol": "btc/usd", "value": 100_999, "timestamp": 1_001},
            }
        ),
    ]
    fake_ws = FakeWebSocket(raw_messages)

    captured_kwargs: dict[str, object] = {}

    def fake_connect(*args, **kwargs):  # noqa: ANN002, ANN003
        captured_kwargs.update(kwargs)
        return FakeConnection(fake_ws)

    monkeypatch.setattr("price_correlator.rtds_client.websockets.connect", fake_connect)

    client = RtdsClient("wss://example.test/ws")
    collected = []
    async for tick in client.stream_ticks("BTC/USD"):
        collected.append(tick)

    assert len(collected) == 2
    assert collected[0].source == PriceSource.POLYMARKET
    assert collected[1].source == PriceSource.CHAINLINK
    assert len(fake_ws.sent_messages) == 1
    assert fake_ws.sent_messages[0]["action"] == "subscribe"
    assert len(fake_ws.sent_messages[0]["subscriptions"]) == 2
    assert captured_kwargs["ping_interval"] == 20.0
    assert captured_kwargs["ping_timeout"] == 60.0
    assert captured_kwargs["proxy"] is None
