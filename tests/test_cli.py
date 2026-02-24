from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import price_correlator.cli as cli_module
from price_correlator.cli import AlertLogger, TeeLogger, build_parser
from price_correlator.strategy import StrategyEventResult, StrategySummary


def test_build_parser_alert_options_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.price_threshold_30s_usd == 150.0
    assert args.no_console_output is False
    assert args.market_timeframe_minutes == 5
    assert args.run_both_timeframes is False
    assert args.log_file_path == Path("logs/price_correlator.log")
    assert args.log_file_path_5m == Path("logs/strategy_test_5.log")
    assert args.log_file_path_15m == Path("logs/strategy_test_15.log")
    assert args.result_log_file_path == Path("logs/strategy_test_result.log")
    assert args.alerts_file_path == Path("logs/alerts.log")
    assert args.disable_alerts is False


def test_build_parser_accepts_15m_timeframe() -> None:
    parser = build_parser()
    args = parser.parse_args(["--market-timeframe-minutes", "15"])
    assert args.market_timeframe_minutes == 15


def test_build_parser_accepts_run_both_timeframes_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["--run-both-timeframes"])
    assert args.run_both_timeframes is True


def test_alert_logger_writes_only_warning_messages() -> None:
    test_dir = Path("logs") / f"test_cli_{uuid4().hex}"
    test_dir.mkdir(parents=True, exist_ok=True)
    main_log_path = test_dir / "main.log"
    alert_log_path = test_dir / "alerts.log"

    base_logger = TeeLogger(main_log_path)
    alert_logger = AlertLogger(base_logger, alert_log_path)
    try:
        alert_logger("hello")
        alert_logger("warning: stream interrupted")
        alert_logger("Warning: metadata fetch failed")
    finally:
        alert_logger.close()
        base_logger.close()

    main_lines = main_log_path.read_text(encoding="utf-8").strip().splitlines()
    alert_lines = alert_log_path.read_text(encoding="utf-8").strip().splitlines()

    assert main_lines == [
        "hello",
        "warning: stream interrupted",
        "Warning: metadata fetch failed",
    ]
    assert len(alert_lines) == 2
    assert "warning: stream interrupted" in alert_lines[0]
    assert "Warning: metadata fetch failed" in alert_lines[1]
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_run_from_args_returns_nonzero_and_logs_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_dir = Path("logs") / f"test_cli_{uuid4().hex}"
    test_dir.mkdir(parents=True, exist_ok=True)
    main_log_path = test_dir / "main.log"
    alerts_log_path = test_dir / "alerts.log"

    class _FailingRunner:
        def __init__(self, **kwargs) -> None:
            pass

        async def run(self, config) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "StrategyRunner", _FailingRunner)

    args = SimpleNamespace(
        symbol_pair="BTC/USD",
        market_timeframe_minutes=5,
        run_both_timeframes=False,
        duration_seconds=60,
        entry_seconds_before_end=5,
        final_price_delay_seconds=2,
        price_threshold_30s_usd=150.0,
        price_threshold_usd=50.0,
        price_threshold_4s_usd=40.0,
        price_threshold_near_end_usd=30.0,
        stake_usd=100.0,
        log_file_path=main_log_path,
        log_file_path_5m=test_dir / "unused_5m.log",
        log_file_path_15m=test_dir / "unused_15m.log",
        result_log_file_path=test_dir / "unused_result.log",
        alerts_file_path=alerts_log_path,
        disable_alerts=False,
        no_console_output=True,
    )

    try:
        exit_code = await cli_module.run_from_args(args)
    finally:
        log_lines = main_log_path.read_text(encoding="utf-8").splitlines()
        alert_lines = alerts_log_path.read_text(encoding="utf-8").splitlines()
        shutil.rmtree(test_dir, ignore_errors=True)

    assert exit_code == 1
    assert any(line.startswith("warning: fatal strategy error: RuntimeError: boom") for line in log_lines)
    assert any("warning: fatal strategy error: RuntimeError: boom" in line for line in alert_lines)


