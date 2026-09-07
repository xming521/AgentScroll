"""SQLite-backed, platform-independent share scheduling."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic as monotonic_time
from time import sleep as sleep_time
from typing import Any, Literal, Protocol

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.blocking import BlockingScheduler

from agentscroll.config import ShareDestinationSettings, SharingSettings
from agentscroll.storage import database_transaction, resolve_database_path

from .message import render_share_messages
from .policy import HOTLIST_SCORES, DeliveryDecision, decide_delivery, legacy_share_trigger, score_rules


@dataclass(frozen=True, slots=True)
class SendResult:
    status: Literal["sent", "failed", "unknown"]
    detail: str
    attempts: int


class ShareTransport(Protocol):
    def validate_target(self, target: str) -> None: ...

    def send(self, target: str, message: str) -> SendResult: ...


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _isoformat(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _earliest_normal_time(
    *,
    now: datetime,
    reservations: list[datetime],
    window: timedelta,
    limit: int,
    min_interval: timedelta,
    interval_reservations: list[datetime] | None = None,
) -> datetime:
    candidate = now
    ordered = sorted(reservations)
    interval_ordered = sorted(
        reservations if interval_reservations is None else interval_reservations
    )
    while True:
        recent = [event_at for event_at in ordered if event_at > candidate - window]
        next_candidate = candidate
        if len(recent) >= limit:
            next_candidate = max(next_candidate, recent[-limit] + window)
        if interval_ordered:
            next_candidate = max(
                next_candidate, interval_ordered[-1] + min_interval
            )
        if next_candidate == candidate:
            return candidate
        candidate = next_candidate


def _earliest_interval_time(
    *,
    now: datetime,
    reservations: list[datetime],
    min_interval: timedelta,
) -> datetime:
    if not reservations:
        return now
    return max(now, max(reservations) + min_interval)


class ShareDispatcher:
    """Plan share batches and dispatch due messages through injected transports."""

    def __init__(
        self,
        settings: SharingSettings,
        *,
        transports: Mapping[str, ShareTransport],
        database_path: str | Path | None = None,
        sharing_output_dir: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError("Instant sharing is not enabled")

        self.settings = settings
        self.transports = dict(transports)
        self._destinations: dict[str, ShareDestinationSettings] = {}
        for destination in settings.destinations:
            transport = self.transports.get(destination.transport)
            if transport is None:
                raise ValueError(
                    f"Unsupported sharing transport: {destination.transport}"
                )
            transport.validate_target(destination.target)
            self._destinations[self._destination_id(destination)] = destination

        self.database_path = resolve_database_path(database_path)
        self.sharing_output_dir = (
            Path(sharing_output_dir).expanduser().resolve()
            if sharing_output_dir is not None
            else (Path.cwd() / "outputs" / "sharing").resolve()
        )
        self._now_factory = now or (lambda: datetime.now().astimezone())
        self._monotonic = monotonic or monotonic_time
        self._sleep = sleep or sleep_time
        self._window = timedelta(minutes=settings.policy.window.window_minutes)
        self._min_interval = timedelta(
            minutes=settings.policy.delivery.min_interval_minutes
        )
        self._lock = RLock()
        self._destination_locks = {
            destination_id: RLock() for destination_id in self._destinations
        }
        self._last_delivery: dict[str, tuple[float, bool]] = {}
        self._scheduler: BlockingScheduler | None = None

    @staticmethod
    def _destination_id(destination: ShareDestinationSettings) -> str:
        identity = f"{destination.transport}\0{destination.target}"
        return sha256(identity.encode("utf-8")).hexdigest()[:24]

    def _now(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return value

    def attach_scheduler(self, scheduler: BlockingScheduler) -> None:
        """Attach the workflow scheduler and restore waiting database jobs."""
        with self._lock:
            if self._scheduler is not None:
                raise RuntimeError("Share dispatcher is already attached")
            self._scheduler = scheduler
            now = self._now()
            now_text = _isoformat(now)
            configured_ids = set(self._destinations)
            audit_events: list[tuple[str, dict[str, Any]]] = []
            with database_transaction(self.database_path, immediate=True) as connection:
                interrupted = connection.execute(
                    "SELECT job_id, destination_id FROM share_jobs "
                    "WHERE status = 'inflight'"
                ).fetchall()
                connection.execute(
                    """
                    UPDATE share_jobs
                    SET status = 'unknown', finished_at = ?, updated_at = ?,
                        result_detail = 'interrupted_restart'
                    WHERE status = 'inflight'
                    """,
                    (now_text, now_text),
                )
                for row in interrupted:
                    audit_events.append(
                        (
                            "share_marked_unknown_after_restart",
                            {
                                "destination_id": row["destination_id"],
                                "job_id": row["job_id"],
                            },
                        )
                    )

                waiting_rows = connection.execute(
                    "SELECT job_id, destination_id, expires_at FROM share_jobs "
                    "WHERE status = 'waiting'"
                ).fetchall()
                for row in waiting_rows:
                    if str(row["destination_id"]) not in configured_ids:
                        status = "dropped"
                        detail = "destination_removed"
                        event = "share_dropped_destination_removed"
                    elif (
                        self.settings.policy.mode == "window"
                        and _parse_datetime(str(row["expires_at"])) <= now
                    ):
                        status = "expired"
                        detail = "expired_before_restore"
                        event = "share_dropped_expired"
                    else:
                        continue
                    connection.execute(
                        """
                        UPDATE share_jobs
                        SET status = ?, finished_at = ?, result_detail = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'waiting'
                        """,
                        (status, now_text, detail, now_text, row["job_id"]),
                    )
                    audit_events.append(
                        (
                            event,
                            {
                                "destination_id": row["destination_id"],
                                "job_id": row["job_id"],
                            },
                        )
                    )

                waiting = connection.execute(
                    "SELECT job_id, due_at FROM share_jobs WHERE status = 'waiting'"
                ).fetchall()

            for event, fields in audit_events:
                self._audit(event, **fields)
            for row in waiting:
                self._schedule_job(str(row["job_id"]), str(row["due_at"]))

    def submit_shares(
        self,
        share_group_id: str,
        generated_at: datetime | str,
        shares: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist one generated share batch for every configured destination."""
        group_id = str(share_group_id or "").strip()
        if not group_id:
            raise ValueError("即时分享批次缺少 share_group_id")
        generated = (
            _parse_datetime(generated_at)
            if isinstance(generated_at, str)
            else generated_at
        )
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=self._now().tzinfo)
        expires_at = generated + self._window
        normalized_shares: list[
            tuple[int, dict[str, Any], str, DeliveryDecision]
        ] = []
        try:
            for index, raw_share in enumerate(shares):
                if not isinstance(raw_share, Mapping) or isinstance(
                    raw_share.get("score"), bool
                ):
                    raise TypeError
                share = dict(raw_share)
                score = float(share["score"])
                if score < 3 or score > 4:
                    raise ValueError
                raw_general_score = share.get("general_score", score)
                if isinstance(raw_general_score, bool):
                    raise TypeError
                general_score = float(raw_general_score)
                if not (
                    general_score == 0 or 1 <= general_score <= 4
                ) or abs(
                    general_score * 10 - round(general_score * 10)
                ) >= 1e-9:
                    raise ValueError
                share["score"] = score
                share["general_score"] = general_score
                raw_hotlist_title_count = share.get("hotlist_title_count", 1)
                if (
                    isinstance(raw_hotlist_title_count, bool)
                    or not isinstance(raw_hotlist_title_count, int)
                    or raw_hotlist_title_count < 1
                ):
                    raise ValueError
                hotlist_score = share.get("hotlist_score", 0)
                if isinstance(hotlist_score, bool) or hotlist_score not in HOTLIST_SCORES:
                    raise ValueError
                if hotlist_score and share.get("relation") != "new":
                    raise ValueError
                interest_score = float(share.get("interest_score", 0))
                if not (interest_score == 0 or 1 <= interest_score <= 3.9):
                    raise ValueError
                rules = score_rules(general_score, interest_score, hotlist_score)
                if "share_rules" in share and tuple(share["share_rules"]) != rules:
                    raise ValueError
                if "hotlist_score" in share and score != max(
                    general_score, interest_score, hotlist_score
                ):
                    raise ValueError
                share["share_rules"] = list(rules)
                delivery = decide_delivery(
                    score=score,
                    general_score=general_score,
                    hotlist_score=hotlist_score,
                    policy=self.settings.policy,
                )
                share["delivery_mode"] = delivery.mode
                share["delivery_reasons"] = list(delivery.reasons)
                share_trigger = legacy_share_trigger(rules, general_score)
                render_share_messages(share)
                normalized_shares.append((index, share, share_trigger, delivery))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("即时分享批次字段错误") from exc
        normalized_shares.sort(key=lambda item: (-item[1]["score"], item[0]))

        now = self._now()
        now_text = _isoformat(now)
        expires_text = _isoformat(expires_at)
        scheduled: list[tuple[str, str]] = []
        removed_job_ids: list[str] = []
        audit_events: list[tuple[str, dict[str, Any]]] = []
        policy = self.settings.policy
        score_only = policy.mode == "score_only"
        summary: dict[str, Any] = {
            "status": "planned",
            "share_group_id": group_id,
            "destination_count": len(self._destinations),
            "bypass_scheduled": 0,
            "normal_scheduled": 0,
            "dropped": 0,
        }

        with self._lock:
            with database_transaction(self.database_path, immediate=True) as connection:
                existing = connection.execute(
                    "SELECT COUNT(*) FROM share_jobs WHERE share_group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                if existing:
                    return {**summary, "status": "already_processed"}

                for destination_id, destination in self._destinations.items():
                    superseded = connection.execute(
                        """
                        SELECT job_id FROM share_jobs
                        WHERE destination_id = ? AND bypass = 0 AND status = 'waiting'
                        """,
                        (destination_id,),
                    ).fetchall()
                    connection.execute(
                        """
                        UPDATE share_jobs
                        SET status = 'superseded', finished_at = ?, updated_at = ?,
                            result_detail = 'newer_batch'
                        WHERE destination_id = ? AND bypass = 0 AND status = 'waiting'
                        """,
                        (now_text, now_text, destination_id),
                    )
                    for row in superseded:
                        job_id = str(row["job_id"])
                        removed_job_ids.append(job_id)
                        audit_events.append(
                            (
                                "share_dropped_superseded",
                                {"destination_id": destination_id, "job_id": job_id},
                            )
                        )

                    reservations = (
                        self._normal_event_times(connection, destination_id, now)
                        if not score_only
                        else []
                    )
                    interval_reservations = self._interval_event_times(
                        connection, destination_id, include_waiting=True
                    )
                    ordinary_count = 0
                    for share_index, share, share_trigger, delivery in normalized_shares:
                        score = float(share["score"])
                        eligible = delivery.eligible
                        immediate = delivery.mode == "immediate"
                        bypass = immediate
                        status = "waiting"
                        detail: str | None = None
                        due_at = now
                        if not eligible:
                            status = "dropped"
                            detail = "score_below_threshold"
                        elif not score_only and expires_at <= now:
                            status = "expired"
                            detail = "batch_expired"
                        elif immediate:
                            pass
                        elif not score_only:
                            ordinary_count += 1
                            if ordinary_count > policy.window.max_messages_per_window:
                                status = "dropped"
                                detail = "batch_limit"
                            else:
                                due_at = _earliest_normal_time(
                                    now=now,
                                    reservations=reservations,
                                    window=self._window,
                                    limit=policy.window.max_messages_per_window,
                                    min_interval=self._min_interval,
                                    interval_reservations=interval_reservations,
                                )
                                if due_at >= expires_at:
                                    status = "dropped"
                                    detail = "rate_limit"
                                else:
                                    reservations.append(due_at)
                                    interval_reservations.append(due_at)
                        else:
                            due_at = _earliest_interval_time(
                                now=now,
                                reservations=interval_reservations,
                                min_interval=self._min_interval,
                            )
                            interval_reservations.append(due_at)

                        job_id = self._job_id(
                            group_id, destination_id, share_index, score, bypass
                        )
                        finished_at = now_text if status != "waiting" else None
                        connection.execute(
                            """
                            INSERT INTO share_jobs(
                                job_id, share_group_id, topic_id, destination_id,
                                transport, target, share_index, score,
                                share_trigger, due_at,
                                expires_at, bypass, status, reserved_at, finished_at,
                                result_detail, payload_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                            """,
                            (
                                job_id,
                                group_id,
                                str(share.get("topic_id") or "") or None,
                                destination_id,
                                destination.transport,
                                destination.target,
                                share_index,
                                score,
                                share_trigger,
                                _isoformat(due_at),
                                expires_text,
                                int(bypass),
                                status,
                                finished_at,
                                detail,
                                json.dumps(
                                    share,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                now_text,
                                now_text,
                            ),
                        )
                        if status == "waiting":
                            scheduled.append((job_id, _isoformat(due_at)))
                            if bypass:
                                summary["bypass_scheduled"] += 1
                            else:
                                summary["normal_scheduled"] += 1
                        else:
                            summary["dropped"] += 1
                            dropped_event = {
                                "batch_expired": "share_dropped_expired",
                                "score_below_threshold": (
                                    "share_dropped_score_threshold"
                                ),
                            }.get(detail, "share_dropped_rate_limit")
                            audit_events.append(
                                (
                                    dropped_event,
                                    {
                                        "destination_id": destination_id,
                                        "job_id": job_id,
                                        "detail": detail,
                                    },
                                )
                            )

                if not score_only and expires_at <= now:
                    summary["status"] = "expired"

            for job_id in removed_job_ids:
                self._remove_scheduled_job(job_id)
            for job_id, due_at in scheduled:
                self._schedule_job(job_id, due_at)
            for event, fields in audit_events:
                self._audit(event, **fields)
            self._audit(
                "share_batch_planned",
                share_group_id=group_id,
                mode=policy.mode,
                bypass_scheduled=summary["bypass_scheduled"],
                normal_scheduled=summary["normal_scheduled"],
                dropped=summary["dropped"],
            )
        return summary

    @staticmethod
    def _job_id(
        group_id: str,
        destination_id: str,
        share_index: int,
        score: float,
        bypass: bool,
    ) -> str:
        identity = f"{group_id}|{destination_id}|{share_index}|{score}|{bypass}"
        digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"sharing-{digest}"

    def _normal_event_times(
        self,
        connection: sqlite3.Connection,
        destination_id: str,
        now: datetime,
        *,
        exclude_job_id: str | None = None,
    ) -> list[datetime]:
        parameters: list[Any] = [destination_id]
        exclude_sql = ""
        if exclude_job_id is not None:
            exclude_sql = " AND job_id != ?"
            parameters.append(exclude_job_id)
        rows = connection.execute(
            """
            SELECT reserved_at FROM share_jobs
            WHERE destination_id = ? AND bypass = 0
              AND status IN ('inflight', 'sent', 'unknown')
              AND reserved_at IS NOT NULL
            """
            + exclude_sql,
            parameters,
        ).fetchall()
        cutoff = now - self._window
        return [
            event_at
            for row in rows
            if (event_at := _parse_datetime(str(row["reserved_at"]))) > cutoff
        ]

    def _interval_event_times(
        self,
        connection: sqlite3.Connection,
        destination_id: str,
        *,
        exclude_job_id: str | None = None,
        include_waiting: bool = False,
    ) -> list[datetime]:
        parameters: list[Any] = [destination_id]
        exclude_sql = ""
        if exclude_job_id is not None:
            exclude_sql = " AND job_id != ?"
            parameters.append(exclude_job_id)
        rows = connection.execute(
            """
            SELECT reserved_at FROM share_jobs
            WHERE destination_id = ? AND bypass = 0
              AND status IN ('inflight', 'sent', 'unknown')
              AND reserved_at IS NOT NULL
            """
            + exclude_sql,
            parameters,
        ).fetchall()
        events = [_parse_datetime(str(row["reserved_at"])) for row in rows]
        if include_waiting:
            waiting_parameters: list[Any] = [destination_id]
            waiting_exclude_sql = ""
            if exclude_job_id is not None:
                waiting_exclude_sql = " AND job_id != ?"
                waiting_parameters.append(exclude_job_id)
            waiting_rows = connection.execute(
                "SELECT due_at FROM share_jobs "
                "WHERE destination_id = ? AND bypass = 0 AND status = 'waiting'"
                + waiting_exclude_sql,
                waiting_parameters,
            ).fetchall()
            events.extend(
                _parse_datetime(str(row["due_at"])) for row in waiting_rows
            )
        return events

    def _schedule_job(self, job_id: str, due_at: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Share dispatcher is not attached")
        self._scheduler.add_job(
            self._execute_job,
            "date",
            run_date=_parse_datetime(due_at),
            id=job_id,
            args=(job_id,),
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=None,
        )

    def _remove_scheduled_job(self, job_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    def _execute_job(self, job_id: str) -> None:
        with self._lock:
            with database_transaction(self.database_path) as connection:
                row = connection.execute(
                    "SELECT destination_id FROM share_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        if row is None:
            return
        destination_id = str(row["destination_id"])
        destination_lock = self._destination_locks.get(destination_id)
        if destination_lock is None:
            return
        with destination_lock:
            self._execute_serialized_job(job_id)

    def _execute_serialized_job(self, job_id: str) -> None:
        rescheduled_at: str | None = None
        job: dict[str, Any] | None = None
        with self._lock:
            now = self._now()
            now_text = _isoformat(now)
            policy = self.settings.policy
            audit_event: tuple[str, dict[str, Any]] | None = None
            with database_transaction(self.database_path, immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM share_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None or row["status"] != "waiting":
                    return
                expires_at = _parse_datetime(str(row["expires_at"]))
                if (
                    policy.mode == "window"
                    and expires_at <= now
                ):
                    connection.execute(
                        """
                        UPDATE share_jobs
                        SET status = 'expired', finished_at = ?, updated_at = ?,
                            result_detail = 'expired_before_send'
                        WHERE job_id = ? AND status = 'waiting'
                        """,
                        (now_text, now_text, job_id),
                    )
                    audit_event = (
                        "share_dropped_expired",
                        {"destination_id": row["destination_id"], "job_id": job_id},
                    )
                else:
                    if float(row["score"]) < policy.minimum_score:
                        connection.execute(
                            """
                            UPDATE share_jobs
                            SET status = 'dropped', finished_at = ?, updated_at = ?,
                                result_detail = 'score_below_threshold'
                            WHERE job_id = ? AND status = 'waiting'
                            """,
                            (now_text, now_text, job_id),
                        )
                        audit_event = (
                            "share_dropped_score_threshold",
                            {
                                "destination_id": row["destination_id"],
                                "job_id": job_id,
                                "score": row["score"],
                            },
                        )
                    elif not bool(row["bypass"]):
                        destination_id = str(row["destination_id"])
                        interval_reservations = self._interval_event_times(
                            connection,
                            destination_id,
                            exclude_job_id=job_id,
                        )
                        if policy.mode == "window":
                            reservations = self._normal_event_times(
                                connection,
                                destination_id,
                                now,
                                exclude_job_id=job_id,
                            )
                            due_at = _earliest_normal_time(
                                now=now,
                                reservations=reservations,
                                window=self._window,
                                limit=policy.window.max_messages_per_window,
                                min_interval=self._min_interval,
                                interval_reservations=interval_reservations,
                            )
                        else:
                            due_at = _earliest_interval_time(
                                now=now,
                                reservations=interval_reservations,
                                min_interval=self._min_interval,
                            )
                        if due_at > now:
                            if policy.mode == "window" and due_at >= expires_at:
                                connection.execute(
                                    """
                                    UPDATE share_jobs
                                    SET status = 'dropped', finished_at = ?, updated_at = ?,
                                        result_detail = 'rate_limit'
                                    WHERE job_id = ? AND status = 'waiting'
                                    """,
                                    (now_text, now_text, job_id),
                                )
                                audit_event = (
                                    "share_dropped_rate_limit",
                                    {
                                        "destination_id": row["destination_id"],
                                        "job_id": job_id,
                                    },
                                )
                            else:
                                rescheduled_at = _isoformat(due_at)
                                connection.execute(
                                    "UPDATE share_jobs SET due_at = ?, updated_at = ? "
                                    "WHERE job_id = ? AND status = 'waiting'",
                                    (rescheduled_at, now_text, job_id),
                                )
                    if audit_event is None and rescheduled_at is None:
                        claimed = connection.execute(
                            """
                            UPDATE share_jobs
                            SET status = 'inflight', reserved_at = ?, updated_at = ?
                            WHERE job_id = ? AND status = 'waiting'
                            """,
                            (now_text, now_text, job_id),
                        )
                        if claimed.rowcount:
                            job = dict(row)
                            job["reserved_at"] = now_text

            if audit_event is not None:
                self._audit(audit_event[0], **audit_event[1])
                return
            if rescheduled_at is not None:
                self._schedule_job(job_id, rescheduled_at)
                return
        if job is None:
            return

        try:
            share = json.loads(str(job["payload_json"]))
            if not isinstance(share, Mapping):
                raise ValueError("payload_json must be an object")
            messages = render_share_messages(share)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._finish_job(job, SendResult("failed", type(exc).__name__, 0))
            return

        transport = self.transports[str(job["transport"])]
        target = str(job["target"])
        self._wait_for_immediate_interval(job)
        attempts = 0
        sent_count = 0
        for message in messages:
            result = transport.send(target, message)
            attempts += result.attempts
            if result.status == "sent":
                sent_count += 1
                continue
            if sent_count:
                result = SendResult(
                    "unknown",
                    f"partial_{result.status}:{result.detail}",
                    attempts,
                )
            break
        else:
            result = SendResult("sent", "ok", attempts)
        self._finish_job(job, result)
        self._last_delivery[str(job["destination_id"])] = (
            self._monotonic(),
            bool(job["bypass"]),
        )

    def _wait_for_immediate_interval(self, job: Mapping[str, Any]) -> None:
        interval = self.settings.policy.delivery.immediate_interval_seconds
        if interval <= 0:
            return
        destination_id = str(job["destination_id"])
        previous = self._last_delivery.get(destination_id)
        if previous is None:
            return
        previous_at, previous_was_immediate = previous
        if not (bool(job["bypass"]) or previous_was_immediate):
            return
        remaining = interval - (self._monotonic() - previous_at)
        if remaining > 0:
            self._sleep(remaining)

    def _finish_job(self, job: Mapping[str, Any], result: SendResult) -> None:
        job_id = str(job["job_id"])
        now_text = _isoformat(self._now())
        with self._lock:
            with database_transaction(self.database_path, immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE share_jobs
                    SET status = ?, finished_at = ?, updated_at = ?,
                        result_detail = ?,
                        reserved_at = CASE WHEN ? = 'failed' THEN NULL ELSE reserved_at END
                    WHERE job_id = ? AND status = 'inflight'
                    """,
                    (
                        result.status,
                        now_text,
                        now_text,
                        result.detail,
                        result.status,
                        job_id,
                    ),
                )
            self._audit(
                f"share_{result.status}",
                destination_id=job["destination_id"],
                job_id=job_id,
                score=job["score"],
                attempts=result.attempts,
                detail=result.detail,
            )

    def _audit(self, event: str, **fields: Any) -> None:
        now = self._now()
        record = {"at": _isoformat(now), "event": event, **fields}
        self.sharing_output_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self.sharing_output_dir / f"{now:%Y-%m-%d}.jsonl"
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        audit_path.chmod(0o600)


__all__ = ["SendResult", "ShareDispatcher", "ShareTransport"]
