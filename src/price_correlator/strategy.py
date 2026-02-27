from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from price_correlator.clob_client import ClobClient
from price_correlator.event_client import GammaEventsClient
from price_correlator.models import EventMarketInfo, PriceSource, PriceTick
from price_correlator.rtds_client import RtdsClient, build_subscribe_message

PRICE_TO_BEAT_REFRESH_SECONDS = 10
DEFAULT_RTDS_RECONNECT_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0)
DEFAULT_RTDS_MAX_SILENCE_SECONDS = 45.0
_BTC_UPDOWN_SLUG_RE = re.compile(r"^(btc-updown-(\d+)m)-(\d+)$")


@dataclass(frozen=True)
class StrategyConfig:
    symbol_pair: str = "BTC/USD"
    market_timeframe_minutes: int = 5
    duration_seconds: int = 3600
    entry_seconds_before_end: int = 5
    final_price_delay_seconds: int = 2
    threshold_30s_usd: float = 150.0
    threshold_usd: float = 50.0
    threshold_4s_usd: float = 40.0
    threshold_near_end_usd: float = 30.0
    stake_usd: float = 100.0

    def __post_init__(self) -> None:
        build_subscribe_message(self.symbol_pair)
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0.")
        if self.market_timeframe_minutes <= 0:
            raise ValueError("market_timeframe_minutes must be > 0.")
        event_duration_seconds = self.market_timeframe_minutes * 60
        if self.entry_seconds_before_end <= 0 or self.entry_seconds_before_end > event_duration_seconds:
            raise ValueError(
                f"entry_seconds_before_end must be in range [1, {event_duration_seconds}]."
            )
        if self.final_price_delay_seconds < 0:
            raise ValueError("final_price_delay_seconds must be >= 0.")
        if self.stake_usd <= 0:
            raise ValueError("stake_usd must be > 0.")
        if (
            self.threshold_30s_usd < 0
            or self.threshold_usd < 0
            or self.threshold_4s_usd < 0
            or self.threshold_near_end_usd < 0
        ):
            raise ValueError("threshold values must be >= 0.")
        if self.threshold_30s_usd < self.threshold_usd:
            raise ValueError("threshold_30s_usd must be >= threshold_usd.")
        if not (self.threshold_usd >= self.threshold_4s_usd >= self.threshold_near_end_usd):
            raise ValueError("threshold ladder must satisfy threshold_usd >= threshold_4s_usd >= threshold_near_end_usd.")


@dataclass(frozen=True)
class StrategySummary:
    total_events: int
    wins: int
    losses: int
    skips: int
    total_profit_usd: float


@dataclass(frozen=True)
class StrategyEventResult:
    event_slug: str
    end_timestamp_s: int
    result: str
    profit_usd: float
    stake_usd: float


@dataclass
class _EventState:
    market: EventMarketInfo
    price_to_beat: float | None = None
    entry_price: float | None = None
    entry_side: str | None = None
    entry_yes_price: float | None = None
    entry_stake_usd: float | None = None
    final_price: float | None = None
    entered: bool = False
    result: str = "pending"
    reason: str = ""
    profit_usd: float = 0.0
    next_price_refresh_at_monotonic: float = 0.0


