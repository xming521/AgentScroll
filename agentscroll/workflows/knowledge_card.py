"""Generate model-learning knowledge cards from collected hot-list evidence."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentscroll.prompts.knowledge_card import (
    KNOWLEDGE_CARD_LABEL_PROMPTS,
    KNOWLEDGE_CARD_PROMPT,
    KNOWLEDGE_CARD_RESEARCH_PROMPT,
)
from agentscroll.prompts.hotlist import ZHIHU_SEARCH_QUERY_PROMPT

_LABEL_ALIASES = {
    "news": "news",
    "breaking": "news",
    "major": "news",
    "fun": "fun",
    "meme": "fun",
}
_REMOVED_LABELS = {"conversation", "discussion"}
_SHARE_LABELS = {"news", "fun"}
_SHARE_SCORE_THRESHOLD = 7
_STATUSES = {"complete", "needs_research", "rejected"}
_write_lock = threading.Lock()

_ZHIHU_SEARCH_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "topic_index": {"type": "integer", "minimum": 1},
                    "search_queries": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {"type": "string", "maxLength": 30},
                    },
                },
                "required": ["topic_index", "search_queries"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def _card_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": sorted(_STATUSES),
                        },
                        "rejection_reason": {
                            "type": "string",
                            "maxLength": 120,
                        },
                        "knowledge": {"type": "string", "maxLength": 260},
                        "chat_context": {"type": "string", "maxLength": 180},
                        "share_score": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 10,
                        },
                        "share": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "text": {
                                            "type": "string",
                                            "maxLength": 180,
                                        },
                                        "source_id": {
                                            "type": "string",
                                            "maxLength": 40,
                                        },
                                        "comment_id": {
                                            "type": "string",
                                            "maxLength": 60,
                                        },
                                        "generated_comment": {
                                            "type": "string",
                                            "maxLength": 80,
                                        },
                                    },
                                    "required": [
                                        "text",
                                        "source_id",
                                        "comment_id",
                                        "generated_comment",
                                    ],
                                    "additionalProperties": False,
                                },
                                {"type": "null"},
                            ],
                        },
                    },
                    "required": [
                        "status",
                        "rejection_reason",
                        "knowledge",
                        "chat_context",
                        "share_score",
                        "share",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cards"],
        "additionalProperties": False,
    }


def _research_card_schema() -> dict[str, Any]:
    schema = _card_schema()
    item_schema = schema["properties"]["cards"]["items"]
    item_schema["properties"]["research_sources"] = {
        "type": "array",
        "maxItems": 3,
        "items": {"type": "string", "maxLength": 20},
    }
    item_schema["required"].append("research_sources")
    return schema


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def _normalize_label(value: Any) -> str:
    raw_label = str(value or "")
    label = _LABEL_ALIASES.get(raw_label)
    if label is None:
        raise ValueError(f"无效的 label：{raw_label!r}")
    return label


def _selection_with_zhihu_search_queries(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
    *,
    config_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate one batch of non-Zhihu search queries from Zhihu clues."""
    from agentscroll.collector.hotlist import list_hotlist_entries
    from agentscroll.inference_config import (
        build_configured_client,
        load_inference_settings,
        make_configured_request,
    )

    raw_topics = selection.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("selection 缺少非空 topics 数组")
    entries = list_hotlist_entries(hotlist)
    query_inputs: list[dict[str, Any]] = []
    normalized_topics: list[dict[str, Any]] = []
    for topic_index, raw_topic in enumerate(raw_topics, start=1):
        if not isinstance(raw_topic, Mapping):
            raise ValueError("selection.topics 包含无效话题")
        representative_id = raw_topic.get("representative_id")
        related_ids = raw_topic.get("related_ids")
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

        topic_ids = list(dict.fromkeys([representative_id, *related_ids]))
        non_zhihu_ids = [
            entry_id
            for entry_id in topic_ids
            if entries[entry_id - 1]["source_id"] != "zhihu"
        ]
        if entries[representative_id - 1]["source_id"] == "zhihu" and non_zhihu_ids:
            representative_id = non_zhihu_ids[0]
            related_ids = [
                entry_id for entry_id in topic_ids if entry_id != representative_id
            ]

        topic = dict(raw_topic)
        topic["representative_id"] = representative_id
        topic["representative"] = dict(entries[representative_id - 1])
        topic["related_ids"] = related_ids
        topic["related"] = [dict(entries[entry_id - 1]) for entry_id in related_ids]
        normalized_topics.append(topic)

        if not non_zhihu_ids:
            zhihu_titles = [
                str(entries[entry_id - 1].get("title") or "").strip()
                for entry_id in topic_ids
            ]
            query_inputs.append(
                {"topic_index": topic_index, "zhihu_titles": zhihu_titles}
            )

    if not query_inputs:
        enriched_selection = dict(selection)
        enriched_selection["topics"] = normalized_topics
        return enriched_selection, {}

    payload = json.dumps(query_inputs, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        f"{ZHIHU_SEARCH_QUERY_PROMPT.strip()}\n\n"
        f"待转换的知乎线索（JSON）：\n{payload}"
    )
    settings = load_inference_settings(config_path)
    request = make_configured_request(
        prompt,
        settings,
        json_schema=_ZHIHU_SEARCH_QUERY_SCHEMA,
        timeout=300,
    )
    client = build_configured_client(settings)
    started_at = time.monotonic()
    try:
        response = client.generate(request)
    finally:
        client.close()
    elapsed_s = round(time.monotonic() - started_at, 3)

    if not response.ok:
        raise RuntimeError(
            f"知乎线索检索词生成失败：{response.error or 'unknown error'}"
        )
    if not isinstance(response.parsed_json, Mapping):
        raise ValueError("检索词模型返回值不是包含 topics 的 JSON object")
    raw_query_topics = response.parsed_json.get("topics")
    if not isinstance(raw_query_topics, list):
        raise ValueError("检索词模型返回值缺少 topics 数组")

    expected_indices = {item["topic_index"] for item in query_inputs}
    queries_by_index: dict[int, list[str]] = {}
    for raw_query_topic in raw_query_topics:
        if not isinstance(raw_query_topic, Mapping):
            raise ValueError("检索词模型返回了无效的话题对象")
        topic_index = raw_query_topic.get("topic_index")
        raw_queries = raw_query_topic.get("search_queries")
        if (
            isinstance(topic_index, bool)
            or not isinstance(topic_index, int)
            or topic_index not in expected_indices
            or topic_index in queries_by_index
        ):
            raise ValueError(f"检索词模型返回了无效的话题序号：{topic_index!r}")
        if not isinstance(raw_queries, list):
            raise ValueError(f"话题 {topic_index} 缺少 search_queries 数组")
        queries: list[str] = []
        for value in raw_queries:
            query = " ".join(str(value).split())
            if query and query not in queries:
                queries.append(query)
        if not 2 <= len(queries) <= 3:
            raise ValueError(f"话题 {topic_index} 必须返回 2 至 3 个不同检索词")
        queries_by_index[topic_index] = queries
    if set(queries_by_index) != expected_indices:
        missing = sorted(expected_indices - set(queries_by_index))
        raise ValueError(f"检索词模型遗漏话题序号：{missing}")

    enriched_topics = []
    for topic_index, normalized_topic in enumerate(normalized_topics, start=1):
        topic = dict(normalized_topic)
        topic["search_queries"] = queries_by_index.get(topic_index, [])
        enriched_topics.append(topic)
    enriched_selection = dict(selection)
    enriched_selection["topics"] = enriched_topics
    inference = {
        "provider": response.provider,
        "model": response.model,
        "elapsed_s": elapsed_s,
        "usage": dict(response.metadata.get("usage") or {}),
        "request_count": 1,
        "topic_count": len(query_inputs),
        "prompt_chars": len(prompt),
    }
    enriched_selection["search_query_inference"] = inference
    return enriched_selection, inference


