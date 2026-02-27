import asyncio

import pytest

from price_correlator.models import EventMarketInfo, PriceSource, PriceTick
from price_correlator.strategy import StrategyConfig, StrategyRunner


class _FakeEventClient:
    async def discover_latest_btc_updown_5m_slug(self) -> str:
        return "btc-updown-5m-300"

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        assert slug == "btc-updown-5m-300"
        return EventMarketInfo(
            slug=slug,
            title=slug,
            start_timestamp_s=0,
            end_timestamp_s=300,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )


class _FakeClobClient:
    def get_best_ask(self, token_id: str) -> float | None:
        assert token_id in {"up-token", "down-token"}
        return 0.5


class _FakeClobClientWithFee(_FakeClobClient):
    def get_taker_fee_rate(self, token_id: str) -> float | None:
        assert token_id in {"up-token", "down-token"}
        return 0.015


class _FakeClobClientFailingFee(_FakeClobClient):
    def get_taker_fee_rate(self, token_id: str) -> float | None:
        raise RuntimeError(f"fee endpoint unavailable for {token_id}")


class _FakeRtdsClient:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_single_event_win() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.skips == 0
    assert summary.total_profit_usd == 100.0
    assert any(log.startswith("| event_slug |") for log in logs)
    assert any("| up | 0.500000 | win | 100.00 | filled |" in log for log in logs)


@pytest.mark.asyncio
async def test_strategy_runner_uses_dynamic_stake_provider_per_event() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        stake_provider=lambda _market: 50.0,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.total_profit_usd == 50.0
    assert any("| up | 0.500000 | win | 50.00 | filled |" in log for log in logs)


@pytest.mark.asyncio
async def test_strategy_runner_skips_when_dynamic_stake_is_zero() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        stake_provider=lambda _market: 0.0,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.skips == 1
    assert any("| skip | 0.00 | insufficient_stake |" in log for log in logs)


@pytest.mark.asyncio
async def test_strategy_runner_applies_taker_fee_to_profit() -> None:
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClientWithFee(),
        logger=lambda _line: None,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
            apply_taker_fees=True,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.total_profit_usd == pytest.approx(98.5)


