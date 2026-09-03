"""Generate model-learning knowledge cards from collected hot-list evidence."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agentscroll.prompts.knowledge_card import (
    KNOWLEDGE_CARD_LABEL_PROMPTS,
    KNOWLEDGE_CARD_INTEREST_PROMPT,
    KNOWLEDGE_CARD_PROMPT,
    KNOWLEDGE_CARD_RESEARCH_PROMPT,
)
from agentscroll.prompts.hotlist import ZHIHU_SEARCH_QUERY_PROMPT

from .knowledge_card_artifacts import save_card_batch, save_share_batch
from .knowledge_card_models import (
    KnowledgeCard,
    REMOVED_LABELS as _REMOVED_LABELS,
    card_response_schema,
    cards_to_dicts,
    http_url as _http_url,
    normalize_label as _normalize_label,
    validate_cards as _validate_cards,
    validate_research_cards as _validate_research_cards,
)

_RESEARCH_SOURCES = {
    "news": ("weibo", "wechat", "toutiao"),
    "fun": ("weibo", "xiaohongshu"),
}
_RESEARCH_ITEM_LIMIT = 3
_COMMENT_REPLY_PREFIX_RE = re.compile(r"^回复\s*@[^:：]+[:：]\s*")

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


def _comment_body(value: Any) -> str:
    text = str(value or "").strip()
    return _COMMENT_REPLY_PREFIX_RE.sub("", text, count=1).strip()


def _selection_with_zhihu_search_queries(
    hotlist: Mapping[str, Any] | str | Path,
    selection: Mapping[str, Any],
    *,
    config_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate one batch of non-Zhihu search queries from Zhihu clues."""
    from agentscroll.collector.hotlist import list_hotlist_entries
    from agentscroll.config import (
        build_configured_client,
        load_settings,
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
    settings = load_settings(config_path)
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
        event_relation = str(raw_topic.get("event_relation") or "new")
        if event_relation not in {"new", "update"}:
            raise ValueError(f"话题 {topic_id} 的事件关系无效：{event_relation!r}")
        raw_interest_keywords = raw_topic.get("candidate_interest_keywords") or []
        if not isinstance(raw_interest_keywords, list) or any(
            not isinstance(keyword, str) or not keyword.strip()
            for keyword in raw_interest_keywords
        ):
            raise ValueError(
                f"话题 {topic_id} 的 candidate_interest_keywords 无效"
            )
        candidate_interest_keywords = [
            keyword.strip() for keyword in raw_interest_keywords
        ]
        if len(candidate_interest_keywords) != len(
            set(candidate_interest_keywords)
        ):
            raise ValueError(
                f"话题 {topic_id} 的 candidate_interest_keywords 包含重复值"
            )

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
                text = _comment_body(comment.get("text"))
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
        topic = {
            "topic_id": topic_id,
            "title": str(raw_topic.get("title") or ""),
            "label": label,
            "event_relation": event_relation,
            "matched_event_id": str(raw_topic.get("matched_event_id") or ""),
            "previous_card": (
                dict(raw_topic["previous_card"])
                if isinstance(raw_topic.get("previous_card"), Mapping)
                else None
            ),
            "timeline": [
                str(title)
                for title in raw_topic.get("timeline") or []
                if str(title).strip()
            ],
            "evidence": compact_evidence,
        }
        if candidate_interest_keywords:
            topic["candidate_interest_keywords"] = candidate_interest_keywords
        topics.append(topic)
    if not topics:
        raise ValueError("evidence 中没有 news 或 fun 话题")
    return topics


def _selection_with_update_contexts(
    evidence: Mapping[str, Any],
    selection: Mapping[str, Any],
    *,
    at: datetime,
) -> dict[str, Any]:
    """Attach only the current card fields needed to evaluate an update."""
    from .hotlist_state import load_history, recent_update_timeline

    selected_by_id = {
        topic.get("representative_id"): topic
        for topic in selection.get("topics") or []
        if isinstance(topic, Mapping)
    }
    update_topics = [
        topic
        for topic in selected_by_id.values()
        if topic.get("event_relation") == "update"
    ]
    history: Mapping[str, Any] = {"events": []}
    if update_topics:
        database_path = str(selection.get("database_path") or "").strip()
        if not database_path:
            raise ValueError("update 话题缺少 database_path")
        history = load_history(database_path)
    events_by_id = {
        str(event.get("event_id") or ""): event
        for event in history.get("events") or []
        if isinstance(event, Mapping)
    }
    enriched_topics = []
    for raw_topic in evidence.get("topics") or []:
        if not isinstance(raw_topic, Mapping):
            continue
        topic = dict(raw_topic)
        selected = selected_by_id.get(topic.get("topic_id"))
        if not isinstance(selected, Mapping):
            raise ValueError(f"找不到话题 {topic.get('topic_id')!r} 的筛选结果")
        relation = str(selected.get("event_relation") or "new")
        if relation not in {"new", "update"}:
            raise ValueError(f"无效的事件关系：{relation!r}")
        topic["event_relation"] = relation
        if relation == "new":
            enriched_topics.append(topic)
            continue

        event_id = str(selected.get("matched_event_id") or "")
        event = events_by_id.get(event_id)
        if not isinstance(event, Mapping):
            raise ValueError(f"找不到 update 对应的历史事件：{event_id!r}")
        raw_latest_update = event.get("latest_update")
        if str(raw_latest_update or "").strip():
            updates = [
                update
                for update in event.get("updates") or []
                if isinstance(update, Mapping)
            ]
            last_update = updates[-1] if updates else {}
            update_titles = last_update.get("titles") or []
            previous_latest_update = {
                "updated_at": str(
                    last_update.get("updated_at") or event.get("updated_at") or ""
                ),
                "title": str(update_titles[0] if update_titles else event.get("title") or ""),
                "summary": str(raw_latest_update).strip(),
            }
        else:
            previous_latest_update = None
        topic.update(
            {
                "matched_event_id": event_id,
                "timeline": recent_update_timeline(event, at=at),
                "previous_card": {
                    "title": str(event.get("title") or ""),
                    "status": str(event.get("status") or ""),
                    "knowledge": str(event.get("knowledge") or ""),
                    "chat_context": str(event.get("chat_context") or ""),
                    "latest_update": previous_latest_update,
                },
            }
        )
        enriched_topics.append(topic)

    enriched = dict(evidence)
    enriched["topics"] = enriched_topics
    return enriched


def _model_topic_payload(topic: Mapping[str, Any], *, research: bool) -> dict[str, Any]:
    def compact_items(raw_items: Any) -> list[dict[str, Any]]:
        compacted = []
        for item in raw_items or []:
            if not isinstance(item, Mapping):
                continue
            url = _http_url(item.get("url"))
            model_item: dict[str, Any] = {
                "platform": str(item.get("platform") or ""),
                "source_title": str(item.get("source_title") or ""),
                "published_at": item.get("published_at"),
                "content": str(item.get("content") or ""),
                "comments": [],
            }
            if url:
                model_item["source_id"] = str(item.get("source_id") or "")
            for comment in item.get("comments") or []:
                if not isinstance(comment, Mapping):
                    continue
                model_item["comments"].append(
                    {
                        "comment_id": str(comment.get("comment_id") or ""),
                        "text": str(comment.get("text") or ""),
                    }
                )
            compacted.append(model_item)
        return compacted

    payload: dict[str, Any] = {
        "title": str(topic.get("title") or ""),
        "label": str(topic.get("label") or ""),
        "relation": str(topic.get("event_relation") or "new"),
        "evidence": compact_items(topic.get("evidence")),
    }
    candidate_keywords = list(topic.get("candidate_interest_keywords") or [])
    if candidate_keywords:
        payload["interest"] = {"candidate_keywords": candidate_keywords}
    if payload["relation"] == "update":
        payload["previous_card"] = dict(topic.get("previous_card") or {})
        payload["timeline"] = list(topic.get("timeline") or [])
    if research:
        payload["research_evidence"] = compact_items(
            topic.get("research_evidence")
        )
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
        f"{KNOWLEDGE_CARD_INTEREST_PROMPT.strip()}\n\n"
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
        f"{KNOWLEDGE_CARD_INTEREST_PROMPT.strip()}\n\n"
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
) -> KnowledgeCard:
    return KnowledgeCard.needs_research(
        topic["topic_id"],
        research=research,
        candidate_interest_keywords=tuple(
            topic.get("candidate_interest_keywords") or []
        ),
    )


