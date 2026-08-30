"""Persist and match recent hot-list titles across learning batches."""

from __future__ import annotations

import json
import math
import re
import threading
import unicodedata
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentscroll.collector.newsnow import _title_dedupe_key
from agentscroll.collector.sources.cjk import CHINESE_STOPWORDS, segment_with_pos

HISTORY_FILENAME = "hotlist_history.json"
HISTORY_VERSION = 1
ACTIVE_DAYS = 7
MATCHES_PER_TITLE = 3
MATCH_SCORE_THRESHOLD = 0.4
SIMILARITY_ORDER_THRESHOLD = 0.4
RECENT_PERSON_WINDOW = timedelta(hours=6)

_write_lock = threading.Lock()
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_VISIBLE_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]", re.UNICODE)
_EVENT_STATUSES = {"complete", "needs_research", "rejected"}


def history_path(hotlist: Mapping[str, Any] | str | Path) -> Path:
    """Resolve history beside the hot-list snapshot, replacing the old cache."""
    snapshot_file: str | Path | None = None
    if isinstance(hotlist, (str, Path)):
        snapshot_file = hotlist
    elif isinstance(hotlist.get("snapshot_file"), (str, Path)):
        snapshot_file = hotlist["snapshot_file"]
    if snapshot_file:
        return Path(snapshot_file).expanduser().resolve().parent / HISTORY_FILENAME
    return (Path.cwd() / "outputs" / "hotlists" / HISTORY_FILENAME).resolve()


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


def empty_history() -> dict[str, Any]:
    return {
        "version": HISTORY_VERSION,
        "updated_at": None,
        "ignored_titles": [],
        "events": [],
    }