class StrategyRunner:
    """Entry strategy for BTC Up/Down N-minute events during the final seconds."""

    def __init__(
        self,
        event_client: GammaEventsClient,
        rtds_client: RtdsClient,
        clob_client: ClobClient,
        logger: Callable[[str], None] = print,
        stake_provider: Callable[[EventMarketInfo], float] | None = None,
        on_event_closed: Callable[[StrategyEventResult], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now_seconds: Callable[[], int] = lambda: int(time.time()),
        reconnect_delays_seconds: tuple[float, ...] = DEFAULT_RTDS_RECONNECT_DELAYS_SECONDS,
        max_tick_silence_seconds: float = DEFAULT_RTDS_MAX_SILENCE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._event_client = event_client
        self._rtds_client = rtds_client
        self._clob_client = clob_client
        self._log = logger
        self._stake_provider = stake_provider
        self._on_event_closed = on_event_closed
        self._monotonic = monotonic
        self._now_seconds = now_seconds
        self._reconnect_delays_seconds = reconnect_delays_seconds or (1.0,)
        if max_tick_silence_seconds <= 0:
            raise ValueError("max_tick_silence_seconds must be > 0.")
        self._max_tick_silence_seconds = max_tick_silence_seconds
        self._sleep = sleep
        self._discover_latest_slug_fn: Callable[[int], Awaitable[str]] | None = None
        self._fetch_active_market_fn: Callable[[int, int], Awaitable[EventMarketInfo | None]] | None = None

    async def run(self, config: StrategyConfig) -> StrategySummary:
        self._log(format_strategy_table_header())

        # Add a short tail so final_price can be captured after event end.
        deadline = self._monotonic() + config.duration_seconds + config.final_price_delay_seconds + 1
        state = await self._build_initial_event_state(config=config)

        wins = 0
        losses = 0
        skips = 0
        total_events = 0
        total_profit = 0.0

        tick_stream = self._rtds_client.stream_ticks(config.symbol_pair).__aiter__()
        reconnect_attempt = 0
        last_tick_at_monotonic = self._monotonic()
        pending_tick_task: asyncio.Task | None = None
        try:
            while self._monotonic() < deadline:
                await self._maybe_refresh_price_to_beat(state)

                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break

                try:
                    if pending_tick_task is None:
                        pending_tick_task = asyncio.create_task(tick_stream.__anext__())
                    tick = await asyncio.wait_for(asyncio.shield(pending_tick_task), timeout=min(1.0, remaining))
                    pending_tick_task = None
                except asyncio.TimeoutError:
                    now = self._monotonic()
                    if now - last_tick_at_monotonic >= self._max_tick_silence_seconds:
                        if pending_tick_task is not None:
                            pending_tick_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await pending_tick_task
                            pending_tick_task = None
                        reconnect_attempt += 1
                        tick_stream = await self._reconnect_tick_stream(
                            symbol_pair=config.symbol_pair,
                            reconnect_attempt=reconnect_attempt,
                            reason=(
                                f"no ticks for {self._max_tick_silence_seconds:.1f}s"
                            ),
                            deadline=deadline,
                        )
                        if tick_stream is None:
                            break
                    continue
                except StopAsyncIteration:
                    pending_tick_task = None
                    break
                except Exception as exc:  # noqa: BLE001
                    pending_tick_task = None
                    reconnect_attempt += 1
                    tick_stream = await self._reconnect_tick_stream(
                        symbol_pair=config.symbol_pair,
                        reconnect_attempt=reconnect_attempt,
                        reason=str(exc),
                        deadline=deadline,
                    )
                    if tick_stream is None:
                        break
                    continue

                reconnect_attempt = 0
                last_tick_at_monotonic = self._monotonic()

                if tick.source != PriceSource.CHAINLINK:
                    continue

                self._process_tick(state=state, tick=tick, config=config)
                if state.final_price is None:
                    continue

                self._log(format_strategy_table_row(state))
                total_events += 1
                total_profit += state.profit_usd
                if state.result == "win":
                    wins += 1
                elif state.result == "lose":
                    losses += 1
                else:
                    skips += 1
                if self._on_event_closed is not None:
                    self._on_event_closed(
                        StrategyEventResult(
                            event_slug=state.market.slug,
                            end_timestamp_s=state.market.end_timestamp_s,
                            result=state.result,
                            profit_usd=state.profit_usd,
                            stake_usd=state.entry_stake_usd or 0.0,
                        )
                    )

                try:
                    state = await self._advance_to_next_event_state(previous_state=state, config=config)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"warning: stopping strategy, failed to switch event: {exc}")
                    break
        finally:
            if pending_tick_task is not None:
                pending_tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending_tick_task

        summary = StrategySummary(
            total_events=total_events,
            wins=wins,
            losses=losses,
            skips=skips,
            total_profit_usd=total_profit,
        )
        self._log(format_strategy_summary(summary))
        return summary

    async def _build_initial_event_state(self, config: StrategyConfig) -> _EventState:
        active_market = await self._try_fetch_active_market_info(config=config)
        if active_market is not None:
            state = _EventState(market=active_market, price_to_beat=active_market.price_to_beat)
            self._log(f"switch_event: init -> {state.market.slug} (source=active_market)")
            return state

        slug = await self._discover_latest_slug(config=config)
        state = await self._build_event_state(slug)
        self._log(f"switch_event: init -> {state.market.slug} (source=latest_discovery)")
        return state

    async def _advance_to_next_event_state(self, previous_state: _EventState, config: StrategyConfig) -> _EventState:
        active_market = await self._try_fetch_active_market_info(config=config)
        if active_market is not None and active_market.slug != previous_state.market.slug:
            state = _EventState(market=active_market, price_to_beat=active_market.price_to_beat)
            self._log(
                f"switch_event: {previous_state.market.slug} -> {state.market.slug} "
                "(source=active_market)"
            )
            return state

        event_duration_seconds = _market_duration_seconds(previous_state.market, config)
        predicted_slug = _build_next_slug(
            previous_slug=previous_state.market.slug,
            previous_end_timestamp_s=previous_state.market.end_timestamp_s,
            fallback_timeframe_minutes=config.market_timeframe_minutes,
            event_duration_seconds=event_duration_seconds,
        )
        try:
            state = await self._build_event_state(predicted_slug)
            self._log(
                f"switch_event: {previous_state.market.slug} -> {state.market.slug} "
                "(source=predicted_next_slug)"
            )
            return state
        except Exception as exc:
            self._log(
                f"warning: predicted next slug failed: slug={predicted_slug}, "
                f"error={type(exc).__name__}: {exc}"
            )

        discovered_slug = await self._discover_latest_slug(config=config)
        if discovered_slug == previous_state.market.slug:
            raise RuntimeError(f"Could not resolve next event after slug={previous_state.market.slug}.")
        state = await self._build_event_state(discovered_slug)
        self._log(
            f"switch_event: {previous_state.market.slug} -> {state.market.slug} "
            "(source=latest_discovery)"
        )
        return state

    async def _build_event_state(self, slug: str) -> _EventState:
        market = await self._event_client.fetch_event_market_info(slug)
        return _EventState(market=market, price_to_beat=market.price_to_beat)

    async def _discover_latest_slug(self, config: StrategyConfig) -> str:
        if self._discover_latest_slug_fn is None:
            self._discover_latest_slug_fn = self._resolve_discover_latest_slug_fn(self._event_client)
        return await self._discover_latest_slug_fn(config.market_timeframe_minutes)

    async def _try_fetch_active_market_info(self, config: StrategyConfig) -> EventMarketInfo | None:
        now_seconds = self._now_seconds()
        if self._fetch_active_market_fn is None:
            self._fetch_active_market_fn = self._resolve_fetch_active_market_fn(self._event_client)
        try:
            return await self._fetch_active_market_fn(config.market_timeframe_minutes, now_seconds)
        except Exception as exc:  # noqa: BLE001
            self._log(f"warning: active market fetch failed: {type(exc).__name__}: {exc}")
            return None

    def _resolve_discover_latest_slug_fn(
        self,
        event_client: GammaEventsClient,
    ) -> Callable[[int], Awaitable[str]]:
        generic_fetcher = getattr(event_client, "discover_latest_btc_updown_slug", None)
        if callable(generic_fetcher):
            async def _discover(timeframe_minutes: int) -> str:
                return await generic_fetcher(timeframe_minutes=timeframe_minutes)
            return _discover

        legacy_fetcher = getattr(event_client, "discover_latest_btc_updown_5m_slug", None)
        if callable(legacy_fetcher):
            async def _discover_legacy(timeframe_minutes: int) -> str:
                if timeframe_minutes != 5:
                    raise LookupError("Legacy event client supports only 5m timeframe discovery.")
                return await legacy_fetcher()
            return _discover_legacy

        raise LookupError("Event client has no discovery method.")

    def _resolve_fetch_active_market_fn(
        self,
        event_client: GammaEventsClient,
    ) -> Callable[[int, int], Awaitable[EventMarketInfo | None]]:
        generic_fetcher = getattr(event_client, "fetch_active_btc_updown_market_info", None)
        if callable(generic_fetcher):
            async def _fetch(timeframe_minutes: int, now_seconds: int) -> EventMarketInfo | None:
                market = await generic_fetcher(timeframe_minutes=timeframe_minutes, now_seconds=now_seconds)
                if not isinstance(market, EventMarketInfo):
                    raise TypeError("Active market fetch returned unexpected payload type.")
                return market
            return _fetch

        legacy_fetcher = getattr(event_client, "fetch_active_btc_updown_5m_market_info", None)
        if callable(legacy_fetcher):
            async def _fetch_legacy(timeframe_minutes: int, now_seconds: int) -> EventMarketInfo | None:
                if timeframe_minutes != 5:
                    return None
                market = await legacy_fetcher(now_seconds=now_seconds)
                if not isinstance(market, EventMarketInfo):
                    raise TypeError("Active market fetch returned unexpected payload type.")
                return market
            return _fetch_legacy

        async def _no_active(*_args, **_kwargs) -> EventMarketInfo | None:
            return None

        return _no_active

    async def _reconnect_tick_stream(
        self,
        symbol_pair: str,
        reconnect_attempt: int,
        reason: str,
        deadline: float,
    ):
        delay = self._resolve_reconnect_delay(reconnect_attempt)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return None

        bounded_delay = min(delay, max(0.0, remaining))
        self._log(
            f"warning: rtds reconnect scheduled in {bounded_delay:.1f}s "
            f"(attempt={reconnect_attempt}, reason={reason})"
        )
        if bounded_delay > 0:
            await self._sleep(bounded_delay)
        return self._rtds_client.stream_ticks(symbol_pair).__aiter__()

    def _resolve_reconnect_delay(self, reconnect_attempt: int) -> float:
        index = min(max(reconnect_attempt, 1) - 1, len(self._reconnect_delays_seconds) - 1)
        return self._reconnect_delays_seconds[index]

    async def _maybe_refresh_price_to_beat(self, state: _EventState) -> None:
        if state.price_to_beat is not None:
            return

        now = self._monotonic()
        if now < state.next_price_refresh_at_monotonic:
            return
        state.next_price_refresh_at_monotonic = now + PRICE_TO_BEAT_REFRESH_SECONDS

        try:
            refreshed_market = await self._event_client.fetch_event_market_info(state.market.slug)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"warning: price_to_beat refresh failed for slug={state.market.slug}: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        if refreshed_market.price_to_beat is None:
            return

        state.market = refreshed_market
        state.price_to_beat = refreshed_market.price_to_beat
        self._log(
            f"price_to_beat_refresh: slug={state.market.slug}, "
            f"price_to_beat={state.price_to_beat:.2f}"
        )

    def _process_tick(self, state: _EventState, tick: PriceTick, config: StrategyConfig) -> None:
        tick_timestamp_s = tick.source_timestamp_ms // 1000
        end_s = state.market.end_timestamp_s
        final_trigger_s = end_s + config.final_price_delay_seconds

        if not state.entered and tick_timestamp_s < end_s:
            self._try_open_entry(state=state, tick=tick, end_s=end_s, config=config)

        if not state.entered and tick_timestamp_s >= end_s and state.result == "pending":
            state.result = "skip"
            if not state.reason:
                state.reason = "entry_not_opened"

        if state.final_price is None and tick_timestamp_s >= final_trigger_s:
            self._finalize_event(state=state, final_price=tick.price, config=config)

    def _try_open_entry(self, state: _EventState, tick: PriceTick, end_s: int, config: StrategyConfig) -> None:
        remaining_s = end_s - (tick.source_timestamp_ms // 1000)
        threshold = _entry_threshold_for_remaining_seconds(remaining_seconds=remaining_s, config=config)
        if threshold is None:
            return
        if state.price_to_beat is None:
            state.reason = "missing_price_to_beat"
            return

        price_delta = tick.price - state.price_to_beat
        if price_delta >= threshold:
            side = "up"
            token_id = state.market.up_token_id
        elif price_delta <= -threshold:
            side = "down"
            token_id = state.market.down_token_id
        else:
            state.reason = "condition_not_met"
            return

        if not token_id:
            state.reason = f"missing_{side}_token_id"
            return

        try:
            best_ask = self._clob_client.get_best_ask(token_id)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"warning: clob price fetch failed: token_id={token_id}, "
                f"error={type(exc).__name__}: {exc}"
            )
            state.reason = f"clob_error:{type(exc).__name__}"
            return

        if best_ask is None:
            state.reason = "no_liquidity"
            return
        if best_ask <= 0 or best_ask >= 1:
            state.reason = "invalid_taker_price"
            return

        stake_usd = config.stake_usd
        if self._stake_provider is not None:
            try:
                stake_usd = float(self._stake_provider(state.market))
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"warning: stake provider failed for slug={state.market.slug}: "
                    f"{type(exc).__name__}: {exc}"
                )
                state.reason = f"stake_provider_error:{type(exc).__name__}"
                return
        if stake_usd <= 0:
            state.reason = "insufficient_stake"
            return

        state.entered = True
        state.entry_side = side
        state.entry_price = tick.price
        state.entry_yes_price = best_ask
        state.entry_stake_usd = stake_usd
        state.reason = "filled"

    @staticmethod
    def _finalize_event(state: _EventState, final_price: float, config: StrategyConfig) -> None:
        state.final_price = final_price
        if state.price_to_beat is None:
            state.result = "skip"
            state.reason = "missing_price_to_beat"
            state.profit_usd = 0.0
            return

        if not state.entered:
            if state.result == "pending":
                state.result = "skip"
                if not state.reason:
                    state.reason = "entry_not_opened"
            state.profit_usd = 0.0
            return

        assert state.entry_yes_price is not None
        stake_usd = state.entry_stake_usd if state.entry_stake_usd is not None else config.stake_usd
        shares = stake_usd / state.entry_yes_price
        is_up_win = state.final_price >= state.price_to_beat
        is_win = is_up_win if state.entry_side == "up" else not is_up_win
        payout = shares if is_win else 0.0
        state.profit_usd = payout - stake_usd
        state.result = "win" if is_win else "lose"
        if not state.reason:
            state.reason = "filled"