def _validated_interest_topics(
    topics: list[dict[str, Any]],
    settings: Any,
) -> list[dict[str, Any]]:
    interest = getattr(settings, "interest", None)
    configured_keywords = tuple(getattr(interest, "keywords", ()) or ())
    allowed = set(configured_keywords)
    validated: list[dict[str, Any]] = []
    for raw_topic in topics:
        topic = dict(raw_topic)
        candidate_keywords = list(
            topic.get("candidate_interest_keywords") or []
        )
        unknown = set(candidate_keywords) - allowed
        if unknown:
            raise ValueError(
                f"话题 {topic['topic_id']} 包含未配置的兴趣关键词："
                f"{sorted(unknown)!r}"
            )
        if candidate_keywords:
            topic["candidate_interest_keywords"] = candidate_keywords
        else:
            topic.pop("candidate_interest_keywords", None)
        validated.append(topic)
    return validated


def _generate_topic_cards(
    topics: list[dict[str, Any]],
    *,
    settings: Any,
    research: bool,
    max_tokens: int,
    timeout: int,
    effort: str,
    minimum_evidence_count: int = 1,
) -> tuple[list[KnowledgeCard], dict[str, Any]]:
    topics = _validated_interest_topics(topics, settings)
    cards_by_id: dict[int, KnowledgeCard] = {}
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

    from agentscroll.config import (
        build_configured_client,
        make_configured_request,
    )

    prompt_builder = (
        _knowledge_card_research_prompt if research else _knowledge_card_prompt
    )
    prompts = [prompt_builder(topic) for topic in model_topics]
    requests = [
        make_configured_request(
            prompt,
            settings,
            json_schema=card_response_schema(research=research),
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


def _recheck_immediate_history_matches(
    cards: list[KnowledgeCard],
    *,
    evidence: Mapping[str, Any],
    selection: Mapping[str, Any],
    settings: Any,
    at: datetime,
    immediate_score: float,
    effort: str,
) -> tuple[
    list[KnowledgeCard],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Re-evaluate provisional immediate shares that match recent history."""
    from .hotlist_state import load_history, match_new_topics_from_evidence

    def attach_research_evidence(
        prompt_topics: list[dict[str, Any]],
        raw_evidence: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        raw_topics_by_id = {
            topic.get("topic_id"): topic
            for topic in raw_evidence.get("topics") or []
            if isinstance(topic, Mapping)
        }
        for topic in prompt_topics:
            raw_topic = raw_topics_by_id.get(topic["topic_id"])
            topic["research_evidence"] = [
                dict(item)
                for item in (raw_topic or {}).get("research_evidence") or []
                if isinstance(item, Mapping)
            ]
        return prompt_topics

    topics = attach_research_evidence(_prompt_payload(evidence), evidence)
    cards_by_id = {card.topic_id: card for card in cards}
    triggered_topics = [
        topic
        for topic in topics
        if topic.get("event_relation") == "new"
        and (card := cards_by_id.get(topic["topic_id"])) is not None
        and card.status == "complete"
        and card.share is not None
        and card.general_share_score >= immediate_score
    ]
    if not triggered_topics:
        return cards, dict(evidence), dict(selection), {}

    database_path = str(selection.get("database_path") or "").strip()
    history: Mapping[str, Any] = {"events": []}
    if database_path:
        history = load_history(database_path)
    matches = match_new_topics_from_evidence(
        triggered_topics,
        history,
        at=at,
    )
    diagnostics: dict[str, Any] = {
        "immediate_score": immediate_score,
        "triggered_topic_count": len(triggered_topics),
        "history_match_count": len(matches),
        "matches": [
            {"topic_id": topic_id, **match}
            for topic_id, match in sorted(matches.items())
        ],
        "request_count": 0,
        "usage": {},
    }
    if not matches:
        return cards, dict(evidence), dict(selection), diagnostics

    revised_selection = dict(selection)
    revised_selection["topics"] = [
        {
            **dict(topic),
            **(
                {
                    "event_relation": "update",
                    "matched_event_id": matches[topic.get("representative_id")][
                        "event_id"
                    ],
                }
                if topic.get("representative_id") in matches
                else {}
            ),
        }
        for topic in selection.get("topics") or []
        if isinstance(topic, Mapping)
    ]
    revised_evidence = _selection_with_update_contexts(
        evidence,
        revised_selection,
        at=at,
    )
    revised_topics_by_id = {
        topic["topic_id"]: topic
        for topic in attach_research_evidence(
            _prompt_payload(revised_evidence),
            revised_evidence,
        )
    }
    recheck_topics = [
        revised_topics_by_id[topic_id] for topic_id in matches
    ]
    rechecked_cards, recheck_inference = _generate_topic_cards(
        recheck_topics,
        settings=settings,
        research=True,
        max_tokens=8_000,
        timeout=900,
        effort=effort,
    )
    rechecked_by_id = {card.topic_id: card for card in rechecked_cards}
    diagnostics.update(recheck_inference)
    return (
        [rechecked_by_id.get(card.topic_id, card) for card in cards],
        revised_evidence,
        revised_selection,
        diagnostics,
    )


def _research_date_window(evidence: Mapping[str, Any]) -> tuple[int, str | None]:
    raw_range = evidence.get("date_range")
    if not isinstance(raw_range, Mapping):
        return 7, None
    raw_from = str(raw_range.get("from") or "").strip()
    raw_to = str(raw_range.get("to") or "").strip()
    try:
        from_date = date.fromisoformat(raw_from)
        to_date = date.fromisoformat(raw_to)
    except ValueError as exc:
        raise ValueError("evidence.date_range 必须使用 YYYY-MM-DD 日期") from exc
    if from_date > to_date:
        raise ValueError("evidence.date_range.from 不能晚于 to")
    # collector.get_date_range() subtracts ``days`` from the end date.
    return max(1, (to_date - from_date).days), raw_to


def _collect_active_search_evidence(
    topics: list[dict[str, Any]],
    *,
    max_workers: int,
    days: int,
    as_of: str | None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    from agentscroll.collector import collect
    from agentscroll.collector.knowledge_artifacts import build_knowledge_document

    def search_topic(
        topic: dict[str, Any],
    ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
        label = _normalize_label(topic.get("label"))
        sources = _RESEARCH_SOURCES[label]
        try:
            result = collect(
                str(topic.get("title") or ""),
                sources=sources,
                days=days,
                as_of=as_of,
                depth="default",
                max_items=_RESEARCH_ITEM_LIMIT,
                save=False,
            )
        except Exception as exc:
            return topic["topic_id"], [], [
                {
                    "topic_id": topic["topic_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ]

        source_errors = []
        for source, payload in (result.get("sources") or {}).items():
            if isinstance(payload, Mapping) and payload.get("error"):
                source_errors.append(
                    {
                        "topic_id": topic["topic_id"],
                        "source": str(source),
                        "error": str(payload["error"]),
                    }
                )

        items = build_knowledge_document(result)["items"][:_RESEARCH_ITEM_LIMIT]
        compact_evidence = []
        for item_index, item in enumerate(items, start=1):
            comments = []
            for comment_index, comment in enumerate(
                item.get("comments") or [], start=1
            ):
                if not isinstance(comment, Mapping):
                    continue
                text = _comment_body(comment.get("text"))
                if text:
                    comments.append(
                        {
                            "comment_id": f"r{item_index}c{comment_index}",
                            "text": text,
                        }
                    )
            compact_evidence.append(
                {
                    "source_id": f"r{item_index}",
                    "platform": str(item.get("platform") or ""),
                    "source_title": str(item.get("title") or ""),
                    "published_at": item.get("published_at"),
                    "url": _http_url(item.get("url")),
                    "content": str(item.get("content") or ""),
                    "comments": comments,
                }
            )
        return topic["topic_id"], compact_evidence, source_errors

    by_id: dict[int, list[dict[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(topics))) as executor:
        futures = [executor.submit(search_topic, topic) for topic in topics]
        for future in as_completed(futures):
            topic_id, results, topic_errors = future.result()
            by_id[topic_id] = results
            errors.extend(topic_errors)
    diagnostics = {
        "active_search_topic_count": len(topics),
        "active_search_source_requests": sum(
            len(_RESEARCH_SOURCES[_normalize_label(topic.get("label"))])
            for topic in topics
        ),
        "active_search_item_count": sum(len(items) for items in by_id.values()),
        "active_search_failure_count": len(errors),
        "active_search_failures": errors,
    }
    return by_id, diagnostics


def _save_selected_cards(
    cards: list[KnowledgeCard],
    *,
    evidence: Mapping[str, Any],
    selection: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    share_output_dir: str | Path | None,
    at: datetime,
    record_history: bool,
) -> dict[str, Any]:
    """Persist current topic state and write an immutable review batch."""
    topics_by_id = {
        topic["topic_id"]: topic
        for topic in evidence.get("topics") or []
        if isinstance(topic, Mapping)
    }
    updated_at = datetime.now().astimezone()
    updated_at_text = updated_at.isoformat(timespec="seconds")
    event_results: list[dict[str, Any]] = []
    for card in cards:
        topic = topics_by_id[card.topic_id]
        relation = str(topic.get("event_relation") or "new")
        if relation not in {"new", "update"}:
            raise ValueError(f"无效的事件关系：{relation!r}")
        used_urls = {
            source.url for source in card.research_sources or ()
        }
        if card.share is not None:
            used_urls.add(card.share.url)
        used_evidence = [
            dict(item)
            for field in ("evidence", "research_evidence")
            for item in topic.get(field) or []
            if isinstance(item, Mapping)
            and _http_url(item.get("url")) in used_urls
        ]
        event_results.append(
            {
                "topic_id": card.topic_id,
                "status": card.status,
                "title": str(topic.get("title") or ""),
                "updated_at": updated_at_text,
                "knowledge": card.knowledge,
                "chat_context": card.chat_context,
                "latest_update": card.latest_update,
                "share_score": card.share_score,
                "share": (
                    card.share.model_dump(mode="json")
                    if card.share is not None
                    else None
                ),
                "evidence": used_evidence,
            }
        )

    stored_topic_ids: dict[Any, str] = {}
    if record_history:
        from .hotlist_state import record_final_batch

        database_path = str(selection.get("database_path") or "").strip()
        if not database_path:
            raise ValueError("记录热点状态时缺少 database_path")
        stored_topic_ids = record_final_batch(
            database_path,
            selection,
            event_results,
            at=at,
        )

    files = save_card_batch(
        cards,
        evidence=evidence,
        inference=inference,
        output_dir=output_dir,
    )
    files.update(
        save_share_batch(
            cards,
            evidence=evidence,
            inference=inference,
            output_dir=share_output_dir,
            generated_at=updated_at,
            stored_topic_ids=stored_topic_ids,
        )
    )
    files["updated_card_count"] = sum(
        topics_by_id[card.topic_id].get("event_relation") == "update"
        and card.status == "complete"
        for card in cards
    )
    files["new_card_count"] = sum(
        topics_by_id[card.topic_id].get("event_relation") != "update"
        for card in cards
    )
    return files


def _save_generated_outputs(
    cards: list[KnowledgeCard],
    *,
    evidence: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    share_output_dir: str | Path | None,
    save_share_queue: bool = True,
) -> dict[str, Any]:
    generated_at = datetime.now().astimezone()
    files = save_card_batch(
        cards,
        evidence=evidence,
        inference=inference,
        output_dir=output_dir,
        generated_at=generated_at,
    )
    if save_share_queue:
        files.update(
            save_share_batch(
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
    from agentscroll.config import load_settings

    topics = _prompt_payload(evidence)
    settings = load_settings(config_path)
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
        "complete_count": sum(card.status == "complete" for card in cards),
        "needs_research_count": sum(
            card.status == "needs_research" for card in cards
        ),
        "rejected_count": sum(card.status == "rejected" for card in cards),
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
    files = _save_generated_outputs(
        result["cards"],
        evidence=evidence,
        inference=result["inference"],
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        save_share_queue=save_share_queue,
    )
    return {**result, "cards": cards_to_dicts(result["cards"]), **files}


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
    record_history: bool = False,
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
    from .hotlist_state import reference_time

    evidence = _selection_with_update_contexts(
        evidence,
        enriched_selection,
        at=reference_time(hotlist),
    )
    initial_result = _generate_hotlist_knowledge_cards(
        evidence,
        config_path=config_path,
        effort=generation_effort,
    )
    saving_evidence = evidence
    if supplement_failed and initial_result["needs_research_count"]:
        final_result = supplement_hotlist_knowledge_cards(
            evidence,
            initial_result,
            config_path=config_path,
            output_dir=output_dir,
            share_output_dir=share_output_dir,
            effort=supplement_effort,
            _save_result=False,
        )
        saving_evidence = final_result.pop("_saving_evidence", evidence)
    else:
        final_result = initial_result

    from agentscroll.config import load_settings

    settings = load_settings(config_path)
    immediate_score = (
        settings.sharing.policy.delivery.immediate_score
        if settings.sharing.enabled
        else None
    )
    if immediate_score is not None:
        (
            rechecked_cards,
            saving_evidence,
            enriched_selection,
            recheck_inference,
        ) = _recheck_immediate_history_matches(
            final_result["cards"],
            evidence=saving_evidence,
            selection=enriched_selection,
            settings=settings,
            at=reference_time(hotlist),
            immediate_score=immediate_score,
            effort=supplement_effort,
        )
        if recheck_inference:
            final_result = dict(final_result)
            final_result["cards"] = rechecked_cards
            final_result["complete_count"] = sum(
                card.status == "complete" for card in rechecked_cards
            )
            final_result["needs_research_count"] = sum(
                card.status == "needs_research" for card in rechecked_cards
            )
            final_result["rejected_count"] = sum(
                card.status == "rejected" for card in rechecked_cards
            )
            inference = dict(final_result.get("inference") or {})
            inference["immediate_history_recheck"] = recheck_inference
            final_result["inference"] = inference
    files = _save_selected_cards(
        final_result["cards"],
        evidence=saving_evidence,
        selection=enriched_selection,
        inference=final_result["inference"],
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        at=reference_time(hotlist),
        record_history=record_history,
    )
    final_result.update(files)
    final_result["search_query_inference"] = search_query_inference
    final_result["cards"] = cards_to_dicts(final_result["cards"])
    return final_result


def supplement_hotlist_knowledge_cards(
    evidence: Mapping[str, Any],
    initial_result: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    effort: str = "xhigh",
    _save_result: bool = True,
) -> dict[str, Any]:
    """Actively collect platform evidence for failed topics and save one batch."""
    from agentscroll.config import load_settings

    topics = _prompt_payload(evidence)
    initial_cards = _validate_cards(initial_result.get("cards"), topics)
    initial_by_id = {card.topic_id: card for card in initial_cards}
    research_topics = []
    for topic in topics:
        initial_card = initial_by_id[topic["topic_id"]]
        if initial_card.status != "needs_research":
            continue
        research_topics.append(
            {
                "topic_id": topic["topic_id"],
                "title": topic["title"],
                "label": topic["label"],
                "event_relation": topic["event_relation"],
                "matched_event_id": topic["matched_event_id"],
                "previous_card": topic["previous_card"],
                "timeline": topic["timeline"],
                "evidence": topic["evidence"],
            }
        )

    if not research_topics:
        result = dict(initial_result)
        result["supplemented_count"] = 0
        return result

    settings = load_settings(config_path)
    days, as_of = _research_date_window(evidence)
    search_results, active_search_diagnostics = _collect_active_search_evidence(
        research_topics,
        max_workers=settings.max_workers,
        days=days,
        as_of=as_of,
    )
    for topic in research_topics:
        topic["research_evidence"] = search_results.get(topic["topic_id"], [])

    model_research_topics = [
        topic
        for topic in research_topics
        if topic["evidence"] or topic["research_evidence"]
    ]
    skipped_empty_evidence_topics = [
        topic["topic_id"]
        for topic in research_topics
        if not topic["evidence"] and not topic["research_evidence"]
    ]
    research_cards, supplement_inference = _generate_topic_cards(
        model_research_topics,
        settings=settings,
        research=True,
        max_tokens=8_000,
        timeout=900,
        effort=effort,
    )
    research_by_id = {card.topic_id: card for card in research_cards}
    merged_cards = [
        research_by_id.get(card.topic_id, card) for card in initial_cards
    ]
    supplement_inference["web_search_calls"] = 0
    supplement_inference.update(active_search_diagnostics)
    supplement_inference["skipped_empty_evidence_count"] = len(
        skipped_empty_evidence_topics
    )
    supplement_inference["skipped_empty_evidence_topics"] = (
        skipped_empty_evidence_topics
    )
    inference = {
        "initial": dict(initial_result.get("inference") or {}),
        "supplement": supplement_inference,
    }
    research_evidence_by_id = {
        topic["topic_id"]: topic.get("research_evidence") or []
        for topic in research_topics
    }
    saving_evidence = dict(evidence)
    saving_evidence["topics"] = [
        {
            **dict(topic),
            "research_evidence": research_evidence_by_id.get(
                topic.get("topic_id"), []
            ),
        }
        for topic in evidence.get("topics") or []
        if isinstance(topic, Mapping)
    ]
    files = (
        _save_generated_outputs(
            merged_cards,
            evidence=saving_evidence,
            inference=inference,
            output_dir=output_dir,
            share_output_dir=share_output_dir,
        )
        if _save_result
        else {"_saving_evidence": saving_evidence}
    )
    return {
        "topic_count": len(merged_cards),
        "complete_count": sum(card.status == "complete" for card in merged_cards),
        "needs_research_count": sum(
            card.status == "needs_research" for card in merged_cards
        ),
        "rejected_count": sum(
            card.status == "rejected" for card in merged_cards
        ),
        "supplemented_count": len(research_cards),
        "cards": cards_to_dicts(merged_cards) if _save_result else merged_cards,
        "inference": inference,
        **files,
    }


__all__ = [
    "generate_hotlist_knowledge_cards",
    "generate_selected_hotlist_knowledge_cards",
    "supplement_hotlist_knowledge_cards",
]
