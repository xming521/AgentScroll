"""Foreground interval scheduling for AgentScroll workflows."""

from __future__ import annotations

import signal
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler


def _minutes_since_midnight(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _is_within_daily_window(now: datetime, start_time: str, end_time: str) -> bool:
    current = now.hour * 60 + now.minute
    start = _minutes_since_midnight(start_time)
    end = _minutes_since_midnight(end_time)
    if start == end:
        return True
    if start < end:
        return start <= current <= end
    return current >= start or current <= end


def run_at_interval(
    job: Callable[[], None],
    *,
    interval_seconds: int,
    start_time: str,
    end_time: str,
    configure_scheduler: Callable[[BlockingScheduler], None] | None = None,
) -> None:
    """Run from process start at a fixed interval within a daily window."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")

    def run_in_window() -> None:
        if _is_within_daily_window(
            datetime.now().astimezone(),
            start_time,
            end_time,
        ):
            job()

    scheduler = BlockingScheduler()
    job_options = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": None,
    }
    scheduler.add_job(
        run_in_window,
        "interval",
        seconds=interval_seconds,
        next_run_time=datetime.now().astimezone(),
        **job_options,
    )

    if configure_scheduler is not None:
        configure_scheduler(scheduler)

    previous_handlers: dict[signal.Signals, signal.Handlers] = {}
    shutdown_started = Event()

    def stop_scheduler(_signum: int, _frame: FrameType | None) -> None:
        if scheduler.running and not shutdown_started.is_set():
            shutdown_started.set()
            Thread(
                target=scheduler.shutdown,
                kwargs={"wait": True},
                name="agentscroll-scheduler-shutdown",
            ).start()

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[handled_signal] = signal.getsignal(handled_signal)
        signal.signal(handled_signal, stop_scheduler)

    try:
        scheduler.start()
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)


__all__ = ["run_at_interval"]
