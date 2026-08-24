"""Generate model-learning knowledge cards from collected hot-list evidence."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentscroll.prompts.knowledge_card import (
    KNOWLEDGE_CARD_PROMPT,
    KNOWLEDGE_CARD_RESEARCH_PROMPT,
)

_LABEL_ALIASES = {
    "news": "news",
    "breaking": "news",
    "major": "news",
    "fun": "fun",
    "meme": "fun",
}
_REMOVED_LABELS = {"conversation", "discussion"}
_SHARE_LABELS = {"news", "fun"}
_STATUSES = {"complete", "needs_research", "rejected"}
_write_lock = threading.Lock()


def _card_schema(topic_ids: list[int]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "minItems": len(topic_ids),
                "maxItems": len(topic_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "integer", "enum": topic_ids},
                        "status": {
                            "type": "string",
                            "enum": sorted(_STATUSES),
                        },
                        "rejection_reason": {
                            "type": "string",
                            "maxLength": 120,
                        },
                        "knowledge": {"type": "string", "maxLength": 260},
                        "talking_points": {
                            "type": "array",
                            "maxItems": 3,
                            "items": {"type": "string", "maxLength": 80},
                        },
                        "community_context": {"type": "string", "maxLength": 180},
                        "uncertainties": {
                            "type": "array",
                            "maxItems": 3,
                            "items": {"type": "string", "maxLength": 80},
                        },
                        "missing": {
                            "type": "array",
                            "maxItems": 3,
                            "items": {"type": "string", "maxLength": 80},
                        },
                        "share": {
                            "type": "object",
                            "properties": {
                                "ready": {"type": "boolean"},
                                "text": {"type": "string", "maxLength": 180},
                                "source_id": {"type": "string", "maxLength": 40},
                                "comment_id": {"type": "string", "maxLength": 60},
                                "generated_comment": {
                                    "type": "string",
                                    "maxLength": 80,
                                },
                            },
                            "required": [
                                "ready",
                                "text",
                                "source_id",
                                "comment_id",
                                "generated_comment",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "topic_id",
                        "status",
                        "rejection_reason",
                        "knowledge",
                        "talking_points",
                        "community_context",
                        "uncertainties",
                        "missing",
                        "share",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cards"],
        "additionalProperties": False,
    }


def _research_card_schema(topic_ids: list[int]) -> dict[str, Any]:
    schema = _card_schema(topic_ids)
    item_schema = schema["properties"]["cards"]["items"]
    item_schema["properties"]["research_sources"] = {
        "type": "array",
        "maxItems": 3,
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "url": {"type": "string", "maxLength": 500},
            },
            "required": ["title", "url"],
            "additionalProperties": False,
        },
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


def _prompt_payload(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_topics = evidence.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("evidence 缺少非空 topics 数组")
    if len(raw_topics) > 20:
        raise ValueError("单次知识卡生成最多处理 20 个话题")

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
            source_id = f"{topic_id}:e{evidence_index}"
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
                        "comment_id": f"{source_id}:c{comment_index}",
                        "text": text,
                    }
                )
            compact_evidence.append(
                {
                    "source_id": source_id,
                    "platform": str(raw_item.get("platform") or ""),
                    "matched_title": str(raw_item.get("title") or ""),
                    "published_at": raw_item.get("published_at"),
                    "url": _http_url(raw_item.get("url")),
                    "content": str(raw_item.get("content") or ""),
                    "comments": comments,
                }
            )
        attempts = []
        for raw_attempt in raw_topic.get("attempts") or []:
            if not isinstance(raw_attempt, Mapping):
                continue
            attempts.append(
                {
                    "source": str(raw_attempt.get("source") or ""),
                    "query": str(raw_attempt.get("query") or ""),
                    "status": str(raw_attempt.get("status") or ""),
                    "post_count": raw_attempt.get("post_count"),
                    "error": str(raw_attempt.get("error") or "")[:240],
                }
            )
        topics.append(
            {
                "topic_id": topic_id,
                "title": str(raw_topic.get("title") or ""),
                "label": label,
                "related_titles": [
                    str(value) for value in raw_topic.get("related_titles") or []
                ],
                "collection_status": str(raw_topic.get("collection_status") or ""),
                "attempts": attempts,
                "evidence": compact_evidence,
            }
        )
    if not topics:
        raise ValueError("evidence 中没有 news 或 fun 话题")
    return topics


def _knowledge_card_prompt(topics: list[dict[str, Any]]) -> str:
    payload = json.dumps(topics, ensure_ascii=False, separators=(",", ":"))
    evaluated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"{KNOWLEDGE_CARD_PROMPT.strip()}\n\n"
        f"当前评估时间：{evaluated_at}\n"
        f"话题总数：{len(topics)}\n"
        f"待生成知识卡的证据（JSON）：\n{payload}"
    )


def _knowledge_card_research_prompt(topics: list[dict[str, Any]]) -> str:
    payload = json.dumps(topics, ensure_ascii=False, separators=(",", ":"))
    evaluated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"{KNOWLEDGE_CARD_RESEARCH_PROMPT.strip()}\n\n"
        f"当前评估时间：{evaluated_at}\n"
        f"待补搜话题总数：{len(topics)}\n"
        f"待补搜材料（JSON）：\n{payload}"
    )


def _research_search_queries(topic: Mapping[str, Any]) -> list[str]:
    title = str(topic.get("title") or "").strip()
    queries = [title]
    if str(topic.get("label") or "") == "fun":
        context = " ".join(
            [
                title,
                *(str(value) for value in topic.get("missing") or []),
            ]
        )
        latin = re.findall(
            r"(?i)(?<![a-z0-9])[a-z][a-z0-9._+-]*(?![a-z0-9])",
            context,
        )
        numbers = re.findall(r"(?<!\d)\d+(?:\.\d+)?%?(?!\d)", context)
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
                        "source_id": f"{topic['topic_id']}:s{len(results) + 1}",
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
    status: str,
    topic: Mapping[str, Any],
) -> dict[str, Any]:
    topic_id = topic["topic_id"]
    if not isinstance(raw_share, Mapping):
        raise ValueError(f"话题 {topic_id} 缺少有效 share 对象")
    ready = raw_share.get("ready")
    if not isinstance(ready, bool):
        raise ValueError(f"话题 {topic_id} 的 share.ready 不是布尔值")

    text = str(raw_share.get("text") or "").strip()
    source_id = str(raw_share.get("source_id") or "").strip()
    comment_id = str(raw_share.get("comment_id") or "").strip()
    generated_comment = str(raw_share.get("generated_comment") or "").strip()
    if not generated_comment and raw_share.get("comment_type") == "generated":
        generated_comment = str(raw_share.get("comment") or "").strip()

    if not ready:
        if text or source_id or comment_id or generated_comment:
            raise ValueError(f"不可分享话题 {topic_id} 的 share 字段必须为空")
        return {
            "ready": False,
            "text": "",
            "source_id": "",
            "url": "",
            "comment_id": "",
            "comment_type": "",
            "comment": "",
        }

    if status != "complete":
        raise ValueError(f"待补搜话题 {topic_id} 不能进入即时分享队列")
    if topic.get("label") not in _SHARE_LABELS:
        raise ValueError(f"话题 {topic_id} 的 label 不允许即时分享")
    if not text or not source_id:
        raise ValueError(f"可分享话题 {topic_id} 缺少分享内容或来源")
    if bool(comment_id) == bool(generated_comment):
        raise ValueError(f"可分享话题 {topic_id} 必须且只能选择真实评论或自拟评论之一")

    sources = [
        *list(topic.get("evidence") or []),
        *list(topic.get("public_search_results") or []),
    ]
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
            for comment in source.get("comments") or []
            if isinstance(comment, Mapping)
        }
        comment = comments.get(comment_id, "")
        if not comment:
            raise ValueError(
                f"话题 {topic_id} 的 comment_id 不属于所选来源：{comment_id!r}"
            )
        comment_type = "platform"
    else:
        comment = generated_comment
        comment_type = "generated"

    return {
        "ready": True,
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
        if topic_id not in topic_by_id or topic_id in card_by_id:
            raise ValueError(f"模型返回了无效或重复 topic_id：{topic_id!r}")
        status = raw_card.get("status")
        rejection_reason = str(raw_card.get("rejection_reason") or "").strip()
        knowledge = str(raw_card.get("knowledge") or "").strip()
        talking_points = raw_card.get("talking_points")
        community_context = str(raw_card.get("community_context") or "").strip()
        uncertainties = raw_card.get("uncertainties")
        missing = raw_card.get("missing")
        if status not in _STATUSES:
            raise ValueError(f"模型返回了无效 status：{status!r}")
        if not all(
            isinstance(value, list)
            for value in (talking_points, uncertainties, missing)
        ):
            raise ValueError(f"话题 {topic_id} 的数组字段无效")
        if status == "complete":
            if not knowledge or missing or rejection_reason:
                raise ValueError(
                    f"完整知识卡 {topic_id} 的字段状态不一致"
                )
        elif status == "needs_research":
            if (
                rejection_reason
                or knowledge
                or talking_points
                or community_context
                or not missing
            ):
                raise ValueError(f"待补搜知识卡 {topic_id} 的字段状态不一致")
        elif (
            not rejection_reason
            or knowledge
            or talking_points
            or community_context
            or uncertainties
            or missing
        ):
            raise ValueError(f"已淘汰知识卡 {topic_id} 的字段状态不一致")
        card = dict(raw_card)
        card["rejection_reason"] = rejection_reason
        card["share"] = _validate_share(
            raw_card.get("share"),
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
        sources = card.get("research_sources")
        if not isinstance(sources, list):
            raise TypeError(f"补搜知识卡 {card['topic_id']} 缺少 research_sources 数组")
        normalized_sources = []
        for source in sources:
            if not isinstance(source, Mapping):
                raise TypeError(f"补搜知识卡 {card['topic_id']} 包含无效来源")
            title = str(source.get("title") or "").strip()
            url = str(source.get("url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                raise ValueError(f"补搜知识卡 {card['topic_id']} 包含无效来源")
            normalized_sources.append({"title": title, "url": url})
        card["research_sources"] = normalized_sources
    return cards


def _render_card_text(card: Mapping[str, Any]) -> str:
    lines = [
        f"话题: {card['title']}",
        f"类别: {card['label']}",
        f"状态: {card['status']}",
    ]
    if card.get("knowledge"):
        lines.extend(("", str(card["knowledge"])))
    if card.get("talking_points"):
        lines.append("\n聊天要点:")
        lines.extend(f"- {value}" for value in card["talking_points"])
    if card.get("community_context"):
        lines.extend(("", f"网友语境: {card['community_context']}"))
    if card.get("uncertainties"):
        lines.append("\n仍需谨慎:")
        lines.extend(f"- {value}" for value in card["uncertainties"])
    if card.get("missing"):
        lines.append("\n缺少材料:")
        lines.extend(f"- {value}" for value in card["missing"])
    if card.get("rejection_reason"):
        lines.extend(("", f"淘汰原因: {card['rejection_reason']}"))
    share = card.get("share") or {}
    if share.get("ready"):
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
        share = card.get("share") or {}
        if not share.get("ready"):
            continue
        topic = evidence_by_id[card["topic_id"]]
        shares.append(
            {
                "topic_id": card["topic_id"],
                "title": str(topic.get("title") or ""),
                "label": _normalize_label(topic.get("label")),
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
            "talking_points": card["talking_points"],
            "community_context": card["community_context"],
            "uncertainties": card["uncertainties"],
            "missing": card["missing"],
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


def generate_hotlist_knowledge_cards(
    evidence: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    share_output_dir: str | Path | None = None,
    save_share_queue: bool = True,
    effort: str = "xhigh",
) -> dict[str, Any]:
    """Generate knowledge cards and a share queue in one configured LLM request."""
    from agentscroll.inference_config import (
        build_configured_client,
        load_inference_settings,
        make_configured_request,
    )

    topics = _prompt_payload(evidence)
    topic_ids = [topic["topic_id"] for topic in topics]
    prompt = _knowledge_card_prompt(topics)
    settings = load_inference_settings(config_path)
    request = make_configured_request(
        prompt,
        settings,
        json_schema=_card_schema(topic_ids),
        max_tokens=12_000,
        timeout=600,
        effort=effort,
    )
    client = build_configured_client(settings)
    try:
        response = client.generate(request)
    finally:
        client.close()
    if not response.ok:
        raise RuntimeError(f"热榜知识卡生成失败：{response.error or 'unknown error'}")
    if not isinstance(response.parsed_json, dict):
        raise ValueError("模型返回值不是包含 cards 的 JSON object")
    cards = _validate_cards(response.parsed_json.get("cards"), topics)
    inference = {
        "provider": response.provider,
        "model": response.model,
        "elapsed_s": response.elapsed_s,
        "usage": response.metadata.get("usage") or {},
        "effort": effort,
        "prompt_chars": len(prompt),
    }
    files = _save_cards(
        cards,
        evidence=evidence,
        inference=inference,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        save_share_queue=save_share_queue,
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
        **files,
    }


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
    from agentscroll.collector.hotlist import collect_selected_hotlist_evidence

    evidence = collect_selected_hotlist_evidence(
        hotlist,
        selection,
        posts_per_entry=posts_per_entry,
        max_entries_per_topic=max_entries_per_topic,
    )
    initial_result = generate_hotlist_knowledge_cards(
        evidence,
        config_path=config_path,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        save_share_queue=not supplement_failed,
        effort=generation_effort,
    )
    if not supplement_failed or not initial_result["needs_research_count"]:
        if supplement_failed:
            initial_result.update(
                _save_share_batch(
                    initial_result["cards"],
                    evidence=evidence,
                    inference=initial_result["inference"],
                    output_dir=share_output_dir,
                )
            )
        return initial_result
    return supplement_hotlist_knowledge_cards(
        evidence,
        initial_result,
        config_path=config_path,
        output_dir=output_dir,
        share_output_dir=share_output_dir,
        effort=supplement_effort,
    )


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
    from agentscroll.inference_config import (
        build_configured_client,
        load_inference_settings,
        make_configured_request,
    )

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
                "related_titles": topic["related_titles"],
                "missing": initial_card["missing"],
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

    topic_ids = [topic["topic_id"] for topic in research_topics]
    prompt = _knowledge_card_research_prompt(research_topics)
    request = make_configured_request(
        prompt,
        settings,
        json_schema=_research_card_schema(topic_ids),
        max_tokens=8_000,
        timeout=900,
        effort=effort,
    )
    client = build_configured_client(settings)
    try:
        response = client.generate(request)
    finally:
        client.close()
    if not response.ok:
        raise RuntimeError(f"热榜知识卡补搜失败：{response.error or 'unknown error'}")
    if not isinstance(response.parsed_json, dict):
        raise ValueError("补搜模型返回值不是包含 cards 的 JSON object")

    research_cards = _validate_research_cards(
        response.parsed_json.get("cards"),
        research_topics,
    )
    research_by_id = {card["topic_id"]: card for card in research_cards}
    merged_cards = [
        research_by_id.get(card["topic_id"], card) for card in initial_cards
    ]
    supplement_inference = {
        "provider": response.provider,
        "model": response.model,
        "elapsed_s": response.elapsed_s,
        "usage": response.metadata.get("usage") or {},
        "web_search_calls": 0,
        "public_search_requests": public_search_requests,
        "effort": effort,
        "prompt_chars": len(prompt),
    }
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