def _prompt_payload(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_topics = evidence.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("evidence 缺少非空 topics 数组")
    if len(raw_topics) > 20:
        raise ValueError("单次知识卡生成任务最多处理 20 个话题")

    topics: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, Mapping):
            raise ValueError("evidence.topics 包含无效话题")
        if str(raw_topic.get("label") or "") in _REMOVED_LABELS:
            continue
        topic_id = raw_topic.get("topic_id")
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 0:
            raise ValueError(f"无效的 topic_id：{topic_id!r}")
        if topic_id in seen_ids:
            raise ValueError(f"重复的 topic_id：{topic_id}")
        seen_ids.add(topic_id)
        label = _normalize_label(raw_topic.get("label"))

        compact_evidence: list[dict[str, Any]] = []
        for evidence_index, raw_item in enumerate(
            raw_topic.get("evidence") or [], start=1
        ):
            if not isinstance(raw_item, Mapping):
                continue
            source_id = f"e{evidence_index}"
            comments = []
            for comment_index, comment in enumerate(
                raw_item.get("comments") or [], start=1
            ):
                if not isinstance(comment, Mapping):
                    continue
                text = str(comment.get("text") or "").strip()
                if not text:
                    continue
                comments.append(
                    {
                        "comment_id": f"e{evidence_index}c{comment_index}",
                        "text": text,
                    }
                )
            compact_evidence.append(
                {
                    "source_id": source_id,
                    "platform": str(raw_item.get("platform") or ""),
                    "source_title": str(raw_item.get("title") or ""),
                    "published_at": raw_item.get("published_at"),
                    "url": _http_url(raw_item.get("url")),
                    "content": str(raw_item.get("content") or ""),
                    "comments": comments,
                }
            )
        topics.append(
            {
                "topic_id": topic_id,
                "title": str(raw_topic.get("title") or ""),
                "label": label,
                "evidence": compact_evidence,
            }
        )
    if not topics:
        raise ValueError("evidence 中没有 news 或 fun 话题")
    return topics


