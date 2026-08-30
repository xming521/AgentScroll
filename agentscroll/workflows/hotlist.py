"""Run hot-list selection and collection workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscroll.prompts.hotlist import HOTLIST_FIRST_PASS_PROMPT

_FIRST_PASS_LABELS = {"news", "fun"}
_FIRST_PASS_MAX_TOPICS = 15

_HOTLIST_FIRST_PASS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "maxItems": _FIRST_PASS_MAX_TOPICS,
            "items": {
                "type": "object",
                "properties": {
                    "representative_id": {"type": "integer"},
                    "related_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "label": {"type": "string", "enum": sorted(_FIRST_PASS_LABELS)},
                    "relation": {"type": "string", "enum": ["new", "update"]},
                    "history_id": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}]
                    },
                },
                "required": [
                    "representative_id",
                    "related_ids",
                    "label",
                    "relation",
                    "history_id",
                ],
                "additionalProperties": False,
            },
        },
        "seen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "representative_id": {"type": "integer"},
                    "related_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "label": {"type": "string", "enum": sorted(_FIRST_PASS_LABELS)},
                    "history_id": {"type": "integer"},
                },
                "required": [
                    "representative_id",
                    "related_ids",
                    "label",
                    "history_id",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topics", "seen"],
    "additionalProperties": False,
}


def _first_pass_prompt(candidates: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"candidates": candidates},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{HOTLIST_FIRST_PASS_PROMPT.strip()}\n\n"
        f"热榜条目总数：{len(candidates)}\n"
        f"待筛选条目及本地召回的历史标题（JSON）：\n{payload}"
    )


def select_hotlist_first_pass(
    hotlist: Mapping[str, Any] | str | Path,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Select topic-level candidates without opening URLs or collecting details."""
    from agentscroll.collector.hotlist import list_hotlist_entries
    from agentscroll.collector.newsnow import _title_dedupe_key
    from agentscroll.inference_config import (
        build_configured_client,
        load_inference_settings,
        make_configured_request,
    )
    from .hotlist_history import (
        active_exact_title_keys,
        attach_history_matches,
        history_path,
        load_history,
        order_candidates_by_similarity,
        record_first_pass,
        reference_time,
    )

    entries = list_hotlist_entries(hotlist)
    state_path = history_path(hotlist)
    evaluated_at = reference_time(hotlist)
    history_state = load_history(state_path)
    cached_title_keys = active_exact_title_keys(history_state, at=evaluated_at)
    candidates: list[dict[str, Any]] = []
    exact_titles: list[str] = []
    for index, item in enumerate(entries, start=1):
        title = str(item.get("title") or "")
        if _title_dedupe_key(title) in cached_title_keys:
            exact_titles.append(title)
            continue
        candidate: dict[str, Any] = {
            "id": index,
            "source": str(item.get("source_id") or ""),
            "title": title,
        }
        candidates.append(candidate)

    cached_count = len(entries) - len(candidates)
    if not candidates:
        record_first_pass(
            state_path,
            history_state,
            exact_titles=exact_titles,
            ignored_titles=[],
            seen_topics=[],
            at=evaluated_at,
        )
        return {
            "input_count": len(entries),
            "cached_count": cached_count,
            "candidate_count": 0,
            "topic_count": 0,
            "seen_count": 0,
            "topics": [],
            "seen_topics": [],
            "history_file": str(state_path),
            "history_event_count": len(history_state.get("events") or []),
            "history_match_count": 0,
            "history_prompt_chars": 0,
            "inference": {
                "skipped": True,
                "reason": "no_new_titles",
            },
        }

    candidate_payloads, history_lookup, history_stats = (
        attach_history_matches(
            candidates,
            history_state,
            at=evaluated_at,
        )
    )
    candidate_payloads = order_candidates_by_similarity(candidate_payloads)
    settings = load_inference_settings(config_path)
    request = make_configured_request(
        _first_pass_prompt(candidate_payloads),
        settings,
        json_schema=_HOTLIST_FIRST_PASS_SCHEMA,
        timeout=300,
    )
    client = build_configured_client(settings)
    try:
        response = client.generate(request)
    finally:
        client.close()

    if not response.ok:
        raise RuntimeError(f"热榜第一轮粗筛失败：{response.error or 'unknown error'}")
    if not isinstance(response.parsed_json, dict):
        raise ValueError("模型返回值不是包含 topics 的 JSON object")
    raw_topics = response.parsed_json.get("topics")
    if not isinstance(raw_topics, list):
        raise ValueError("模型返回值缺少 topics 数组")
    raw_seen_topics = response.parsed_json.get("seen")
    if not isinstance(raw_seen_topics, list):
        raise ValueError("模型返回值缺少 seen 数组")
    raw_topics = raw_topics[:_FIRST_PASS_MAX_TOPICS]

    candidate_ids = {candidate["id"] for candidate in candidates}
    candidates_by_id = {candidate["id"]: candidate for candidate in candidate_payloads}
    selected_ids: set[int] = set()
    matched_event_ids: set[str] = set()
    topics: list[dict[str, Any]] = []
    seen_topics: list[dict[str, Any]] = []

    def validated_identity(
        raw_topic: Any,
    ) -> tuple[int, list[int], str, list[int]]:
        if not isinstance(raw_topic, Mapping):
            raise ValueError("模型返回了无效的话题对象")
        representative_id = raw_topic.get("representative_id")
        related_ids = raw_topic.get("related_ids")
        label = raw_topic.get("label")
        if (
            isinstance(representative_id, bool)
            or not isinstance(representative_id, int)
            or representative_id not in candidate_ids
        ):
            raise ValueError(f"模型返回了不存在的代表 ID：{representative_id!r}")
        if not isinstance(related_ids, list) or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in candidate_ids
            for value in related_ids
        ):
            raise ValueError(f"模型返回了无效的 related_ids：{related_ids!r}")
        topic_ids = [representative_id, *related_ids]
        if len(topic_ids) != len(set(topic_ids)) or selected_ids.intersection(
            topic_ids
        ):
            raise ValueError(f"模型重复使用了热榜 ID：{topic_ids!r}")
        if not isinstance(label, str) or label not in _FIRST_PASS_LABELS:
            raise ValueError(f"模型返回了无效的 label：{label!r}")
        selected_ids.update(topic_ids)
        return representative_id, list(related_ids), label, topic_ids

    def validated_history(
        raw_history_id: Any,
        *,
        topic_ids: list[int],
    ) -> dict[str, Any]:
        if isinstance(raw_history_id, bool) or not isinstance(raw_history_id, int):
            raise ValueError(f"模型返回了无效的 history_id：{raw_history_id!r}")
        history_entries = [
            history_entry
            for entry_id in topic_ids
            for history_entry in candidates_by_id[entry_id].get("history") or []
            if isinstance(history_entry, Mapping)
        ]
        allowed_history_ids = {
            history_entry.get("history_id") for history_entry in history_entries
        }
        if raw_history_id not in allowed_history_ids:
            raise ValueError(
                f"history_id {raw_history_id!r} 不属于当前话题的本地召回结果"
            )
        matched = history_lookup.get(raw_history_id)
        if matched is None:
            raise ValueError(f"history_id {raw_history_id!r} 不存在")
        matched_title = next(
            str(history_entry.get("title") or "")
            for history_entry in history_entries
            if history_entry.get("history_id") == raw_history_id
        )
        return {**matched, "title": matched_title}

    def claim_matched_event(matched: Mapping[str, Any]) -> None:
        event_id = str(matched["event_id"])
        if event_id in matched_event_ids:
            raise ValueError(f"模型把同一历史事件拆成了多个话题：{event_id}")
        matched_event_ids.add(event_id)

    def mapped_topic(
        representative_id: int,
        related_ids: list[int],
        label: str,
        *,
        relation: str,
        matched: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        topic = {
            "representative_id": representative_id,
            "representative": dict(entries[representative_id - 1]),
            "related_ids": related_ids,
            "related": [dict(entries[value - 1]) for value in related_ids],
            "label": label,
            "event_relation": relation,
        }
        if matched is not None:
            topic["matched_event_id"] = matched["event_id"]
            topic["matched_history_title"] = matched["title"]
        return topic

    for raw_topic in raw_topics:
        representative_id, related_ids, label, topic_ids = validated_identity(raw_topic)
        relation = raw_topic.get("relation")
        raw_history_id = raw_topic.get("history_id")
        if relation == "new":
            if raw_history_id is not None:
                raise ValueError("new 话题的 history_id 必须为 null")
            matched = None
        elif relation == "update":
            matched = validated_history(
                raw_history_id,
                topic_ids=topic_ids,
            )
            claim_matched_event(matched)
        else:
            raise ValueError(f"模型返回了无效的 relation：{relation!r}")
        topics.append(
            mapped_topic(
                representative_id,
                related_ids,
                label,
                relation=relation,
                matched=matched,
            )
        )

    for raw_topic in raw_seen_topics:
        representative_id, related_ids, label, topic_ids = validated_identity(raw_topic)
        matched = validated_history(
            raw_topic.get("history_id"),
            topic_ids=topic_ids,
        )
        claim_matched_event(matched)
        seen_topics.append(
            mapped_topic(
                representative_id,
                related_ids,
                label,
                relation="seen",
                matched=matched,
            )
        )

    ignored_titles = [
        candidate["title"]
        for candidate in candidates
        if candidate["id"] not in selected_ids
    ]
    record_first_pass(
        state_path,
        history_state,
        exact_titles=exact_titles,
        ignored_titles=ignored_titles,
        seen_topics=seen_topics,
        at=evaluated_at,
    )

    return {
        "input_count": len(entries),
        "cached_count": cached_count,
        "candidate_count": len(candidates),
        "topic_count": len(topics),
        "seen_count": len(seen_topics),
        "topics": topics,
        "seen_topics": seen_topics,
        "history_file": str(state_path),
        "history_event_count": history_stats["active_event_count"],
        "history_match_count": history_stats["history_match_count"],
        "history_prompt_chars": history_stats["history_prompt_chars"],
        "inference": {
            "provider": response.provider,
            "model": response.model,
            "elapsed_s": response.elapsed_s,
        },
    }


def _save_first_pass_selection(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
    *,
    output_dir: str | Path | None,
) -> Path:
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "outputs" / "knowledge").resolve()
    )
    generated_at = datetime.now().astimezone()
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S-%f%z")
    raw_topics = selection.get("topics") or []
    items = []
    for topic in raw_topics:
        if not isinstance(topic, Mapping):
            continue
        representative = topic.get("representative")
        related = topic.get("related") or []
        if not isinstance(representative, Mapping):
            continue
        items.append(
            {
                "title": str(representative.get("title") or ""),
                "source": str(representative.get("source_id") or ""),
                "label": str(topic.get("label") or ""),
                "relation": str(topic.get("event_relation") or "new"),
                "matched_history_title": str(
                    topic.get("matched_history_title") or ""
                ),
                "related_titles": [
                    str(item.get("title") or "")
                    for item in related
                    if isinstance(item, Mapping)
                ],
            }
        )

    seen_items = []
    for topic in selection.get("seen_topics") or []:
        if not isinstance(topic, Mapping):
            continue
        representative = topic.get("representative")
        if not isinstance(representative, Mapping):
            continue
        seen_items.append(
            {
                "title": str(representative.get("title") or ""),
                "source": str(representative.get("source_id") or ""),
                "label": str(topic.get("label") or ""),
                "matched_history_title": str(
                    topic.get("matched_history_title") or ""
                ),
                "related_titles": [
                    str(item.get("title") or "")
                    for item in topic.get("related") or []
                    if isinstance(item, Mapping)
                ],
            }
        )

    snapshot_file = None
    if isinstance(hotlist, (str, Path)):
        snapshot_file = str(Path(hotlist).expanduser().resolve())
    document = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "snapshot_file": snapshot_file,
        "input_count": selection.get("input_count"),
        "cached_count": selection.get("cached_count"),
        "candidate_count": selection.get("candidate_count"),
        "topic_count": len(items),
        "seen_count": len(seen_items),
        "history_file": selection.get("history_file"),
        "history_event_count": selection.get("history_event_count"),
        "history_match_count": selection.get("history_match_count"),
        "history_prompt_chars": selection.get("history_prompt_chars"),
        "inference": dict(selection.get("inference") or {}),
        "items": items,
        "seen_items": seen_items,
    }
    path = destination / f"{timestamp}_热榜标题筛选结果.json"
    temporary = path.with_suffix(".json.tmp")
    destination.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def learn_hotlist_snapshot(
    hotlist: Mapping[str, Any] | str | Path,
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    posts_per_entry: int = 1,
    max_entries_per_topic: int = 3,
    supplement_failed: bool = True,
    generation_effort: str = "xhigh",
    supplement_effort: str = "xhigh",
) -> dict[str, Any]:
    """Select topics from one snapshot and complete the learning workflow."""
    from .knowledge_card import generate_selected_hotlist_knowledge_cards

    selection = select_hotlist_first_pass(hotlist, config_path=config_path)
    selection_file = _save_first_pass_selection(
        hotlist,
        selection,
        output_dir=output_dir,
    )
    result = generate_selected_hotlist_knowledge_cards(
        hotlist,
        selection,
        config_path=config_path,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        posts_per_entry=posts_per_entry,
        max_entries_per_topic=max_entries_per_topic,
        supplement_failed=supplement_failed,
        generation_effort=generation_effort,
        supplement_effort=supplement_effort,
        record_history=True,
    )
    result["selection_file"] = str(selection_file)
    result["history_file"] = str(selection["history_file"])
    result["seen_count"] = int(selection.get("seen_count") or 0)
    return result


