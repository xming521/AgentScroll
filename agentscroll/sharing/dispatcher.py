"""Persistent, platform-independent share scheduling."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.blocking import BlockingScheduler

from agentscroll.config import ShareDestinationSettings, SharingSettings

from .message import render_share_message


_STATE_VERSION = 1
_MANIFEST_GLOB = "*_即时分享批次.json"
_NORMAL_EVENT_STATUSES = {"inflight", "sent", "unknown"}


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
) -> datetime:
    candidate = now
    ordered = sorted(reservations)
    while True:
        recent = [event_at for event_at in ordered if event_at > candidate - window]
        next_candidate = candidate
        if len(recent) >= limit:
            next_candidate = max(next_candidate, recent[-limit] + window)
        if ordered:
            next_candidate = max(next_candidate, ordered[-1] + min_interval)
        if next_candidate == candidate:
            return candidate
        candidate = next_candidate


class ShareDispatcher:
    """Plan share batches and dispatch due messages through injected transports."""

    def __init__(
        self,
        settings: SharingSettings,
        *,
        transports: Mapping[str, ShareTransport],
        share_output_dir: str | Path | None = None,
        sharing_output_dir: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
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
            destination_id = self._destination_id(destination)
            self._destinations[destination_id] = destination

        self.share_output_dir = (
            Path(share_output_dir).expanduser().resolve()
            if share_output_dir is not None
            else (Path.cwd() / "outputs" / "shares").resolve()
        )
        self.sharing_output_dir = (
            Path(sharing_output_dir).expanduser().resolve()
            if sharing_output_dir is not None
            else self.share_output_dir.parent / "sharing"
        )
        self.state_path = self.sharing_output_dir / "state.json"
        self._now_factory = now or (lambda: datetime.now().astimezone())
        self._window = timedelta(minutes=settings.policy.window_minutes)
        self._min_interval = timedelta(minutes=settings.policy.min_interval_minutes)
        self._lock = RLock()
        self._scheduler: BlockingScheduler | None = None
        self._state: dict[str, Any] = {}

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
        """Attach to the workflow scheduler and restore pending shares."""
        with self._lock:
            if self._scheduler is not None:
                raise RuntimeError("Share dispatcher is already attached")
            self._scheduler = scheduler
            new_state = not self.state_path.is_file()
            self._state = self._initial_state() if new_state else self._load_state()
            self._ensure_configured_destinations()
            if new_state:
                manifests = sorted(self.share_output_dir.glob(_MANIFEST_GLOB))
                if manifests:
                    self._state["last_manifest"] = str(manifests[-1].resolve())
                self._audit("state_initialized", baseline_manifest=bool(manifests))
            else:
                self._reconcile_interrupted_state()
            self._save_state()

        if not new_state:
            cursor_name = Path(str(self._state.get("last_manifest", ""))).name
            for manifest_path in sorted(self.share_output_dir.glob(_MANIFEST_GLOB)):
                if manifest_path.name > cursor_name:
                    self.submit_manifest(manifest_path)

        with self._lock:
            waiting = [
                (destination_id, dict(job))
                for destination_id, destination_state in self._state[
                    "destinations"
                ].items()
                for job in destination_state["pending"]
                if job.get("status") == "waiting"
            ]
        for destination_id, job in waiting:
            self._schedule_job(destination_id, job)

    def submit_manifest(self, manifest_path: str | Path) -> dict[str, Any]:
        """Plan one new share batch for every configured destination."""
        path = Path(manifest_path).expanduser().resolve()
        document = self._load_manifest(path)
        generated_at = _parse_datetime(str(document["generated_at"]))
        expires_at = generated_at + self._window
        now = self._now()
        cursor_name = Path(str(self._state.get("last_manifest", ""))).name
        if path.name <= cursor_name:
            return {"status": "already_processed", "manifest_file": str(path)}

        shares = list(enumerate(document["shares"]))
        shares.sort(key=lambda item: (-float(item[1]["score"]), item[0]))
        bypass = [
            item
            for item in shares
            if float(item[1]["score"]) >= self.settings.policy.bypass_score
        ]
        ordinary = [
            item
            for item in shares
            if float(item[1]["score"]) < self.settings.policy.bypass_score
        ]
        normal = ordinary[: self.settings.policy.max_messages_per_window]

        scheduled_jobs: list[tuple[str, dict[str, Any]]] = []
        removed_job_ids: list[str] = []
        summary = {
            "status": "planned",
            "manifest_file": str(path),
            "destination_count": len(self._destinations),
            "bypass_scheduled": 0,
            "normal_scheduled": 0,
            "dropped": 0,
        }
        with self._lock:
            self._state["last_manifest"] = str(path)
            if expires_at <= now:
                summary["status"] = "expired"
                summary["dropped"] = len(shares) * len(self._destinations)
                self._audit("manifest_expired", manifest=path.name)
                self._save_state()
                return summary

            for destination_id in self._destinations:
                destination_state = self._state["destinations"][destination_id]
                kept_pending: list[dict[str, Any]] = []
                for pending in destination_state["pending"]:
                    if not pending.get("bypass") and pending.get("status") == "waiting":
                        removed_job_ids.append(str(pending["job_id"]))
                        self._audit(
                            "share_dropped_superseded",
                            destination_id=destination_id,
                            job_id=pending["job_id"],
                        )
                    else:
                        kept_pending.append(pending)
                destination_state["pending"] = kept_pending

                reservations = self._normal_event_times(destination_state, now)
                for index, share in bypass:
                    job = self._make_job(
                        destination_id=destination_id,
                        manifest_path=path,
                        share_index=index,
                        score=float(share["score"]),
                        due_at=now,
                        expires_at=expires_at,
                        bypass=True,
                    )
                    destination_state["pending"].append(job)
                    scheduled_jobs.append((destination_id, dict(job)))
                    summary["bypass_scheduled"] += 1

                for index, share in normal:
                    due_at = _earliest_normal_time(
                        now=now,
                        reservations=reservations,
                        window=self._window,
                        limit=self.settings.policy.max_messages_per_window,
                        min_interval=self._min_interval,
                    )
                    if due_at >= expires_at:
                        summary["dropped"] += 1
                        self._audit(
                            "share_dropped_rate_limit",
                            destination_id=destination_id,
                            manifest=path.name,
                            share_index=index,
                        )
                        continue
                    job = self._make_job(
                        destination_id=destination_id,
                        manifest_path=path,
                        share_index=index,
                        score=float(share["score"]),
                        due_at=due_at,
                        expires_at=expires_at,
                        bypass=False,
                    )
                    destination_state["pending"].append(job)
                    scheduled_jobs.append((destination_id, dict(job)))
                    reservations.append(due_at)
                    summary["normal_scheduled"] += 1

                summary["dropped"] += len(ordinary) - len(normal)

            self._audit(
                "manifest_planned",
                manifest=path.name,
                bypass_scheduled=summary["bypass_scheduled"],
                normal_scheduled=summary["normal_scheduled"],
                dropped=summary["dropped"],
            )
            self._save_state()

        for job_id in removed_job_ids:
            self._remove_scheduled_job(job_id)
        for destination_id, job in scheduled_jobs:
            self._schedule_job(destination_id, job)
        return summary

    def _initial_state(self) -> dict[str, Any]:
        return {"version": _STATE_VERSION, "last_manifest": "", "destinations": {}}

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取即时分享状态：{self.state_path}") from exc
        if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
            raise ValueError(f"不支持的即时分享状态：{self.state_path}")
        if not isinstance(state.get("destinations"), dict):
            raise ValueError(f"即时分享状态缺少 destinations：{self.state_path}")
        return state

    def _ensure_configured_destinations(self) -> None:
        destination_states = self._state["destinations"]
        for removed_id in set(destination_states) - set(self._destinations):
            for pending in destination_states[removed_id].get("pending", []):
                self._audit(
                    "share_dropped_destination_removed",
                    destination_id=removed_id,
                    job_id=pending.get("job_id"),
                )
            del destination_states[removed_id]

        for destination_id, destination in self._destinations.items():
            destination_state = destination_states.setdefault(
                destination_id,
                {
                    "transport": destination.transport,
                    "target": destination.target,
                    "normal_events": [],
                    "pending": [],
                },
            )
            if (
                destination_state.get("transport") != destination.transport
                or destination_state.get("target") != destination.target
                or not isinstance(destination_state.get("normal_events"), list)
                or not isinstance(destination_state.get("pending"), list)
            ):
                raise ValueError("即时分享 destination 状态格式错误")

    def _reconcile_interrupted_state(self) -> None:
        now = self._now()
        for destination_id, destination_state in self._state[
            "destinations"
        ].items():
            for event in destination_state["normal_events"]:
                if event.get("status") not in _NORMAL_EVENT_STATUSES:
                    raise ValueError("即时分享普通消息事件状态无效")
                if event["status"] == "inflight":
                    event["status"] = "unknown"
                    self._audit(
                        "share_marked_unknown_after_restart",
                        destination_id=destination_id,
                        job_id=event.get("job_id"),
                    )
            self._normal_event_times(destination_state, now)

            waiting: list[dict[str, Any]] = []
            for pending in destination_state["pending"]:
                if pending.get("status") == "inflight":
                    self._audit(
                        "share_dropped_ambiguous_restart",
                        destination_id=destination_id,
                        job_id=pending.get("job_id"),
                    )
                    continue
                if pending.get("status") != "waiting":
                    raise ValueError("即时分享待发送消息状态无效")
                if _parse_datetime(str(pending["expires_at"])) <= now:
                    self._audit(
                        "share_dropped_expired",
                        destination_id=destination_id,
                        job_id=pending.get("job_id"),
                    )
                    continue
                waiting.append(pending)
            destination_state["pending"] = waiting

    def _normal_event_times(
        self,
        destination_state: dict[str, Any],
        now: datetime,
    ) -> list[datetime]:
        retained: list[dict[str, Any]] = []
        event_times: list[datetime] = []
        for event in destination_state["normal_events"]:
            event_at = _parse_datetime(str(event["at"]))
            if event_at > now - self._window:
                retained.append(event)
                event_times.append(event_at)
        destination_state["normal_events"] = retained
        return event_times

    def _make_job(
        self,
        *,
        destination_id: str,
        manifest_path: Path,
        share_index: int,
        score: float,
        due_at: datetime,
        expires_at: datetime,
        bypass: bool,
    ) -> dict[str, Any]:
        identity = f"{destination_id}|{manifest_path}|{share_index}|{score}|{bypass}"
        digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
        return {
            "job_id": f"sharing-{digest}",
            "manifest_file": str(manifest_path),
            "share_index": share_index,
            "score": score,
            "due_at": _isoformat(due_at),
            "expires_at": _isoformat(expires_at),
            "bypass": bypass,
            "status": "waiting",
        }

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取即时分享批次：{path}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("shares"), list):
            raise ValueError(f"即时分享批次格式错误：{path}")
        try:
            _parse_datetime(str(document["generated_at"]))
            for share in document["shares"]:
                if not isinstance(share, dict) or isinstance(share.get("score"), bool):
                    raise TypeError
                score = float(share["score"])
                if score < 3 or score > 4:
                    raise ValueError
                render_share_message(share)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"即时分享批次字段错误：{path}") from exc
        return document

    def _schedule_job(self, destination_id: str, job: Mapping[str, Any]) -> None:
        if self._scheduler is None:
            raise RuntimeError("Share dispatcher is not attached")
        self._scheduler.add_job(
            self._execute_job,
            "date",
            run_date=_parse_datetime(str(job["due_at"])),
            id=str(job["job_id"]),
            args=(destination_id, str(job["job_id"])),
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

    def _execute_job(self, destination_id: str, job_id: str) -> None:
        rescheduled: dict[str, Any] | None = None
        job: dict[str, Any] | None = None
        with self._lock:
            destination_state = self._state["destinations"].get(destination_id)
            if destination_state is None:
                return
            pending = next(
                (item for item in destination_state["pending"] if item["job_id"] == job_id),
                None,
            )
            if pending is None or pending.get("status") != "waiting":
                return

            now = self._now()
            if _parse_datetime(str(pending["expires_at"])) <= now:
                destination_state["pending"].remove(pending)
                self._audit(
                    "share_dropped_expired",
                    destination_id=destination_id,
                    job_id=job_id,
                )
                self._save_state()
                return

            if not pending["bypass"]:
                reservations = self._normal_event_times(destination_state, now)
                due_at = _earliest_normal_time(
                    now=now,
                    reservations=reservations,
                    window=self._window,
                    limit=self.settings.policy.max_messages_per_window,
                    min_interval=self._min_interval,
                )
                if due_at > now:
                    if due_at >= _parse_datetime(str(pending["expires_at"])):
                        destination_state["pending"].remove(pending)
                        self._audit(
                            "share_dropped_rate_limit",
                            destination_id=destination_id,
                            job_id=job_id,
                        )
                        self._save_state()
                        return
                    pending["due_at"] = _isoformat(due_at)
                    rescheduled = dict(pending)
                    self._save_state()
                else:
                    destination_state["normal_events"].append(
                        {"job_id": job_id, "at": _isoformat(now), "status": "inflight"}
                    )

            if rescheduled is None:
                pending["status"] = "inflight"
                job = dict(pending)
                self._save_state()

        if rescheduled is not None:
            self._schedule_job(destination_id, rescheduled)
            return
        if job is None:
            return

        try:
            manifest = self._load_manifest(Path(str(job["manifest_file"])))
            share = manifest["shares"][int(job["share_index"])]
            message = render_share_message(share)
        except (IndexError, OSError, TypeError, ValueError) as exc:
            self._finish_job(
                destination_id,
                job,
                SendResult("failed", type(exc).__name__, 0),
            )
            return

        destination_state = self._state["destinations"][destination_id]
        transport = self.transports[str(destination_state["transport"])]
        result = transport.send(str(destination_state["target"]), message)
        self._finish_job(destination_id, job, result)

    def _finish_job(
        self,
        destination_id: str,
        job: Mapping[str, Any],
        result: SendResult,
    ) -> None:
        job_id = str(job["job_id"])
        with self._lock:
            destination_state = self._state["destinations"].get(destination_id)
            if destination_state is None:
                return
            destination_state["pending"] = [
                pending
                for pending in destination_state["pending"]
                if pending.get("job_id") != job_id
            ]
            if not job["bypass"]:
                matching_event = next(
                    (
                        event
                        for event in destination_state["normal_events"]
                        if event.get("job_id") == job_id
                    ),
                    None,
                )
                if matching_event is not None:
                    if result.status in {"sent", "unknown"}:
                        matching_event["status"] = result.status
                    else:
                        destination_state["normal_events"].remove(matching_event)
            self._audit(
                f"share_{result.status}",
                destination_id=destination_id,
                job_id=job_id,
                score=job["score"],
                attempts=result.attempts,
                detail=result.detail,
            )
            self._save_state()

    def _save_state(self) -> None:
        self.sharing_output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
        self.state_path.chmod(0o600)

    def _audit(self, event: str, **fields: Any) -> None:
        now = self._now()
        record = {"at": _isoformat(now), "event": event, **fields}
        self.sharing_output_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self.sharing_output_dir / f"{now:%Y-%m-%d}.jsonl"
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        audit_path.chmod(0o600)


__all__ = ["SendResult", "ShareDispatcher", "ShareTransport"]
