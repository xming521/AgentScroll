"""Route model-selected hot-list entries to platform detail collectors."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .knowledge_artifacts import build_knowledge_document
from .newsnow import _title_dedupe_key
from .sources import bilibili, dates, hupu, tieba
from .sources.weibo import collect_hot_topic_posts

_RECENT_POST_DAYS = 7
_QUERY_SEARCH_SOURCES = ("weibo",)


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
    from_date: str,
    to_date: str,
) -> tuple[list[dict[str, Any] | None], list[str]]:
    """Collect platform groups concurrently and preserve input order."""
    details: list[dict[str, Any] | None] = [None] * len(matched)
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, (source_id, item) in enumerate(matched):
        grouped.setdefault(source_id, []).append((index, item))

    unsupported_sources: list[str] = []
    if not grouped:
        return details, unsupported_sources

    def collect_source(
        source_id: str,
        indexed_items: list[tuple[int, Mapping[str, Any]]],
    ) -> tuple[list[dict[str, Any]], bool]:
        source_items = [item for _, item in indexed_items]
        if source_id == "weibo":
            source_details = collect_hot_topic_posts(
                [str(item.get("title") or "") for item in source_items],
                posts_per_topic=posts_per_topic,
                from_date=from_date,
                to_date=to_date,
                require_known_date=True,
            )
        elif source_id == "hupu":
            source_details = hupu.collect_hotlist_threads(source_items)
        elif source_id == "tieba":
            source_details = tieba.collect_hotlist_threads(
                source_items,
                posts_per_topic=posts_per_topic,
            )
        elif source_id == "bilibili-hot-search":
            source_details = bilibili.collect_hot_topic_posts(
                [str(item.get("title") or "") for item in source_items],
                posts_per_topic=posts_per_topic,
                from_date=from_date,
                to_date=to_date,
                require_known_date=True,
            )
        else:
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
            return source_details, False
        return source_details, True

    with ThreadPoolExecutor(max_workers=min(len(grouped), 5)) as executor:
        futures = {
            executor.submit(collect_source, source_id, indexed_items): (
                source_id,
                indexed_items,
            )
            for source_id, indexed_items in grouped.items()
        }
        for future in as_completed(futures):
            source_id, indexed_items = futures[future]
            source_details, supported = future.result()
            if not supported:
                unsupported_sources.append(source_id)
            for (index, _), detail in zip(indexed_items, source_details):
                details[index] = detail
    return details, sorted(set(unsupported_sources))


def _platform_diverse_entry_ids(
    candidate_ids: list[int],
    entries: list[dict[str, Any]],
) -> list[int]:
    picked: list[int] = []
    seen_sources: set[str] = set()
    for entry_id in candidate_ids:
        source_id = entries[entry_id - 1]["source_id"]
        if source_id in seen_sources:
            continue
        picked.append(entry_id)
        seen_sources.add(source_id)
    picked.extend(entry_id for entry_id in candidate_ids if entry_id not in picked)
    return picked


def _topic_entry_candidates(
    topic: Mapping[str, Any],
    entries: list[dict[str, Any]],
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

    candidates = [
        entry_id
        for entry_id in dict.fromkeys([representative_id, *related_ids])
        if entries[entry_id - 1]["source_id"] != "zhihu"
    ]
    return _platform_diverse_entry_ids(candidates, entries)


def _snapshot_date_range(payload: Mapping[str, Any]) -> tuple[str, str]:
    collected_at = dates.parse_date(str(payload.get("collected_at") or ""))
    end_date = (
        collected_at.date() if collected_at is not None else datetime.now(dates.CST).date()
    )
    start_date = end_date - timedelta(days=_RECENT_POST_DAYS - 1)
    return start_date.isoformat(), end_date.isoformat()


def _post_dedupe_key(post: Mapping[str, Any]) -> str:
    for field in ("url", "content_url"):
        value = str(post.get(field) or "").strip()
        if value:
            return value
    return ""


def _filter_recent_unique_detail(
    detail: Mapping[str, Any] | None,
    *,
    from_date: str,
    to_date: str,
    seen_post_keys: set[str],
) -> Mapping[str, Any] | None:
    if not isinstance(detail, Mapping):
        return None
    filtered = dict(detail)
    raw_posts = detail.get("posts")
    if not isinstance(raw_posts, list):
        return filtered

    start = dates.parse_date(from_date)
    end = dates.parse_date(to_date)
    if start is None or end is None:
        raise ValueError(f"无效的帖子日期范围：{from_date!r} 至 {to_date!r}")
    retained: list[dict[str, Any]] = []
    for raw_post in raw_posts:
        if not isinstance(raw_post, Mapping):
            continue
        published_at = dates.parse_date(str(raw_post.get("date") or ""))
        if (
            published_at is None
            or published_at.date() < start.date()
            or published_at.date() > end.date()
        ):
            continue
        key = _post_dedupe_key(raw_post)
        if key and key in seen_post_keys:
            continue
        if key:
            seen_post_keys.add(key)
        retained.append(dict(raw_post))

    filtered["posts"] = retained
    filtered["post_count"] = len(retained)
    if retained:
        filtered["status"] = "readable"
    elif raw_posts:
        filtered["status"] = "filtered"
        filtered["error"] = (
            f"没有发布时间可确认且位于 {from_date} 至 {to_date} 的帖子"
        )
    return filtered


def _topic_search_queries(
    topic: Mapping[str, Any],
    entries: list[dict[str, Any]],
) -> list[str]:
    entry_ids = [topic.get("representative_id"), *(topic.get("related_ids") or [])]
    only_zhihu = all(
        isinstance(entry_id, int)
        and not isinstance(entry_id, bool)
        and 1 <= entry_id <= len(entries)
        and entries[entry_id - 1]["source_id"] == "zhihu"
        for entry_id in entry_ids
    )
    if not only_zhihu:
        queries: list[str] = []
        for entry_id in entry_ids:
            if (
                isinstance(entry_id, int)
                and not isinstance(entry_id, bool)
                and 1 <= entry_id <= len(entries)
                and entries[entry_id - 1]["source_id"] != "zhihu"
            ):
                query = " ".join(str(entries[entry_id - 1].get("title") or "").split())
                if query and query not in queries:
                    queries.append(query)
        return queries
    raw_queries = topic.get("search_queries")
    if not isinstance(raw_queries, list):
        raise ValueError("仅含知乎线索的话题缺少 LLM 生成的 search_queries")
    queries: list[str] = []
    for value in raw_queries:
        query = " ".join(str(value).split())
        if query and query not in queries:
            queries.append(query)
    if not 2 <= len(queries) <= 3:
        raise ValueError("仅含知乎线索的话题必须提供 2 至 3 个不同检索词")
    return queries


def _detail_is_readable(
    source_id: str,
    item: Mapping[str, Any],
    detail: Mapping[str, Any] | None,
) -> bool:
    source_results = _knowledge_sources(
        [(source_id, item)],
        [dict(detail) if isinstance(detail, Mapping) else None],
    )
    compact = build_knowledge_document(
        {"topic": item.get("title"), "sources": source_results}
    )
    return bool(compact["items"])


def collect_selected_hotlist_evidence(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
    *,
    posts_per_entry: int = 1,
    max_entries_per_topic: int = 3,
) -> dict[str, Any]:
    """Collect compact evidence and diagnostics for first-pass selected topics.

    The representative entry names the topic but does not have to be collected
    first. The configured cap is the target number of readable posts; failed
    candidates are replaced from the remaining related entries when possible.
    Collector failures remain in ``attempts`` for saved diagnostics.
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

    hotlist_payload = _load_hotlist(hotlist)
    entries = list_hotlist_entries(hotlist_payload)
    from_date, to_date = _snapshot_date_range(hotlist_payload)
    candidate_specs: list[
        list[tuple[int | None, str, Mapping[str, Any]]]
    ] = []
    for raw_topic in raw_topics:
        topic_candidates = [
            (
                entry_id,
                str(entries[entry_id - 1]["source_id"]),
                entries[entry_id - 1],
            )
            for entry_id in _topic_entry_candidates(raw_topic, entries)
        ]
        direct_query_keys = {
            (source_id, _title_dedupe_key(str(item.get("title") or "")))
            for _, source_id, item in topic_candidates
        }
        for query in _topic_search_queries(raw_topic, entries):
            topic_candidates.extend(
                (None, source_id, {"title": query, "generated_search": True})
                for source_id in _QUERY_SEARCH_SOURCES
                if (source_id, _title_dedupe_key(query)) not in direct_query_keys
            )
        candidate_specs.append(topic_candidates)

    attempted: list[
        list[
            tuple[
                int | None,
                str,
                Mapping[str, Any],
                Mapping[str, Any] | None,
                bool,
            ]
        ]
    ] = [[] for _ in raw_topics]
    unsupported_sources: set[str] = set()
    next_candidate = [0 for _ in raw_topics]
    readable_counts = [0 for _ in raw_topics]
    seen_post_keys = [set() for _ in raw_topics]

    while True:
        batch_candidates: list[
            list[tuple[int | None, str, Mapping[str, Any]]]
        ] = []
        flat_entries: list[tuple[str, Mapping[str, Any]]] = []
        posts_per_search = posts_per_entry
        for topic_index, topic_candidates in enumerate(candidate_specs):
            missing = max(0, max_entries_per_topic - readable_counts[topic_index])
            start = next_candidate[topic_index]
            selected = topic_candidates[start : start + missing]
            if selected:
                posts_per_search = max(
                    posts_per_search, (missing + len(selected) - 1) // len(selected)
                )
            next_candidate[topic_index] += len(selected)
            batch_candidates.append(selected)
            flat_entries.extend((source_id, item) for _, source_id, item in selected)
        if not flat_entries:
            break

        flat_details, batch_unsupported = _collect_hotlist_details(
            flat_entries,
            posts_per_topic=posts_per_search,
            from_date=from_date,
            to_date=to_date,
        )
        unsupported_sources.update(batch_unsupported)
        offset = 0
        for topic_index, selected_candidates in enumerate(batch_candidates):
            for entry_id, source_id, item in selected_candidates:
                raw_detail = flat_details[offset]
                offset += 1
                detail = _filter_recent_unique_detail(
                    raw_detail if isinstance(raw_detail, Mapping) else None,
                    from_date=from_date,
                    to_date=to_date,
                    seen_post_keys=seen_post_keys[topic_index],
                )
                readable = _detail_is_readable(source_id, item, detail)
                attempted[topic_index].append(
                    (entry_id, source_id, item, detail, readable)
                )
                if readable:
                    readable_counts[topic_index] += len(
                        build_knowledge_document({
                            "topic": "",
                            "sources": _knowledge_sources([(source_id, item)], [detail]),
                        })["items"]
                    )

    topics: list[dict[str, Any]] = []
    for raw_topic, topic_attempts in zip(raw_topics, attempted):
        representative_id = int(raw_topic["representative_id"])
        representative = entries[representative_id - 1]
        retained = [attempt for attempt in topic_attempts if attempt[4]][
            :max_entries_per_topic
        ]
        matched = [(attempt[1], attempt[2]) for attempt in retained]
        details = [attempt[3] for attempt in retained]
        source_results = _knowledge_sources(matched, details)
        compact = build_knowledge_document(
            {
                "topic": "",
                "from_date": from_date,
                "to_date": to_date,
                "routing": {"selection": "first-pass-topic"},
                "sources": source_results,
            }
        )
        display_title = str(representative.get("title") or "").strip()
        attempts: list[dict[str, Any]] = []
        for entry_id, source_id, item, detail, _ in topic_attempts:
            detail = detail if isinstance(detail, Mapping) else {}
            attempt = {
                "source": source_id,
                "query": str(item.get("title") or ""),
                "status": str(detail.get("status") or "unavailable"),
                "post_count": len(detail.get("posts") or []),
                "error": str(detail.get("error") or "").strip(),
            }
            if entry_id is not None:
                attempt["entry_id"] = entry_id
            else:
                attempt["generated_search"] = True
            attempts.append(attempt)
        topic = {
            "topic_id": representative_id,
            "title": display_title,
            "label": str(raw_topic.get("label") or ""),
            "event_relation": str(raw_topic.get("event_relation") or "new"),
            "matched_event_id": str(raw_topic.get("matched_event_id") or ""),
            "hotlist_title_count": 1 + len(raw_topic.get("related_ids") or []),
            "related_titles": [
                str(entries[entry_id - 1].get("title") or "")
                for entry_id in raw_topic.get("related_ids") or []
                if entries[entry_id - 1].get("source_id") != "zhihu"
            ],
            "attempts": attempts,
            "evidence": compact["items"][:max_entries_per_topic],
        }
        candidate_keywords = list(
            raw_topic.get("candidate_interest_keywords") or []
        )
        if candidate_keywords:
            topic["candidate_interest_keywords"] = candidate_keywords
        topics.append(topic)

    return {
        "provider": "agentscroll-hotlist-evidence",
        "topic_count": len(topics),
        "posts_per_entry": posts_per_entry,
        "max_entries_per_topic": max_entries_per_topic,
        "date_range": {"from": from_date, "to": to_date},
        "unsupported_sources": sorted(unsupported_sources),
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
