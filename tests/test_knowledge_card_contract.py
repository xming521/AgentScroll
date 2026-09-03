"""Offline contracts for normalized knowledge cards and persisted batches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentscroll.workflows.knowledge_card_artifacts import (
    save_card_batch,
    save_share_batch,
)
from agentscroll.workflows.knowledge_card_models import (
    validate_cards,
    validate_research_cards,
)
from agentscroll.workflows.hotlist_state import load_history


def _topic(
    *,
    relation: str = "new",
    candidate_interest_keywords: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "topic_id": 1,
        "title": "测试热点",
        "label": "news",
        "event_relation": relation,
        "candidate_interest_keywords": list(candidate_interest_keywords),
        "evidence": [
            {
                "source_id": "e1",
                "platform": "weibo",
                "source_title": "原始标题",
                "url": "https://example.com/post/1",
                "content": "正文",
                "comments": [
                    {"comment_id": "e1c1", "text": "真实评论"},
                ],
            }
        ],
        "research_evidence": [
            {
                "source_id": "r1",
                "platform": "wechat",
                "source_title": "补搜标题",
                "url": "https://example.com/post/2",
                "content": "补搜正文",
                "comments": [],
            }
        ],
    }


def _raw_card(**overrides: object) -> dict[str, object]:
    card: dict[str, object] = {
        "status": "complete",
        "rejection_reason": "",
        "knowledge": "热点知识",
        "chat_context": "聊天时可以提起。",
        "latest_update": None,
        "general_share_score": 3.0,
        "interest_share_score": 0,
        "share": {
            "text": "分享正文",
            "source_id": "e1",
            "comment_id": "e1c1",
            "generated_comment": "",
        },
    }
    card.update(overrides)
    return card


def test_validate_cards_normalizes_topic_and_platform_comment() -> None:
    cards = validate_cards([_raw_card()], [_topic()])

    assert [card.to_dict() for card in cards] == [
        {
            "status": "complete",
            "rejection_reason": "",
            "knowledge": "热点知识",
            "chat_context": "聊天时可以提起。",
            "latest_update": None,
            "share_score": 3.0,
            "general_share_score": 3.0,
            "interest_share_score": 0,
            "share": {
                "text": "分享正文",
                "source_id": "e1",
                "url": "https://example.com/post/1",
                "comment_id": "e1c1",
                "comment_type": "platform",
                "comment": "真实评论",
            },
            "topic_id": 1,
        }
    ]


def test_validate_research_cards_resolves_source_ids() -> None:
    cards = validate_research_cards(
        [
            _raw_card(
                general_share_score=0,
                share=None,
                research_sources=["r1"],
            )
        ],
        [_topic()],
    )

    assert cards[0].to_dict()["research_sources"] == [
        {"title": "补搜标题", "url": "https://example.com/post/2"}
    ]


def test_validate_update_card_requires_latest_update() -> None:
    with pytest.raises(ValueError, match="缺少 latest_update"):
        validate_cards([_raw_card()], [_topic(relation="update")])


@pytest.mark.parametrize("score", [True, 0.5, 3.14, 4.1])
def test_validate_cards_rejects_invalid_general_share_scores(score: object) -> None:
    with pytest.raises(ValueError, match="general_share_score"):
        validate_cards(
            [_raw_card(general_share_score=score, share=None)],
            [_topic()],
        )


def test_interest_score_can_raise_final_score_without_changing_general_score() -> None:
    cards = validate_cards(
        [
            _raw_card(
                general_share_score=2.7,
                interest_share_score=3.6,
            )
        ],
        [_topic(candidate_interest_keywords=("MCP",))],
    )

    assert cards[0].general_share_score == 2.7
    assert cards[0].interest_share_score == 3.6
    assert cards[0].share_score == 3.6
    assert cards[0].candidate_interest_keywords == ("MCP",)


def test_validate_cards_rejects_interest_score_without_candidate_keyword() -> None:
    with pytest.raises(ValueError, match="必须有候选兴趣关键词"):
        validate_cards(
            [
                _raw_card(
                    interest_share_score=3.6,
                )
            ],
            [_topic()],
        )


def test_validate_cards_normalizes_generated_comment() -> None:
    cards = validate_cards(
        [
            _raw_card(
                share={
                    "text": "分享正文",
                    "source_id": "e1",
                    "comment_id": "",
                    "generated_comment": "我觉得挺有意思",
                }
            )
        ],
        [_topic()],
    )

    assert cards[0].share is not None
    assert cards[0].share.comment_type == "generated"
    assert cards[0].share.comment == "我觉得挺有意思"


def test_validate_cards_preserves_needs_research_state() -> None:
    cards = validate_cards(
        [
            _raw_card(
                status="needs_research",
                knowledge="",
                chat_context="",
                general_share_score=0,
                share=None,
            )
        ],
        [_topic()],
    )

    assert cards[0].to_dict() == {
        "status": "needs_research",
        "rejection_reason": "",
        "knowledge": "",
        "chat_context": "",
        "latest_update": None,
        "share_score": 0,
        "general_share_score": 0,
        "interest_share_score": 0,
        "share": None,
        "topic_id": 1,
    }


def test_save_cards_preserves_batch_and_share_contract(tmp_path: Path) -> None:
    topic = _topic()
    cards = validate_cards([_raw_card()], [topic])
    evidence = {"topics": [topic]}
    generated_at = datetime.now().astimezone()

    files = save_card_batch(
        cards,
        evidence=evidence,
        inference={"request_count": 1},
        output_dir=tmp_path / "knowledge",
        generated_at=generated_at,
    )
    files.update(
        save_share_batch(
            cards,
            evidence=evidence,
            inference={"request_count": 1},
            output_dir=tmp_path / "shares",
            generated_at=generated_at,
        )
    )

    batch = json.loads(Path(files["batch_json_file"]).read_text(encoding="utf-8"))
    share_batch = json.loads(
        Path(files["share_review_file"]).read_text(encoding="utf-8")
    )
    assert batch["generated_at"] == share_batch["generated_at"]
    assert batch["status_counts"] == {
        "complete": 1,
        "needs_research": 0,
        "rejected": 0,
    }
    assert batch["cards"][0]["topic_id"] == 1
    assert share_batch["shares"] == [
        {
            "topic_id": 1,
            "title": "测试热点",
            "label": "news",
            "score": 3.0,
            "general_score": 3.0,
            "interest_score": 0,
            "text": "分享正文",
            "url": "https://example.com/post/1",
            "comment": "真实评论",
            "comment_type": "platform",
            "source_id": "e1",
            "comment_id": "e1c1",
        }
    ]
    assert Path(files["batch_text_file"]).read_text(encoding="utf-8").startswith(
        "话题: 测试热点\n"
    )
    assert Path(files["share_text_file"]).read_text(encoding="utf-8")


def test_public_generation_keeps_dict_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscroll import config
    from agentscroll.workflows.knowledge_card import generate_hotlist_knowledge_cards

    requests: list[SimpleNamespace] = []
    client_closed = False

    def make_request(
        prompt: str,
        _settings: SimpleNamespace,
        **kwargs: object,
    ) -> SimpleNamespace:
        request = SimpleNamespace(prompt=prompt, **kwargs)
        requests.append(request)
        return request

    class FakeClient:
        def generate_batch(
            self,
            _requests: list[SimpleNamespace],
        ) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    ok=True,
                    parsed_json={"cards": [_raw_card()]},
                    provider="fake",
                    model="fake-model",
                    metadata={"usage": {"input_tokens": 10}},
                    error=None,
                )
            ]

        def close(self) -> None:
            nonlocal client_closed
            client_closed = True

    settings = SimpleNamespace(provider="fake", model="fake-model", max_workers=2)
    monkeypatch.setattr(config, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(config, "make_configured_request", make_request)
    monkeypatch.setattr(
        config,
        "build_configured_client",
        lambda _settings: FakeClient(),
    )

    result = generate_hotlist_knowledge_cards(
        {"topics": [_topic()], "max_entries_per_topic": 1},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
    )

    assert client_closed
    assert len(requests) == 1
    assert "$defs" not in requests[0].json_schema
    assert requests[0].json_schema["properties"]["cards"]["maxItems"] == 1
    card_schema = requests[0].json_schema["properties"]["cards"]["items"]
    assert "general_share_score" in card_schema["properties"]
    assert "interest_share_score" in card_schema["properties"]
    assert "matched_interest_keywords" not in card_schema["properties"]
    assert "share_score" not in card_schema["properties"]
    assert "不提供事件信息的讨论性问句" in requests[0].prompt
    assert "问句本身承载核心事件信息时可以保留" in requests[0].prompt
    assert "interest.candidate_keywords" in requests[0].prompt
    prompt_payload = json.loads(requests[0].prompt.rsplit("\n", 1)[-1])
    assert "interest" not in prompt_payload
    assert result["topic_count"] == result["complete_count"] == 1
    assert isinstance(result["cards"][0], dict)
    assert result["cards"][0]["topic_id"] == 1
    assert Path(result["batch_json_file"]).is_file()
    assert Path(result["share_review_file"]).is_file()


def test_selected_save_records_state_and_rewrites_share_topic_id(
    tmp_path: Path,
) -> None:
    from agentscroll.workflows.knowledge_card import _save_selected_cards

    topic = _topic()
    cards = validate_cards([_raw_card()], [topic])
    database = tmp_path / "agentscroll.sqlite3"
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

    files = _save_selected_cards(
        cards,
        evidence={"topics": [topic]},
        selection=selection,
        inference={"request_count": 1},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
        at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
        record_history=True,
    )

    share_batch = json.loads(
        Path(files["share_review_file"]).read_text(encoding="utf-8")
    )
    stored_topic_id = share_batch["shares"][0]["topic_id"]
    assert isinstance(stored_topic_id, str)
    assert load_history(database)["events"][0]["event_id"] == stored_topic_id


def test_supplement_workflow_keeps_dict_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscroll import config
    from agentscroll.workflows import knowledge_card as workflow
    from agentscroll.workflows.knowledge_card_models import KnowledgeCard

    research_item = {
        "source_id": "r1",
        "platform": "wechat",
        "source_title": "补搜标题",
        "url": "https://example.com/post/2",
        "content": "补搜正文",
        "comments": [],
    }
    diagnostics = {
        "active_search_topic_count": 1,
        "active_search_source_requests": 3,
        "active_search_item_count": 1,
        "active_search_failure_count": 0,
        "active_search_failures": [],
    }
    monkeypatch.setattr(
        config,
        "load_settings",
        lambda _path=None: SimpleNamespace(max_workers=2),
    )
    monkeypatch.setattr(
        workflow,
        "_collect_active_search_evidence",
        lambda _topics, **_kwargs: ({1: [research_item]}, diagnostics),
    )

    def generate_research_cards(
        topics: list[dict[str, object]],
        **_kwargs: object,
    ) -> tuple[list[object], dict[str, object]]:
        cards = validate_research_cards(
            [
                _raw_card(
                    general_share_score=0,
                    share=None,
                    research_sources=["r1"],
                )
            ],
            topics,
        )
        return cards, {"request_count": 1}

    monkeypatch.setattr(workflow, "_generate_topic_cards", generate_research_cards)
    initial_card = KnowledgeCard.needs_research(1).to_dict()
    result = workflow.supplement_hotlist_knowledge_cards(
        {"topics": [_topic()]},
        {"cards": [initial_card], "inference": {"request_count": 1}},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
    )

    assert result["supplemented_count"] == 1
    assert isinstance(result["cards"][0], dict)
    assert result["cards"][0]["research_sources"] == [
        {"title": "补搜标题", "url": "https://example.com/post/2"}
    ]
    assert Path(result["batch_json_file"]).is_file()
