"""Run hot-list selection and collection workflows."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscroll.prompts.hotlist import HOTLIST_FIRST_PASS_PROMPT

_FIRST_PASS_LABELS = {"news", "fun"}
_FIRST_PASS_MAX_TOPICS = 15
_TITLE_CACHE_FILENAME = "hotlist_title_cache.txt"
_title_cache_lock = threading.Lock()

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


def _title_cache_path(hotlist: Mapping[str, Any] | str | Path) -> Path:
    snapshot_file: str | Path | None = None
    if isinstance(hotlist, (str, Path)):
        snapshot_file = hotlist
    elif isinstance(hotlist.get("snapshot_file"), (str, Path)):
        snapshot_file = hotlist["snapshot_file"]
    if snapshot_file:
        return Path(snapshot_file).expanduser().resolve().parent / _TITLE_CACHE_FILENAME
    return (Path.cwd() / "outputs" / "hotlists" / _TITLE_CACHE_FILENAME).resolve()


def _cached_title_keys(path: Path) -> set[str]:
    from agentscroll.collector.newsnow import _title_dedupe_key

    if not path.is_file():
        return set()
    return {
        _title_dedupe_key(title)
        for title in path.read_text(encoding="utf-8").splitlines()
        if title.strip()
    }


def _cache_titles(path: Path, titles: list[str]) -> None:
    from agentscroll.collector.newsnow import _title_dedupe_key

    normalized_titles = [" ".join(title.split()) for title in titles if title.strip()]
    with _title_cache_lock:
        existing_titles = (
            [
                title
                for title in path.read_text(encoding="utf-8").splitlines()
                if title.strip()
            ]
            if path.is_file()
            else []
        )
        cached_keys = {_title_dedupe_key(title) for title in existing_titles}
        for title in normalized_titles:
            key = _title_dedupe_key(title)
            if key in cached_keys:
                continue
            cached_keys.add(key)
            existing_titles.append(title)

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text("\n".join(existing_titles) + "\n", encoding="utf-8")
        temporary.replace(path)


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

    entries = list_hotlist_entries(hotlist)
    cache_path = _title_cache_path(hotlist)
    cached_title_keys = _cached_title_keys(cache_path)
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(entries, start=1):
        title = str(item.get("title") or "")
        if _title_dedupe_key(title) in cached_title_keys:
            continue
        candidate: dict[str, Any] = {
            "id": index,
            "source": str(item.get("source_id") or ""),
            "title": title,
        }
        candidates.append(candidate)

    cached_count = len(entries) - len(candidates)
    if not candidates:
        return {
            "input_count": len(entries),
            "cached_count": cached_count,
            "candidate_count": 0,
            "topic_count": 0,
            "topics": [],
            "cache_file": str(cache_path),
            "inference": {
                "skipped": True,
                "reason": "no_new_titles",
            },
        }

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

    _cache_titles(cache_path, [candidate["title"] for candidate in candidates])

    if not response.ok:
        raise RuntimeError(f"热榜第一轮粗筛失败：{response.error or 'unknown error'}")
    if not isinstance(response.parsed_json, dict):
        raise ValueError("模型返回值不是包含 topics 的 JSON object")
    raw_topics = response.parsed_json.get("topics")
    if not isinstance(raw_topics, list):
        raise ValueError("模型返回值缺少 topics 数组")
    raw_topics = raw_topics[:_FIRST_PASS_MAX_TOPICS]

    candidate_ids = {candidate["id"] for candidate in candidates}
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

        if entries[representative_id - 1]["source_id"] == "zhihu":
            non_zhihu_representative = next(
                (
                    entry_id
                    for entry_id in related_ids
                    if entries[entry_id - 1]["source_id"] != "zhihu"
                ),
                None,
            )
            if non_zhihu_representative is not None:
                representative_id = non_zhihu_representative
                related_ids = [
                    entry_id
                    for entry_id in topic_ids
                    if entry_id != representative_id
                ]

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
        "cached_count": cached_count,
        "candidate_count": len(candidates),
        "topic_count": len(topics),
        "topics": topics,
        "cache_file": str(cache_path),
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
                "related_titles": [
                    str(item.get("title") or "")
                    for item in related
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
        "cache_file": selection.get("cache_file"),
        "inference": dict(selection.get("inference") or {}),
        "items": items,
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
    )
    result["selection_file"] = str(selection_file)
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
