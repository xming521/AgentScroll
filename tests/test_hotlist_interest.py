from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("include_empty_fields", [True, False])
@pytest.mark.parametrize("builtin_content_enabled", [True, False])
@pytest.mark.parametrize("title_threshold", [1, 3, 5])
@pytest.mark.parametrize("soft_blocked_keywords", [(), ("社会负面", "民生纠纷")])
@pytest.mark.parametrize("blocked_keywords", [(), ("体育赛事", "电竞赛事", "赛车")])
def test_first_pass_propagates_semantic_interest_keywords(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_empty_fields: bool,
    builtin_content_enabled: bool,
    title_threshold: int,
    blocked_keywords: tuple[str, ...],
    soft_blocked_keywords: tuple[str, ...],
) -> None:
    from agentscroll import config
    from agentscroll.workflows.hotlist import select_hotlist_first_pass

    requests: list[SimpleNamespace] = []

    def make_request(
        prompt: str,
        _settings: SimpleNamespace,
        **kwargs: object,
    ) -> SimpleNamespace:
        request = SimpleNamespace(prompt=prompt, **kwargs)
        requests.append(request)
        return request

    class FakeClient:
        def generate(self, _request: SimpleNamespace) -> SimpleNamespace:
            topics = [
                {
                    "representative_id": 1,
                    "soft_blocked": False,
                    "label": "news",
                    "candidate_interest_keywords": ["MCP"],
                    "relation": "new",
                },
                {
                    "representative_id": 2,
                    "soft_blocked": False,
                    "label": "news",
                    "relation": "new",
                },
            ]
            parsed_json = {"topics": topics}
            if include_empty_fields:
                for topic in topics:
                    topic.update(
                        related_ids=[],
                        candidate_interest_keywords=topic.get(
                            "candidate_interest_keywords", []
                        ),
                        history_id=None,
                    )
                parsed_json["seen"] = None
            return SimpleNamespace(
                ok=True,
                parsed_json=parsed_json,
                provider="fake",
                model="fake-model",
                elapsed_s=0.1,
                error=None,
            )

        def close(self) -> None:
            pass

    settings = SimpleNamespace(
        storage=SimpleNamespace(database_path=tmp_path / "state.sqlite3"),
        interest=config.InterestSettings(
            keywords=("MCP", "机器人"), blocked_keywords=blocked_keywords,
            soft_blocked_keywords=soft_blocked_keywords,
        ),
        hotlist=config.HotlistSettings(
            force_share_title_count=title_threshold,
            builtin_content_enabled=builtin_content_enabled,
        ),
    )
    monkeypatch.setattr(config, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(config, "make_configured_request", make_request)
    monkeypatch.setattr(config, "build_configured_client", lambda _settings: FakeClient())

    result = select_hotlist_first_pass(
        {
            "collected_at": "2026-09-02T08:00:00+08:00",
            "sources": {
                "weibo": {
                    "items": [
                        {"title": "MCP 发布重要新能力"},
                        {"title": "台风登陆带来强降雨"},
                    ],
                }
            },
        }
    )

    assert result["topics"][0]["candidate_interest_keywords"] == ["MCP"]
    expected_ids = [1, 2] if builtin_content_enabled or title_threshold == 1 else [1]
    assert [topic["representative_id"] for topic in result["topics"]] == expected_ids
    if len(result["topics"]) == 2:
        assert "candidate_interest_keywords" not in result["topics"][1]
    assert ("本轮关闭内置 news/fun 通用推荐" in requests[0].prompt) is not builtin_content_enabled
    assert result["seen_topics"] == []
    assert "exact_interest_keywords" not in result["topics"][0]
    payload = json.loads(requests[0].prompt.rsplit("\n", 1)[-1])
    assert len(requests) == 1
    assert f"至少 {title_threshold} 个标题" in requests[0].prompt
    assert "{force_share_title_count}" not in requests[0].prompt
    assert set(payload) == {"interest", "candidates"}
    (tmp_path / "first-pass-prompt.txt").write_text(requests[0].prompt, encoding="utf-8")
    assert payload["interest"] == {
        "keywords": ["MCP", "机器人"],
        "blocked_keywords": list(blocked_keywords),
        "soft_blocked_keywords": list(soft_blocked_keywords),
    }
    assert "exact_interest_keywords" not in payload["candidates"][0]
    schema = requests[0].json_schema
    max_topics = schema["properties"]["topics"]["maxItems"]
    assert f"最多 {max_topics} 个" in requests[0].prompt
    assert "{max_topics}" not in requests[0].prompt
    topic_schema = schema["properties"]["topics"]["items"]
    seen_schema = schema["properties"]["seen"]["items"]
    assert set(topic_schema["required"]) == set(topic_schema["properties"])
    assert set(seen_schema["required"]) == set(seen_schema["properties"])
    assert set(schema["required"]) == set(schema["properties"])
    history_types = {
        variant["type"]
        for variant in topic_schema["properties"]["history_id"]["anyOf"]
    }
    assert history_types == {"integer", "null"}


def test_invalid_local_history_is_downgraded_without_failing_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentscroll import config
    from agentscroll.workflows import hotlist_state
    from agentscroll.workflows.hotlist import select_hotlist_first_pass

    class FakeClient:
        def generate(self, _request: SimpleNamespace) -> SimpleNamespace:
            return SimpleNamespace(
                ok=True,
                parsed_json={
                    "topics": [
                        {
                            "representative_id": 1,
                            "related_ids": [],
                            "label": "news",
                            "candidate_interest_keywords": [],
                            "relation": "update",
                            "history_id": 31,
                        },
                        {
                            "representative_id": 2,
                            "related_ids": [],
                            "label": "news",
                            "candidate_interest_keywords": [],
                            "relation": "new",
                            "history_id": None,
                        },
                    ],
                    "seen": [],
                },
                provider="fake",
                model="fake-model",
                elapsed_s=0.1,
                error=None,
            )

        def close(self) -> None:
            pass

    def fake_attach(candidates, _history, *, at):
        del at
        payloads = [dict(candidate) for candidate in candidates]
        payloads[1]["history"] = [
            {
                "history_id": 31,
                "title": "家长投诉老师婚姻状况",
                "last_seen_date": "2026-09-02",
            }
        ]
        return (
            payloads,
            {31: {"event_id": "teacher-event", "score": 0.45}},
            {
                "active_event_count": 1,
                "history_match_count": 1,
                "history_prompt_chars": 80,
            },
        )

    settings = SimpleNamespace(
        storage=SimpleNamespace(database_path=tmp_path / "state.sqlite3"),
        interest=config.InterestSettings(),
        hotlist=config.HotlistSettings(),
    )
    monkeypatch.setattr(config, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(
        config,
        "make_configured_request",
        lambda prompt, _settings, **kwargs: SimpleNamespace(
            prompt=prompt,
            **kwargs,
        ),
    )
    monkeypatch.setattr(config, "build_configured_client", lambda _settings: FakeClient())
    monkeypatch.setattr(hotlist_state, "attach_history_matches", fake_attach)

    result = select_hotlist_first_pass(
        {
            "collected_at": "2026-09-03T11:04:28+08:00",
            "sources": {
                "zhihu": {
                    "items": [
                        {"title": "家长因老师不婚主义向学校投诉"},
                        {"title": "另一条值得关注的公共事件"},
                    ]
                }
            },
        }
    )

    assert [topic["event_relation"] for topic in result["topics"]] == [
        "new",
        "new",
    ]
    assert "matched_event_id" not in result["topics"][0]
    assert result["inference"]["history_fallbacks"] == [
        {
            "representative_id": 1,
            "requested_relation": "update",
            "history_id": 31,
            "reason": "history_id 31 不属于当前话题的本地召回结果",
            "action": "downgraded_to_new",
        }
    ]


@pytest.mark.parametrize(
    "threshold,count,relation,blocked,expected",
    [
        (3, 1, "new", True, 0),
        (5, 2, "new", True, 0),
        (5, 3, "new", True, 1),
        (5, 4, "new", True, 1),
        (5, 5, "new", True, 1),
        (1, 1, "new", True, 1),
        (3, 3, "update", True, 0),
        (3, 1, "new", False, 1),
        (3, 3, "update", False, 1),
        (3, 3, "seen", True, 0),
    ],
)
def test_soft_block_filter_uses_validated_relation_and_hotlist_score(
    tmp_path, monkeypatch, threshold, count, relation, blocked, expected,
):
    from agentscroll import config
    from agentscroll.workflows import hotlist_state
    from agentscroll.workflows.hotlist import select_hotlist_first_pass

    settings = SimpleNamespace(
        storage=SimpleNamespace(database_path=tmp_path / "state.sqlite3"),
        interest=config.InterestSettings(soft_blocked_keywords=["社会负面"]),
        hotlist=config.HotlistSettings(force_share_title_count=threshold),
    )
    raw_topic = {
        "representative_id": 1,
        "related_ids": list(range(2, count + 1)),
        "label": "news",
        "candidate_interest_keywords": [],
        "soft_blocked": blocked,
        "history_id": None if relation == "new" else 31,
    }
    if relation != "seen":
        raw_topic["relation"] = relation

    def fake_attach(candidates, _history, *, at):
        payloads = [dict(candidate) for candidate in candidates]
        payloads[0]["history"] = [{"history_id": 31, "title": "历史事件"}]
        return (
            payloads,
            {31: {"event_id": "event-31"}},
            {"active_event_count": 1, "history_match_count": 1, "history_prompt_chars": 0},
        )

    class FakeClient:
        def generate(self, request):
            (tmp_path / "prompt.txt").write_text(request.prompt, encoding="utf-8")
            return SimpleNamespace(
                ok=True,
                parsed_json={
                    "topics": [] if relation == "seen" else [raw_topic],
                    "seen": [raw_topic] if relation == "seen" else [],
                },
                provider="fake", model="fake", elapsed_s=0, error=None,
            )

        def close(self):
            pass

    monkeypatch.setattr(config, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(config, "make_configured_request",
                        lambda prompt, _settings, **kwargs: SimpleNamespace(prompt=prompt))
    monkeypatch.setattr(config, "build_configured_client", lambda _settings: FakeClient())
    monkeypatch.setattr(hotlist_state, "attach_history_matches", fake_attach)
    monkeypatch.setattr(
        hotlist_state, "record_first_pass",
        lambda _path, **kwargs: (tmp_path / "record-first-pass.json").write_text(
            json.dumps(kwargs, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        ),
    )
    result = select_hotlist_first_pass({
        "collected_at": "2026-09-09T08:00:00+08:00",
        "sources": {"weibo": {"items": [
            {"title": f"事件报道 {index}"} for index in range(count)
        ]}},
    })
    (tmp_path / "selection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    assert result["topic_count"] == expected
    assert result["seen_count"] == (1 if relation == "seen" else 0)
    if expected:
        assert result["topics"][0]["event_relation"] == relation
        assert len(result["topics"][0]["related_ids"]) == count - 1
