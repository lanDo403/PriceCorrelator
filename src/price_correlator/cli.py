from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from price_correlator.clob_client import ClobClient
from price_correlator.event_client import GammaEventsClient
from price_correlator.models import EventMarketInfo
from price_correlator.rtds_client import RtdsClient
from price_correlator.strategy import StrategyConfig, StrategyEventResult, StrategyRunner, StrategySummary

_RESULT_TOTAL_RE = re.compile(
    r"^result_total(?:_cumulative)?(?:_running)?: "
    r"events=(?P<events>\d+), "
    r"win=(?P<wins>\d+), "
    r"lose=(?P<losses>\d+), "
    r"skip=(?P<skips>\d+), "
    r"profit_usd=(?P<profit>-?\d+(?:\.\d+)?)$"
)


class TeeLogger:
    """Write log messages to stdout and to a file."""

    def __init__(self, log_file_path: Path, append: bool = True, echo_to_console: bool = True) -> None:
        self._path = log_file_path
        self._echo_to_console = echo_to_console
        self._path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        self._file = self._path.open(mode, encoding="utf-8")

    def __call__(self, message: str) -> None:
        if self._echo_to_console:
            print(message)
        self._file.write(f"{message}\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    @property
    def path(self) -> Path:
        return self._path


class AlertLogger:
    """Write warning-level events to a dedicated alert file."""

    def __init__(self, base_logger: TeeLogger, alert_file_path: Path) -> None:
        self._base_logger = base_logger
        self._path = alert_file_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        self._base_logger(message)
        if not (message.startswith("warning:") or message.startswith("Warning:")):
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._file.write(f"{timestamp} | {message}\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    @property
    def path(self) -> Path:
        return self._path


class JsonlLogger:
    """Write structured JSON entries to a JSONL file."""

    def __init__(self, path: Path, append: bool = True) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        self._file = self._path.open(mode, encoding="utf-8")

    def write(self, payload: dict[str, object]) -> None:
        self._file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    @property
    def path(self) -> Path:
        return self._path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="price-correlator",
        description="Entry strategy for BTC Up/Down 5m/15m near event end.",
    )
    parser.add_argument(
        "--symbol-pair",
        default="BTC/USD",
        help="Trading pair in BASE/QUOTE format (default: BTC/USD).",
    )
    parser.add_argument("--duration-seconds", type=int, default=3600, help="Strategy runtime in seconds.")
    parser.add_argument(
        "--market-timeframe-minutes",
        type=int,
        choices=(5, 15),
        default=5,
        help="Polymarket BTC Up/Down market timeframe in minutes (5 or 15).",
    )
    parser.add_argument(
        "--run-both-timeframes",
        action="store_true",
        help="Run 5-minute and 15-minute strategies simultaneously.",
    )
    parser.add_argument(
        "--entry-seconds-before-end",
        type=int,
        default=5,
        help="Start checking entry conditions this many seconds before event end.",
    )
    parser.add_argument(
        "--final-price-delay-seconds",
        type=int,
        default=2,
        help="Delay in seconds after event end to capture final Chainlink price.",
    )
    parser.add_argument(
        "--price-threshold-30s-usd",
        type=float,
        default=150.0,
        help="Price distance threshold for 30..6 seconds remaining.",
    )
    parser.add_argument(
        "--price-threshold-usd",
        type=float,
        default=50.0,
        help="Price distance threshold for 5 seconds remaining.",
    )
    parser.add_argument(
        "--price-threshold-4s-usd",
        type=float,
        default=40.0,
        help="Price distance threshold for 4 seconds remaining.",
    )
    parser.add_argument(
        "--price-threshold-near-end-usd",
        type=float,
        default=30.0,
        help="Price distance threshold for 3/2/1 seconds remaining.",
    )
    parser.add_argument(
        "--stake-usd",
        type=float,
        default=100.0,
        help="Entry stake in USD (idealized no-fee model).",
    )
    parser.add_argument(
        "--initial-bankroll-usd",
        type=float,
        default=100.0,
        help="Initial shared bankroll for --run-both-timeframes mode.",
    )
    parser.add_argument(
        "--log-file-path",
        type=Path,
        default=Path("logs/price_correlator.log"),
        help="Path to log file.",
    )
    parser.add_argument(
        "--log-file-path-5m",
        type=Path,
        default=Path("logs/strategy_test_5.log"),
        help="Log path for 5-minute strategy when --run-both-timeframes is enabled.",
    )
    parser.add_argument(
        "--log-file-path-15m",
        type=Path,
        default=Path("logs/strategy_test_15.log"),
        help="Log path for 15-minute strategy when --run-both-timeframes is enabled.",
    )
    parser.add_argument(
        "--result-log-file-path",
        type=Path,
        default=Path("logs/strategy_test_result.log"),
        help="Combined result log path when --run-both-timeframes is enabled.",
    )
    parser.add_argument(
        "--result-jsonl-file-path",
        type=Path,
        default=Path("logs/strategy_test_result.jsonl"),
        help="Structured JSONL result log path when --run-both-timeframes is enabled.",
    )
    parser.add_argument(
        "--log-jsonl-file-path-5m",
        type=Path,
        default=Path("logs/strategy_test_5.jsonl"),
        help="Structured JSONL log path for 5-minute strategy in dual-timeframe mode.",
    )
    parser.add_argument(
        "--log-jsonl-file-path-15m",
        type=Path,
        default=Path("logs/strategy_test_15.jsonl"),
        help="Structured JSONL log path for 15-minute strategy in dual-timeframe mode.",
    )
    parser.add_argument(
        "--alerts-file-path",
        type=Path,
        default=Path("logs/alerts.log"),
        help="Path to alerts log file (warning-level events).",
    )
    parser.add_argument(
        "--disable-alerts",
        action="store_true",
        help="Disable writing warning-level events to alerts file.",
    )
    parser.add_argument(
        "--no-console-output",
        action="store_true",
        help="Disable log echo to stdout (file logging only).",
    )
    return parser


def _build_config(args: argparse.Namespace, timeframe_minutes: int) -> StrategyConfig:
    return StrategyConfig(
        symbol_pair=args.symbol_pair,
        market_timeframe_minutes=timeframe_minutes,
        duration_seconds=args.duration_seconds,
        entry_seconds_before_end=args.entry_seconds_before_end,
        final_price_delay_seconds=args.final_price_delay_seconds,
        threshold_30s_usd=args.price_threshold_30s_usd,
        threshold_usd=args.price_threshold_usd,
        threshold_4s_usd=args.price_threshold_4s_usd,
        threshold_near_end_usd=args.price_threshold_near_end_usd,
        stake_usd=args.stake_usd,
    )


def _build_logger(
    log_path: Path,
    alerts_enabled: bool,
    alerts_file_path: Path,
    append: bool,
    echo_to_console: bool,
) -> tuple[Callable[[str], None], TeeLogger, AlertLogger | None]:
    base_logger = TeeLogger(log_path, append=append, echo_to_console=echo_to_console)
    alert_logger = AlertLogger(base_logger, alerts_file_path) if alerts_enabled else None
    logger = alert_logger or base_logger
    logger(f"log_file: {base_logger.path}")
    return logger, base_logger, alert_logger


def _parse_result_total_line(line: str) -> StrategySummary | None:
    match = _RESULT_TOTAL_RE.fullmatch(line.strip())
    if match is None:
        return None
    return StrategySummary(
        total_events=int(match.group("events")),
        wins=int(match.group("wins")),
        losses=int(match.group("losses")),
        skips=int(match.group("skips")),
        total_profit_usd=float(match.group("profit")),
    )


def _empty_summary() -> StrategySummary:
    return StrategySummary(total_events=0, wins=0, losses=0, skips=0, total_profit_usd=0.0)


def _load_previous_cumulative_summary(result_log_path: Path) -> StrategySummary:
    if not result_log_path.exists():
        return _empty_summary()

    lines = result_log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    last_total: StrategySummary | None = None
    for line in reversed(lines):
        if line.startswith("result_total_cumulative:"):
            parsed = _parse_result_total_line(line)
            if parsed is not None:
                return parsed
            continue
        if not line.startswith("result_total:"):
            continue
        parsed = _parse_result_total_line(line)
        if parsed is not None and last_total is None:
            last_total = parsed

    return last_total or _empty_summary()


def _merge_summaries(base: StrategySummary, delta: StrategySummary) -> StrategySummary:
    return StrategySummary(
        total_events=base.total_events + delta.total_events,
        wins=base.wins + delta.wins,
        losses=base.losses + delta.losses,
        skips=base.skips + delta.skips,
        total_profit_usd=base.total_profit_usd + delta.total_profit_usd,
    )


def _summary_from_event_result(event: StrategyEventResult) -> StrategySummary:
    wins = 1 if event.result == "win" else 0
    losses = 1 if event.result == "lose" else 0
    skips = 1 if event.result not in {"win", "lose"} else 0
    return StrategySummary(
        total_events=1,
        wins=wins,
        losses=losses,
        skips=skips,
        total_profit_usd=event.profit_usd,
    )


async def _run_strategy_for_timeframe(
    args: argparse.Namespace,
    timeframe_minutes: int,
    logger: Callable[[str], None],
    stake_provider: Callable[[EventMarketInfo], float] | None = None,
    on_event_closed: Callable[[StrategyEventResult], None] | None = None,
) -> StrategySummary:
    config = _build_config(args, timeframe_minutes)
    async with GammaEventsClient(logger=logger) as event_client:
        runner = StrategyRunner(
            event_client=event_client,
            rtds_client=RtdsClient(),
            clob_client=ClobClient(),
            logger=logger,
            stake_provider=stake_provider,
            on_event_closed=on_event_closed,
        )
        return await runner.run(config)


def _compute_even_tradable_bankroll(bankroll_usd: float) -> int:
    integer_bankroll = max(0, int(bankroll_usd))
    if integer_bankroll % 2 == 1:
        integer_bankroll -= 1
    return integer_bankroll


def _compute_per_market_stake(bankroll_usd: float) -> float:
    return float(_compute_even_tradable_bankroll(bankroll_usd) // 2)


def _build_timeframe_strategy_logger(
    logger: Callable[[str], None],
    jsonl_logger: JsonlLogger,
    timeframe_minutes: int,
) -> Callable[[str], None]:
    """Filter verbose strategy internals; keep warnings in per-timeframe logs."""

    def _log(message: str) -> None:
        if message.startswith("warning:") or message.startswith("Warning:"):
            logger(message)
            jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "warning",
                    "timeframe": timeframe_minutes,
                    "message": message,
                }
            )
            return
        if message.startswith("| ") or message.startswith("summary:"):
            return
        if message.startswith("switch_event:") or message.startswith("price_to_beat_refresh:"):
            return
        logger(message)
        jsonl_logger.write(
            {
                "ts_utc": _utc_now_iso(),
                "type": "log",
                "timeframe": timeframe_minutes,
                "message": message,
            }
        )

    return _log


