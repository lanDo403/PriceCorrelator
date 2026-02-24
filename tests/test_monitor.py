import asyncio
from collections.abc import AsyncIterator

import pytest

from price_correlator.config import MonitorConfig
from price_correlator.lag_analyzer import LagAnalyzer
from price_correlator.models import EventMetadata, PriceSource, PriceTick
from price_correlator.monitor import MonitorService


class _FakeEventClient:
    async def fetch_event_by_slug(self, slug: str) -> EventMetadata:
        return EventMetadata(
            slug=slug,
            question="Will BTC be above ...?",
            description="desc",
            resolution_source="https://data.chain.link/streams/btc-usd",
        )

    async def discover_latest_btc_updown_5m_slug(self) -> str:
        return "btc-updown-5m-1771159800"


class _FailingEventClient:
    async def fetch_event_by_slug(self, slug: str) -> EventMetadata:
        raise RuntimeError("HTTP 403")

    async def discover_latest_btc_updown_5m_slug(self) -> str:
        return "btc-updown-5m-1771159800"


class _FakeRtdsClient:
    def __init__(self, ticks: list[PriceTick]) -> None:
        self._ticks = ticks

    async def stream_ticks(self, symbol_pair: str) -> AsyncIterator[PriceTick]:
        for tick in self._ticks:
            yield tick


class _FailingRtdsClient:
    async def stream_ticks(self, symbol_pair: str) -> AsyncIterator[PriceTick]:
        raise ConnectionError("refused")
        yield  # pragma: no cover


class _HangingRtdsClient:
    async def stream_ticks(self, symbol_pair: str) -> AsyncIterator[PriceTick]:
        while True:
            await asyncio.sleep(10)
            yield  # pragma: no cover


class _StepClock:
    def __init__(self, start: float = 0.0, step: float = 0.25) -> None:
        self._current = start
        self._step = step

    def __call__(self) -> float:
        self._current += self._step
        return self._current


def _tick(source: PriceSource, source_ts: int, received_ts: int, price: float = 100_000.0) -> PriceTick:
    return PriceTick(
        source=source,
        symbol="btc/usd",
        price=price,
        source_timestamp_ms=source_ts,
        received_timestamp_ms=received_ts,
    )


@pytest.mark.asyncio
async def test_monitor_run_produces_summary_and_table_logs() -> None:
    ticks = [
        _tick(PriceSource.POLYMARKET, source_ts=1_000, received_ts=1_020, price=100_100),
        _tick(PriceSource.CHAINLINK, source_ts=1_010, received_ts=1_030, price=100_090),
        _tick(PriceSource.POLYMARKET, source_ts=1_040, received_ts=1_050, price=100_110),
        _tick(PriceSource.CHAINLINK, source_ts=1_042, received_ts=1_051, price=100_120),
    ]
    logs: list[str] = []
    monitor = MonitorService(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(ticks),
        lag_analyzer=LagAnalyzer(),
        logger=logs.append,
        monotonic=_StepClock(step=0.2),
    )
    config = MonitorConfig(
        event_url="https://polymarket.com/ru/event/btc-updown-5m-1771157400",
        duration_seconds=2,
        report_interval_seconds=1,
        stale_threshold_ms=0,
    )
    summary = await monitor.run(config)

    assert summary.sample_count >= 1
    assert any("Resolution source:" in log for log in logs)
    assert any(log.startswith("| observed_utc |") for log in logs)
    assert any("| chainlink |" in log or "| tie |" in log or "| polymarket |" in log for log in logs)
    assert any("Summary:" in log for log in logs)


@pytest.mark.asyncio
async def test_monitor_run_continues_when_event_metadata_unavailable() -> None:
    ticks = [
        _tick(PriceSource.POLYMARKET, source_ts=1_000, received_ts=1_020),
        _tick(PriceSource.CHAINLINK, source_ts=1_010, received_ts=1_030),
    ]
    logs: list[str] = []
    monitor = MonitorService(
        event_client=_FailingEventClient(),
        rtds_client=_FakeRtdsClient(ticks),
        lag_analyzer=LagAnalyzer(),
        logger=logs.append,
        monotonic=_StepClock(step=0.1),
    )
    config = MonitorConfig(
        event_url="https://polymarket.com/ru/event/btc-updown-5m-1771157400",
        duration_seconds=2,
        report_interval_seconds=1,
        stale_threshold_ms=0,
    )
    summary = await monitor.run(config)

    assert summary.sample_count == 1
    assert any("Warning: failed to fetch event metadata" in log for log in logs)
    assert any("Summary:" in log for log in logs)


@pytest.mark.asyncio
async def test_monitor_auto_event_slug_discovery() -> None:
    ticks = [
        _tick(PriceSource.POLYMARKET, source_ts=1_000, received_ts=1_020),
        _tick(PriceSource.CHAINLINK, source_ts=1_010, received_ts=1_030),
    ]
    logs: list[str] = []
    monitor = MonitorService(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(ticks),
        lag_analyzer=LagAnalyzer(),
        logger=logs.append,
        monotonic=_StepClock(step=0.1),
    )
    config = MonitorConfig(event_url="auto", duration_seconds=2, report_interval_seconds=1)
    summary = await monitor.run(config)

    assert summary.sample_count == 1
    assert any("Auto-selected event: https://polymarket.com/ru/event/btc-updown-5m-1771159800" in log for log in logs)


@pytest.mark.asyncio
async def test_monitor_run_continues_when_rtds_unavailable() -> None:
    logs: list[str] = []
    monitor = MonitorService(
        event_client=_FakeEventClient(),
        rtds_client=_FailingRtdsClient(),
        lag_analyzer=LagAnalyzer(),
        logger=logs.append,
        monotonic=_StepClock(step=0.6),
    )
    config = MonitorConfig(
        event_url="auto",
        duration_seconds=1,
        report_interval_seconds=1,
        stale_threshold_ms=0,
    )
    summary = await monitor.run(config)

    assert summary.sample_count == 0
    assert any("Warning: failed to connect to RTDS" in log for log in logs)
    assert any("Summary: insufficient data" in log for log in logs)


@pytest.mark.asyncio
async def test_monitor_respects_duration_when_stream_hangs() -> None:
    import time

    logs: list[str] = []
    monitor = MonitorService(
        event_client=_FakeEventClient(),
        rtds_client=_HangingRtdsClient(),
        lag_analyzer=LagAnalyzer(),
        logger=logs.append,
    )
    started = time.monotonic()
    summary = await monitor.run(MonitorConfig(event_url="auto", duration_seconds=1))
    elapsed = time.monotonic() - started

    assert summary.sample_count == 0
    assert elapsed < 2.5
