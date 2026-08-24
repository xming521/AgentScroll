"""Run hot-list selection and collection workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
                },
                "required": ["representative_id", "related_ids", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def _first_pass_prompt(candidates: list[dict[str, Any]]) -> str:
    payload = json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{HOTLIST_FIRST_PASS_PROMPT.strip()}\n\n"
        f"热榜条目总数：{len(candidates)}\n"
        f"待筛选条目（JSON）：\n{payload}"
    )


def select_hotlist_first_pass(
    hotlist: Mapping[str, Any] | str | Path,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Select topic-level candidates without opening URLs or collecting details."""
    from agentscroll.collector.hotlist import list_hotlist_entries
    from agentscroll.inference_config import (
        build_configured_client,
        load_inference_settings,
        make_configured_request,
    )

    entries = list_hotlist_entries(hotlist)
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(entries, start=1):
        candidate: dict[str, Any] = {
            "id": index,
            "source": str(item.get("source_id") or ""),
            "title": str(item.get("title") or ""),
        }
        candidates.append(candidate)

    settings = load_inference_settings(config_path)
    request = make_configured_request(
        _first_pass_prompt(candidates),
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
    raw_topics = raw_topics[:_FIRST_PASS_MAX_TOPICS]

    selected_ids: set[int] = set()
    topics: list[dict[str, Any]] = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, Mapping):
            raise ValueError("模型返回了无效的话题对象")
        representative_id = raw_topic.get("representative_id")
        related_ids = raw_topic.get("related_ids")
        label = raw_topic.get("label")
        if (
            isinstance(representative_id, bool)
            or not isinstance(representative_id, int)
            or representative_id < 1
            or representative_id > len(entries)
        ):
            raise ValueError(f"模型返回了不存在的代表 ID：{representative_id!r}")
        if not isinstance(related_ids, list) or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > len(entries)
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
        topics.append(
            {
                "representative_id": representative_id,
                "representative": dict(entries[representative_id - 1]),
                "related_ids": related_ids,
                "related": [dict(entries[value - 1]) for value in related_ids],
                "label": label,
            }
        )

    return {
        "input_count": len(entries),
        "topic_count": len(topics),
        "topics": topics,
        "inference": {
            "provider": response.provider,
            "model": response.model,
            "elapsed_s": response.elapsed_s,
        },
    }


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
    return generate_selected_hotlist_knowledge_cards(
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
    )


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