def _model_topic_payload(topic: Mapping[str, Any], *, research: bool) -> dict[str, Any]:
    topic_title = str(topic.get("title") or "")
    evidence = []
    for item in topic.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        url = _http_url(item.get("url"))
        model_item: dict[str, Any] = {
            "platform": str(item.get("platform") or ""),
            "published_at": item.get("published_at"),
            "content": str(item.get("content") or ""),
            "comments": [],
        }
        if url:
            model_item["source_id"] = str(item.get("source_id") or "")
        for comment in item.get("comments") or []:
            if not isinstance(comment, Mapping):
                continue
            model_comment = {
                "comment_id": str(comment.get("comment_id") or ""),
                "text": str(comment.get("text") or ""),
            }
            model_item["comments"].append(model_comment)
        evidence.append(model_item)

    payload: dict[str, Any] = {
        "title": topic_title,
        "label": str(topic.get("label") or ""),
        "evidence": evidence,
    }
    if research:
        search_results = []
        for result in topic.get("public_search_results") or []:
            if not isinstance(result, Mapping):
                continue
            model_result = {
                "title": str(result.get("title") or ""),
                "snippet": str(result.get("snippet") or ""),
            }
            if _http_url(result.get("url")):
                model_result["source_id"] = str(result.get("source_id") or "")
            search_results.append(model_result)
        payload["public_search_results"] = search_results
    return payload


