from __future__ import annotations

from datetime import datetime
from typing import Any

from agentscroll import scheduler as scheduler_module


class FakeScheduler:
    def __init__(self) -> None:
        self.running = False
        self.added_job: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def add_job(self, *args: Any, **kwargs: Any) -> None:
        self.added_job = (args, kwargs)

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool) -> None:
        return None


def test_interval_scheduler_runs_immediately_from_process_start(monkeypatch: Any) -> None:
    scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", lambda: scheduler)
    job = lambda: None
    before = datetime.now().astimezone()

    scheduler_module.run_at_interval(
        job,
        interval_seconds=3600,
        start_time="08:00",
        end_time="00:00",
    )

    after = datetime.now().astimezone()
    assert scheduler.added_job is not None
    args, kwargs = scheduler.added_job
    assert args[1] == "interval"
    assert kwargs["seconds"] == 3600
    assert before <= kwargs["next_run_time"] <= after


def test_daily_window_keeps_process_anchored_time_inside_cross_midnight_window() -> None:
    assert scheduler_module._is_within_daily_window(
        datetime(2026, 9, 3, 10, 13), "08:00", "00:00"
    )
    assert scheduler_module._is_within_daily_window(
        datetime(2026, 9, 4, 0, 0), "08:00", "00:00"
    )
    assert not scheduler_module._is_within_daily_window(
        datetime(2026, 9, 4, 0, 13), "08:00", "00:00"
    )
