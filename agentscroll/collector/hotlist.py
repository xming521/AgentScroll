"""Route model-selected hot-list entries to platform detail collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_store import build_knowledge_document
from .newsnow import _title_dedupe_key
from .sources import bilibili, hupu, tieba, zhihu
from .sources.weibo import collect_hot_topic_posts


def _load_hotlist(
    hotlist: Mapping[str, Any] | str | Path,
) -> Mapping[str, Any]:
    if isinstance(hotlist, Mapping):
        return hotlist
    path = Path(hotlist).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("NewsNow 热榜 JSON 根节点必须是 object")
    return payload


def _normalize_selected_titles(selected_titles: str | Iterable[str]) -> list[str]:
    values = (selected_titles,) if isinstance(selected_titles, str) else selected_titles
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        title = " ".join(str(value).split())
        key = _title_dedupe_key(title)
        if not title or key in seen:
            continue
        seen.add(key)
        normalized.append(title)
    if not normalized:
        raise ValueError("至少需要一个模型选中的热榜标题")
    return normalized


def list_hotlist_entries(
    hotlist: Mapping[str, Any] | str | Path,
) -> list[dict[str, Any]]:
    """Return normalized hot-list items in the snapshot's source/rank order."""
    payload = _load_hotlist(hotlist)
    sources = payload.get("sources")
    if not isinstance(sources, Mapping):
        raise ValueError("NewsNow 热榜缺少 sources object")

    entries: list[dict[str, Any]] = []
    for source_id, source in sources.items():
        if not isinstance(source, Mapping):
            continue
        raw_items = source.get("items")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            title = " ".join(str(item.get("title") or "").split())
            if not title:
                continue
            entry = dict(item)
            entry["source_id"] = str(source_id)
            entry["title"] = title
            entries.append(entry)
    if not entries:
        raise ValueError("NewsNow 热榜没有可筛选标题")
    return entries