def _extract_slug_timestamp(slug: str) -> int | None:
    match = _BTC_UPDOWN_SLUG_RE.fullmatch(slug)
    if match is None:
        return None
    return int(match.group(3))


def _extract_slug_duration_seconds(slug: str) -> int | None:
    match = _BTC_UPDOWN_SLUG_RE.fullmatch(slug)
    if match is None:
        return None
    return int(match.group(2)) * 60


def _market_duration_seconds(market: EventMarketInfo, config: StrategyConfig) -> int:
    slug_duration_seconds = _extract_slug_duration_seconds(market.slug)
    if slug_duration_seconds is not None:
        return slug_duration_seconds

    observed = market.end_timestamp_s - market.start_timestamp_s
    if 0 < observed <= 86_400:
        return observed
    return config.market_timeframe_minutes * 60


def _build_next_slug(
    previous_slug: str,
    previous_end_timestamp_s: int,
    fallback_timeframe_minutes: int,
    event_duration_seconds: int,
) -> str:
    match = _BTC_UPDOWN_SLUG_RE.fullmatch(previous_slug)
    if match is not None:
        slug_prefix = match.group(1)
        slug_timestamp = int(match.group(3))
        return f"{slug_prefix}-{slug_timestamp + event_duration_seconds}"
    return f"btc-updown-{fallback_timeframe_minutes}m-{previous_end_timestamp_s + event_duration_seconds}"


