"""Match and persist hot-list topic state across learning batches."""

from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentscroll.collector.newsnow import _title_dedupe_key
from agentscroll.collector.sources.cjk import CHINESE_STOPWORDS, segment_with_pos
from agentscroll.storage import connect_database, database_transaction

ACTIVE_DAYS = 7
MATCHES_PER_TITLE = 3
MATCH_SCORE_THRESHOLD = 0.4
SIMILARITY_ORDER_THRESHOLD = 0.4
RECENT_PERSON_WINDOW = timedelta(hours=6)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_VISIBLE_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]", re.UNICODE)
_EVENT_STATUSES = {"complete", "needs_research", "rejected"}


def reference_time(hotlist: Mapping[str, Any] | str | Path) -> datetime:
    """Use snapshot time so replaying a saved snapshot remains deterministic."""
    payload: Mapping[str, Any] | None = hotlist if isinstance(hotlist, Mapping) else None
    if payload is None:
        path = Path(hotlist).expanduser().resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, Mapping):
            payload = raw
    collected_at = str((payload or {}).get("collected_at") or "").strip()
    if collected_at:
        try:
            parsed = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            return parsed.astimezone()
        except ValueError:
            pass
    return datetime.now().astimezone()


def load_history(path: str | Path) -> dict[str, Any]:
    """Load durable topic state and the exact-title cache from SQLite."""
    connection = connect_database(path)
    try:
        events = [_topic_row(row) for row in connection.execute(
            "SELECT * FROM hotlist_topics ORDER BY first_seen_at, topic_id"
        )]
        title_cache = [
            {
                "normalized": str(row["normalized_title"]),
                "text": str(row["title"]),
                "last_seen_at": str(row["last_seen_at"]),
            }
            for row in connection.execute(
                "SELECT normalized_title, title, last_seen_at "
                "FROM hotlist_title_cache ORDER BY last_seen_at, normalized_title"
            )
        ]
        return {"title_cache": title_cache, "events": events}
    finally:
        connection.close()


def _topic_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"热点 {row['topic_id']!r} 的 payload_json 无效") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"热点 {row['topic_id']!r} 的 payload_json 必须是 object")
    titles = payload.get("titles") or []
    updates = payload.get("updates") or []
    if not isinstance(titles, list) or not isinstance(updates, list):
        raise ValueError(f"热点 {row['topic_id']!r} 的 titles/updates 必须是数组")
    return {
        "event_id": str(row["topic_id"]),
        "label": str(row["label"]),
        "status": str(row["status"]),
        "last_result_status": str(row["last_result_status"]),
        "first_seen_at": str(row["first_seen_at"]),
        "last_seen_at": str(row["last_seen_at"]),
        "updated_at": str(row["updated_at"]),
        "title": str(row["title"]),
        "knowledge": str(row["knowledge"]),
        "chat_context": str(row["chat_context"]),
        "latest_update": row["latest_update"],
        "share_score": float(row["share_score"]),
        "titles": [dict(item) for item in titles if isinstance(item, Mapping)],
        "updates": [dict(item) for item in updates if isinstance(item, Mapping)],
    }


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _is_active(value: Any, *, at: datetime) -> bool:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return False
    first_date = at.date() - timedelta(days=ACTIVE_DAYS - 1)
    return timestamp.date() >= first_date


def active_exact_title_keys(history: Mapping[str, Any], *, at: datetime) -> set[str]:
    keys: set[str] = set()
    for item in history.get("title_cache") or []:
        if not isinstance(item, Mapping) or not _is_active(item.get("last_seen_at"), at=at):
            continue
        key = _title_dedupe_key(str(item.get("text") or ""))
        if key:
            keys.add(key)
    return keys


def recent_update_timeline(event: Mapping[str, Any], *, at: datetime) -> list[str]:
    """Return recent successful-update titles from oldest to newest."""
    raw_updates = event.get("updates") or []
    if not isinstance(raw_updates, list):
        raise ValueError(f"事件 {event.get('event_id')!r} 的 updates 必须是数组")
    ordered_updates: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for index, update in enumerate(raw_updates):
        if not isinstance(update, Mapping):
            continue
        updated_at = _parse_timestamp(update.get("updated_at"))
        if updated_at is None or not _is_active(updated_at, at=at):
            continue
        ordered_updates.append((updated_at, index, update))
    ordered_updates.sort(key=lambda item: (item[0], item[1]))

    titles: list[str] = []
    seen: set[str] = set()
    for _updated_at, _index, update in ordered_updates:
        for raw_title in update.get("titles") or []:
            title = " ".join(str(raw_title or "").split())
            key = _title_dedupe_key(title)
            if not key or key in seen:
                continue
            seen.add(key)
            titles.append(title)
    return titles