@pytest.mark.asyncio
async def test_run_from_args_both_timeframes_writes_split_and_result_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_dir = Path("logs") / f"test_cli_{uuid4().hex}"
    test_dir.mkdir(parents=True, exist_ok=True)
    log_5m = test_dir / "strategy_test_5.log"
    log_15m = test_dir / "strategy_test_15.log"
    result_log = test_dir / "strategy_test_result.log"
    alerts_log = test_dir / "alerts.log"
    log_5m.write_text("old5\n", encoding="utf-8")
    log_15m.write_text("old15\n", encoding="utf-8")
    result_log.write_text(
        "result_total_cumulative: events=10, win=6, lose=4, skip=0, profit_usd=111.00\n",
        encoding="utf-8",
    )

    class _FakeEventClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeRunner:
        def __init__(self, **kwargs) -> None:
            self._logger = kwargs["logger"]
            self._on_event_closed = kwargs.get("on_event_closed")

        async def run(self, config) -> StrategySummary:
            self._logger(f"summary: fake timeframe={config.market_timeframe_minutes}")
            if self._on_event_closed is not None:
                self._on_event_closed(
                    StrategyEventResult(
                        event_slug=f"btc-updown-{config.market_timeframe_minutes}m-123",
                        end_timestamp_s=123,
                        result="win",
                        profit_usd=10.0,
                    )
                )
            if config.market_timeframe_minutes == 5:
                return StrategySummary(total_events=2, wins=1, losses=1, skips=0, total_profit_usd=10.0)
            return StrategySummary(total_events=3, wins=2, losses=1, skips=0, total_profit_usd=20.0)

    monkeypatch.setattr(cli_module, "GammaEventsClient", lambda **kwargs: _FakeEventClient())
    monkeypatch.setattr(cli_module, "StrategyRunner", _FakeRunner)

    args = SimpleNamespace(
        symbol_pair="BTC/USD",
        market_timeframe_minutes=5,
        run_both_timeframes=True,
        duration_seconds=60,
        entry_seconds_before_end=5,
        final_price_delay_seconds=2,
        price_threshold_30s_usd=150.0,
        price_threshold_usd=50.0,
        price_threshold_4s_usd=40.0,
        price_threshold_near_end_usd=30.0,
        stake_usd=100.0,
        log_file_path=test_dir / "unused_single.log",
        log_file_path_5m=log_5m,
        log_file_path_15m=log_15m,
        result_log_file_path=result_log,
        alerts_file_path=alerts_log,
        disable_alerts=True,
        no_console_output=True,
    )

    try:
        exit_code = await cli_module.run_from_args(args)
        result_lines = result_log.read_text(encoding="utf-8").splitlines()
        log_5m_lines = log_5m.read_text(encoding="utf-8").splitlines()
        log_15m_lines = log_15m.read_text(encoding="utf-8").splitlines()
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    assert exit_code == 0
    assert any("summary: fake timeframe=5" in line for line in log_5m_lines)
    assert any("summary: fake timeframe=15" in line for line in log_15m_lines)
    assert not any("old5" in line for line in log_5m_lines)
    assert not any("old15" in line for line in log_15m_lines)
    assert any("result: timeframe=5m, events=2" in line for line in result_lines)
    assert any("result: timeframe=15m, events=3" in line for line in result_lines)
    assert any("result_event: timeframe=5m, slug=btc-updown-5m-123, result=win, profit_usd=10.00" in line for line in result_lines)
    assert any("result_event: timeframe=15m, slug=btc-updown-15m-123, result=win, profit_usd=10.00" in line for line in result_lines)
    assert any("result_total_running:" in line for line in result_lines)
    assert any("result_total_cumulative_running:" in line for line in result_lines)
    assert any("result_total: events=5, win=3, lose=2, skip=0, profit_usd=30.00" in line for line in result_lines)
    assert any("result_total_cumulative: events=15, win=9, lose=6, skip=0, profit_usd=141.00" in line for line in result_lines)
