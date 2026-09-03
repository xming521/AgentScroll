"""Runtime logging for AgentScroll commands."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO


DEFAULT_RUNTIME_LOG_DIR = Path("outputs/logs")
RUNTIME_LOG_RETENTION_DAYS = 14
_RUNTIME_LOG_NAME = re.compile(r"^agentscroll-(\d{4}-\d{2}-\d{2})\.log$")
_RUNTIME_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_runtime_handler: DailyFileHandler | None = None
_loguru_sink_id: int | None = None


class DailyFileHandler(logging.Handler):
    """Write records to one local-date log file per day."""

    terminator = "\n"

    def __init__(
        self,
        directory: str | Path = DEFAULT_RUNTIME_LOG_DIR,
        *,
        retention_days: int = RUNTIME_LOG_RETENTION_DAYS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        if retention_days < 1:
            raise ValueError("retention_days 必须大于 0")
        self.directory = Path(directory).expanduser().resolve()
        self.retention_days = retention_days
        self._now = now or (lambda: datetime.now().astimezone())
        self._current_day: date | None = None
        self._stream: TextIO | None = None
        self._switch_file(self._now().date())

    @property
    def current_path(self) -> Path:
        day = self._current_day or self._now().date()
        return self.directory / f"agentscroll-{day.isoformat()}.log"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = self._now().date()
            if day != self._current_day:
                self._switch_file(day)
            if self._stream is None:
                raise RuntimeError("运行日志文件未打开")
            self._stream.write(self.format(record) + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            self.release()
        super().close()

    def _switch_file(self, day: date) -> None:
        if self._stream is not None:
            self._stream.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._current_day = day
        self._stream = self.current_path.open("a", encoding="utf-8")
        self._delete_expired_files(day)

    def _delete_expired_files(self, day: date) -> None:
        earliest_kept_day = day - timedelta(days=self.retention_days - 1)
        for path in self.directory.glob("agentscroll-*.log"):
            match = _RUNTIME_LOG_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                file_day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if file_day < earliest_kept_day:
                path.unlink(missing_ok=True)


def configure_runtime_logging(
    directory: str | Path = DEFAULT_RUNTIME_LOG_DIR,
    *,
    retention_days: int = RUNTIME_LOG_RETENTION_DAYS,
    now: Callable[[], datetime] | None = None,
) -> Path:
    """Configure daily file logging while retaining terminal warnings."""
    global _loguru_sink_id, _runtime_handler

    _remove_runtime_logging()
    root_logger = logging.getLogger()
    had_handlers = bool(root_logger.handlers)

    handler = DailyFileHandler(
        directory,
        retention_days=retention_days,
        now=now,
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_RUNTIME_LOG_FORMAT))
    root_logger.addHandler(handler)
    root_logger.setLevel(min(root_logger.level, logging.INFO))

    if not had_handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(console_handler, "_agentscroll_console_handler", True)
        root_logger.addHandler(console_handler)

    from loguru import logger as loguru_logger

    _loguru_sink_id = loguru_logger.add(handler, level="INFO", format="{message}")
    _runtime_handler = handler
    logging.getLogger(__name__).info(
        "AgentScroll runtime log initialized: %s", handler.current_path
    )
    return handler.current_path


def _remove_runtime_logging() -> None:
    global _loguru_sink_id, _runtime_handler

    root_logger = logging.getLogger()
    if _loguru_sink_id is not None:
        from loguru import logger as loguru_logger

        loguru_logger.remove(_loguru_sink_id)
        _loguru_sink_id = None
    if _runtime_handler is not None:
        root_logger.removeHandler(_runtime_handler)
        _runtime_handler.close()
        _runtime_handler = None
    for handler in list(root_logger.handlers):
        if getattr(handler, "_agentscroll_console_handler", False):
            root_logger.removeHandler(handler)
            handler.close()


__all__ = [
    "DEFAULT_RUNTIME_LOG_DIR",
    "DailyFileHandler",
    "RUNTIME_LOG_RETENTION_DAYS",
    "configure_runtime_logging",
]