def _knowledge_sources(
    matched: list[tuple[str, Mapping[str, Any]]],
    details: list[dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    source_results: dict[str, dict[str, Any]] = {}
    for (source_id, _), detail in zip(matched, details):
        result = source_results.setdefault(source_id, {"items": [], "error": None})
        if not isinstance(detail, Mapping):
            result["error"] = "详情 collector 没有返回结果"
            continue
        posts = detail.get("posts")
        if isinstance(posts, list):
            result["items"].extend(
                dict(post) for post in posts if isinstance(post, Mapping)
            )
        error = str(detail.get("error") or "").strip()
        if error:
            previous = str(result.get("error") or "").strip()
            result["error"] = f"{previous}; {error}" if previous else error
    return source_results


def _collect_hotlist_details(
    matched: list[tuple[str, Mapping[str, Any]]],
    *,
    posts_per_topic: int,
) -> tuple[list[dict[str, Any] | None], list[str]]:
    """Collect details for normalized hot-list entries in input order."""
    details: list[dict[str, Any] | None] = [None] * len(matched)
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, (source_id, item) in enumerate(matched):
        grouped.setdefault(source_id, []).append((index, item))

    unsupported_sources: list[str] = []
    for source_id, indexed_items in grouped.items():
        source_items = [item for _, item in indexed_items]
        if source_id == "weibo":
            source_details = collect_hot_topic_posts(
                [str(item.get("title") or "") for item in source_items],
                posts_per_topic=posts_per_topic,
            )
        elif source_id == "hupu":
            source_details = hupu.collect_hotlist_threads(source_items)
        elif source_id == "tieba":
            source_details = tieba.collect_hotlist_threads(
                source_items,
                posts_per_topic=posts_per_topic,
            )
        elif source_id == "zhihu":
            source_details = zhihu.collect_hotlist_threads(source_items)
        elif source_id == "bilibili-hot-search":
            source_details = bilibili.collect_hot_topic_posts(
                [str(item.get("title") or "") for item in source_items],
                posts_per_topic=posts_per_topic,
            )
        else:
            unsupported_sources.append(source_id)
            source_details = [
                {
                    "query": str(item.get("title") or ""),
                    "status": "unsupported",
                    "post_count": 0,
                    "posts": [],
                    "error": f"尚未实现 {source_id} 热榜详情 collector",
                }
                for item in source_items
            ]
        for (index, _), detail in zip(indexed_items, source_details):
            details[index] = detail
    return details, sorted(set(unsupported_sources))


def _topic_entry_ids(
    topic: Mapping[str, Any],
    entries: list[dict[str, Any]],
    *,
    max_entries: int,
) -> list[int]:
    representative_id = topic.get("representative_id")
    related_ids = topic.get("related_ids")
    if (
        isinstance(representative_id, bool)
        or not isinstance(representative_id, int)
        or representative_id < 1
        or representative_id > len(entries)
    ):
        raise ValueError(f"无效的 representative_id：{representative_id!r}")
    if not isinstance(related_ids, list) or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > len(entries)
        for value in related_ids
    ):
        raise ValueError(f"无效的 related_ids：{related_ids!r}")

    candidates = list(dict.fromkeys([representative_id, *related_ids]))
    picked = [representative_id]
    seen_sources = {entries[representative_id - 1]["source_id"]}

    # Prefer a different platform before taking another wording from the same
    # platform. This retains cross-platform context without hard-coding the
    # current snapshot's titles or sources.
    for entry_id in candidates[1:]:
        source_id = entries[entry_id - 1]["source_id"]
        if source_id in seen_sources:
            continue
        picked.append(entry_id)
        seen_sources.add(source_id)
        if len(picked) >= max_entries:
            return picked
    for entry_id in candidates[1:]:
        if entry_id in picked:
            continue
        picked.append(entry_id)
        if len(picked) >= max_entries:
            break
    return picked


def collect_selected_hotlist_evidence(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
    *,
    posts_per_entry: int = 1,
    max_entries_per_topic: int = 3,
) -> dict[str, Any]:
    """Collect compact model-facing evidence for first-pass selected topics.

    Every topic keeps its representative entry. Related entries are selected
    with platform diversity first, then original order, up to the configured
    cap. Collector failures remain in ``attempts`` so the card model can decide
    whether the remaining evidence is sufficient.
    """
    if posts_per_entry <= 0:
        raise ValueError("posts_per_entry 必须大于 0")
    if max_entries_per_topic <= 0:
        raise ValueError("max_entries_per_topic 必须大于 0")
    raw_topics = selection.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("selection 缺少非空 topics 数组")
    filtered_topics = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, Mapping):
            raise ValueError("selection.topics 包含无效话题")
        if str(raw_topic.get("label") or "") in {"conversation", "discussion"}:
            continue
        filtered_topics.append(raw_topic)
    raw_topics = filtered_topics
    if not raw_topics:
        raise ValueError("selection 中没有 news 或 fun 话题")

    entries = list_hotlist_entries(hotlist)
    selected_ids: list[list[int]] = []
    flat_entries: list[tuple[str, Mapping[str, Any]]] = []
    for raw_topic in raw_topics:
        entry_ids = _topic_entry_ids(
            raw_topic,
            entries,
            max_entries=max_entries_per_topic,
        )
        selected_ids.append(entry_ids)
        flat_entries.extend(
            (str(entries[entry_id - 1]["source_id"]), entries[entry_id - 1])
            for entry_id in entry_ids
        )

    flat_details, unsupported_sources = _collect_hotlist_details(
        flat_entries,
        posts_per_topic=posts_per_entry,
    )
    topics: list[dict[str, Any]] = []
    offset = 0
    for raw_topic, entry_ids in zip(raw_topics, selected_ids):
        count = len(entry_ids)
        matched = flat_entries[offset : offset + count]
        details = flat_details[offset : offset + count]
        offset += count
        representative_id = entry_ids[0]
        representative = entries[representative_id - 1]
        source_results = _knowledge_sources(matched, details)
        compact = build_knowledge_document(
            {
                "topic": representative["title"],
                "routing": {"selection": "first-pass-topic"},
                "sources": source_results,
            }
        )
        attempts: list[dict[str, Any]] = []
        for entry_id, (source_id, item), detail in zip(entry_ids, matched, details):
            detail = detail if isinstance(detail, Mapping) else {}
            attempts.append(
                {
                    "entry_id": entry_id,
                    "source": source_id,
                    "query": str(item.get("title") or ""),
                    "status": str(detail.get("status") or "unavailable"),
                    "post_count": len(detail.get("posts") or []),
                    "error": str(detail.get("error") or "").strip(),
                }
            )
        topics.append(
            {
                "topic_id": representative_id,
                "title": str(representative.get("title") or ""),
                "label": str(raw_topic.get("label") or ""),
                "related_titles": [
                    str(entries[entry_id - 1].get("title") or "")
                    for entry_id in raw_topic.get("related_ids") or []
                ],
                "collection_status": "readable" if compact["items"] else "unavailable",
                "attempts": attempts,
                "evidence": compact["items"],
            }
        )

    return {
        "provider": "agentscroll-hotlist-evidence",
        "topic_count": len(topics),
        "posts_per_entry": posts_per_entry,
        "max_entries_per_topic": max_entries_per_topic,
        "unsupported_sources": unsupported_sources,
        "topics": topics,
    }