def fetch_and_learn_hotlists(
    groups: str | tuple[str, ...] = "综合",
    *,
    base_url: str | None = None,
    latest: bool = False,
    per_source_limit: int | None = None,
    timeout: int = 15,
    snapshot_output_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    posts_per_entry: int = 1,
    max_entries_per_topic: int = 3,
    supplement_failed: bool = True,
    generation_effort: str = "xhigh",
    supplement_effort: str = "xhigh",
) -> dict[str, Any]:
    """Fetch a snapshot and immediately complete the learning workflow."""
    from agentscroll.collector import fetch_newsnow_hotlists

    fetched = fetch_newsnow_hotlists(
        groups,
        base_url=base_url,
        latest=latest,
        per_source_limit=per_source_limit,
        timeout=timeout,
        output_dir=snapshot_output_dir,
        save=True,
    )
    if not fetched["total_items"]:
        raise RuntimeError("NewsNow 没有返回可学习的热榜条目")
    snapshot = fetched.get("snapshot_file")
    if not snapshot:
        raise RuntimeError("NewsNow 热榜快照未保存")

    learned = learn_hotlist_snapshot(
        snapshot,
        config_path=config_path,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        posts_per_entry=posts_per_entry,
        max_entries_per_topic=max_entries_per_topic,
        supplement_failed=supplement_failed,
        generation_effort=generation_effort,
        supplement_effort=supplement_effort,
    )
    return {"fetch": fetched, "learn": learned}


__all__ = [
    "fetch_and_learn_hotlists",
    "learn_hotlist_snapshot",
    "select_hotlist_first_pass",
]
