"""Offline contracts for normalized knowledge cards and persisted batches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentscroll.config import SharePolicySettings

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


@pytest.mark.parametrize("research", [False, True])
def test_card_prompt_uses_workflow_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    research: bool,
) -> None:
    from agentscroll.workflows import knowledge_card as workflow

    monkeypatch.setattr(workflow, "ACTIVE_DAYS", 14)
    topic = _topic(relation="update")
    topic["title"] = "标题中的 {history_days} 是数据"
    builder = (
        workflow._knowledge_card_research_prompt
        if research else workflow._knowledge_card_prompt
    )
    prompt = builder(topic)
    instruction, payload_text = prompt.rsplit("\n", 1)
    assert "最近 14 天内已确认更新" in instruction
    assert "{history_days}" not in instruction
    assert json.loads(payload_text)["title"] == topic["title"]
    if research:
        schema = workflow.card_response_schema(research=True)
        limit = schema["properties"]["cards"]["items"]["properties"]["research_sources"]["maxItems"]
        assert limit == workflow._RESEARCH_ITEM_LIMIT
        assert f"最多 {limit} 项" in instruction
        assert f"最多 {limit} 个实际用于理解话题的 source_id" in instruction
        assert "{research_item_limit}" not in instruction
    (tmp_path / "card-prompt.txt").write_text(prompt, encoding="utf-8")


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("label", ["news", "fun"])
@pytest.mark.parametrize("interest_score,title_count,expected_score", [
    (2.5, 1, 2.5), (3.6, 1, 3.6), (0, 3, 4),
])
def test_disabled_builtin_content_keeps_interest_and_heat_through_save(
    tmp_path, monkeypatch, research, label, interest_score, title_count, expected_score,
) -> None:
    from agentscroll import config
    from agentscroll.workflows import knowledge_card as workflow

    settings = SimpleNamespace(
        sharing=SimpleNamespace(policy=SharePolicySettings()),
        interest=config.InterestSettings(keywords=["AI"]),
        hotlist=config.HotlistSettings(builtin_content_enabled=False),
        max_workers=10,
    )
    topic = _topic(candidate_interest_keywords=("AI",) if interest_score else ())
    topic.update(label=label, hotlist_title_count=title_count)
    raw = _raw_card(general_share_score=0, interest_share_score=interest_score)
    if expected_score < 3:
        raw["share"] = None
    if research:
        raw["research_sources"] = ["r1"]

    class Client:
        def generate_batch(self, requests):
            assert len(requests) == 1
            prompt = requests[0].prompt
            assert "本轮关闭内置 news/fun 的大众分享评分" in prompt
            payload = json.loads(prompt.rsplit("\n", 1)[-1])
            if interest_score:
                assert payload["interest"]["candidate_keywords"] == ["AI"]
            (tmp_path / "card-prompt.txt").write_text(prompt, encoding="utf-8")
            return [SimpleNamespace(
                ok=True, parsed_json={"cards": [raw]}, provider="fake",
                model="fake", metadata={},
            )]

        def close(self):
            pass

    monkeypatch.setattr(config, "build_configured_client", lambda _: Client())
    monkeypatch.setattr(
        config, "make_configured_request",
        lambda prompt, _settings, **kwargs: SimpleNamespace(prompt=prompt, **kwargs),
    )
    cards, inference = workflow._generate_topic_cards(
        [topic], settings=settings, research=research,
        max_tokens=4000, timeout=60, effort="low",
    )
    card = cards[0]
    assert card.status == "complete"
    assert card.general_share_score == 0
    assert card.interest_share_score == interest_score
    assert card.share_score == expected_score
    assert (card.share is not None) == (expected_score >= 3)

    database = tmp_path / "state.sqlite3"
    files = workflow._save_selected_cards(
        cards, evidence={"topics": [topic]},
        selection={"database_path": str(database), "topics": [{
            "representative_id": 1, "representative": {"title": topic["title"]},
            "related": [], "label": label, "event_relation": "new",
        }]},
        inference=inference, output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
        at=datetime(2026, 9, 10, tzinfo=timezone.utc), record_history=True,
    )
    event = load_history(database)["events"][0]
    assert event["general_share_score"] == 0
    assert event["interest_share_score"] == interest_score
    assert event["share_score"] == expected_score
    shares = json.loads(Path(files["share_review_file"]).read_text())["shares"]
    assert len(shares) == int(expected_score >= 3)
    if shares:
        assert shares[0]["general_score"] == 0
        assert shares[0]["score"] == expected_score


@pytest.mark.parametrize("research", [False, True])
def test_disabled_builtin_content_rejects_nonzero_general_score(research) -> None:
    topic = _topic(candidate_interest_keywords=("AI",))
    topic["builtin_content_enabled"] = False
    raw = _raw_card(general_share_score=4, interest_share_score=3.6)
    if research:
        raw["research_sources"] = []
    validator = validate_research_cards if research else validate_cards
    with pytest.raises(ValueError, match="general_share_score 必须为 0"):
        validator([raw], [topic])


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("mode,min_score", [("window", 3.3), ("score_only", 3.7)])
@pytest.mark.parametrize("score,floor", [(3.1, 0), (3.3, 0), (3.7, 0), (2.8, 3), (2.8, 4)])
def test_generation_uses_active_policy_min_score(
    monkeypatch, research, mode, min_score, score, floor,
) -> None:
    from agentscroll import config
    from agentscroll.workflows import knowledge_card as workflow

    policy = SharePolicySettings(
        mode=mode, window={"min_score": 3.3}, score_only={"min_score": 3.7}
    )
    settings = SimpleNamespace(
        sharing=SimpleNamespace(policy=policy), max_workers=1,
    )
    topic = _topic()
    topic["hotlist_floor_score"] = floor
    monkeypatch.setattr(workflow, "_validated_interest_topics", lambda topics, _: topics)
    raw = _raw_card(general_share_score=score)
    if max(score, floor) < min_score:
        raw["share"] = None
    if research:
        raw["research_sources"] = []
    prompts = []

    class Client:
        def generate_batch(self, requests):
            prompts.extend(requests)
            return [SimpleNamespace(
                ok=True, parsed_json={"cards": [raw]}, provider="fake",
                model="fake", metadata={},
            )]

        def close(self):
            pass

    monkeypatch.setattr(config, "make_configured_request", lambda prompt, *args, **kwargs: prompt)
    monkeypatch.setattr(config, "build_configured_client", lambda _: Client())
    cards, inference = workflow._generate_topic_cards(
        [topic], settings=settings, research=research,
        max_tokens=8000, timeout=900, effort="xhigh",
    )
    assert inference["failed_topic_count"] == 0
    assert cards[0].status == "complete"
    assert (cards[0].share is not None) == (max(score, floor) >= min_score)
    if floor >= min_score:
        assert "status=complete 且有可用 source_id 时填写 share" in prompts[0]
    else:
        assert f"两项评分的最大值达到 {min_score:g} 时填写 share" in prompts[0]


@pytest.mark.parametrize("query_count", [1, 2, 3, 4])
def test_zhihu_query_prompt_matches_schema_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_count: int,
) -> None:
    from agentscroll import config
    from agentscroll.workflows import knowledge_card as workflow

    requests: list[SimpleNamespace] = []
    queries = [f"检索词{i}" for i in range(query_count)]

    class FakeClient:
        def generate(self, request: SimpleNamespace) -> SimpleNamespace:
            requests.append(request)
            return SimpleNamespace(
                ok=True,
                parsed_json={"topics": [{"topic_index": 1, "search_queries": queries}]},
                provider="fake", model="fake-model", metadata={},
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(config, "load_settings", lambda _path: SimpleNamespace())
    monkeypatch.setattr(config, "build_configured_client", lambda _settings: FakeClient())
    monkeypatch.setattr(
        config, "make_configured_request",
        lambda prompt, _settings, **kwargs: SimpleNamespace(prompt=prompt, **kwargs),
    )
    hotlist = {"sources": {"zhihu": {"items": [{"title": "如何看待 {max_queries}？"}]}}}
    selection = {"topics": [{"representative_id": 1, "related_ids": []}]}
    if query_count in (2, 3):
        enriched, inference = workflow._selection_with_zhihu_search_queries(
            hotlist, selection, config_path=None,
        )
        assert enriched["topics"][0]["search_queries"] == queries
        assert inference["request_count"] == 1
    else:
        with pytest.raises(ValueError, match="必须返回 2 至 3 个不同检索词"):
            workflow._selection_with_zhihu_search_queries(hotlist, selection, config_path=None)
    assert len(requests) == 1
    request = requests[0]
    bounds = request.json_schema["properties"]["topics"]["items"]["properties"]["search_queries"]
    instruction, payload_text = request.prompt.rsplit("\n", 1)
    assert f"{bounds['minItems']} 至 {bounds['maxItems']} 个" in instruction
    assert "{min_queries}" not in instruction
    assert "{max_queries}" not in instruction
    assert json.loads(payload_text) == [{"topic_index": 1, "zhihu_titles": ["如何看待 {max_queries}？"]}]
    (tmp_path / "query-prompt.txt").write_text(request.prompt, encoding="utf-8")


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
            "hotlist_share_score": 0,
            "share_rules": ["general_score"],
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


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize(
    "relation,status,general,interest,expected,hotlist",
    [
        ("new", "complete", 2.8, 0, 4, 4),
        ("new", "complete", 4, 3.9, 4, 4),
        ("update", "complete", 2.8, 0, 2.8, 0),
        ("update", "complete", 4, 0, 4, 0),
        ("new", "complete", 2.8, 3.9, 4, 4),
        ("new", "needs_research", 0, 0, 0, 0),
        ("update", "rejected", 0, 0, 0, 0),
    ],
)
def test_hotlist_floor_respects_content_and_update_gates(
    research, relation, status, general, interest, expected, hotlist
) -> None:
    topic = _topic(relation=relation, candidate_interest_keywords=("游戏",))
    topic.update(hotlist_floor_score=4, label="fun")
    raw = _raw_card(
        status=status,
        general_share_score=general,
        interest_share_score=interest,
        latest_update="新增进展" if relation == "update" and status == "complete" else None,
    )
    if status != "complete":
        raw.update(knowledge="", chat_context="", rejection_reason="没有新进展" if status == "rejected" else "")
    if expected < 3:
        raw["share"] = None
    if research:
        raw["research_sources"] = []
    validate = validate_research_cards if research else validate_cards
    card = validate([raw], [topic])[0]
    assert card.general_share_score == general
    assert card.interest_share_score == interest
    assert card.share_score == expected
    assert card.hotlist_share_score == hotlist
    assert (card.share is not None) == (expected >= 3)
    if general == 4 and interest == 3.9:
        assert card.share_rules == ("general_score", "interest_score", "hotlist_title_count")


def test_hotlist_floor_requires_a_source_and_valid_share() -> None:
    topic = _topic()
    topic.update(hotlist_floor_score=4, evidence=[], research_evidence=[])
    card = validate_cards([_raw_card(general_share_score=0, share=None)], [topic])[0]
    assert card.share_score == 0
    topic = _topic()
    topic["hotlist_floor_score"] = 4
    with pytest.raises(ValueError, match="缺少有效对象"):
        validate_cards([_raw_card(general_share_score=2.8, share=None)], [topic])
    raw = _raw_card(general_share_score=2.8)
    raw["share"]["source_id"] = "missing"
    with pytest.raises(ValueError, match="未知 source_id"):
        validate_cards([raw], [topic])


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
        "hotlist_share_score": 0,
        "share_rules": [],
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
            "hotlist_score": 0,
            "share_rules": ["general_score"],
            "relation": "new",
            "hotlist_title_count": 1,
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


@pytest.mark.parametrize("threshold,title_count,expected_score", [
    (3, 3, 4), (3, 2, 3.5), (4, 2, 3), (3, 1, 2.8),
])
def test_public_generation_keeps_dict_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int,
    title_count: int,
    expected_score: float,
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
            raw_card = _raw_card(general_share_score=2.8)
            if expected_score < 3:
                raw_card["share"] = None
            return [
                SimpleNamespace(
                    ok=True,
                    parsed_json={"cards": [raw_card]},
                    provider="fake",
                    model="fake-model",
                    metadata={"usage": {"input_tokens": 10}},
                    error=None,
                )
            ]

        def close(self) -> None:
            nonlocal client_closed
            client_closed = True

    settings = SimpleNamespace(
        provider="fake",
        model="fake-model",
        max_workers=2,
        sharing=SimpleNamespace(policy=SharePolicySettings()),
        hotlist=SimpleNamespace(force_share_title_count=threshold),
    )
    monkeypatch.setattr(config, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(config, "make_configured_request", make_request)
    monkeypatch.setattr(
        config,
        "build_configured_client",
        lambda _settings: FakeClient(),
    )

    topic = _topic()
    topic["hotlist_title_count"] = title_count
    result = generate_hotlist_knowledge_cards(
        {"topics": [topic], "max_entries_per_topic": 1},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
    )

    assert client_closed
    assert len(requests) == 1
    assert "$defs" not in requests[0].json_schema
    assert requests[0].json_schema["properties"]["cards"]["maxItems"] == 1
    card_schema = requests[0].json_schema["properties"]["cards"]["items"]
    assert set(card_schema["properties"]["status"]["enum"]) == {
        "complete", "needs_research", "rejected"
    }
    assert "const" not in card_schema["properties"]["general_share_score"]
    assert any(option.get("type") == "null" for option in card_schema["properties"]["share"]["anyOf"])
    assert "general_shareable" not in card_schema["properties"]
    assert "interest_share_score" in card_schema["properties"]
    assert "matched_interest_keywords" not in card_schema["properties"]
    assert "share_score" not in card_schema["properties"]
    assert "不提供事件信息的讨论性问句" in requests[0].prompt
    assert "问句本身承载核心事件信息时可以保留" in requests[0].prompt
    assert "interest.candidate_keywords" in requests[0].prompt
    prompt_payload = json.loads(requests[0].prompt.rsplit("\n", 1)[-1])
    assert "interest" not in prompt_payload
    assert "hotlist" not in prompt_payload
    assert "hotlist_floor_score" not in prompt_payload
    assert "保底" not in requests[0].prompt
    assert ("status=complete 且有可用 source_id 时填写 share" in requests[0].prompt) == (expected_score >= 3)
    assert result["topic_count"] == result["complete_count"] == 1
    assert isinstance(result["cards"][0], dict)
    assert result["cards"][0]["topic_id"] == 1
    assert result["cards"][0]["general_share_score"] == 2.8
    assert result["cards"][0]["hotlist_share_score"] == (expected_score if expected_score >= 3 else 0)
    assert result["cards"][0]["share_score"] == expected_score
    assert (result["cards"][0]["share"] is not None) == (expected_score >= 3)
    assert Path(result["batch_json_file"]).is_file()
    assert Path(result["share_review_file"]).is_file()
    saved = json.loads(Path(result["batch_json_file"]).read_text())
    assert saved["cards"][0]["share_score"] == expected_score
    (tmp_path / "card-prompt.txt").write_text(requests[0].prompt, encoding="utf-8")


def test_model_can_score_four_without_hotlist_floor() -> None:
    card = validate_cards([_raw_card(general_share_score=4)], [_topic()])[0]
    assert card.general_share_score == card.share_score == 4
    assert card.hotlist_share_score == 0
    assert card.share_rules == ("general_score",)


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("floor", [3, 3.5, 4])
def test_hotlist_prepared_share_survives_low_general_readability(research: bool, floor: float) -> None:
    topic = _topic()
    topic["hotlist_floor_score"] = floor
    raw = _raw_card(general_share_score=2.8)
    if research:
        raw["research_sources"] = []
    validate = validate_research_cards if research else validate_cards
    card = validate([raw], [topic])[0]
    assert card.status == "complete"
    assert card.general_share_score == 2.8
    assert card.share_score == floor
    assert card.hotlist_share_score == floor
    assert card.share is not None


def test_current_hotlist_related_ids_set_title_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscroll.collector import hotlist as collector

    hotlist = {
        "collected_at": "2026-09-03T12:00:00+08:00",
        "sources": {
            "weibo": {
                "items": [
                    {"title": "同一事件标题一"},
                    {"title": "同一事件标题二"},
                    {"title": "同一事件标题三"},
                ]
            }
        },
    }
    selection = {
        "topics": [
            {
                "representative_id": 1,
                "related_ids": [2, 3],
                "label": "news",
                "event_relation": "new",
            }
        ]
    }

    def collect_details(entries: list[tuple[str, object]], **_kwargs: object):
        return (
            [
                {
                    "status": "readable",
                    "posts": [
                        {
                            "title": item["title"],
                            "published_at": "2026-09-03",
                            "url": f"https://example.com/{index}",
                            "content": "同一事件正文",
                            "comments": [],
                        }
                    ],
                    "error": "",
                }
                for index, (_source, item) in enumerate(entries, start=1)
            ],
            [],
        )

    monkeypatch.setattr(collector, "_collect_hotlist_details", collect_details)

    evidence = collector.collect_selected_hotlist_evidence(hotlist, selection)

    assert evidence["topics"][0]["hotlist_title_count"] == 3


@pytest.mark.parametrize("threshold,title_count,expected", [
    (1, 1, 4), (2, 1, 0), (2, 2, 4),
    (3, 1, 0), (3, 2, 3.5), (3, 3, 4), (3, 5, 4),
    (4, 1, 0), (4, 2, 3), (4, 3, 3.5), (4, 4, 4),
    (5, 2, 0), (5, 3, 3), (6, 3, 0),
])
def test_force_share_respects_configured_hotlist_title_count(
    threshold: int, title_count: int, expected: float,
) -> None:
    from agentscroll.workflows.knowledge_card import (
        _prompt_payload,
        _validated_interest_topics,
    )

    raw_topic = _topic()
    raw_topic["hotlist_title_count"] = title_count
    topic = _prompt_payload({"topics": [raw_topic]})[0]

    result = _validated_interest_topics(
        [topic],
        SimpleNamespace(
            hotlist=SimpleNamespace(force_share_title_count=threshold),
            interest=SimpleNamespace(keywords=()),
        ),
    )
    assert result[0]["hotlist_floor_score"] == expected


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


@pytest.mark.parametrize("interest_keywords", [(), ("AI",)])
def test_supplement_workflow_keeps_dict_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interest_keywords: tuple[str, ...],
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
        lambda _path=None: SimpleNamespace(
            max_workers=2, sharing=SimpleNamespace(policy=SharePolicySettings())
        ),
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
        prompt = workflow._knowledge_card_research_prompt(topics[0])
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        assert payload.get("interest") == (
            {"candidate_keywords": list(interest_keywords)}
            if interest_keywords else None
        )
        (tmp_path / "research-prompt.txt").write_text(prompt, encoding="utf-8")
        cards = validate_research_cards(
            [
                _raw_card(
                    general_share_score=0,
                    interest_share_score=2.8 if interest_keywords else 0,
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
        {"topics": [_topic(candidate_interest_keywords=interest_keywords)]},
        {"cards": [initial_card], "inference": {"request_count": 1}},
        output_dir=tmp_path / "knowledge",
        share_output_dir=tmp_path / "shares",
    )

    assert result["supplemented_count"] == 1
    assert isinstance(result["cards"][0], dict)
    assert result["cards"][0].get("candidate_interest_keywords", []) == list(interest_keywords)
    assert result["cards"][0]["interest_share_score"] == (2.8 if interest_keywords else 0)
    assert result["cards"][0]["research_sources"] == [
        {"title": "补搜标题", "url": "https://example.com/post/2"}
    ]
    assert Path(result["batch_json_file"]).is_file()


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("converted", ["", "😭😭😭官方涨 结果渠道商自己撑不住了🐶🐶"])
def test_platform_comment_conversion_reaches_message(research: bool, converted: str) -> None:
    from agentscroll.sharing.message import render_share_messages

    original = "[泪奔][泪奔][泪奔]官方涨 结果渠道商自己撑不住了[doge][doge]"
    topic = _topic()
    source = topic["research_evidence" if research else "evidence"][0]
    source["comments"] = [{"comment_id": "r1c1" if research else "e1c1", "text": original}]
    raw = _raw_card()
    raw["share"].update(comment_id=source["comments"][0]["comment_id"], converted_comment=converted)
    if research:
        raw["research_sources"] = ["r1"]
    validate = validate_research_cards if research else validate_cards
    card = validate([raw], [topic])[0]
    share = card.to_dict()["share"]
    assert share["comment_type"] == "platform"
    assert share["comment_id"] == source["comments"][0]["comment_id"]
    assert render_share_messages(share)[1] == (converted or original)
    assert source["comments"][0]["text"] == original


def test_converted_comment_requires_valid_comment_id() -> None:
    raw = _raw_card()
    raw["share"].update(comment_id="missing", converted_comment="😭")
    with pytest.raises(ValueError, match="未知 comment_id"):
        validate_cards([raw], [_topic()])
    raw["share"].update(comment_id="", generated_comment="😭")
    with pytest.raises(ValueError, match="converted_comment 必须对应真实评论"):
        validate_cards([raw], [_topic()])
