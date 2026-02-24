from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PriceSource(str, Enum):
    """Price source identifier."""

    POLYMARKET = "polymarket_binance"
    CHAINLINK = "chainlink_stream"


@dataclass(frozen=True)
class EventMetadata:
    """Minimal Polymarket event metadata."""

    slug: str
    question: str
    description: str
    resolution_source: str


@dataclass(frozen=True)
class EventMarketInfo:
    """5-minute market metadata required by the strategy."""

    slug: str
    title: str
    start_timestamp_s: int
    end_timestamp_s: int
    up_token_id: str
    down_token_id: str
    price_to_beat: float | None = None


@dataclass(frozen=True)
class PriceTick:
    """Single RTDS price tick."""

    source: PriceSource
    symbol: str
    price: float
    source_timestamp_ms: int
    received_timestamp_ms: int


@dataclass(frozen=True)
class LagSnapshot:
    """Snapshot of current lag between sources."""

    observed_at_ms: int
    lagging_source: PriceSource | None
    lag_ms: int
    polymarket_price: float
    chainlink_price: float
    polymarket_source_timestamp_ms: int
    chainlink_source_timestamp_ms: int
    polymarket_age_ms: int
    chainlink_age_ms: int
    source_time_delta_ms: int


@dataclass(frozen=True)
class LagSummary:
    """Aggregated lag statistics."""

    sample_count: int
    polymarket_lag_count: int
    chainlink_lag_count: int
    tie_count: int
    average_lag_ms: float
    max_lag_ms: int
    max_lagging_source: PriceSource | None