def _weighted_terms(text: str) -> dict[str, float]:
    terms: dict[str, float] = {}
    normalized = unicodedata.normalize("NFKC", text).lower()
    for token, flag in segment_with_pos(normalized):
        word = token.strip()
        if (
            not word
            or word in CHINESE_STOPWORDS
            or flag == "m"
            or _NUMBER_RE.fullmatch(word)
            or _VISIBLE_TOKEN_RE.search(word) is None
        ):
            continue
        if len(word) == 1 and "\u3400" <= word <= "\u9fff":
            continue
        weight = 2.0 if flag.startswith("n") or flag in {"j", "eng"} else 1.0
        terms[word] = max(terms.get(word, 0.0), weight)
    return terms


def _person_terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return {
        token.strip()
        for token, flag in segment_with_pos(normalized)
        if flag.startswith("nr") and len(token.strip()) >= 2
    }


def _elapsed_since(value: Any, *, at: datetime) -> timedelta | None:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return None
    elapsed = at - timestamp
    return elapsed if elapsed >= timedelta() else None


def _character_bigrams(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _NUMBER_RE.sub("", normalized)
    normalized = "".join(
        char
        for char in normalized
        if char.isalpha() or "\u3400" <= char <= "\u9fff"
    )
    return Counter(normalized[index : index + 2] for index in range(len(normalized) - 1))


def _idf(document_terms: list[set[str]]) -> dict[str, float]:
    document_count = len(document_terms)
    frequencies = Counter(term for terms in document_terms for term in terms)
    return {
        term: math.log((document_count + 1) / (count + 1)) + 1
        for term, count in frequencies.items()
    }


def _term_coverage(
    query: Mapping[str, float],
    document: Mapping[str, float],
    idf: Mapping[str, float],
) -> float:
    denominator = sum(idf.get(term, 1.0) * weight for term, weight in query.items())
    if denominator <= 0:
        return 0.0
    overlap = sum(
        idf.get(term, 1.0) * min(weight, document.get(term, 0.0))
        for term, weight in query.items()
        if term in document
    )
    return overlap / denominator


def _cosine(
    query: Counter[str],
    document: Counter[str],
    idf: Mapping[str, float],
) -> float:
    query_weights = {
        term: count * idf.get(term, 1.0) for term, count in query.items()
    }
    document_weights = {
        term: count * idf.get(term, 1.0) for term, count in document.items()
    }
    query_norm = math.sqrt(sum(value * value for value in query_weights.values()))
    document_norm = math.sqrt(sum(value * value for value in document_weights.values()))
    if query_norm <= 0 or document_norm <= 0:
        return 0.0
    return sum(
        query_weights.get(term, 0.0) * value
        for term, value in document_weights.items()
    ) / (query_norm * document_norm)


def order_candidates_by_similarity(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Place directly similar current titles together without merging them."""
    if len(candidates) < 2:
        return list(candidates)

    terms = [_weighted_terms(str(item.get("title") or "")) for item in candidates]
    bigrams = [
        _character_bigrams(str(item.get("title") or "")) for item in candidates
    ]
    term_idf = _idf([set(item) for item in terms])
    bigram_idf = _idf([set(item) for item in bigrams])

    def similarity(left: int, right: int) -> float:
        left_score = _term_coverage(terms[left], terms[right], term_idf) or _cosine(
            bigrams[left], bigrams[right], bigram_idf
        )
        right_score = _term_coverage(terms[right], terms[left], term_idf) or _cosine(
            bigrams[right], bigrams[left], bigram_idf
        )
        return math.sqrt(left_score * right_score)

    remaining = set(range(len(candidates)))
    ordered: list[dict[str, Any]] = []
    for anchor in range(len(candidates)):
        if anchor not in remaining:
            continue
        remaining.remove(anchor)
        ordered.append(candidates[anchor])
        neighbours = sorted(
            (
                (similarity(anchor, candidate_index), candidate_index)
                for candidate_index in remaining
            ),
            key=lambda item: (-item[0], item[1]),
        )
        for score, candidate_index in neighbours:
            if score < SIMILARITY_ORDER_THRESHOLD:
                break
            remaining.remove(candidate_index)
            ordered.append(candidates[candidate_index])
    return ordered


def _active_event_titles(
    history: Mapping[str, Any], *, at: datetime
) -> list[dict[str, Any]]:
    titles: list[dict[str, Any]] = []
    for event in history.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        event_id = str(event.get("event_id") or "").strip()
        label = str(event.get("label") or "").strip()
        if not event_id or label not in {"news", "fun"}:
            continue
        for item in event.get("titles") or []:
            if not isinstance(item, Mapping) or not _is_active(
                item.get("last_seen_at"), at=at
            ):
                continue
            title = " ".join(str(item.get("text") or "").split())
            if not title:
                continue
            titles.append(
                {
                    "event_id": event_id,
                    "label": label,
                    "title": title,
                    "last_seen_at": str(item.get("last_seen_at") or ""),
                    "terms": _weighted_terms(title),
                    "people": _person_terms(title),
                    "bigrams": _character_bigrams(title),
                }
            )
    return titles


def attach_history_matches(
    candidates: list[dict[str, Any]],
    history: Mapping[str, Any],
    *,
    at: datetime,
) -> tuple[
    list[dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[str, int],
]:
    """Attach bounded local title matches without making a duplicate decision."""
    historical_titles = _active_event_titles(history, at=at)
    candidate_payloads = [dict(candidate) for candidate in candidates]
    if not historical_titles:
        return candidate_payloads, {}, {
            "active_event_count": 0,
            "history_match_count": 0,
            "history_prompt_chars": 0,
        }

    term_idf = _idf([set(item["terms"]) for item in historical_titles])
    bigram_idf = _idf([set(item["bigrams"]) for item in historical_titles])
    ranked_by_candidate: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidate_id = int(candidate["id"])
        query_terms = _weighted_terms(str(candidate.get("title") or ""))
        query_people = _person_terms(str(candidate.get("title") or ""))
        query_bigrams = _character_bigrams(str(candidate.get("title") or ""))
        best_by_event: dict[str, dict[str, Any]] = {}
        for item in historical_titles:
            word_score = _term_coverage(query_terms, item["terms"], term_idf)
            score = word_score or _cosine(query_bigrams, item["bigrams"], bigram_idf)
            elapsed = _elapsed_since(item["last_seen_at"], at=at)
            recent_same_person = bool(query_people.intersection(item["people"])) and (
                elapsed is not None and elapsed <= RECENT_PERSON_WINDOW
            )
            if score < MATCH_SCORE_THRESHOLD and not recent_same_person:
                continue
            current = best_by_event.get(item["event_id"])
            match = {
                **item,
                "score": score,
                "recent_same_person": recent_same_person,
            }
            if current is None or (
                recent_same_person,
                score,
            ) > (
                current["recent_same_person"],
                current["score"],
            ):
                best_by_event[item["event_id"]] = match
        ranked_by_candidate[candidate_id] = sorted(
            best_by_event.values(),
            key=lambda item: (
                -int(item["recent_same_person"]),
                -item["score"],
                item["title"],
            ),
        )[:MATCHES_PER_TITLE]

    payload_by_id = {int(item["id"]): item for item in candidate_payloads}
    history_lookup: dict[int, dict[str, Any]] = {}
    history_id_by_event: dict[str, int] = {}
    for rank in range(MATCHES_PER_TITLE):
        proposals = [
            (candidate_id, matches[rank])
            for candidate_id, matches in ranked_by_candidate.items()
            if len(matches) > rank
        ]
        proposals.sort(
            key=lambda pair: (
                -int(pair[1]["recent_same_person"]),
                -pair[1]["score"],
                pair[0],
            )
        )
        for candidate_id, match in proposals:
            event_id = match["event_id"]
            history_id = history_id_by_event.get(event_id)
            if history_id is None:
                history_id = len(history_lookup) + 1
                history_id_by_event[event_id] = history_id
                history_lookup[history_id] = {
                    "event_id": event_id,
                    "score": match["score"],
                }
            history_item = {
                "history_id": history_id,
                "title": match["title"],
                "last_seen_date": str(match["last_seen_at"])[:10],
            }
            payload_by_id[candidate_id].setdefault("history", []).append(history_item)

    prompt_chars = len(
        json.dumps(candidate_payloads, ensure_ascii=False, separators=(",", ":"))
    ) - len(
        json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    )
    active_event_count = len({item["event_id"] for item in historical_titles})
    return candidate_payloads, history_lookup, {
        "active_event_count": active_event_count,
        "history_match_count": len(history_lookup),
        "history_prompt_chars": prompt_chars,
    }


def _add_event_title(
    event: dict[str, Any], title: str, *, origin: str, at: datetime
) -> None:
    normalized_title = " ".join(title.split())
    key = _title_dedupe_key(normalized_title)
    if not key:
        return
    timestamp = at.isoformat(timespec="seconds")
    raw_titles = event.setdefault("titles", [])
    for item in raw_titles:
        if _title_dedupe_key(str(item.get("text") or "")) == key:
            item["last_seen_at"] = timestamp
            return
    raw_titles.append(
        {
            "text": normalized_title,
            "normalized": key,
            "origin": origin,
            "last_seen_at": timestamp,
        }
    )


def record_first_pass(
    path: str | Path,
    *,
    exact_titles: list[str],
    analyzed_titles: list[str],
    seen_topics: list[Mapping[str, Any]],
    at: datetime,
) -> None:
    """Cache every judged title and refresh model-confirmed seen topics."""
    timestamp = at.isoformat(timespec="seconds")
    first_date = at.date() - timedelta(days=ACTIVE_DAYS - 1)
    cached_titles: dict[str, str] = {}
    for raw_title in [*exact_titles, *analyzed_titles]:
        title = " ".join(str(raw_title or "").split())
        key = _title_dedupe_key(title)
        if key:
            cached_titles[key] = title

    with database_transaction(path, immediate=True) as connection:
        connection.execute(
            "DELETE FROM hotlist_title_cache WHERE substr(last_seen_at, 1, 10) < ?",
            (first_date.isoformat(),),
        )
        connection.executemany(
            """
            INSERT INTO hotlist_title_cache(normalized_title, title, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(normalized_title) DO UPDATE SET
                title = excluded.title,
                last_seen_at = excluded.last_seen_at
            """,
            [(key, title, timestamp) for key, title in cached_titles.items()],
        )

        for topic in seen_topics:
            event_id = str(topic.get("matched_event_id") or "")
            row = connection.execute(
                "SELECT * FROM hotlist_topics WHERE topic_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"热榜历史中不存在事件：{event_id}")
            event = _topic_row(row)
            representative = topic.get("representative")
            if isinstance(representative, Mapping):
                _add_event_title(
                    event,
                    str(representative.get("title") or ""),
                    origin="representative",
                    at=at,
                )
            for related in topic.get("related") or []:
                if isinstance(related, Mapping):
                    _add_event_title(
                        event,
                        str(related.get("title") or ""),
                        origin="related",
                        at=at,
                    )
            payload = {
                "titles": event["titles"],
                "updates": event["updates"],
            }
            connection.execute(
                """
                UPDATE hotlist_topics
                SET last_seen_at = ?, payload_json = ?
                WHERE topic_id = ?
                """,
                (
                    timestamp,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    event_id,
                ),
            )


def _card_titles(
    topic: Mapping[str, Any], card: Mapping[str, Any]
) -> list[tuple[str, str]]:
    titles: list[tuple[str, str]] = []
    representative = topic.get("representative")
    if isinstance(representative, Mapping):
        titles.append((str(representative.get("title") or ""), "representative"))
    for related in topic.get("related") or []:
        if isinstance(related, Mapping):
            titles.append((str(related.get("title") or ""), "related"))
    titles.append((str(card.get("title") or ""), "card"))
    for field in ("evidence", "research_evidence"):
        for item in card.get(field) or []:
            if isinstance(item, Mapping):
                titles.append(
                    (
                        str(item.get("title") or item.get("source_title") or ""),
                        "evidence",
                    )
                )
    return [(title, origin) for title, origin in titles if title.strip()]


def _update_titles(topic: Mapping[str, Any], card: Mapping[str, Any]) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()
    for title, origin in _card_titles(topic, card):
        if origin == "evidence":
            continue
        key = _title_dedupe_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        titles.append(title)
    return titles


def record_final_batch(
    path: str | Path,
    selection: Mapping[str, Any],
    event_results: list[Mapping[str, Any]],
    *,
    at: datetime,
) -> dict[Any, str]:
    """Persist current topic knowledge and return selection-to-topic IDs."""
    topics_by_id = {
        topic["representative_id"]: topic
        for topic in selection.get("topics") or []
        if isinstance(topic, Mapping)
    }
    timestamp = at.isoformat(timespec="seconds")
    stored_ids: dict[Any, str] = {}
    with database_transaction(path, immediate=True) as connection:
        for card in event_results:
            selection_topic_id = card.get("topic_id")
            topic = topics_by_id.get(selection_topic_id)
            if not isinstance(topic, Mapping):
                raise ValueError(f"找不到话题 {selection_topic_id!r} 的筛选结果")
            relation = str(topic.get("event_relation") or "new")
            label = str(topic.get("label") or "")
            status = str(card.get("status") or "")
            if status not in _EVENT_STATUSES:
                raise ValueError(f"知识卡包含无效状态：{status!r}")

            if relation == "update":
                stored_topic_id = str(topic.get("matched_event_id") or "")
                row = connection.execute(
                    "SELECT * FROM hotlist_topics WHERE topic_id = ?",
                    (stored_topic_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"热榜历史中不存在事件：{stored_topic_id}")
                event = _topic_row(row)
            elif relation == "new":
                stored_topic_id = uuid.uuid4().hex
                event = {
                    "event_id": stored_topic_id,
                    "label": label,
                    "status": status,
                    "last_result_status": status,
                    "first_seen_at": timestamp,
                    "last_seen_at": timestamp,
                    "updated_at": str(card.get("updated_at") or timestamp),
                    "title": str(card.get("title") or topic.get("title") or ""),
                    "knowledge": str(card.get("knowledge") or ""),
                    "chat_context": str(card.get("chat_context") or ""),
                    "latest_update": None,
                    "share_score": float(card.get("share_score") or 0),
                    "titles": [],
                    "updates": [],
                }
            else:
                raise ValueError(f"无效的事件关系：{relation!r}")

            stored_ids[selection_topic_id] = stored_topic_id
            event["last_seen_at"] = timestamp
            event["last_result_status"] = status
            for title, origin in _card_titles(topic, card):
                _add_event_title(event, title, origin=origin, at=at)

            if relation == "new" or status == "complete":
                event.update(
                    {
                        "status": status,
                        "updated_at": str(card.get("updated_at") or timestamp),
                        "knowledge": str(card.get("knowledge") or ""),
                        "chat_context": str(card.get("chat_context") or ""),
                        "share_score": float(card.get("share_score") or 0),
                    }
                )
                if relation == "new":
                    event["title"] = str(card.get("title") or topic.get("title") or "")
                    event["latest_update"] = None
                else:
                    event["latest_update"] = str(card.get("latest_update") or "").strip()
            if relation == "update" and status == "complete":
                update_titles = _update_titles(topic, card)
                knowledge = str(card.get("knowledge") or "").strip()
                latest_update = str(card.get("latest_update") or "").strip()
                if not update_titles or not knowledge or not latest_update:
                    raise ValueError(
                        "完整更新缺少可序列化的 titles、knowledge 或 latest_update"
                    )
                event["updates"].append(
                    {
                        "updated_at": str(card.get("updated_at") or timestamp),
                        "titles": update_titles,
                        "knowledge": knowledge,
                        "latest_update": latest_update,
                        "share_score": card.get("share_score"),
                    }
                )

            payload = {
                "titles": event["titles"],
                "updates": event["updates"],
            }
            values = (
                stored_topic_id,
                str(event["label"]),
                str(event["status"]),
                str(event["last_result_status"]),
                str(event["first_seen_at"]),
                str(event["last_seen_at"]),
                str(event["updated_at"]),
                str(event["title"]),
                str(event["knowledge"]),
                str(event["chat_context"]),
                event.get("latest_update"),
                float(event["share_score"]),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
            connection.execute(
                """
                INSERT INTO hotlist_topics(
                    topic_id, label, status, last_result_status,
                    first_seen_at, last_seen_at, updated_at, title,
                    knowledge, chat_context, latest_update, share_score, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    label = excluded.label,
                    status = excluded.status,
                    last_result_status = excluded.last_result_status,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    title = excluded.title,
                    knowledge = excluded.knowledge,
                    chat_context = excluded.chat_context,
                    latest_update = excluded.latest_update,
                    share_score = excluded.share_score,
                    payload_json = excluded.payload_json
                """,
                values,
            )
    return stored_ids


__all__ = [
    "active_exact_title_keys",
    "attach_history_matches",
    "load_history",
    "order_candidates_by_similarity",
    "recent_update_timeline",
    "record_final_batch",
    "record_first_pass",
    "reference_time",
]
