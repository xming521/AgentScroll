"""Tests for durable hot-list topic state and title matching."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentscroll.storage import connect_database
from agentscroll.workflows.hotlist_state import (
    active_exact_title_keys,
    load_history,
    match_new_topics_from_evidence,
    recent_update_timeline,
    record_final_batch,
    record_first_pass,
)
from agentscroll.workflows.knowledge_card_models import KnowledgeCard, KnowledgeShare
from agentscroll.workflows.knowledge_card import (
    _model_topic_payload,
    _prompt_payload,
    _recheck_share_history_matches,
    _save_selected_cards,
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
        "general_share_score": 2.5,
        "interest_share_score": 3.0,
        "hotlist_share_score": 0.0,
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
                general_share_score=4.0,
                interest_share_score=3.6,
                hotlist_share_score=0.0,
            )
        ],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )

    event = load_history(database)["events"][0]
    assert (event["general_share_score"], event["interest_share_score"], event["hotlist_share_score"]) == (4.0, 3.6, 0.0)
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
            "general_share_score": 4.0,
            "interest_share_score": 3.6,
            "hotlist_share_score": 0.0,
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
    assert (event["general_share_score"], event["interest_share_score"], event["hotlist_share_score"]) == (2.5, 3.0, 0.0)
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


def test_evidence_match_recalls_history_missing_from_hotlist_title() -> None:
    at = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
    history = {
        "events": [
            {
                "event_id": "jilong-event",
                "label": "news",
                "status": "complete",
                "title": "吉隆口岸上游堰塞湖已完全泄洪",
                "knowledge": (
                    "尼泊尔山洪泥石流波及西藏吉隆口岸，抢险机械已抵达"
                    "核心区，陆上通道正在抢通。"
                ),
                "latest_update": "抢险机械已进入吉隆口岸核心搜救区。",
                "titles": [
                    {
                        "text": "吉隆口岸陆上通道即将打通",
                        "last_seen_at": "2026-09-01T12:00:00+00:00",
                    }
                ],
            },
            {
                "event_id": "unrelated-event",
                "label": "news",
                "status": "complete",
                "title": "多所高校调整国庆假期",
                "knowledge": "多所高校调整放假安排，学生需要提前规划行程。",
                "latest_update": None,
                "titles": [
                    {
                        "text": "高校公布国庆放假安排",
                        "last_seen_at": "2026-09-01T12:00:00+00:00",
                    }
                ],
            },
        ]
    }
    topics = [
        {
            "topic_id": 10,
            "title": "失联人员深埋巨石和淤泥之下",
            "label": "news",
            "event_relation": "new",
            "evidence": [
                {
                    "source_title": "西藏吉隆救援现场",
                    "content": (
                        "尼泊尔山洪泥石流波及西藏吉隆口岸，失联人员深埋"
                        "巨石和淤泥之下，抢险机械已进入核心搜救区，陆上"
                        "通道仍在抢通。"
                    ),
                }
            ],
            "research_evidence": [],
        },
        {
            "topic_id": 11,
            "title": "张继科带课一个半小时25元",
            "label": "fun",
            "event_relation": "new",
            "evidence": [{"content": "一场面向公众的乒乓球体验课。"}],
            "research_evidence": [],
        },
    ]

    matches = match_new_topics_from_evidence(topics, history, at=at)

    assert set(matches) == {10}
    assert matches[10]["event_id"] == "jilong-event"
    assert matches[10]["coverage"] >= 0.65


@pytest.mark.parametrize("duplicate_history", [False, True])
@pytest.mark.parametrize("hotlist_score,interest_score", [(0, 0), (4, 0), (0, 3.8)])
def test_new_share_is_rerun_once_as_update(
    tmp_path: Path,
    monkeypatch,
    hotlist_score: int,
    interest_score: float,
    duplicate_history: bool,
) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    event_id = _seed_topic(database)
    if duplicate_history:
        history = load_history(database)
        original = next(event for event in history["events"] if event["event_id"] == event_id)
        history["events"].append({**original, "event_id": "duplicate", "knowledge": "其他历史已经报道救援机械到场。"})
        monkeypatch.setattr("agentscroll.workflows.hotlist_state.load_history", lambda _: history)
        monkeypatch.setattr("agentscroll.workflows.hotlist_state.match_new_topics_from_evidence", lambda *args, **kwargs: {
            1: {"event_id": event_id, "candidates": [{"event_id": event_id}, {"event_id": "duplicate"}]}
        })
        monkeypatch.setattr("agentscroll.workflows.knowledge_card._resolve_ambiguous_history_matches", lambda *args, **kwargs: (
            {1: {"event_id": event_id, "related_event_ids": ["duplicate"]}}, [],
            {"request_count": 1, "usage": {"input_tokens": 20}},
        ))
    selection = {
        "database_path": str(database),
        "topics": [
            {
                "representative_id": 1,
                "representative": {"title": "失联人员深埋巨石和淤泥之下"},
                "related": [],
                "label": "news",
                "event_relation": "new",
            }
        ],
    }
    evidence = {
        "topics": [
            {
                "topic_id": 1,
                "title": "失联人员深埋巨石和淤泥之下",
                "label": "news",
                "event_relation": "new",
                "evidence": [
                    {
                        "platform": "weibo",
                        "title": "失联人员深埋巨石和淤泥之下",
                        "url": "https://example.com/clue",
                        "content": "大型机械进入受灾区域继续搜寻失联人员。",
                        "comments": [{"text": "希望平安"}],
                    }
                ],
                "research_evidence": [
                    {
                        "source_id": "r1",
                        "platform": "wechat",
                        "source_title": "尼泊尔泥石流救援仍在继续",
                        "url": "https://example.com/rescue",
                        "content": (
                            "尼泊尔发生严重泥石流，已有人员伤亡和失联，"
                            "救援人员正在灾区持续搜寻。"
                        ),
                        "comments": [],
                    }
                ],
            }
        ]
    }
    provisional = KnowledgeCard(
        status="complete",
        rejection_reason="",
        knowledge="尼泊尔泥石流救援仍在继续。",
        chat_context="可以聊现场救援。",
        latest_update=None,
        share_score=interest_score or 4,
        general_share_score=0 if interest_score else (2.8 if hotlist_score else 4),
        interest_share_score=interest_score,
        hotlist_share_score=hotlist_score,
        share=KnowledgeShare(
            text="尼泊尔泥石流救援现场",
            source_id="e1",
            url="https://example.com/rescue",
            comment_id="e1c1",
            comment_type="platform",
            comment="希望平安",
        ),
        topic_id=1,
    )
    rerun = KnowledgeCard(
        status="rejected",
        rejection_reason="未找到新进展",
        knowledge="",
        chat_context="",
        latest_update=None,
        share_score=0,
        share=None,
        topic_id=1,
        research_sources=(),
    )
    captured: dict[str, object] = {}

    def fake_generate(topics, **kwargs):
        captured["topics"] = topics
        captured["kwargs"] = kwargs
        return [rerun], {"request_count": 1, "usage": {"input_tokens": 100}}

    monkeypatch.setattr(
        "agentscroll.workflows.knowledge_card._generate_topic_cards",
        fake_generate,
    )
    cards, revised_evidence, revised_selection, diagnostics = (
        _recheck_share_history_matches(
            [provisional],
            evidence=evidence,
            selection=selection,
            settings=SimpleNamespace(max_workers=1),
            at=datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc),
            effort="xhigh",
        )
    )

    rerun_topic = captured["topics"][0]
    assert rerun_topic["event_relation"] == "update"
    assert rerun_topic["matched_event_id"] == event_id
    assert rerun_topic["previous_card"]["knowledge"] == (
        "尼泊尔发生严重泥石流，已有人员伤亡和失联。"
    )
    assert captured["kwargs"]["research"] is True
    assert rerun_topic["research_evidence"][0]["source_id"] == "r1"
    assert cards == [rerun]
    assert revised_evidence["topics"][0]["event_relation"] == "update"
    assert revised_selection["topics"][0]["matched_event_id"] == event_id
    assert diagnostics["history_match_count"] == 1
    assert diagnostics["request_count"] == (2 if duplicate_history else 1)
    if duplicate_history:
        assert _model_topic_payload(rerun_topic, research=True)["related_history"][0]["knowledge"] == "其他历史已经报道救援机械到场。"
        assert diagnostics["usage"]["input_tokens"] == 120


def test_interest_only_score_triggers_share_history_recheck() -> None:
    card = KnowledgeCard(
        status="complete",
        rejection_reason="",
        knowledge="MCP 出现一项领域内更新。",
        chat_context="聊到 Agent 工具时可以提起。",
        latest_update=None,
        share_score=3.9,
        general_share_score=2.7,
        interest_share_score=3.9,
        candidate_interest_keywords=("MCP",),
        share=KnowledgeShare(
            text="MCP 出现一项领域内更新",
            source_id="e1",
            url="https://example.com/mcp",
            comment_id="",
            comment_type="generated",
            comment="这个值得看看",
        ),
        topic_id=1,
    )
    evidence = {
        "topics": [
            {
                "topic_id": 1,
                "title": "MCP 出现一项领域内更新",
                "label": "news",
                "candidate_interest_keywords": ["MCP"],
                "event_relation": "new",
                "evidence": [],
            }
        ]
    }

    cards, _evidence, _selection, diagnostics = (
        _recheck_share_history_matches(
            [card],
            evidence=evidence,
            selection={"topics": []},
            settings=SimpleNamespace(),
            at=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc),
            effort="xhigh",
        )
    )

    assert cards == [card]
    assert diagnostics["triggered_topic_count"] == 1
    assert diagnostics["history_match_count"] == 0


def test_selected_save_records_share_and_used_source_titles(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    topic = {
        "topic_id": 1,
        "title": "测试热点",
        "label": "news",
        "event_relation": "new",
        "evidence": [
            {
                "title": "实际使用的来源标题",
                "url": "https://example.com/used",
            },
            {
                "title": "没有使用的来源标题",
                "url": "https://example.com/unused",
            },
        ],
    }
    selection = {
        "database_path": str(database),
        "topics": [
            {
                "representative_id": 1,
                "representative": {"title": "测试热点"},
                "related": [],
                "label": "news",
                "event_relation": "new",
            }
        ],
    }
    card = KnowledgeCard(
        status="complete",
        rejection_reason="",
        knowledge="热点知识。",
        chat_context="聊天时可以提起。",
        latest_update=None,
        share_score=4,
        general_share_score=2.8,
        interest_share_score=3.6,
        hotlist_share_score=4,
        share=KnowledgeShare(
            text="最终分享文案",
            source_id="e1",
            url="https://example.com/used",
            comment_id="",
            comment_type="generated",
            comment="确实值得聊聊",
        ),
        topic_id=1,
    )

    _save_selected_cards(
        [card],
        evidence={"topics": [topic]},
        selection=selection,
        inference={},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
        record_history=True,
    )

    event = load_history(database)["events"][0]
    assert (event["general_share_score"], event["interest_share_score"], event["hotlist_share_score"]) == (2.8, 3.6, 4.0)

    titles = {
        item["text"]: item["origin"]
        for item in load_history(database)["events"][0]["titles"]
    }
    assert titles["最终分享文案"] == "share"
    assert titles["实际使用的来源标题"] == "evidence"
    assert "没有使用的来源标题" not in titles


def test_v2_topic_scores_migrate_without_inventing_components(tmp_path: Path) -> None:
    database = tmp_path / "agentscroll.sqlite3"
    topic_id = _seed_topic(database)
    connection = connect_database(database)
    for name in ("general_share_score", "interest_share_score", "hotlist_share_score"):
        connection.execute(f"ALTER TABLE hotlist_topics DROP COLUMN {name}")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    for _ in range(2):
        event = load_history(database)["events"][0]
        assert event["event_id"] == topic_id
        assert event["share_score"] == 3.0
        assert event["general_share_score"] is None
        assert event["interest_share_score"] is None
        assert event["hotlist_share_score"] is None

    record_final_batch(
        database,
        _update_selection(topic_id),
        [_card(latest_update="新增进展", general_share_score=2.6, interest_share_score=3.7, share_score=3.7)],
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )
    event = load_history(database)["events"][0]
    assert event["general_share_score"] == 2.6
    assert event["interest_share_score"] == 3.7
    assert event["hotlist_share_score"] == 0.0


def test_close_history_candidates_are_returned_for_semantic_resolution() -> None:
    text = "尼泊尔山洪泥石流波及西藏吉隆口岸，失联人员深埋巨石和淤泥，陆上通道仍在抢通。"
    events = [{
        "event_id": event_id, "label": "news", "status": "complete",
        "title": text, "knowledge": text, "latest_update": None,
        "titles": [{"text": text, "last_seen_at": "2026-09-01T12:00:00+00:00"}],
    } for event_id in ("a", "b")]
    matches = match_new_topics_from_evidence([{
        "topic_id": 1, "title": text, "label": "news", "event_relation": "new",
        "evidence": [{"content": text}],
    }], {"events": events}, at=datetime(2026, 9, 1, 13, tzinfo=timezone.utc))
    assert matches[1]["margin"] == 0
    assert [item["event_id"] for item in matches[1]["candidates"]] == ["a", "b"]


@pytest.mark.parametrize("ids,ok,expected,failed", [
    (["a", "b"], True, "a", []),
    (["b"], True, "b", []),
    ([], True, None, []),
    (["unknown"], True, None, [1]),
    (None, False, None, [1]),
])
def test_ambiguous_history_resolution(monkeypatch, ids, ok, expected, failed) -> None:
    from agentscroll.workflows.knowledge_card import _resolve_ambiguous_history_matches
    captured = []
    class Client:
        def generate_batch(self, requests):
            captured.extend(requests)
            return [SimpleNamespace(ok=ok, parsed_json={"event_ids": ids}, metadata={"usage": {"input_tokens": 10}})]
        def close(self):
            pass
    monkeypatch.setattr("agentscroll.config.build_configured_client", lambda _: Client())
    monkeypatch.setattr("agentscroll.config.make_configured_request", lambda prompt, settings, **kwargs: prompt)
    matches = {1: {"event_id": "a", "candidates": [{"event_id": "a"}, {"event_id": "b"}]}}
    cards = {1: SimpleNamespace(knowledge="当前知识", share=SimpleNamespace(text="当前分享"))}
    history = {"events": [{"event_id": value, "title": value, "knowledge": "历史知识"} for value in ("a", "b")]}
    resolved, failures, diagnostics = _resolve_ambiguous_history_matches(matches, cards, history, settings=None, effort="low")
    assert failures == failed
    assert (resolved.get(1) or {}).get("event_id") == expected
    if ids == ["a", "b"]:
        assert resolved[1]["related_event_ids"] == ["b"]
    assert "当前分享" in captured[0] and "历史知识" in captured[0]
    assert diagnostics["usage"]["input_tokens"] == 10