def _knowledge_card_prompt(topic: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _model_topic_payload(topic, research=False),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    evaluated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    label_prompt = KNOWLEDGE_CARD_LABEL_PROMPTS[_normalize_label(topic.get("label"))]
    return (
        f"{KNOWLEDGE_CARD_PROMPT.strip()}\n\n"
        f"{label_prompt.strip()}\n\n"
        f"当前评估时间：{evaluated_at}\n"
        f"待生成知识卡的证据（JSON）：\n{payload}"
    )


def _knowledge_card_research_prompt(topic: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _model_topic_payload(topic, research=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    evaluated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    label_prompt = KNOWLEDGE_CARD_LABEL_PROMPTS[_normalize_label(topic.get("label"))]
    return (
        f"{KNOWLEDGE_CARD_RESEARCH_PROMPT.strip()}\n\n"
        f"{label_prompt.strip()}\n\n"
        f"当前评估时间：{evaluated_at}\n"
        f"待补搜材料（JSON）：\n{payload}"
    )


def _sum_response_usage(responses: list[Any]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for response in responses:
        raw_usage = response.metadata.get("usage") or {}
        if not isinstance(raw_usage, Mapping):
            continue
        for key, value in raw_usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage[str(key)] = usage.get(str(key), 0) + value
    return usage


def _needs_research_card(
    topic: Mapping[str, Any],
    *,
    research: bool = False,
) -> dict[str, Any]:
    card = {
        "topic_id": topic["topic_id"],
        "status": "needs_research",
        "rejection_reason": "",
        "knowledge": "",
        "chat_context": "",
        "share_score": 0,
        "share": None,
    }
    if research:
        card["research_sources"] = []
    return card


def _generate_topic_cards(
    topics: list[dict[str, Any]],
    *,
    settings: Any,
    research: bool,
    max_tokens: int,
    timeout: int,
    effort: str,
    minimum_evidence_count: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cards_by_id: dict[int, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    model_topics = topics
    if not research:
        model_topics = []
        for topic in topics:
            if len(topic.get("evidence") or []) >= minimum_evidence_count:
                model_topics.append(topic)
                continue
            cards_by_id[topic["topic_id"]] = _needs_research_card(topic)

    if not model_topics:
        return [cards_by_id[topic["topic_id"]] for topic in topics], {
            "provider": settings.provider,
            "model": settings.model,
            "elapsed_s": 0.0,
            "usage": {},
            "request_count": 0,
            "max_workers": 0,
            "effort": effort,
            "prompt_chars": 0,
            "failed_topic_count": 0,
            "failed_topics": [],
        }

    from agentscroll.inference_config import (
        build_configured_client,
        make_configured_request,
    )

    prompt_builder = (
        _knowledge_card_research_prompt if research else _knowledge_card_prompt
    )
    schema_builder = _research_card_schema if research else _card_schema
    prompts = [prompt_builder(topic) for topic in model_topics]
    requests = [
        make_configured_request(
            prompt,
            settings,
            json_schema=schema_builder(),
            max_tokens=max_tokens,
            timeout=timeout,
            effort=effort,
        )
        for topic, prompt in zip(model_topics, prompts, strict=True)
    ]

    client = build_configured_client(settings)
    started_at = time.monotonic()
    try:
        responses = client.generate_batch(requests)
    finally:
        client.close()
    elapsed_s = round(time.monotonic() - started_at, 3)

    if len(responses) != len(model_topics):
        raise RuntimeError(
            "知识卡并发请求返回数量异常："
            f"期望 {len(model_topics)}，实际 {len(responses)}"
        )

    validator = _validate_research_cards if research else _validate_cards
    for topic, response in zip(model_topics, responses, strict=True):
        topic_id = topic["topic_id"]
        if not response.ok:
            failure = response.error or "unknown error"
        elif not isinstance(response.parsed_json, dict):
            failure = "返回值不是包含 cards 的 JSON object"
        else:
            try:
                cards_by_id[topic_id] = validator(
                    response.parsed_json.get("cards"),
                    [topic],
                )[0]
                continue
            except (TypeError, ValueError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
        failures.append({"topic_id": topic_id, "error": failure})
        cards_by_id[topic_id] = _needs_research_card(
            topic,
            research=research,
        )

    first_response = responses[0]
    inference = {
        "provider": first_response.provider,
        "model": first_response.model,
        "elapsed_s": elapsed_s,
        "usage": _sum_response_usage(responses),
        "request_count": len(requests),
        "max_workers": min(settings.max_workers, len(requests)),
        "effort": effort,
        "prompt_chars": sum(len(prompt) for prompt in prompts),
        "failed_topic_count": len(failures),
        "failed_topics": failures,
    }
    return [cards_by_id[topic["topic_id"]] for topic in topics], inference


def _research_search_queries(topic: Mapping[str, Any]) -> list[str]:
    title = str(topic.get("title") or "").strip()
    queries = [title]
    if str(topic.get("label") or "") == "fun":
        latin = re.findall(
            r"(?i)(?<![a-z0-9])[a-z][a-z0-9._+-]*(?![a-z0-9])",
            title,
        )
        numbers = re.findall(r"(?<!\d)\d+(?:\.\d+)?%?(?!\d)", title)
        if latin and numbers:
            query = f"{latin[0]} {numbers[0]} 是什么梗"
            if query not in queries:
                queries.append(query)
    return queries


def _collect_public_search_results(
    topics: list[dict[str, Any]],
    *,
    max_workers: int,
    limit: int = 3,
) -> tuple[dict[int, list[dict[str, str]]], int]:
    from agentscroll.collector.sources import web_search

    def search_topic(topic: dict[str, Any]) -> tuple[int, list[dict[str, str]], int]:
        results: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        request_count = 0
        for query in _research_search_queries(topic):
            request_count += 1
            try:
                candidates = web_search.search_bing(query, limit=limit)
            except Exception:
                request_count += 1
                try:
                    candidates = web_search.search_duckduckgo(query, limit=limit)
                except Exception:
                    candidates = []
            for candidate in candidates:
                url = str(candidate.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append(
                    {
                        "source_id": f"s{len(results) + 1}",
                        "query": query,
                        "title": str(candidate.get("title") or "")[:160],
                        "snippet": str(candidate.get("snippet") or "")[:300],
                        "url": url,
                    }
                )
                if len(results) >= limit:
                    return topic["topic_id"], results, request_count
        return topic["topic_id"], results, request_count

    by_id: dict[int, list[dict[str, str]]] = {}
    total_requests = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(topics))) as executor:
        futures = [executor.submit(search_topic, topic) for topic in topics]
        for future in as_completed(futures):
            topic_id, results, request_count = future.result()
            by_id[topic_id] = results
            total_requests += request_count
    return by_id, total_requests


def _validate_share(
    raw_share: Any,
    *,
    score: Any,
    status: str,
    topic: Mapping[str, Any],
) -> dict[str, Any] | None:
    topic_id = topic["topic_id"]
    if (
        isinstance(score, bool)
        or not isinstance(score, int)
        or not 0 <= score <= 10
    ):
        raise ValueError(f"话题 {topic_id} 的 share_score 不是 0 至 10 的整数")
    ready = score >= _SHARE_SCORE_THRESHOLD

    sources = [
        *list(topic.get("evidence") or []),
        *list(topic.get("public_search_results") or []),
    ]
    has_share_source = any(
        isinstance(item, Mapping)
        and item.get("source_id")
        and _http_url(item.get("url"))
        for item in sources
    )
    if status != "complete" and score != 0:
        raise ValueError(f"非完整话题 {topic_id} 的 share_score 必须为 0")
    if not has_share_source and score != 0:
        raise ValueError(f"话题 {topic_id} 没有可用分享来源，share_score 必须为 0")

    if not ready:
        if raw_share is not None:
            raise ValueError(
                f"话题 {topic_id} 的 share_score 低于 "
                f"{_SHARE_SCORE_THRESHOLD} 时 share 必须为 null"
            )
        return None

    if not isinstance(raw_share, Mapping):
        raise ValueError(f"话题 {topic_id} 的高分 share 缺少有效对象")
    text = str(raw_share.get("text") or "").strip()
    source_id = str(raw_share.get("source_id") or "").strip()
    comment_id = str(raw_share.get("comment_id") or "").strip()
    generated_comment = str(raw_share.get("generated_comment") or "").strip()
    if not generated_comment and raw_share.get("comment_type") == "generated":
        generated_comment = str(raw_share.get("comment") or "").strip()

    if status != "complete":
        raise ValueError(f"待补搜话题 {topic_id} 不能进入即时分享队列")
    if topic.get("label") not in _SHARE_LABELS:
        raise ValueError(f"话题 {topic_id} 的 label 不允许即时分享")
    if not text or not source_id:
        raise ValueError(f"可分享话题 {topic_id} 缺少分享内容或来源")
    if bool(comment_id) == bool(generated_comment):
        raise ValueError(f"可分享话题 {topic_id} 必须且只能选择真实评论或自拟评论之一")

    source = next(
        (
            item
            for item in sources
            if isinstance(item, Mapping) and item.get("source_id") == source_id
        ),
        None,
    )
    if source is None:
        raise ValueError(f"话题 {topic_id} 返回了未知 source_id：{source_id!r}")
    url = _http_url(source.get("url"))
    if not url:
        raise ValueError(f"话题 {topic_id} 选择的分享来源没有有效 URL")

    if comment_id:
        comments = {
            str(comment.get("comment_id") or ""): str(comment.get("text") or "").strip()
            for item in topic.get("evidence") or []
            if isinstance(item, Mapping)
            for comment in item.get("comments") or []
            if isinstance(comment, Mapping) and comment.get("comment_id")
        }
        comment = comments.get(comment_id, "")
        if not comment:
            raise ValueError(
                f"话题 {topic_id} 返回了未知 comment_id：{comment_id!r}"
            )
        comment_type = "platform"
    else:
        comment = generated_comment
        comment_type = "generated"

    return {
        "text": text,
        "source_id": source_id,
        "url": url,
        "comment_id": comment_id,
        "comment_type": comment_type,
        "comment": comment,
    }


def _validate_cards(
    raw_cards: Any,
    topics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_cards, list):
        raise ValueError("模型返回值缺少 cards 数组")
    topic_by_id = {topic["topic_id"]: topic for topic in topics}
    card_by_id: dict[int, dict[str, Any]] = {}
    for raw_card in raw_cards:
        if not isinstance(raw_card, Mapping):
            raise ValueError("模型返回了无效知识卡")
        topic_id = raw_card.get("topic_id")
        if topic_id is None and len(topics) == 1:
            topic_id = topics[0]["topic_id"]
        if topic_id not in topic_by_id or topic_id in card_by_id:
            raise ValueError(f"模型返回了无效或重复 topic_id：{topic_id!r}")
        status = raw_card.get("status")
        rejection_reason = str(raw_card.get("rejection_reason") or "").strip()
        knowledge = str(raw_card.get("knowledge") or "").strip()
        chat_context = str(raw_card.get("chat_context") or "").strip()
        if status not in _STATUSES:
            raise ValueError(f"模型返回了无效 status：{status!r}")
        if status == "complete":
            if not knowledge or not chat_context or rejection_reason:
                raise ValueError(
                    f"完整知识卡 {topic_id} 的字段状态不一致"
                )
        elif status == "needs_research":
            if rejection_reason or knowledge or chat_context:
                raise ValueError(f"待补搜知识卡 {topic_id} 的字段状态不一致")
        elif not rejection_reason or knowledge or chat_context:
            raise ValueError(f"已淘汰知识卡 {topic_id} 的字段状态不一致")
        card = dict(raw_card)
        card["topic_id"] = topic_id
        card["rejection_reason"] = rejection_reason
        card["chat_context"] = chat_context
        card["share_score"] = raw_card.get("share_score")
        card["share"] = _validate_share(
            raw_card.get("share"),
            score=card["share_score"],
            status=str(status),
            topic=topic_by_id[topic_id],
        )
        card_by_id[topic_id] = card
    missing_ids = set(topic_by_id) - set(card_by_id)
    if missing_ids:
        raise ValueError(f"模型漏掉 topic_id：{sorted(missing_ids)}")
    return [card_by_id[topic["topic_id"]] for topic in topics]


def _validate_research_cards(
    raw_cards: Any,
    topics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cards = _validate_cards(raw_cards, topics)
    for card in cards:
        source_ids = card.get("research_sources")
        if not isinstance(source_ids, list):
            raise TypeError(f"补搜知识卡 {card['topic_id']} 缺少 research_sources 数组")
        topic = next(
            topic for topic in topics if topic["topic_id"] == card["topic_id"]
        )
        available_sources = {
            str(source.get("source_id") or ""): source
            for source in [
                *list(topic.get("evidence") or []),
                *list(topic.get("public_search_results") or []),
            ]
            if isinstance(source, Mapping) and source.get("source_id")
        }
        normalized_sources = []
        for source_id in source_ids:
            source = available_sources.get(str(source_id))
            if source is None:
                raise ValueError(
                    f"补搜知识卡 {card['topic_id']} 返回了未知来源：{source_id!r}"
                )
            url = _http_url(source.get("url"))
            if not url:
                raise ValueError(
                    f"补搜知识卡 {card['topic_id']} 选择的来源没有有效 URL"
                )
            title = str(
                source.get("source_title")
                or source.get("title")
                or source.get("platform")
                or source_id
            ).strip()
            normalized_sources.append({"title": title, "url": url})
        card["research_sources"] = normalized_sources
    return cards


def _render_card_text(card: Mapping[str, Any]) -> str:
    share = card.get("share")
    lines = [
        f"话题: {card['title']}",
        f"类别: {card['label']}",
        f"状态: {card['status']}",
        f"分享评分: {card.get('share_score', 0)}/10",
    ]
    if card.get("knowledge"):
        lines.extend(("", str(card["knowledge"])))
    if card.get("chat_context"):
        lines.extend(("", f"聊天语境: {card['chat_context']}"))
    if card.get("rejection_reason"):
        lines.extend(("", f"淘汰原因: {card['rejection_reason']}"))
    if isinstance(share, Mapping):
        comment_label = (
            "网友评论" if share.get("comment_type") == "platform" else "我的评论"
        )
        lines.extend(
            (
                "\n即时分享:",
                str(share["text"]),
                str(share["url"]),
                f"{comment_label}：{share['comment']}",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _share_documents(
    cards: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    evidence_by_id = {
        topic["topic_id"]: topic
        for topic in evidence.get("topics") or []
        if isinstance(topic, Mapping)
    }
    shares = []
    for card in cards:
        share = card.get("share")
        if not isinstance(share, Mapping):
            continue
        topic = evidence_by_id[card["topic_id"]]
        shares.append(
            {
                "topic_id": card["topic_id"],
                "title": str(topic.get("title") or ""),
                "label": _normalize_label(topic.get("label")),
                "score": card["share_score"],
                "text": share["text"],
                "url": share["url"],
                "comment": share["comment"],
                "comment_type": share["comment_type"],
                "source_id": share["source_id"],
                "comment_id": share["comment_id"],
            }
        )
    return shares


def _render_share_text(share: Mapping[str, Any]) -> str:
    comment_label = (
        "网友评论" if share.get("comment_type") == "platform" else "我的评论"
    )
    return "\n".join(
        (
            str(share["text"]),
            str(share["url"]),
            f"{comment_label}：{share['comment']}",
        )
    )


def _save_share_batch(
    cards: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "outputs" / "shares").resolve()
    )
    generated_at = generated_at or datetime.now().astimezone()
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S-%f%z")
    shares = _share_documents(cards, evidence=evidence)
    document = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "share_count": len(shares),
        "inference": dict(inference),
        "shares": shares,
    }
    json_path = destination / f"{timestamp}_即时分享批次.json"
    text_path = destination / f"{timestamp}_即时分享批次.txt"
    serialized_text = "\n\n=====\n\n".join(
        _render_share_text(share) for share in shares
    )
    if serialized_text:
        serialized_text += "\n"
    with _write_lock:
        destination.mkdir(parents=True, exist_ok=True)
        json_tmp = json_path.with_suffix(".json.tmp")
        text_tmp = text_path.with_suffix(".txt.tmp")
        json_tmp.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        text_tmp.write_text(serialized_text, encoding="utf-8")
        json_tmp.replace(json_path)
        text_tmp.replace(text_path)
    return {
        "share_count": len(shares),
        "share_manifest_file": str(json_path),
        "share_text_file": str(text_path),
    }


def _save_cards(
    cards: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    share_output_dir: str | Path | None = None,
    save_share_queue: bool = True,
    batch_name: str = "热榜知识卡批次",
) -> dict[str, Any]:
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "outputs" / "knowledge").resolve()
    )
    generated_at = datetime.now().astimezone()
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S-%f%z")
    evidence_by_id = {
        topic["topic_id"]: topic
        for topic in evidence.get("topics") or []
        if isinstance(topic, Mapping)
    }
    documents: list[dict[str, Any]] = []
    batch_text: list[str] = []
    for card in cards:
        topic = evidence_by_id[card["topic_id"]]
        document = {
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "topic_id": card["topic_id"],
            "title": topic["title"],
            "label": _normalize_label(topic.get("label")),
            "status": card["status"],
            "rejection_reason": card["rejection_reason"],
            "knowledge": card["knowledge"],
            "chat_context": card["chat_context"],
            "share_score": card["share_score"],
            "share": card["share"],
            "research_sources": card.get("research_sources") or [],
            "evidence": topic.get("evidence") or [],
            "collection_attempts": topic.get("attempts") or [],
        }
        documents.append(document)
        batch_text.append(_render_card_text(document).rstrip())

    status_counts = {
        status: sum(card["status"] == status for card in cards)
        for status in sorted(_STATUSES)
    }
    batch = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "topic_count": len(cards),
        "status_counts": status_counts,
        "inference": dict(inference),
        "cards": documents,
    }
    batch_stem = f"{timestamp}_{batch_name}"
    batch_json_path = destination / f"{batch_stem}.json"
    batch_text_path = destination / f"{batch_stem}.txt"
    batch_json_tmp = batch_json_path.with_suffix(".json.tmp")
    batch_text_tmp = batch_text_path.with_suffix(".txt.tmp")
    with _write_lock:
        destination.mkdir(parents=True, exist_ok=True)
        batch_json_tmp.write_text(
            json.dumps(batch, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        batch_text_tmp.write_text(
            "\n\n=====\n\n".join(batch_text) + "\n",
            encoding="utf-8",
        )
        batch_json_tmp.replace(batch_json_path)
        batch_text_tmp.replace(batch_text_path)
    files = {
        "batch_json_file": str(batch_json_path),
        "batch_text_file": str(batch_text_path),
    }
    if save_share_queue:
        files.update(
            _save_share_batch(
                cards,
                evidence=evidence,
                inference=inference,
                output_dir=share_output_dir,
                generated_at=generated_at,
            )
        )
    return files


def _generate_hotlist_knowledge_cards(
    evidence: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
    effort: str = "xhigh",
) -> dict[str, Any]:
    from agentscroll.inference_config import load_inference_settings

    topics = _prompt_payload(evidence)
    settings = load_inference_settings(config_path)
    minimum_evidence_count = evidence.get("max_entries_per_topic", 1)
    if (
        isinstance(minimum_evidence_count, bool)
        or not isinstance(minimum_evidence_count, int)
        or minimum_evidence_count <= 0
    ):
        raise ValueError("evidence.max_entries_per_topic 必须是正整数")
    cards, inference = _generate_topic_cards(
        topics,
        settings=settings,
        research=False,
        max_tokens=12_000,
        timeout=600,
        effort=effort,
        minimum_evidence_count=minimum_evidence_count,
    )
    return {
        "topic_count": len(cards),
        "complete_count": sum(card["status"] == "complete" for card in cards),
        "needs_research_count": sum(
            card["status"] == "needs_research" for card in cards
        ),
        "rejected_count": sum(card["status"] == "rejected" for card in cards),
        "cards": cards,
        "inference": inference,
    }


def generate_hotlist_knowledge_cards(
    evidence: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    save_share_queue: bool = True,
    effort: str = "xhigh",
) -> dict[str, Any]:
    """Generate and save cards for sufficiently collected topics."""
    result = _generate_hotlist_knowledge_cards(
        evidence,
        config_path=config_path,
        effort=effort,
    )
    files = _save_cards(
        result["cards"],
        evidence=evidence,
        inference=result["inference"],
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        save_share_queue=save_share_queue,
    )
    return {**result, **files}


def generate_selected_hotlist_knowledge_cards(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
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
    """Collect evidence, generate cards, then batch-search only failed topics."""
    raw_topics = selection.get("topics")
    if isinstance(raw_topics, list) and not raw_topics:
        selection_inference = selection.get("inference")
        return {
            "status": "no_topics_selected",
            "topic_count": 0,
            "complete_count": 0,
            "needs_research_count": 0,
            "rejected_count": 0,
            "share_count": 0,
            "cards": [],
            "inference": (
                dict(selection_inference)
                if isinstance(selection_inference, Mapping)
                else {}
            ),
        }

    enriched_selection, search_query_inference = (
        _selection_with_zhihu_search_queries(
            hotlist,
            selection,
            config_path=config_path,
        )
    )

    from agentscroll.collector.hotlist import collect_selected_hotlist_evidence

    evidence = collect_selected_hotlist_evidence(
        hotlist,
        enriched_selection,
        posts_per_entry=posts_per_entry,
        max_entries_per_topic=max_entries_per_topic,
    )
    initial_result = _generate_hotlist_knowledge_cards(
        evidence,
        config_path=config_path,
        effort=generation_effort,
    )
    if not supplement_failed or not initial_result["needs_research_count"]:
        initial_result.update(
            _save_cards(
                initial_result["cards"],
                evidence=evidence,
                inference=initial_result["inference"],
                output_dir=output_dir,
                share_output_dir=share_output_dir,
            )
        )
        initial_result["search_query_inference"] = search_query_inference
        return initial_result
    supplemented = supplement_hotlist_knowledge_cards(
        evidence,
        initial_result,
        config_path=config_path,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        effort=supplement_effort,
    )
    supplemented["search_query_inference"] = search_query_inference
    return supplemented


def supplement_hotlist_knowledge_cards(
    evidence: Mapping[str, Any],
    initial_result: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    effort: str = "xhigh",
) -> dict[str, Any]:
    """Discover snippets for failed topics and save a new merged card batch."""
    from agentscroll.inference_config import load_inference_settings

    topics = _prompt_payload(evidence)
    initial_cards = _validate_cards(initial_result.get("cards"), topics)
    initial_by_id = {card["topic_id"]: card for card in initial_cards}
    research_topics = []
    for topic in topics:
        initial_card = initial_by_id[topic["topic_id"]]
        if initial_card["status"] != "needs_research":
            continue
        research_topics.append(
            {
                "topic_id": topic["topic_id"],
                "title": topic["title"],
                "label": topic["label"],
                "evidence": topic["evidence"],
            }
        )

    if not research_topics:
        result = dict(initial_result)
        result["supplemented_count"] = 0
        return result

    settings = load_inference_settings(config_path)
    search_results, public_search_requests = _collect_public_search_results(
        research_topics,
        max_workers=settings.max_workers,
    )
    for topic in research_topics:
        topic["public_search_results"] = search_results.get(topic["topic_id"], [])

    research_cards, supplement_inference = _generate_topic_cards(
        research_topics,
        settings=settings,
        research=True,
        max_tokens=8_000,
        timeout=900,
        effort=effort,
    )
    research_by_id = {card["topic_id"]: card for card in research_cards}
    merged_cards = [
        research_by_id.get(card["topic_id"], card) for card in initial_cards
    ]
    supplement_inference["web_search_calls"] = 0
    supplement_inference["public_search_requests"] = public_search_requests
    inference = {
        "initial": dict(initial_result.get("inference") or {}),
        "supplement": supplement_inference,
    }
    files = _save_cards(
        merged_cards,
        evidence=evidence,
        inference=inference,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        batch_name="补搜后热榜知识卡批次",
    )
    return {
        "topic_count": len(merged_cards),
        "complete_count": sum(card["status"] == "complete" for card in merged_cards),
        "needs_research_count": sum(
            card["status"] == "needs_research" for card in merged_cards
        ),
        "rejected_count": sum(
            card["status"] == "rejected" for card in merged_cards
        ),
        "supplemented_count": len(research_cards),
        "cards": merged_cards,
        "inference": inference,
        **files,
    }


__all__ = [
    "generate_hotlist_knowledge_cards",
    "generate_selected_hotlist_knowledge_cards",
    "supplement_hotlist_knowledge_cards",
]
