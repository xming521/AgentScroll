"""Foreground interval scheduling for AgentScroll workflows."""

from __future__ import annotations

import signal
from collections.abc import Callable
from datetime import datetime, tzinfo
from threading import Event, Thread
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger


def _minutes_since_midnight(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _daily_window_slots(
    *,
    interval_seconds: int,
    start_time: str,
    end_time: str,
) -> tuple[tuple[int, int], ...]:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")
    if interval_seconds % 60:
        raise ValueError("时间窗口内的执行间隔必须是整分钟")

    start = _minutes_since_midnight(start_time)
    end = _minutes_since_midnight(end_time)
    if end <= start:
        end += 24 * 60

    interval_minutes = interval_seconds // 60
    slots: list[tuple[int, int]] = []
    current = start
    while current <= end:
        local_minute = current % (24 * 60)
        slot = divmod(local_minute, 60)
        if slot not in slots:
            slots.append(slot)
        current += interval_minutes
    return tuple(slots)


def _daily_window_trigger(
    *,
    interval_seconds: int,
    start_time: str,
    end_time: str,
    timezone: tzinfo,
) -> OrTrigger:
    slots = _daily_window_slots(
        interval_seconds=interval_seconds,
        start_time=start_time,
        end_time=end_time,
    )
    return OrTrigger(
        [
            CronTrigger(hour=hour, minute=minute, second=0, timezone=timezone)
            for hour, minute in slots
        ]
    )


def run_at_interval(
    job: Callable[[], None],
    *,
    interval_seconds: int,
    start_time: str | None = None,
    end_time: str | None = None,
    configure_scheduler: Callable[[BlockingScheduler], None] | None = None,
) -> None:
    """Run a job continuously, optionally at fixed local-time slots."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")
    if (start_time is None) != (end_time is None):
        raise ValueError("start_time 和 end_time 必须同时设置")

    scheduler = BlockingScheduler()
    job_options = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": None,
    }
    if start_time is None or end_time is None:
        scheduler.add_job(
            job,
            "interval",
            seconds=interval_seconds,
            next_run_time=datetime.now().astimezone(),
            **job_options,
        )
    else:
        scheduler.add_job(
            job,
            _daily_window_trigger(
                interval_seconds=interval_seconds,
                start_time=start_time,
                end_time=end_time,
                timezone=scheduler.timezone,
            ),
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