@pytest.mark.asyncio
async def test_strategy_runner_continues_when_fee_fetch_fails() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClientFailingFee(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
            apply_taker_fees=True,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.total_profit_usd == 100.0
    assert any("warning: clob fee fetch failed" in line for line in logs)


class _FakeRtdsClientDown:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=99_930.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=99_920.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_single_event_down_win() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClientDown(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.skips == 0
    assert summary.total_profit_usd == 100.0
    assert any("| down | 0.500000 | win | 100.00 | filled |" in log for log in logs)


class _FakeActiveEventClient:
    def __init__(self) -> None:
        self._calls = 0

    async def fetch_active_btc_updown_5m_market_info(self, now_seconds: int | None = None) -> EventMarketInfo:
        self._calls += 1
        end_timestamp = 305 if self._calls == 1 else 605
        return EventMarketInfo(
            slug=f"btc-updown-5m-{end_timestamp}",
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=end_timestamp - 300,
            end_timestamp_s=end_timestamp,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        assert slug == "btc-updown-5m-605"
        return EventMarketInfo(
            slug=slug,
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=305,
            end_timestamp_s=605,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )


class _FakeRtdsClientActiveTimer:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=10_000,
            received_timestamp_ms=10_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=300_000,
            received_timestamp_ms=300_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=307_000,
            received_timestamp_ms=307_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_uses_active_polymarket_timer() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeActiveEventClient(),
        rtds_client=_FakeRtdsClientActiveTimer(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.skips == 0
    assert summary.total_profit_usd == 100.0
    assert any("| btc-updown-5m-305 |" in log for log in logs)


class _FakeClobAppearsLate:
    def __init__(self) -> None:
        self._calls = 0

    def get_best_ask(self, token_id: str) -> float | None:
        self._calls += 1
        if self._calls == 1:
            return None
        return 0.5


class _FakeRtdsClientLateEntry:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        # remaining=5, delta=45: threshold is 50, no entry.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_045.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        # remaining=4, delta=35: threshold is 40, no entry.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_035.0,
            source_timestamp_ms=296_000,
            received_timestamp_ms=296_010,
        )
        # remaining=2, delta=35: threshold is 30, but first liquidity check fails.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_035.0,
            source_timestamp_ms=298_000,
            received_timestamp_ms=298_010,
        )
        # remaining=1, condition still true and liquidity appears.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_034.0,
            source_timestamp_ms=299_000,
            received_timestamp_ms=299_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_040.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_enters_when_liquidity_appears_late() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClientLateEntry(),
        clob_client=_FakeClobAppearsLate(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert summary.skips == 0
    assert any("| up | 0.500000 | win | 100.00 | filled |" in log for log in logs)


class _FakeRtdsClientThresholdLadder:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        # remaining=4, delta=39 -> no entry by 40-threshold.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_039.0,
            source_timestamp_ms=296_000,
            received_timestamp_ms=296_010,
        )
        # remaining=3, delta=31 -> entry by 30-threshold.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_031.0,
            source_timestamp_ms=297_000,
            received_timestamp_ms=297_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_040.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_uses_5_4_3_second_threshold_ladder() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClientThresholdLadder(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert any("| 100031.000000 |" in log for log in logs)


class _FakeRtdsClient30sGap:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        # remaining=30, delta=160: should enter by 30s condition.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_160.0,
            source_timestamp_ms=270_000,
            received_timestamp_ms=270_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_170.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_enters_on_30s_gap_condition() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient30sGap(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_30s_usd=150.0,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert any("| 100160.000000 |" in log for log in logs)


class _FakeEventClientSlugIsStart:
    def __init__(self) -> None:
        self.next_slug_requested: str | None = None

    async def fetch_active_btc_updown_5m_market_info(self, now_seconds: int | None = None) -> EventMarketInfo:
        return EventMarketInfo(
            slug="btc-updown-5m-100",
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=100,
            end_timestamp_s=400,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        self.next_slug_requested = slug
        assert slug == "btc-updown-5m-400"
        return EventMarketInfo(
            slug=slug,
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=400,
            end_timestamp_s=700,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )


class _FakeRtdsClientForStartSlug:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_000.0,
            source_timestamp_ms=110_000,
            received_timestamp_ms=110_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=395_000,
            received_timestamp_ms=395_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=402_000,
            received_timestamp_ms=402_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_switches_next_by_slug_plus_300() -> None:
    client = _FakeEventClientSlugIsStart()
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=client,
        rtds_client=_FakeRtdsClientForStartSlug(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 150,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert client.next_slug_requested == "btc-updown-5m-400"


class _FakeEventClientSlugDurationPreferred:
    def __init__(self) -> None:
        self.next_slug_requested: str | None = None

    async def fetch_active_btc_updown_5m_market_info(self, now_seconds: int | None = None) -> EventMarketInfo:
        # Intentionally inconsistent timer; slug still says 5m.
        return EventMarketInfo(
            slug="btc-updown-5m-100",
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=10,
            end_timestamp_s=100,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=100_000.0,
        )

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        self.next_slug_requested = slug
        if slug == "btc-updown-5m-400":
            return EventMarketInfo(
                slug=slug,
                title="BTC 5 Minute Up or Down",
                start_timestamp_s=400,
                end_timestamp_s=700,
                up_token_id="up-token",
                down_token_id="down-token",
                price_to_beat=100_000.0,
            )
        raise LookupError(slug)


@pytest.mark.asyncio
async def test_strategy_runner_uses_slug_timeframe_for_next_slug_step() -> None:
    client = _FakeEventClientSlugDurationPreferred()
    runner = StrategyRunner(
        event_client=client,
        rtds_client=_FakeRtdsClientForStartSlug(),
        clob_client=_FakeClobClient(),
        logger=lambda _line: None,
        now_seconds=lambda: 150,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert client.next_slug_requested == "btc-updown-5m-400"


class _FakeEventClient15mGeneric:
    def __init__(self) -> None:
        self.active_timeframes: list[int] = []

    async def discover_latest_btc_updown_slug(self, timeframe_minutes: int = 5) -> str:
        assert timeframe_minutes == 15
        return "btc-updown-15m-900"

    async def fetch_active_btc_updown_market_info(
        self,
        timeframe_minutes: int = 5,
        now_seconds: int | None = None,
    ) -> EventMarketInfo:
        self.active_timeframes.append(timeframe_minutes)
        return EventMarketInfo(
            slug="btc-updown-15m-900",
            title="BTC 15 Minute Up or Down",
            start_timestamp_s=0,
            end_timestamp_s=900,
            up_token_id="up-token-15m",
            down_token_id="down-token-15m",
            price_to_beat=100_000.0,
        )

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        if slug == "btc-updown-15m-900":
            return EventMarketInfo(
                slug=slug,
                title="BTC 15 Minute Up or Down",
                start_timestamp_s=0,
                end_timestamp_s=900,
                up_token_id="up-token-15m",
                down_token_id="down-token-15m",
                price_to_beat=100_000.0,
            )
        if slug == "btc-updown-15m-1800":
            return EventMarketInfo(
                slug=slug,
                title="BTC 15 Minute Up or Down",
                start_timestamp_s=900,
                end_timestamp_s=1800,
                up_token_id="up-token-15m-next",
                down_token_id="down-token-15m-next",
                price_to_beat=100_000.0,
            )
        raise LookupError(f"unexpected slug: {slug}")


class _FakeRtdsClient15m:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=895_000,
            received_timestamp_ms=895_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_080.0,
            source_timestamp_ms=902_000,
            received_timestamp_ms=902_010,
        )


class _FakeRtdsClient15mGap30:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        # remaining=30 (900-870), delta=+160: should enter by 30s rule.
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_160.0,
            source_timestamp_ms=870_000,
            received_timestamp_ms=870_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_180.0,
            source_timestamp_ms=902_000,
            received_timestamp_ms=902_010,
        )


class _FakeClobClientAnyToken:
    def get_best_ask(self, token_id: str) -> float | None:
        assert token_id
        return 0.5


@pytest.mark.asyncio
async def test_strategy_runner_supports_15m_market_with_same_rules() -> None:
    logs: list[str] = []
    client = _FakeEventClient15mGeneric()
    runner = StrategyRunner(
        event_client=client,
        rtds_client=_FakeRtdsClient15m(),
        clob_client=_FakeClobClientAnyToken(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )

    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            market_timeframe_minutes=15,
            duration_seconds=1800,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert client.active_timeframes and all(value == 15 for value in client.active_timeframes)
    assert any("switch_event: init -> btc-updown-15m-900 (source=active_market)" in line for line in logs)
    assert any("switch_event: btc-updown-15m-900 -> btc-updown-15m-1800 (source=predicted_next_slug)" in line for line in logs)


@pytest.mark.asyncio
async def test_strategy_runner_15m_enters_on_30s_gap_condition() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient15mGeneric(),
        rtds_client=_FakeRtdsClient15mGap30(),
        clob_client=_FakeClobClientAnyToken(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )

    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            market_timeframe_minutes=15,
            duration_seconds=1800,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_30s_usd=150.0,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert any("| 100160.000000 |" in line for line in logs)


class _StepMonotonic:
    def __init__(self, step: float = 11.0) -> None:
        self._value = 0.0
        self._step = step

    def __call__(self) -> float:
        current = self._value
        self._value += self._step
        return current


class _FakeEventClientPriceAppearsLate:
    def __init__(self) -> None:
        self.refresh_calls = 0

    async def fetch_active_btc_updown_5m_market_info(self, now_seconds: int | None = None) -> EventMarketInfo:
        return EventMarketInfo(
            slug="btc-updown-5m-300",
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=0,
            end_timestamp_s=300,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=None,
        )

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        assert slug == "btc-updown-5m-300"
        self.refresh_calls += 1
        price_to_beat = None if self.refresh_calls < 2 else 100_000.0
        return EventMarketInfo(
            slug=slug,
            title="BTC 5 Minute Up or Down",
            start_timestamp_s=0,
            end_timestamp_s=300,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=price_to_beat,
        )


class _FakeRtdsClientForLatePriceToBeat:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=99_990.0,
            source_timestamp_ms=1_000,
            received_timestamp_ms=1_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_refreshes_price_to_beat_every_10_seconds_until_available() -> None:
    logs: list[str] = []
    fake_event_client = _FakeEventClientPriceAppearsLate()
    runner = StrategyRunner(
        event_client=fake_event_client,
        rtds_client=_FakeRtdsClientForLatePriceToBeat(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        monotonic=_StepMonotonic(step=11.0),
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert fake_event_client.refresh_calls >= 2
    assert any("price_to_beat_refresh: slug=btc-updown-5m-300, price_to_beat=100000.00" in log for log in logs)


async def _no_sleep(_: float) -> None:
    return None


class _FakeRtdsClientReconnectAfterTimeout:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        self.calls += 1
        if self.calls == 1:
            yield PriceTick(
                source=PriceSource.CHAINLINK,
                symbol="btc/usd",
                price=100_000.0,
                source_timestamp_ms=1_000,
                received_timestamp_ms=1_010,
            )
            raise RuntimeError("sent 1011 (internal error) keepalive ping timeout; no close frame received")

        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


class _FakeRtdsClientSilentThenRecovers:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(3600)
            if False:
                yield
            return

        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_060.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_070.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_reconnects_after_rtds_keepalive_timeout() -> None:
    logs: list[str] = []
    fake_rtds = _FakeRtdsClientReconnectAfterTimeout()
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=fake_rtds,
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
        reconnect_delays_seconds=(0.0,),
        sleep=_no_sleep,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert fake_rtds.calls >= 2
    assert any("warning: rtds reconnect scheduled in 0.0s" in log for log in logs)


@pytest.mark.asyncio
async def test_strategy_runner_reconnects_when_rtds_stream_goes_silent() -> None:
    logs: list[str] = []
    fake_rtds = _FakeRtdsClientSilentThenRecovers()
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=fake_rtds,
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
        reconnect_delays_seconds=(0.0,),
        max_tick_silence_seconds=0.5,
        sleep=_no_sleep,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert fake_rtds.calls >= 2
    assert any("reason=no ticks for 0.5s" in log for log in logs)


class _FailingClobClient:
    def get_best_ask(self, token_id: str) -> float | None:
        raise RuntimeError(f"clob unavailable for {token_id}")


@pytest.mark.asyncio
async def test_strategy_runner_skips_when_clob_price_fetch_fails() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClient(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FailingClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.skips == 1
    assert summary.wins == 0
    assert any("warning: clob price fetch failed" in log for log in logs)
    assert any("| skip | 0.00 | clob_error:RuntimeError |" in log for log in logs)


class _FakeEventClientPredictedSlugFail:
    def __init__(self) -> None:
        self._discover_calls = 0

    async def discover_latest_btc_updown_5m_slug(self) -> str:
        self._discover_calls += 1
        if self._discover_calls == 1:
            return "btc-updown-5m-300"
        return "btc-updown-5m-900"

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        if slug == "btc-updown-5m-300":
            return EventMarketInfo(
                slug=slug,
                title=slug,
                start_timestamp_s=0,
                end_timestamp_s=300,
                up_token_id="up-token",
                down_token_id="down-token",
                price_to_beat=100_000.0,
            )
        if slug == "btc-updown-5m-900":
            return EventMarketInfo(
                slug=slug,
                title=slug,
                start_timestamp_s=600,
                end_timestamp_s=900,
                up_token_id="up-token-2",
                down_token_id="down-token-2",
                price_to_beat=100_100.0,
            )
        raise LookupError(f"market not ready: {slug}")


@pytest.mark.asyncio
async def test_strategy_runner_logs_predicted_slug_failure_and_uses_discovery() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClientPredictedSlugFail(),
        rtds_client=_FakeRtdsClient(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
        sleep=_no_sleep,
    )
    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.wins == 1
    assert any("warning: predicted next slug failed: slug=btc-updown-5m-600" in log for log in logs)
    assert any("switch_event: btc-updown-5m-300 -> btc-updown-5m-900 (source=latest_discovery)" in log for log in logs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"duration_seconds": 0}, "duration_seconds"),
        ({"market_timeframe_minutes": 0}, "market_timeframe_minutes"),
        ({"entry_seconds_before_end": 0}, "entry_seconds_before_end"),
        ({"entry_seconds_before_end": 301}, "entry_seconds_before_end"),
        ({"market_timeframe_minutes": 15, "entry_seconds_before_end": 901}, "entry_seconds_before_end"),
        ({"final_price_delay_seconds": -1}, "final_price_delay_seconds"),
        ({"stake_usd": 0}, "stake_usd"),
        ({"threshold_30s_usd": -1}, "threshold values"),
        ({"threshold_30s_usd": 40, "threshold_usd": 50}, "threshold_30s_usd"),
        ({"threshold_usd": 30, "threshold_4s_usd": 40, "threshold_near_end_usd": 20}, "threshold ladder"),
        ({"symbol_pair": "BTC//USD"}, "symbol_pair"),
    ],
)
def test_strategy_config_rejects_invalid_values(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        StrategyConfig(**kwargs)


def test_strategy_config_accepts_15m_entry_window_upper_bound() -> None:
    config = StrategyConfig(market_timeframe_minutes=15, entry_seconds_before_end=900)
    assert config.market_timeframe_minutes == 15
    assert config.entry_seconds_before_end == 900


class _FailingEventClient:
    async def discover_latest_btc_updown_5m_slug(self) -> str:
        raise LookupError("no events")

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        raise AssertionError("must not be called")


class _EmptyRtdsClient:
    async def stream_ticks(self, symbol_pair: str):
        if False:
            yield  # pragma: no cover


@pytest.mark.asyncio
async def test_strategy_runner_fails_fast_when_initial_event_unavailable() -> None:
    runner = StrategyRunner(
        event_client=_FailingEventClient(),
        rtds_client=_EmptyRtdsClient(),
        clob_client=_FakeClobClient(),
        logger=lambda _msg: None,
    )
    with pytest.raises(LookupError):
        await runner.run(StrategyConfig(duration_seconds=10))


class _FakeEventClientMissingPriceToBeat:
    async def discover_latest_btc_updown_5m_slug(self) -> str:
        return "btc-updown-5m-300"

    async def fetch_event_market_info(self, slug: str) -> EventMarketInfo:
        assert slug == "btc-updown-5m-300"
        return EventMarketInfo(
            slug=slug,
            title=slug,
            start_timestamp_s=0,
            end_timestamp_s=300,
            up_token_id="up-token",
            down_token_id="down-token",
            price_to_beat=None,
        )


class _FakeRtdsClientMissingPriceToBeat:
    async def stream_ticks(self, symbol_pair: str):
        assert symbol_pair == "BTC/USD"
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_001.0,
            source_timestamp_ms=295_000,
            received_timestamp_ms=295_010,
        )
        yield PriceTick(
            source=PriceSource.CHAINLINK,
            symbol="btc/usd",
            price=100_010.0,
            source_timestamp_ms=302_000,
            received_timestamp_ms=302_010,
        )


@pytest.mark.asyncio
async def test_strategy_runner_marks_skip_reason_as_missing_price_to_beat() -> None:
    logs: list[str] = []
    runner = StrategyRunner(
        event_client=_FakeEventClientMissingPriceToBeat(),
        rtds_client=_FakeRtdsClientMissingPriceToBeat(),
        clob_client=_FakeClobClient(),
        logger=logs.append,
        now_seconds=lambda: 0,
    )

    summary = await runner.run(
        StrategyConfig(
            symbol_pair="BTC/USD",
            duration_seconds=1200,
            entry_seconds_before_end=5,
            final_price_delay_seconds=2,
            threshold_usd=50.0,
            threshold_4s_usd=40.0,
            threshold_near_end_usd=30.0,
            stake_usd=100.0,
        )
    )

    assert summary.total_events == 1
    assert summary.skips == 1
    assert any("| skip | 0.00 | missing_price_to_beat |" in line for line in logs)
