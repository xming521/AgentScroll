"""Tests for durable hot-list topic state and title matching."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentscroll.storage import connect_database
from agentscroll.workflows.hotlist_state import (
    active_exact_title_keys,
    load_history,
    recent_update_timeline,
    record_final_batch,
    record_first_pass,
)
from agentscroll.workflows.knowledge_card import (
    _model_topic_payload,
    _prompt_payload,
    _selection_with_update_contexts,
)


def _new_selection() -> dict[str, object]:
    return {
        "topics": [
            {
                "representative_id": 1,
                "representative": {"title": "尼泊尔发生严重泥石流"},
                "related": [],
                "label": "news",
                "event_relation": "new",
            }
        ]
    }


def _update_selection(topic_id: str) -> dict[str, object]:
    return {
        "topics": [
            {
                "representative_id": 1,
                "representative": {"title": "尼泊尔泥石流已致974人遇难"},
                "related": [
                    {"title": "尼泊尔泥石流已致974人遇难"},
                    {"title": "尼泊尔泥石流仍有4247人失联"},
                ],
                "label": "news",
                "event_relation": "update",
                "matched_event_id": topic_id,
            }
        ]
    }


def _card(**overrides: object) -> dict[str, object]:
    card: dict[str, object] = {
        "topic_id": 1,
        "status": "complete",
        "title": "尼泊尔发生严重泥石流",
        "updated_at": "2026-08-31T03:00:00+00:00",
        "knowledge": "尼泊尔发生严重泥石流，已有人员伤亡和失联。",
        "chat_context": "聊到近期灾害时可以提起。",
        "latest_update": None,
        "share_score": 3.0,
        "rejection_reason": "",
        "share": None,
        "research_sources": [],
        "evidence": [],
        "research_evidence": [],
        "collection_attempts": [],
    }
    card.update(overrides)
    return card


def _seed_topic(database: Path) -> str:
    stored = record_final_batch(
        database,
        _new_selection(),
        [_card()],
        at=datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc),
    )
    return stored[1]


def test_complete_update_replaces_current_card_and_appends_timeline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    topic_id = _seed_topic(database)

    record_final_batch(
        database,
        _update_selection(topic_id),
        [
            _card(
                title="尼泊尔泥石流已致974人遇难",
                updated_at="2026-09-01T03:07:00+00:00",
                knowledge="灾情数字已由903人更新为974人。",
                chat_context="聊到灾情新数字时可以提起。",
                latest_update="死亡人数升至974人，仍有4247人失联。",
                share_score=4.0,
            )
        ],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )

    event = load_history(database)["events"][0]
    assert event["knowledge"] == "灾情数字已由903人更新为974人。"
    assert event["latest_update"] == "死亡人数升至974人，仍有4247人失联。"
    assert event["updates"] == [
        {
            "updated_at": "2026-09-01T03:07:00+00:00",
            "titles": [
                "尼泊尔泥石流已致974人遇难",
                "尼泊尔泥石流仍有4247人失联",
            ],
            "knowledge": "灾情数字已由903人更新为974人。",
            "latest_update": "死亡人数升至974人，仍有4247人失联。",
            "share_score": 4.0,
        }
    ]
    connection = connect_database(database)
    try:
        payload = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM hotlist_topics WHERE topic_id = ?",
                    (topic_id,),
                ).fetchone()["payload_json"]
            )
        )
    finally:
        connection.close()
    assert set(payload) == {"titles", "updates"}


def test_non_complete_update_keeps_current_card(tmp_path: Path) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    topic_id = _seed_topic(database)

    record_final_batch(
        database,
        _update_selection(topic_id),
        [
            _card(
                status="needs_research",
                knowledge="尚未确认。",
                chat_context="",
                latest_update=None,
                share_score=0.0,
            )
        ],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )

    event = load_history(database)["events"][0]
    assert event["status"] == "complete"
    assert event["last_result_status"] == "needs_research"
    assert event["knowledge"] == "尼泊尔发生严重泥石流，已有人员伤亡和失联。"
    assert event["updates"] == []


def test_update_context_reads_current_card_from_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    topic_id = _seed_topic(database)
    update_selection = _update_selection(topic_id)
    record_final_batch(
        database,
        update_selection,
        [
            _card(
                title="受灾核心区发现婴儿车和衣物",
                latest_update="搜救人员在核心区发现婴儿车和衣物。",
            )
        ],
        at=datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc),
    )
    selection = _update_selection(topic_id)
    selection["database_path"] = str(database)
    at = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)

    event = load_history(database)["events"][0]
    assert recent_update_timeline(event, at=at) == [
        "尼泊尔泥石流已致974人遇难",
        "尼泊尔泥石流仍有4247人失联",
        "受灾核心区发现婴儿车和衣物",
    ]

    enriched = _selection_with_update_contexts(
        {
            "topics": [
                {
                    "topic_id": 1,
                    "title": "尼泊尔泥石流已致974人遇难",
                    "label": "news",
                    "evidence": [],
                }
            ]
        },
        selection,
        at=at,
    )
    model_payload = _model_topic_payload(_prompt_payload(enriched)[0], research=True)

    assert model_payload["timeline"] == [
        "尼泊尔泥石流已致974人遇难",
        "尼泊尔泥石流仍有4247人失联",
        "受灾核心区发现婴儿车和衣物",
    ]
    assert model_payload["previous_card"]["knowledge"] == (
        "尼泊尔发生严重泥石流，已有人员伤亡和失联。"
    )


def test_title_cache_records_all_successfully_judged_titles(tmp_path: Path) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    at = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    record_first_pass(
        database,
        exact_titles=["缓存命中标题"],
        analyzed_titles=["入选标题", "未入选标题"],
        seen_topics=[],
        at=at,
    )

    history = load_history(database)
    assert {item["text"] for item in history["title_cache"]} == {
        "缓存命中标题",
        "入选标题",
        "未入选标题",
    }
    assert len(active_exact_title_keys(history, at=at)) == 3


def test_sqlite_schema_has_only_three_business_tables(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "agentscroll.sqlite3")
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()

    assert tables == {"hotlist_topics", "hotlist_title_cache", "share_jobs"}
