from __future__ import annotations

from dataclasses import dataclass

from price_correlator.models import LagSnapshot, LagSummary, PriceSource, PriceTick


@dataclass
class _LagCounters:
    sample_count: int = 0
    polymarket_lag_count: int = 0
    chainlink_lag_count: int = 0
    tie_count: int = 0
    sum_lag_ms: int = 0
    max_lag_ms: int = 0
    max_lagging_source: PriceSource | None = None


class LagAnalyzer:
    """Computes and aggregates lag between two price sources."""

    def __init__(self) -> None:
        self._latest_ticks: dict[PriceSource, PriceTick] = {}
        self._counters = _LagCounters()

    def ingest(self, tick: PriceTick, observed_at_ms: int | None = None) -> LagSnapshot | None:
        self._latest_ticks[tick.source] = tick

        polymarket_tick = self._latest_ticks.get(PriceSource.POLYMARKET)
        chainlink_tick = self._latest_ticks.get(PriceSource.CHAINLINK)
        if polymarket_tick is None or chainlink_tick is None:
            return None

        observed = observed_at_ms if observed_at_ms is not None else tick.received_timestamp_ms
        polymarket_age = max(0, observed - polymarket_tick.source_timestamp_ms)
        chainlink_age = max(0, observed - chainlink_tick.source_timestamp_ms)

        lagging_source: PriceSource | None = None
        lag_ms = 0
        if polymarket_age > chainlink_age:
            lagging_source = PriceSource.POLYMARKET
            lag_ms = polymarket_age - chainlink_age
        elif chainlink_age > polymarket_age:
            lagging_source = PriceSource.CHAINLINK
            lag_ms = chainlink_age - polymarket_age

        snapshot = LagSnapshot(
            observed_at_ms=observed,
            lagging_source=lagging_source,
            lag_ms=lag_ms,
            polymarket_price=polymarket_tick.price,
            chainlink_price=chainlink_tick.price,
            polymarket_source_timestamp_ms=polymarket_tick.source_timestamp_ms,
            chainlink_source_timestamp_ms=chainlink_tick.source_timestamp_ms,
            polymarket_age_ms=polymarket_age,
            chainlink_age_ms=chainlink_age,
            source_time_delta_ms=polymarket_tick.source_timestamp_ms - chainlink_tick.source_timestamp_ms,
        )
        self._consume_snapshot(snapshot)
        return snapshot

    def summary(self) -> LagSummary:
        counters = self._counters
        average_lag_ms = counters.sum_lag_ms / counters.sample_count if counters.sample_count else 0.0
        return LagSummary(
            sample_count=counters.sample_count,
            polymarket_lag_count=counters.polymarket_lag_count,
            chainlink_lag_count=counters.chainlink_lag_count,
            tie_count=counters.tie_count,
            average_lag_ms=average_lag_ms,
            max_lag_ms=counters.max_lag_ms,
            max_lagging_source=counters.max_lagging_source,
        )

    def _consume_snapshot(self, snapshot: LagSnapshot) -> None:
        counters = self._counters
        counters.sample_count += 1
        counters.sum_lag_ms += snapshot.lag_ms

        if snapshot.lagging_source == PriceSource.POLYMARKET:
            counters.polymarket_lag_count += 1
        elif snapshot.lagging_source == PriceSource.CHAINLINK:
            counters.chainlink_lag_count += 1
        else:
            counters.tie_count += 1

        if snapshot.lag_ms > counters.max_lag_ms:
            counters.max_lag_ms = snapshot.lag_ms
            counters.max_lagging_source = snapshot.lagging_source
