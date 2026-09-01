from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from agentscroll.config import (
    ShareDestinationSettings,
    SharePolicySettings,
    SharingSettings,
)
from agentscroll.sharing import SendResult, ShareDispatcher
from agentscroll.sharing.dispatcher import _earliest_normal_time


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


def _write_manifest(path, generated_at: datetime, scores: list[float]) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(timespec="seconds"),
                "share_count": len(scores),
                "shares": [
                    _share(score, index) for index, score in enumerate(scores)
                ],
            }
        ),
        encoding="utf-8",
    )


def _dispatcher(tmp_path, current_time, *, targets=("room-one",)):
    shares = tmp_path / "shares"
    sharing = tmp_path / "sharing"
    shares.mkdir()
    transport = FakeTransport()
    dispatcher = ShareDispatcher(
        _settings(targets=targets),
        transports={"fake": transport},
        share_output_dir=shares,
        sharing_output_dir=sharing,
        now=lambda: current_time[0],
    )
    scheduler = BlockingScheduler(timezone=timezone.utc)
    dispatcher.attach_scheduler(scheduler)
    return dispatcher, scheduler, shares, transport


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


def test_first_start_baselines_history_and_new_batch_is_bounded(tmp_path) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    shares = tmp_path / "shares"
    shares.mkdir()
    old = shares / "20260831-115900-000000+0800_即时分享批次.json"
    _write_manifest(old, now[0] - timedelta(minutes=1), [4.0, 3.9])
    dispatcher = ShareDispatcher(
        _settings(),
        transports={"fake": FakeTransport()},
        share_output_dir=shares,
        sharing_output_dir=tmp_path / "sharing",
        now=lambda: now[0],
    )
    scheduler = BlockingScheduler(timezone=timezone.utc)
    dispatcher.attach_scheduler(scheduler)

    assert scheduler.get_jobs() == []

    new = shares / "20260831-120000-000000+0800_即时分享批次.json"
    _write_manifest(new, now[0], [4.0, 3.8, 3.5, 3.4])
    result = dispatcher.submit_manifest(new)

    assert result["bypass_scheduled"] == 1
    assert result["normal_scheduled"] == 2
    assert result["dropped"] == 1

    state = json.loads(dispatcher.state_path.read_text(encoding="utf-8"))
    destination_state = next(iter(state["destinations"].values()))
    pending = destination_state["pending"]
    assert len(pending) == 3
    normal_due = [
        datetime.fromisoformat(job["due_at"])
        for job in pending
        if not job["bypass"]
    ]
    assert normal_due == [now[0], now[0] + timedelta(minutes=10)]


def test_destinations_are_independent_and_new_batch_supersedes_waiting_normal(
    tmp_path,
) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, scheduler, shares, _transport = _dispatcher(
        tmp_path,
        now,
        targets=("room-one", "room-two"),
    )
    first = shares / "20260831-120000-000000+0800_即时分享批次.json"
    _write_manifest(first, now[0], [3.9, 3.8])
    result = dispatcher.submit_manifest(first)
    assert result["normal_scheduled"] == 4

    now[0] += timedelta(minutes=5)
    second = shares / "20260831-120500-000000+0800_即时分享批次.json"
    _write_manifest(second, now[0], [3.7])
    result = dispatcher.submit_manifest(second)

    assert result["normal_scheduled"] == 2
    state = json.loads(dispatcher.state_path.read_text(encoding="utf-8"))
    for destination_state in state["destinations"].values():
        pending = [
            job for job in destination_state["pending"] if not job["bypass"]
        ]
        assert len(pending) == 1
        assert pending[0]["manifest_file"] == str(second)
    assert len(scheduler.get_jobs()) == 2


def test_dispatcher_sends_through_injected_transport_and_records_quota(
    tmp_path,
) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, _scheduler, shares, transport = _dispatcher(tmp_path, now)
    manifest = shares / "20260831-120000-000000+0800_即时分享批次.json"
    _write_manifest(manifest, now[0], [3.9])
    dispatcher.submit_manifest(manifest)
    state = json.loads(dispatcher.state_path.read_text(encoding="utf-8"))
    destination_id, destination_state = next(iter(state["destinations"].items()))
    job = destination_state["pending"][0]

    dispatcher._execute_job(destination_id, job["job_id"])

    state = json.loads(dispatcher.state_path.read_text(encoding="utf-8"))
    destination_state = state["destinations"][destination_id]
    assert destination_state["pending"] == []
    assert destination_state["normal_events"][0]["status"] == "sent"
    assert transport.messages == [
        ("room-one", "message-0\nhttps://example.com/0"),
        ("room-one", "comment-0"),
    ]


def test_restart_marks_inflight_normal_as_unknown_without_resending(tmp_path) -> None:
    now = [datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)]
    dispatcher, _scheduler, shares, _transport = _dispatcher(tmp_path, now)
    manifest = shares / "20260831-120000-000000+0800_即时分享批次.json"
    _write_manifest(manifest, now[0], [3.9])
    dispatcher.submit_manifest(manifest)
    state = json.loads(dispatcher.state_path.read_text(encoding="utf-8"))
    destination_id, destination_state = next(iter(state["destinations"].items()))
    pending = destination_state["pending"][0]
    pending["status"] = "inflight"
    destination_state["normal_events"].append(
        {
            "job_id": pending["job_id"],
            "at": now[0].isoformat(),
            "status": "inflight",
        }
    )
    dispatcher.state_path.write_text(json.dumps(state), encoding="utf-8")

    restarted = ShareDispatcher(
        _settings(),
        transports={"fake": FakeTransport()},
        share_output_dir=shares,
        sharing_output_dir=tmp_path / "sharing",
        now=lambda: now[0],
    )
    restarted.attach_scheduler(BlockingScheduler(timezone=timezone.utc))

    recovered = json.loads(restarted.state_path.read_text(encoding="utf-8"))
    destination_state = recovered["destinations"][destination_id]
    assert destination_state["pending"] == []
    assert destination_state["normal_events"][0]["status"] == "unknown"