def _entry_threshold_for_remaining_seconds(remaining_seconds: int, config: StrategyConfig) -> float | None:
    if remaining_seconds <= 0:
        return None
    if remaining_seconds <= 3:
        return config.threshold_near_end_usd
    if remaining_seconds == 4:
        return config.threshold_4s_usd
    if remaining_seconds <= config.entry_seconds_before_end:
        return config.threshold_usd
    if remaining_seconds <= 30:
        return config.threshold_30s_usd
    return None


def _format_utc(timestamp_s: int | None) -> str:
    if timestamp_s is None:
        return "-"
    dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def _format_profit(value: float) -> str:
    return f"{value:.2f}"


def format_strategy_table_header() -> str:
    return (
        "| event_slug | end_utc | price_to_beat | entry_price | final_price | side | "
        "entry_yes_price | result | profit_usd | reason |"
    )


def format_strategy_table_row(state: _EventState) -> str:
    return (
        f"| {state.market.slug}"
        f" | {_format_utc(state.market.end_timestamp_s)}"
        f" | {_format_price(state.price_to_beat)}"
        f" | {_format_price(state.entry_price)}"
        f" | {_format_price(state.final_price)}"
        f" | {state.entry_side or '-'}"
        f" | {_format_price(state.entry_yes_price)}"
        f" | {state.result}"
        f" | {_format_profit(state.profit_usd)}"
        f" | {state.reason or '-'} |"
    )


def format_strategy_summary(summary: StrategySummary) -> str:
    return (
        "summary: "
        f"events={summary.total_events}, "
        f"win={summary.wins}, "
        f"lose={summary.losses}, "
        f"skip={summary.skips}, "
        f"profit_usd={summary.total_profit_usd:.2f}"
    )

