"""Render and atomically persist knowledge-card and share-review batches."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscroll.sharing.message import render_share_messages

from .knowledge_card_models import CARD_STATUSES, KnowledgeCard, normalize_label

_write_lock = threading.Lock()


def _render_card_text(card: Mapping[str, Any]) -> str:
    share = card.get("share")
    latest_update = card.get("latest_update")
    lines = [
        f"话题: {card['title']}",
        f"类别: {card['label']}",
        f"状态: {card['status']}",
        f"分享评分: {card.get('share_score', 0)}/4",
        f"大众分享评分: {card.get('general_share_score', 0)}/4",
        f"兴趣分享评分: {card.get('interest_share_score', 0)}/3.9",
    ]
    if card.get("candidate_interest_keywords"):
        lines.append(
            "兴趣关键词: "
            + "、".join(
                str(value) for value in card["candidate_interest_keywords"]
            )
        )
    if card.get("knowledge"):
        lines.extend(("", str(card["knowledge"])))
    if card.get("chat_context"):
        lines.extend(("", f"聊天语境: {card['chat_context']}"))
    if isinstance(latest_update, Mapping) and latest_update.get("summary"):
        lines.extend(("", f"最新进展: {latest_update['summary']}"))
    elif isinstance(latest_update, str) and latest_update.strip():
        lines.extend(("", f"最新进展: {latest_update.strip()}"))
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
    cards: list[KnowledgeCard],
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
        share = card.share
        if share is None:
            continue
        topic = evidence_by_id[card.topic_id]
        document = {
            "topic_id": card.topic_id,
            "title": str(topic.get("title") or ""),
            "label": normalize_label(topic.get("label")),
            "score": card.share_score,
            "general_score": card.general_share_score,
            "interest_score": card.interest_share_score,
            "hotlist_title_count": int(topic.get("hotlist_title_count") or 1),
            "text": share.text,
            "url": share.url,
            "comment": share.comment,
            "comment_type": share.comment_type,
            "source_id": share.source_id,
            "comment_id": share.comment_id,
        }
        if card.candidate_interest_keywords:
            document["candidate_interest_keywords"] = list(
                card.candidate_interest_keywords
            )
        shares.append(document)
    shares.sort(key=lambda item: item["score"], reverse=True)
    return shares


def save_share_batch(
    cards: list[KnowledgeCard],
    *,
    evidence: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    generated_at: datetime | None = None,
    stored_topic_ids: Mapping[Any, str] | None = None,
) -> dict[str, Any]:
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "outputs" / "shares").resolve()
    )
    generated_at = generated_at or datetime.now().astimezone()
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S-%f%z")
    shares = _share_documents(cards, evidence=evidence)
    if stored_topic_ids:
        shares = [
            {
                **share,
                "topic_id": stored_topic_ids.get(
                    share["topic_id"], str(share["topic_id"])
                ),
            }
            for share in shares
        ]
    share_group_id = uuid.uuid4().hex
    document = {
        "share_group_id": share_group_id,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "share_count": len(shares),
        "inference": dict(inference),
        "shares": shares,
    }
    json_path = destination / f"{timestamp}_即时分享批次.json"
    text_path = destination / f"{timestamp}_即时分享批次.txt"
    serialized_text = "\n\n=====\n\n".join(
        "\n\n".join(render_share_messages(share)) for share in shares
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
        "share_group_id": share_group_id,
        "share_generated_at": document["generated_at"],
        "share_count": len(shares),
        "shares": shares,
        "share_review_file": str(json_path),
        "share_text_file": str(text_path),
    }


def save_card_batch(
    cards: list[KnowledgeCard],
    *,
    evidence: Mapping[str, Any],
    inference: Mapping[str, Any],
    output_dir: str | Path | None,
    generated_at: datetime | None = None,
    batch_name: str = "热榜知识卡批次",
) -> dict[str, Any]:
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "outputs" / "knowledge").resolve()
    )
    generated_at = generated_at or datetime.now().astimezone()
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S-%f%z")
    evidence_by_id = {
        topic["topic_id"]: topic
        for topic in evidence.get("topics") or []
        if isinstance(topic, Mapping)
    }
    documents: list[dict[str, Any]] = []
    batch_text: list[str] = []
    for card in cards:
        topic = evidence_by_id[card.topic_id]
        document = {
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "topic_id": card.topic_id,
            "title": topic["title"],
            "label": normalize_label(topic.get("label")),
            "status": card.status,
            "rejection_reason": card.rejection_reason,
            "knowledge": card.knowledge,
            "chat_context": card.chat_context,
            "latest_update": card.latest_update,
            "share_score": card.share_score,
            "general_share_score": card.general_share_score,
            "interest_share_score": card.interest_share_score,
            "hotlist_title_count": int(topic.get("hotlist_title_count") or 1),
            "share": (
                card.share.model_dump(mode="json") if card.share is not None else None
            ),
            "research_sources": (
                [source.model_dump(mode="json") for source in card.research_sources]
                if card.research_sources is not None
                else []
            ),
            "evidence": topic.get("evidence") or [],
            "research_evidence": topic.get("research_evidence") or [],
            "collection_attempts": topic.get("attempts") or [],
        }
        if card.candidate_interest_keywords:
            document["candidate_interest_keywords"] = list(
                card.candidate_interest_keywords
            )
        documents.append(document)
        batch_text.append(_render_card_text(document).rstrip())

    status_counts = {
        status: sum(card.status == status for card in cards)
        for status in CARD_STATUSES
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
    return {
        "batch_json_file": str(batch_json_path),
        "batch_text_file": str(batch_text_path),
    }


__all__ = ["save_card_batch", "save_share_batch"]
