from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from price_correlator.config import MonitorConfig
from price_correlator.event_client import GammaEventsClient, parse_event_slug
from price_correlator.lag_analyzer import LagAnalyzer
from price_correlator.models import LagSnapshot, LagSummary, PriceSource
from price_correlator.rtds_client import RtdsClient


class MonitorService:
    """Coordinates clients and prints lag monitoring results."""

    def __init__(
        self,
        event_client: GammaEventsClient,
        rtds_client: RtdsClient,
        lag_analyzer: LagAnalyzer,
        logger: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_client = event_client
        self._rtds_client = rtds_client
        self._lag_analyzer = lag_analyzer
        self._log = logger
        self._monotonic = monotonic

    async def run(self, config: MonitorConfig) -> LagSummary:
        slug = await self._resolve_event_slug(config)

        try:
            metadata = await self._event_client.fetch_event_by_slug(slug)
            self._log(f"Event: {metadata.question or metadata.slug}")
            if metadata.resolution_source:
                self._log(f"Resolution source: {metadata.resolution_source}")
        except Exception as exc:  # noqa: BLE001
            self._log(
                "Warning: failed to fetch event metadata "
                f"(slug={slug}): {exc}. Continuing with RTDS only."
            )

        started = self._monotonic()
        deadline = started + config.duration_seconds
        next_report_at = started
        per_source_ticks = {
            PriceSource.POLYMARKET: 0,
            PriceSource.CHAINLINK: 0,
        }
        table_header_printed = False

        tick_stream = self._rtds_client.stream_ticks(config.symbol_pair).__aiter__()
        pending_tick_task: asyncio.Task | None = None
        try:
            while True:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break

                try:
                    if pending_tick_task is None:
                        pending_tick_task = asyncio.create_task(tick_stream.__anext__())
                    tick = await asyncio.wait_for(
                        asyncio.shield(pending_tick_task),
                        timeout=min(1.0, remaining),
                    )
                    pending_tick_task = None
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break

                per_source_ticks[tick.source] += 1
                snapshot = self._lag_analyzer.ingest(tick)
                if snapshot is None:
                    continue

                now_monotonic = self._monotonic()
                need_periodic_report = now_monotonic >= next_report_at
                need_threshold_report = snapshot.lag_ms >= config.stale_threshold_ms
                if not (need_periodic_report or need_threshold_report):
                    continue

                if not table_header_printed:
                    self._log(format_snapshot_table_header())
                    table_header_printed = True
                self._log(format_snapshot_table_row(snapshot))
                next_report_at = now_monotonic + config.report_interval_seconds
        except Exception as exc:  # noqa: BLE001
            self._log(f"Warning: failed to connect to RTDS or stream interrupted: {exc}")
        finally:
            if pending_tick_task is not None:
                pending_tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending_tick_task

        summary = self._lag_analyzer.summary()
        if summary.sample_count == 0:
            self._log(
                "Diagnostics: failed to collect tick pairs. "
                f"Polymarket ticks={per_source_ticks[PriceSource.POLYMARKET]}, "
                f"Chainlink ticks={per_source_ticks[PriceSource.CHAINLINK]}."
            )
        self._log(format_summary(summary))
        if config.summary_json_path is not None:
            write_summary_json(config.summary_json_path, summary)
            self._log(f"JSON report saved: {config.summary_json_path}")
        return summary

    async def _resolve_event_slug(self, config: MonitorConfig) -> str:
        if config.event_url and config.event_url.lower() != "auto":
            return parse_event_slug(config.event_url)

        slug = await self._event_client.discover_latest_btc_updown_5m_slug()
        self._log(f"Auto-selected event: https://polymarket.com/ru/event/{slug}")
        return slug


def format_timestamp_ms_utc(timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


def format_snapshot_table_header() -> str:
    return (
        "| observed_utc | polymarket_utc | chainlink_utc | pm_price | cl_price | lagging | "
        "lag_ms | age_pm_ms | age_cl_ms |"
    )


def format_snapshot_table_row(snapshot: LagSnapshot) -> str:
    lag_source = "tie"
    if snapshot.lagging_source == PriceSource.POLYMARKET:
        lag_source = "polymarket"
    elif snapshot.lagging_source == PriceSource.CHAINLINK:
        lag_source = "chainlink"

    return (
        f"| {format_timestamp_ms_utc(snapshot.observed_at_ms)}"
        f" | {format_timestamp_ms_utc(snapshot.polymarket_source_timestamp_ms)}"
        f" | {format_timestamp_ms_utc(snapshot.chainlink_source_timestamp_ms)}"
        f" | {snapshot.polymarket_price:.6f}"
        f" | {snapshot.chainlink_price:.6f}"
        f" | {lag_source}"
        f" | {snapshot.lag_ms}"
        f" | {snapshot.polymarket_age_ms}"
        f" | {snapshot.chainlink_age_ms} |"
    )


def format_summary(summary: LagSummary) -> str:
    if summary.sample_count == 0:
        return "Summary: insufficient data, could not collect tick pairs."

    max_source = "none"
    if summary.max_lagging_source == PriceSource.POLYMARKET:
        max_source = "Polymarket"
    elif summary.max_lagging_source == PriceSource.CHAINLINK:
        max_source = "Chainlink"

    return (
        "Summary: "
        f"samples={summary.sample_count}, "
        f"avg_lag={summary.average_lag_ms:.1f}ms ({summary.average_lag_ms / 1000:.3f}s), "
        f"max_lag={summary.max_lag_ms}ms ({summary.max_lag_ms / 60000:.3f}m), "
        f"pm_lag={summary.polymarket_lag_count}, "
        f"cl_lag={summary.chainlink_lag_count}, "
        f"ties={summary.tie_count}, "
        f"max_lagging_source={max_source}"
    )


def write_summary_json(path: Path, summary: LagSummary) -> None:
    serializable = asdict(summary)
    serializable["max_lagging_source"] = (
        summary.max_lagging_source.value if summary.max_lagging_source is not None else None
    )
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