def load_history(path: str | Path) -> dict[str, Any]:
    """Load only the current history schema; old cache files are ignored."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return empty_history()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("version") != HISTORY_VERSION:
        raise ValueError(f"不支持的热榜历史文件：{source}")
    ignored = raw.get("ignored_titles")
    events = raw.get("events")
    if not isinstance(ignored, list) or not isinstance(events, list):
        raise ValueError(f"热榜历史文件结构无效：{source}")
    return {
        "version": HISTORY_VERSION,
        "updated_at": raw.get("updated_at"),
        "ignored_titles": [dict(item) for item in ignored if isinstance(item, Mapping)],
        "events": [dict(item) for item in events if isinstance(item, Mapping)],
    }


def save_history(path: str | Path, history: Mapping[str, Any], *, at: datetime) -> None:
    destination = Path(path).expanduser().resolve()
    document = {
        "version": HISTORY_VERSION,
        "updated_at": at.isoformat(timespec="seconds"),
        "ignored_titles": list(history.get("ignored_titles") or []),
        "events": list(history.get("events") or []),
    }
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    with _write_lock:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)


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
    for item in history.get("ignored_titles") or []:
        if not isinstance(item, Mapping) or not _is_active(item.get("last_seen_at"), at=at):
            continue
        key = _title_dedupe_key(str(item.get("text") or ""))
        if key:
            keys.add(key)
    for event in history.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        for item in event.get("titles") or []:
            if not isinstance(item, Mapping) or not _is_active(
                item.get("last_seen_at"), at=at
            ):
                continue
            key = _title_dedupe_key(str(item.get("text") or ""))
            if key:
                keys.add(key)
    return keys


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
            hours_ago = (
                round(elapsed.total_seconds() / 3600, 1)
                if elapsed is not None
                else None
            )
            recent_same_person = bool(query_people.intersection(item["people"])) and (
                elapsed is not None and elapsed <= RECENT_PERSON_WINDOW
            )
            if score < MATCH_SCORE_THRESHOLD and not recent_same_person:
                continue
            current = best_by_event.get(item["event_id"])
            match = {
                **item,
                "score": score,
                "hours_ago": hours_ago,
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
                "hours_ago": match["hours_ago"],
            }
            if match["recent_same_person"]:
                history_item["recent_same_person"] = True
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


def _refresh_exact_titles(
    history: dict[str, Any], titles: list[str], *, at: datetime
) -> None:
    keys = {_title_dedupe_key(title) for title in titles if title.strip()}
    timestamp = at.isoformat(timespec="seconds")
    for item in history.get("ignored_titles") or []:
        if _title_dedupe_key(str(item.get("text") or "")) in keys:
            item["last_seen_at"] = timestamp
    for event in history.get("events") or []:
        for item in event.get("titles") or []:
            if _title_dedupe_key(str(item.get("text") or "")) in keys:
                item["last_seen_at"] = timestamp
                event["last_seen_at"] = timestamp


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


def _event_by_id(history: Mapping[str, Any], event_id: str) -> dict[str, Any]:
    for event in history.get("events") or []:
        if str(event.get("event_id") or "") == event_id:
            return event
    raise ValueError(f"热榜历史中不存在事件：{event_id}")


def record_first_pass(
    path: str | Path,
    history: dict[str, Any],
    *,
    exact_titles: list[str],
    ignored_titles: list[str],
    seen_topics: list[Mapping[str, Any]],
    at: datetime,
) -> None:
    """Commit exact hits, unselected titles, and model-confirmed seen aliases."""
    _refresh_exact_titles(history, exact_titles, at=at)
    timestamp = at.isoformat(timespec="seconds")
    first_date = at.date() - timedelta(days=ACTIVE_DAYS - 1)
    retained_ignored = []
    for item in history.get("ignored_titles") or []:
        last_seen_at = _parse_timestamp(item.get("last_seen_at"))
        if last_seen_at is not None and last_seen_at.date() >= first_date:
            retained_ignored.append(item)
    ignored_by_key = {
        _title_dedupe_key(str(item.get("text") or "")): item
        for item in retained_ignored
        if _title_dedupe_key(str(item.get("text") or ""))
    }
    for title in ignored_titles:
        normalized_title = " ".join(title.split())
        key = _title_dedupe_key(normalized_title)
        if not key:
            continue
        item = ignored_by_key.get(key)
        if item is None:
            item = {"text": normalized_title, "normalized": key}
            retained_ignored.append(item)
            ignored_by_key[key] = item
        item["last_seen_at"] = timestamp
    history["ignored_titles"] = retained_ignored

    for topic in seen_topics:
        event_id = str(topic.get("matched_event_id") or "")
        event = _event_by_id(history, event_id)
        event["last_seen_at"] = timestamp
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
    save_history(path, history, at=at)


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


def record_final_batch(
    path: str | Path,
    selection: Mapping[str, Any],
    event_results: list[Mapping[str, Any]],
    *,
    at: datetime,
) -> None:
    """Record durable new cards and in-place updates in the event history."""
    history = load_history(path)
    topics_by_id = {
        topic["representative_id"]: topic
        for topic in selection.get("topics") or []
        if isinstance(topic, Mapping)
    }
    timestamp = at.isoformat(timespec="seconds")
    all_event_title_keys: set[str] = set()
    for card in event_results:
        topic_id = card.get("topic_id")
        topic = topics_by_id.get(topic_id)
        if not isinstance(topic, Mapping):
            raise ValueError(f"找不到话题 {topic_id!r} 的筛选结果")
        relation = str(topic.get("event_relation") or "new")
        label = str(topic.get("label") or "")
        status = str(card.get("status") or "")
        card_file = Path(str(card.get("card_file") or "")).expanduser().resolve()
        card_topic_id = card.get("card_topic_id")
        if status not in _EVENT_STATUSES:
            raise ValueError(f"知识卡包含无效状态：{status!r}")
        if not card_file.is_file() or card_topic_id is None:
            raise ValueError(f"话题 {topic_id!r} 缺少可用知识卡位置")
        if relation == "update":
            event = _event_by_id(history, str(topic.get("matched_event_id") or ""))
        elif relation == "new":
            event = {
                "event_id": uuid.uuid4().hex,
                "label": label,
                "status": status,
                "last_result_status": status,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "titles": [],
                "current_card_file": str(card_file),
                "current_topic_id": card_topic_id,
            }
            history.setdefault("events", []).append(event)
        else:
            raise ValueError(f"无效的事件关系：{relation!r}")

        event["last_seen_at"] = timestamp
        event["last_result_status"] = status
        for title, origin in _card_titles(topic, card):
            _add_event_title(event, title, origin=origin, at=at)
            key = _title_dedupe_key(title)
            if key:
                all_event_title_keys.add(key)
        if relation == "new" or status == "complete":
            event["status"] = status
            event["current_card_file"] = str(card_file)
            event["current_topic_id"] = card_topic_id

    history["ignored_titles"] = [
        item
        for item in history.get("ignored_titles") or []
        if _title_dedupe_key(str(item.get("text") or "")) not in all_event_title_keys
    ]
    save_history(path, history, at=at)


__all__ = [
    "active_exact_title_keys",
    "attach_history_matches",
    "history_path",
    "load_history",
    "order_candidates_by_similarity",
    "record_final_batch",
    "record_first_pass",
    "reference_time",
]
