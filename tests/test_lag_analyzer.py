from price_correlator.lag_analyzer import LagAnalyzer
from price_correlator.models import PriceSource, PriceTick


def _tick(source: PriceSource, source_ts: int, received_ts: int = 10_000) -> PriceTick:
    return PriceTick(
        source=source,
        symbol="btc",
        price=100_000.0,
        source_timestamp_ms=source_ts,
        received_timestamp_ms=received_ts,
    )


def test_ingest_returns_none_until_two_sources_present() -> None:
    analyzer = LagAnalyzer()
    snapshot = analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_000))
    assert snapshot is None


def test_detects_polymarket_lagging() -> None:
    analyzer = LagAnalyzer()
    analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=9_800))
    snapshot = analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_000), observed_at_ms=10_000)
    assert snapshot is not None
    assert snapshot.lagging_source == PriceSource.POLYMARKET
    assert snapshot.lag_ms == 800


def test_detects_chainlink_lagging() -> None:
    analyzer = LagAnalyzer()
    analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_900))
    snapshot = analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=9_100), observed_at_ms=10_000)
    assert snapshot is not None
    assert snapshot.lagging_source == PriceSource.CHAINLINK
    assert snapshot.lag_ms == 800


def test_detects_tie() -> None:
    analyzer = LagAnalyzer()
    analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_500))
    snapshot = analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=9_500), observed_at_ms=10_000)
    assert snapshot is not None
    assert snapshot.lagging_source is None
    assert snapshot.lag_ms == 0


def test_summary_aggregation() -> None:
    analyzer = LagAnalyzer()
    analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_900))
    analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=9_100), observed_at_ms=10_000)  # CL lag=800
    analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=9_200), observed_at_ms=10_000)  # CL lag=100
    analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=9_200), observed_at_ms=10_000)  # tie

    summary = analyzer.summary()
    assert summary.sample_count == 3
    assert summary.chainlink_lag_count == 2
    assert summary.polymarket_lag_count == 0
    assert summary.tie_count == 1
    assert summary.max_lag_ms == 800
    assert summary.max_lagging_source == PriceSource.CHAINLINK
    assert summary.average_lag_ms == 300


def test_age_is_clamped_to_zero_when_observed_before_source_timestamp() -> None:
    analyzer = LagAnalyzer()
    analyzer.ingest(_tick(PriceSource.POLYMARKET, source_ts=10_500))
    snapshot = analyzer.ingest(_tick(PriceSource.CHAINLINK, source_ts=10_600), observed_at_ms=10_000)

    assert snapshot is not None
    assert snapshot.polymarket_age_ms == 0
    assert snapshot.chainlink_age_ms == 0
    assert snapshot.lag_ms == 0
