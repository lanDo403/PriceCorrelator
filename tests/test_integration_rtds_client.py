from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from price_correlator.models import PriceSource
from price_correlator.rtds_client import RtdsClient, TOPIC_CHAINLINK, TOPIC_POLYMARKET


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rtds_client_stream_ticks_with_real_websocket_server() -> None:
    seen_subscribe_messages: list[dict] = []

    async def handler(websocket):  # noqa: ANN001
        subscribe_raw = await websocket.recv()
        seen_subscribe_messages.append(json.loads(subscribe_raw))

        # Non-JSON heartbeat should be ignored.
        await websocket.send("PING")
        # Unknown topic should be ignored.
        await websocket.send(json.dumps({"topic": "unknown", "payload": {}}))
        # Snapshot payload branch.
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_POLYMARKET,
                    "payload": {
                        "symbol": "btcusdt",
                        "data": [{"timestamp": 1_000, "value": 101_111.0}],
                    },
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_CHAINLINK,
                    "payload": {"symbol": "btc/usd", "value": "101110.5", "timestamp": 1_001},
                }
            )
        )
        await websocket.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = RtdsClient(websocket_url=f"ws://127.0.0.1:{port}")

        ticks = []
        async for tick in client.stream_ticks("BTC/USD"):
            ticks.append(tick)

    assert len(ticks) == 2
    assert ticks[0].source == PriceSource.POLYMARKET
    assert ticks[0].price == 101_111.0
    assert ticks[1].source == PriceSource.CHAINLINK
    assert ticks[1].price == 101_110.5

    assert len(seen_subscribe_messages) == 1
    subscribe = seen_subscribe_messages[0]
    assert subscribe["action"] == "subscribe"
    assert len(subscribe["subscriptions"]) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rtds_client_handles_two_concurrent_connections() -> None:
    received_subscribe_count = 0
    subscribe_lock = asyncio.Lock()

    async def handler(websocket):  # noqa: ANN001
        nonlocal received_subscribe_count
        _ = await websocket.recv()
        async with subscribe_lock:
            received_subscribe_count += 1
        await websocket.send(
            json.dumps(
                {
                    "topic": TOPIC_CHAINLINK,
                    "payload": {"symbol": "btc/usd", "value": "100000.0", "timestamp": 2_000},
                }
            )
        )
        await websocket.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"

        client_a = RtdsClient(websocket_url=url)
        client_b = RtdsClient(websocket_url=url)

        async def collect_one(client: RtdsClient):
            async for tick in client.stream_ticks("BTC/USD"):
                return tick
            return None

        tick_a, tick_b = await asyncio.gather(collect_one(client_a), collect_one(client_b))

    assert received_subscribe_count == 2
    assert tick_a is not None and tick_b is not None
    assert tick_a.source == PriceSource.CHAINLINK
    assert tick_b.source == PriceSource.CHAINLINK
