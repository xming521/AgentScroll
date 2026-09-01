from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from agentscroll.config import (
    ShareDestinationSettings,
    SharePolicySettings,
    SharingSettings,
)
from agentscroll.sharing import SendResult, ShareDispatcher
from agentscroll.sharing.dispatcher import _earliest_normal_time
from agentscroll.storage import connect_database, database_transaction


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def validate_target(self, target: str) -> None:
        if not target:
            raise ValueError("empty target")

    def send(self, target: str, message: str) -> SendResult:
        self.messages.append((target, message))
        return SendResult("sent", "ok", 1)


def _settings(*, targets: tuple[str, ...] = ("room-one",)) -> SharingSettings:
    return SharingSettings(
        enabled=True,
        destinations=tuple(
            ShareDestinationSettings(transport="fake", target=target)
            for target in targets
        ),
        policy=SharePolicySettings(
            window_minutes=60,
            max_messages_per_window=2,
            min_interval_minutes=10,
            bypass_score=4.0,
        ),
    )


def _share(score: float, index: int) -> dict[str, object]:
    return {
        "topic_id": f"topic-{index}",
        "title": f"title-{index}",
        "score": score,
        "text": f"message-{index}",
        "url": f"https://example.com/{index}",
        "comment": f"comment-{index}",
        "comment_type": "platform",
    }


def _jobs(database: Path) -> list[dict[str, object]]:
    connection = connect_database(database)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM share_jobs ORDER BY destination_id, share_group_id, share_index"
            )
        ]
    finally:
        connection.close()


def _dispatcher(tmp_path: Path, current_time, *, targets=("room-one",)):
    database = tmp_path / "agentscroll.sqlite3"
    transport = FakeTransport()
    dispatcher = ShareDispatcher(
        _settings(targets=targets),
        transports={"fake": transport},
        database_path=database,
        sharing_output_dir=tmp_path / "sharing",
        now=lambda: current_time[0],
    )
    scheduler = BlockingScheduler(timezone=timezone.utc)
    dispatcher.attach_scheduler(scheduler)
    return dispatcher, scheduler, database, transport


def test_rolling_window_and_minimum_interval() -> None:
    base = datetime(2026, 8, 31, tzinfo=timezone.utc)
    now = base + timedelta(minutes=58)
    reservations = [base, base + timedelta(minutes=10)]

    first = _earliest_normal_time(
        now=now,
        reservations=reservations,
        window=timedelta(minutes=60),
        limit=2,
        min_interval=timedelta(minutes=10),
    )
    second = _earliest_normal_time(
        now=now,
        reservations=reservations + [first],
        window=timedelta(minutes=60),
        limit=2,
        min_interval=timedelta(minutes=10),
    )

    assert first == base + timedelta(minutes=60)
    assert second == base + timedelta(minutes=70)


def test_batch_is_persisted_and_bounded_without_reading_review_files(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    review_dir = tmp_path / "shares"
    review_dir.mkdir()
    (review_dir / "old_即时分享批次.json").write_text("{}", encoding="utf-8")
    dispatcher, _scheduler, database, _transport = _dispatcher(tmp_path, now)

    result = dispatcher.submit_shares(
        "batch-1",
        now[0],
        [_share(score, index) for index, score in enumerate([4.0, 3.8, 3.5, 3.4])],
    )

    assert result["bypass_scheduled"] == 1
    assert result["normal_scheduled"] == 2
    assert result["dropped"] == 1
    jobs = _jobs(database)
    assert len(jobs) == 4
    assert [job["status"] for job in jobs].count("waiting") == 3
    normal_due = [
        datetime.fromisoformat(str(job["due_at"]))
        for job in jobs
        if not job["bypass"] and job["status"] == "waiting"
    ]
    assert normal_due == [now[0], now[0] + timedelta(minutes=10)]


def test_destinations_are_independent_and_new_batch_supersedes_waiting_normal(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, scheduler, database, _transport = _dispatcher(
        tmp_path,
        now,
        targets=("room-one", "room-two"),
    )
    result = dispatcher.submit_shares(
        "batch-1", now[0], [_share(3.9, 0), _share(3.8, 1)]
    )
    assert result["normal_scheduled"] == 4

    now[0] += timedelta(minutes=5)
    result = dispatcher.submit_shares("batch-2", now[0], [_share(3.7, 2)])

    assert result["normal_scheduled"] == 2
    jobs = _jobs(database)
    for destination_id in {str(job["destination_id"]) for job in jobs}:
        destination_jobs = [
            job for job in jobs if job["destination_id"] == destination_id
        ]
        assert sum(job["status"] == "superseded" for job in destination_jobs) == 2
        waiting = [job for job in destination_jobs if job["status"] == "waiting"]
        assert len(waiting) == 1
        assert waiting[0]["share_group_id"] == "batch-2"
    assert len(scheduler.get_jobs()) == 2


def test_dispatcher_sends_frozen_payload_and_records_quota(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, _scheduler, database, transport = _dispatcher(tmp_path, now)
    dispatcher.submit_shares("batch-1", now[0], [_share(3.9, 0)])
    job = _jobs(database)[0]

    dispatcher._execute_job(str(job["job_id"]))

    sent = _jobs(database)[0]
    assert sent["status"] == "sent"
    assert sent["reserved_at"] == now[0].isoformat(timespec="seconds")
    assert transport.messages == [
        ("room-one", "message-0\nhttps://example.com/0"),
        ("room-one", "comment-0"),
    ]


def test_restart_marks_inflight_as_unknown_without_resending(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, _scheduler, database, _transport = _dispatcher(tmp_path, now)
    dispatcher.submit_shares("batch-1", now[0], [_share(3.9, 0)])
    job = _jobs(database)[0]
    with database_transaction(database, immediate=True) as connection:
        connection.execute(
            """
            UPDATE share_jobs
            SET status = 'inflight', reserved_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (now[0].isoformat(), now[0].isoformat(), job["job_id"]),
        )

    restarted = ShareDispatcher(
        _settings(),
        transports={"fake": FakeTransport()},
        database_path=database,
        sharing_output_dir=tmp_path / "sharing",
        now=lambda: now[0],
    )
    restarted.attach_scheduler(BlockingScheduler(timezone=timezone.utc))

    recovered = _jobs(database)[0]
    assert recovered["status"] == "unknown"
    assert recovered["reserved_at"] == now[0].isoformat()


def test_share_group_submission_is_idempotent(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, _scheduler, database, _transport = _dispatcher(tmp_path, now)
    dispatcher.submit_shares("batch-1", now[0], [_share(3.9, 0)])

    result = dispatcher.submit_shares("batch-1", now[0], [_share(3.9, 0)])

    assert result["status"] == "already_processed"
    assert len(_jobs(database)) == 1