async def _run_both_timeframes(args: argparse.Namespace) -> int:
    alerts_enabled = not args.disable_alerts
    echo_to_console = not args.no_console_output
    logger_5m, base_5m, alert_5m = _build_logger(
        args.log_file_path_5m,
        alerts_enabled,
        args.alerts_file_path,
        append=False,
        echo_to_console=echo_to_console,
    )
    logger_15m, base_15m, alert_15m = _build_logger(
        args.log_file_path_15m,
        alerts_enabled,
        args.alerts_file_path,
        append=False,
        echo_to_console=echo_to_console,
    )
    result_logger, base_result, alert_result = _build_logger(
        args.result_log_file_path,
        alerts_enabled,
        args.alerts_file_path,
        append=True,
        echo_to_console=echo_to_console,
    )
    jsonl_logger_5m = JsonlLogger(args.log_jsonl_file_path_5m, append=False)
    jsonl_logger_15m = JsonlLogger(args.log_jsonl_file_path_15m, append=False)
    jsonl_logger = JsonlLogger(args.result_jsonl_file_path, append=True)
    previous_cumulative = _load_previous_cumulative_summary(args.result_log_file_path)
    bankroll_usd = float(args.initial_bankroll_usd)
    running_5m = _empty_summary()
    running_15m = _empty_summary()
    result_logger("mode: run_both_timeframes")
    if alerts_enabled:
        result_logger(f"alerts_file: {args.alerts_file_path}")
    result_logger(f"result_jsonl_file: {jsonl_logger.path}")
    logger_5m("mode: timeframe=5m")
    logger_15m("mode: timeframe=15m")
    jsonl_logger_5m.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "run_start",
            "timeframe": 5,
            "log_file": str(args.log_file_path_5m),
            "jsonl_file": str(args.log_jsonl_file_path_5m),
        }
    )
    jsonl_logger_15m.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "run_start",
            "timeframe": 15,
            "log_file": str(args.log_file_path_15m),
            "jsonl_file": str(args.log_jsonl_file_path_15m),
        }
    )
    jsonl_logger.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "run_start",
            "mode": "run_both_timeframes",
            "alerts_enabled": alerts_enabled,
            "alerts_file": str(args.alerts_file_path),
            "result_log_file": str(args.result_log_file_path),
            "result_jsonl_file": str(jsonl_logger.path),
        }
    )
    result_logger(
        "bankroll_start: "
        f"bankroll_usd={bankroll_usd:.2f}, "
        f"tradable_even_usd={_compute_even_tradable_bankroll(bankroll_usd)}, "
        f"stake_per_market_usd={_compute_per_market_stake(bankroll_usd):.2f}"
    )
    logger_5m(
        "bankroll_start: "
        f"bankroll_usd={bankroll_usd:.2f}, "
        f"tradable_even_usd={_compute_even_tradable_bankroll(bankroll_usd)}, "
        f"stake_per_market_usd={_compute_per_market_stake(bankroll_usd):.2f}"
    )
    logger_15m(
        "bankroll_start: "
        f"bankroll_usd={bankroll_usd:.2f}, "
        f"tradable_even_usd={_compute_even_tradable_bankroll(bankroll_usd)}, "
        f"stake_per_market_usd={_compute_per_market_stake(bankroll_usd):.2f}"
    )
    jsonl_logger_5m.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "bankroll_start",
            "timeframe": 5,
            "bankroll_usd": bankroll_usd,
            "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
            "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
        }
    )
    jsonl_logger_15m.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "bankroll_start",
            "timeframe": 15,
            "bankroll_usd": bankroll_usd,
            "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
            "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
        }
    )
    jsonl_logger.write(
        {
            "ts_utc": _utc_now_iso(),
            "type": "bankroll_start",
            "bankroll_usd": bankroll_usd,
            "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
            "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
        }
    )

    def _shared_stake_provider(_market: EventMarketInfo) -> float:
        return _compute_per_market_stake(bankroll_usd)

    def _log_running_totals() -> None:
        running_total = _merge_summaries(running_5m, running_15m)
        cumulative_running = _merge_summaries(previous_cumulative, running_total)
        result_logger(
            "result_total_running: "
            f"events={running_total.total_events}, "
            f"win={running_total.wins}, "
            f"lose={running_total.losses}, "
            f"skip={running_total.skips}, "
            f"profit_usd={running_total.total_profit_usd:.2f}"
        )
        result_logger(
            "result_total_cumulative_running: "
            f"events={cumulative_running.total_events}, "
            f"win={cumulative_running.wins}, "
            f"lose={cumulative_running.losses}, "
            f"skip={cumulative_running.skips}, "
            f"profit_usd={cumulative_running.total_profit_usd:.2f}"
        )
        jsonl_logger.write(
            {
                "ts_utc": _utc_now_iso(),
                "type": "result_total_running",
                "events": running_total.total_events,
                "win": running_total.wins,
                "lose": running_total.losses,
                "skip": running_total.skips,
                "profit_usd": running_total.total_profit_usd,
                "events_cumulative": cumulative_running.total_events,
                "win_cumulative": cumulative_running.wins,
                "lose_cumulative": cumulative_running.losses,
                "skip_cumulative": cumulative_running.skips,
                "profit_usd_cumulative": cumulative_running.total_profit_usd,
            }
        )

    def _build_event_handler(timeframe_minutes: int) -> Callable[[StrategyEventResult], None]:
        def _handle_event(event: StrategyEventResult) -> None:
            nonlocal bankroll_usd, running_5m, running_15m
            timeframe_logger = logger_5m if timeframe_minutes == 5 else logger_15m
            timeframe_jsonl_logger = jsonl_logger_5m if timeframe_minutes == 5 else jsonl_logger_15m
            delta = _summary_from_event_result(event)
            if timeframe_minutes == 5:
                running_5m = _merge_summaries(running_5m, delta)
                timeframe_running = running_5m
            else:
                running_15m = _merge_summaries(running_15m, delta)
                timeframe_running = running_15m
            bankroll_usd += event.profit_usd
            result_event_line = (
                "result_event: "
                f"timeframe={timeframe_minutes}m, "
                f"slug={event.event_slug}, "
                f"result={event.result}, "
                f"stake_usd={event.stake_usd:.2f}, "
                f"fee_usd={event.fee_usd:.4f}, "
                f"profit_usd={event.profit_usd:.2f}"
            )
            result_logger(result_event_line)
            timeframe_logger(result_event_line)
            timeframe_jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result_event",
                    "timeframe": timeframe_minutes,
                    "event_slug": event.event_slug,
                    "event_end_timestamp_s": event.end_timestamp_s,
                    "result": event.result,
                    "stake_usd": event.stake_usd,
                    "fee_usd": event.fee_usd,
                    "profit_usd": event.profit_usd,
                }
            )
            jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result_event",
                    "timeframe": timeframe_minutes,
                    "event_slug": event.event_slug,
                    "event_end_timestamp_s": event.end_timestamp_s,
                    "result": event.result,
                    "stake_usd": event.stake_usd,
                    "fee_usd": event.fee_usd,
                    "profit_usd": event.profit_usd,
                }
            )
            bankroll_update_line = (
                "bankroll_update: "
                f"timeframe={timeframe_minutes}m, "
                f"slug={event.event_slug}, "
                f"bankroll_usd={bankroll_usd:.2f}, "
                f"tradable_even_usd={_compute_even_tradable_bankroll(bankroll_usd)}, "
                f"stake_per_market_usd={_compute_per_market_stake(bankroll_usd):.2f}"
            )
            result_logger(bankroll_update_line)
            timeframe_logger(bankroll_update_line)
            timeframe_jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "bankroll_update",
                    "timeframe": timeframe_minutes,
                    "event_slug": event.event_slug,
                    "bankroll_usd": bankroll_usd,
                    "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
                    "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
                }
            )
            jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "bankroll_update",
                    "timeframe": timeframe_minutes,
                    "event_slug": event.event_slug,
                    "bankroll_usd": bankroll_usd,
                    "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
                    "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
                }
            )
            result_running_line = (
                "result_running: "
                f"timeframe={timeframe_minutes}m, "
                f"events={timeframe_running.total_events}, "
                f"win={timeframe_running.wins}, "
                f"lose={timeframe_running.losses}, "
                f"skip={timeframe_running.skips}, "
                f"profit_usd={timeframe_running.total_profit_usd:.2f}"
            )
            result_logger(result_running_line)
            timeframe_logger(result_running_line)
            timeframe_jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result_running",
                    "timeframe": timeframe_minutes,
                    "events": timeframe_running.total_events,
                    "win": timeframe_running.wins,
                    "lose": timeframe_running.losses,
                    "skip": timeframe_running.skips,
                    "profit_usd": timeframe_running.total_profit_usd,
                }
            )
            jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result_running",
                    "timeframe": timeframe_minutes,
                    "events": timeframe_running.total_events,
                    "win": timeframe_running.wins,
                    "lose": timeframe_running.losses,
                    "skip": timeframe_running.skips,
                    "profit_usd": timeframe_running.total_profit_usd,
                }
            )
            _log_running_totals()

        return _handle_event

    exit_code = 0
    try:
        strategy_logger_5m = _build_timeframe_strategy_logger(logger_5m, jsonl_logger_5m, timeframe_minutes=5)
        strategy_logger_15m = _build_timeframe_strategy_logger(logger_15m, jsonl_logger_15m, timeframe_minutes=15)
        run_5m = asyncio.create_task(
            _run_strategy_for_timeframe(
                args=args,
                timeframe_minutes=5,
                logger=strategy_logger_5m,
                stake_provider=_shared_stake_provider,
                on_event_closed=_build_event_handler(5),
            )
        )
        run_15m = asyncio.create_task(
            _run_strategy_for_timeframe(
                args=args,
                timeframe_minutes=15,
                logger=strategy_logger_15m,
                stake_provider=_shared_stake_provider,
                on_event_closed=_build_event_handler(15),
            )
        )
        results = await asyncio.gather(run_5m, run_15m, return_exceptions=True)

        total_events = 0
        total_wins = 0
        total_losses = 0
        total_skips = 0
        total_profit = 0.0
        for timeframe, outcome in ((5, results[0]), (15, results[1])):
            if isinstance(outcome, Exception):
                exit_code = 1
                warning_line = f"warning: timeframe={timeframe}m failed: {type(outcome).__name__}: {outcome}"
                result_logger(warning_line)
                (logger_5m if timeframe == 5 else logger_15m)(warning_line)
                (jsonl_logger_5m if timeframe == 5 else jsonl_logger_15m).write(
                    {
                        "ts_utc": _utc_now_iso(),
                        "type": "timeframe_failed",
                        "timeframe": timeframe,
                        "error_type": type(outcome).__name__,
                        "error": str(outcome),
                    }
                )
                jsonl_logger.write(
                    {
                        "ts_utc": _utc_now_iso(),
                        "type": "timeframe_failed",
                        "timeframe": timeframe,
                        "error_type": type(outcome).__name__,
                        "error": str(outcome),
                    }
                )
                continue

            result_line = (
                "result: "
                f"timeframe={timeframe}m, "
                f"events={outcome.total_events}, "
                f"win={outcome.wins}, "
                f"lose={outcome.losses}, "
                f"skip={outcome.skips}, "
                f"profit_usd={outcome.total_profit_usd:.2f}"
            )
            result_logger(result_line)
            (logger_5m if timeframe == 5 else logger_15m)(result_line)
            (jsonl_logger_5m if timeframe == 5 else jsonl_logger_15m).write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result",
                    "timeframe": timeframe,
                    "events": outcome.total_events,
                    "win": outcome.wins,
                    "lose": outcome.losses,
                    "skip": outcome.skips,
                    "profit_usd": outcome.total_profit_usd,
                }
            )
            jsonl_logger.write(
                {
                    "ts_utc": _utc_now_iso(),
                    "type": "result",
                    "timeframe": timeframe,
                    "events": outcome.total_events,
                    "win": outcome.wins,
                    "lose": outcome.losses,
                    "skip": outcome.skips,
                    "profit_usd": outcome.total_profit_usd,
                }
            )
            total_events += outcome.total_events
            total_wins += outcome.wins
            total_losses += outcome.losses
            total_skips += outcome.skips
            total_profit += outcome.total_profit_usd

        run_total_summary = StrategySummary(
            total_events=total_events,
            wins=total_wins,
            losses=total_losses,
            skips=total_skips,
            total_profit_usd=total_profit,
        )
        result_logger(
            "result_total: "
            f"events={run_total_summary.total_events}, "
            f"win={run_total_summary.wins}, "
            f"lose={run_total_summary.losses}, "
            f"skip={run_total_summary.skips}, "
            f"profit_usd={run_total_summary.total_profit_usd:.2f}"
        )
        jsonl_logger.write(
            {
                "ts_utc": _utc_now_iso(),
                "type": "result_total",
                "events": run_total_summary.total_events,
                "win": run_total_summary.wins,
                "lose": run_total_summary.losses,
                "skip": run_total_summary.skips,
                "profit_usd": run_total_summary.total_profit_usd,
            }
        )
        cumulative = _merge_summaries(previous_cumulative, run_total_summary)
        result_logger(
            "result_total_cumulative: "
            f"events={cumulative.total_events}, "
            f"win={cumulative.wins}, "
            f"lose={cumulative.losses}, "
            f"skip={cumulative.skips}, "
            f"profit_usd={cumulative.total_profit_usd:.2f}"
        )
        jsonl_logger.write(
            {
                "ts_utc": _utc_now_iso(),
                "type": "result_total_cumulative",
                "events": cumulative.total_events,
                "win": cumulative.wins,
                "lose": cumulative.losses,
                "skip": cumulative.skips,
                "profit_usd": cumulative.total_profit_usd,
            }
        )
        result_logger(
            "bankroll_final: "
            f"bankroll_usd={bankroll_usd:.2f}, "
            f"tradable_even_usd={_compute_even_tradable_bankroll(bankroll_usd)}, "
            f"stake_per_market_usd={_compute_per_market_stake(bankroll_usd):.2f}"
        )
        jsonl_logger.write(
            {
                "ts_utc": _utc_now_iso(),
                "type": "bankroll_final",
                "bankroll_usd": bankroll_usd,
                "tradable_even_usd": _compute_even_tradable_bankroll(bankroll_usd),
                "stake_per_market_usd": _compute_per_market_stake(bankroll_usd),
                "exit_code": exit_code,
            }
        )
    finally:
        jsonl_logger_5m.close()
        jsonl_logger_15m.close()
        jsonl_logger.close()
        if alert_5m is not None:
            alert_5m.close()
        if alert_15m is not None:
            alert_15m.close()
        if alert_result is not None:
            alert_result.close()
        base_5m.close()
        base_15m.close()
        base_result.close()

    return exit_code


async def run_from_args(args: argparse.Namespace) -> int:
    if args.run_both_timeframes:
        return await _run_both_timeframes(args)

    config = _build_config(args, args.market_timeframe_minutes)
    logger, base_logger, alert_logger = _build_logger(
        args.log_file_path,
        alerts_enabled=not args.disable_alerts,
        alerts_file_path=args.alerts_file_path,
        append=True,
        echo_to_console=not args.no_console_output,
    )
    if alert_logger is not None:
        logger(f"alerts_file: {alert_logger.path}")
    exit_code = 0
    try:
        async with GammaEventsClient(logger=logger) as event_client:
            runner = StrategyRunner(
                event_client=event_client,
                rtds_client=RtdsClient(),
                clob_client=ClobClient(),
                logger=logger,
            )
            await runner.run(config)
    except Exception as exc:  # noqa: BLE001
        logger(f"warning: fatal strategy error: {type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        if alert_logger is not None:
            alert_logger.close()
        base_logger.close()
    return exit_code


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_from_args(args)))


if __name__ == "__main__":
    main()
