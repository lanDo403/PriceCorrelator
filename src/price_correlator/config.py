from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MonitorConfig:
    """Configuration for lag monitoring mode."""

    event_url: str | None = None
    symbol_pair: str = "BTC/USD"
    duration_seconds: int = 300
    report_interval_seconds: int = 5
    stale_threshold_ms: int = 500
    summary_json_path: Path | None = None
