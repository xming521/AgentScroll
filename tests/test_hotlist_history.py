from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentscroll.workflows.hotlist_history import (
    recent_update_titles,
    record_final_batch,
)
from agentscroll.workflows.knowledge_card import (
    _model_topic_payload,
    _prompt_payload,
    _selection_with_update_contexts,
)


def _write_history(
    path: Path,
    *,
    event_id: str,
    card_file: Path,
    updates: list[dict[str, object]] | None = None,
) -> None:
    event = {
        "event_id": event_id,
        "label": "news",
        "status": "complete",
        "last_result_status": "complete",
        "first_seen_at": "2026-08-31T00:00:00+00:00",
        "last_seen_at": "2026-08-31T00:00:00+00:00",
        "titles": [],
        "current_card_file": str(card_file),
        "current_topic_id": 1,
    }
    if updates is not None:
        event["updates"] = updates
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-08-31T00:00:00+00:00",
                "ignored_titles": [],
                "events": [event],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _update_selection(event_id: str) -> dict[str, object]:
    return {
        "topics": [
            {
                "representative_id": 1,
                "representative": {"title": "尼泊尔泥石流已致974人遇难"},
                "related": [
                    {"title": "尼泊尔泥石流已致974人遇难"},
                    {"title": "尼泊尔泥石流仍有4247人失联"},
                ],
                "label": "尼泊尔泥石流灾情",
                "event_relation": "update",
                "matched_event_id": event_id,
            }
        ]
    }


def test_complete_update_appends_serialized_update(tmp_path: Path) -> None:
    event_id = "event-1"
    card_file = tmp_path / "card.json"
    card_file.write_text("{}", encoding="utf-8")
    history_file = tmp_path / "hotlist_history.json"
    _write_history(history_file, event_id=event_id, card_file=card_file)

    record_final_batch(
        history_file,
        _update_selection(event_id),
        [
            {
                "topic_id": 1,
                "status": "complete",
                "card_file": str(card_file),
                "card_topic_id": 1,
                "title": "尼泊尔泥石流已致974人遇难",
                "updated_at": "2026-09-01T03:07:00+00:00",
                "knowledge": "灾情数字已由903人更新为974人。",
                "latest_update": "死亡人数升至974人，仍有4247人失联。",
                "share_score": 4.0,
                "evidence": [],
                "research_evidence": [],
            }
        ],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )

    history = json.loads(history_file.read_text(encoding="utf-8"))
    event = history["events"][0]
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


def test_non_complete_update_does_not_append_update(tmp_path: Path) -> None:
    event_id = "event-1"
    card_file = tmp_path / "card.json"
    card_file.write_text("{}", encoding="utf-8")
    history_file = tmp_path / "hotlist_history.json"
    _write_history(history_file, event_id=event_id, card_file=card_file)

    record_final_batch(
        history_file,
        _update_selection(event_id),
        [
            {
                "topic_id": 1,
                "status": "needs_research",
                "card_file": str(card_file),
                "card_topic_id": 1,
                "title": "尼泊尔泥石流已致974人遇难",
                "updated_at": "2026-09-01T03:07:00+00:00",
                "knowledge": "尚未确认。",
                "latest_update": "",
                "share_score": 0.0,
                "evidence": [],
                "research_evidence": [],
            }
        ],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )

    history = json.loads(history_file.read_text(encoding="utf-8"))
    assert "updates" not in history["events"][0]


def test_known_update_titles_are_filtered_sorted_and_passed_to_model(
    tmp_path: Path,
) -> None:
    event_id = "event-1"
    card_file = tmp_path / "card.json"
    card_file.write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "topic_id": 1,
                        "title": "尼泊尔泥石流灾情",
                        "status": "complete",
                        "knowledge": "尼泊尔发生严重泥石流，已有人员伤亡和失联。",
                        "chat_context": "聊到近期灾害时可以提起。",
                        "latest_update": {
                            "updated_at": "2026-08-31T03:00:00+00:00",
                            "title": "受灾核心区发现婴儿车和衣物",
                            "summary": "搜救人员在核心区发现婴儿车和衣物。",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    updates = [
        {
            "updated_at": "2026-08-31T03:00:00+00:00",
            "titles": [
                "尼泊尔泥石流已致９０３人遇难",
                "受灾核心区发现婴儿车和衣物",
            ],
        },
        {
            "updated_at": "2026-08-25T03:00:00+00:00",
            "titles": ["尼泊尔泥石流造成道路中断"],
        },
        {
            "updated_at": "2026-08-29T03:00:00+00:00",
            "titles": ["尼泊尔泥石流已致903人遇难"],
        },
    ]
    history_file = tmp_path / "hotlist_history.json"
    _write_history(
        history_file,
        event_id=event_id,
        card_file=card_file,
        updates=updates,
    )
    selection = _update_selection(event_id)
    selection["history_file"] = str(history_file)
    at = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)

    history_event = json.loads(history_file.read_text(encoding="utf-8"))[
        "events"
    ][0]
    assert recent_update_titles(history_event, at=at) == [
        "尼泊尔泥石流已致903人遇难",
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
    topic = _prompt_payload(enriched)[0]
    model_payload = _model_topic_payload(topic, research=True)

    assert model_payload["known_update_titles"] == [
        "尼泊尔泥石流已致903人遇难",
        "受灾核心区发现婴儿车和衣物",
    ]
    assert model_payload["previous_card"]["knowledge"] == (
        "尼泊尔发生严重泥石流，已有人员伤亡和失联。"
    )