def open_selected_weibo_hotlists(
    hotlist: Mapping[str, Any] | str | Path,
    selected_titles: str | Iterable[str],
    *,
    posts_per_topic: int = 1,
) -> dict[str, Any]:
    """Read posts/comments only for selected Weibo titles; never write files.

    The NewsNow desktop search URL is retained as provenance but is not opened.
    Its title is resolved through ``mcp-server-weibo`` to concrete feed IDs,
    readable post bodies, and the first public comment page.
    """
    if posts_per_topic <= 0:
        raise ValueError("posts_per_topic 必须大于 0")
    payload = _load_hotlist(hotlist)
    selected = _normalize_selected_titles(selected_titles)
    sources = payload.get("sources")
    if not isinstance(sources, Mapping):
        raise ValueError("NewsNow 热榜缺少 sources object")
    weibo_source = sources.get("weibo")
    if not isinstance(weibo_source, Mapping):
        raise ValueError("NewsNow 热榜中没有 weibo source")
    raw_items = weibo_source.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("NewsNow weibo.items 不是数组")

    by_title = {
        _title_dedupe_key(str(item.get("title") or "")): item
        for item in raw_items
        if isinstance(item, Mapping) and item.get("title")
    }
    matched_items: list[Mapping[str, Any]] = []
    missing_titles: list[str] = []
    for selected_title in selected:
        item = by_title.get(_title_dedupe_key(selected_title))
        if item is None:
            missing_titles.append(selected_title)
        else:
            matched_items.append(item)

    details = collect_hot_topic_posts(
        [str(item.get("title") or "") for item in matched_items],
        posts_per_topic=posts_per_topic,
    )
    opened_items = [
        {
            "hotlist_item": dict(item),
            "detail": detail,
        }
        for item, detail in zip(matched_items, details)
    ]
    readable_count = sum(detail.get("status") == "readable" for detail in details)
    if not matched_items:
        status = "empty"
    elif readable_count == len(matched_items) and not missing_titles:
        status = "success"
    elif readable_count:
        status = "partial"
    else:
        status = "unavailable"
    return {
        "provider": "mcp-server-weibo",
        "source_id": "weibo",
        "status": status,
        "selected_title_count": len(selected),
        "matched_title_count": len(matched_items),
        "readable_title_count": readable_count,
        "posts_per_topic": posts_per_topic,
        "missing_titles": missing_titles,
        "items": opened_items,
    }
