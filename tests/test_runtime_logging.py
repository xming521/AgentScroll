from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import click
import pytest
from loguru import logger as loguru_logger

from agentscroll import runtime_logging
from agentscroll.cli import _run


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="apscheduler.executors.default",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_daily_log_handler_switches_files_at_local_midnight(tmp_path: Path) -> None:
    current = [datetime.fromisoformat("2026-09-03T23:59:00+08:00")]
    handler = runtime_logging.DailyFileHandler(tmp_path, now=lambda: current[0])
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler.emit(_record("first day"))
    current[0] = datetime.fromisoformat("2026-09-04T00:01:00+08:00")
    handler.emit(_record("second day"))
    handler.close()

    assert (tmp_path / "agentscroll-2026-09-03.log").read_text() == "first day\n"
    assert (tmp_path / "agentscroll-2026-09-04.log").read_text() == "second day\n"


def test_daily_log_handler_keeps_fourteen_calendar_days(tmp_path: Path) -> None:
    expired = tmp_path / "agentscroll-2026-08-20.log"
    oldest_kept = tmp_path / "agentscroll-2026-08-21.log"
    unrelated = tmp_path / "llm_audit.log"
    for path in (expired, oldest_kept, unrelated):
        path.write_text("existing\n")

    handler = runtime_logging.DailyFileHandler(
        tmp_path,
        retention_days=14,
        now=lambda: datetime.fromisoformat("2026-09-03T12:00:00+08:00"),
    )
    handler.close()

    assert not expired.exists()
    assert oldest_kept.exists()
    assert unrelated.exists()


def test_runtime_logging_records_standard_and_loguru_errors(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    try:
        log_path = runtime_logging.configure_runtime_logging(
            tmp_path,
            now=lambda: datetime.fromisoformat("2026-09-03T12:00:00+08:00"),
        )
        logging.getLogger("apscheduler.executors.default").error(
            "scheduled job failed"
        )
        loguru_logger.error("inference failed")
    finally:
        runtime_logging._remove_runtime_logging()
        root_logger.setLevel(previous_level)

    content = log_path.read_text()
    assert "ERROR apscheduler.executors.default: scheduled job failed" in content
    assert "ERROR test_runtime_logging: inference failed" in content


def test_cli_errors_are_written_to_runtime_log(tmp_path: Path) -> None:
    def fail() -> None:
        raise ValueError("invalid runtime value")

    try:
        runtime_logging.configure_runtime_logging(tmp_path)
        with pytest.raises(click.ClickException, match="invalid runtime value"):
            _run(fail)
    finally:
        runtime_logging._remove_runtime_logging()

    log_path = tmp_path / f"agentscroll-{datetime.now():%Y-%m-%d}.log"
    content = log_path.read_text()
    assert "ValueError: invalid runtime value" in content
