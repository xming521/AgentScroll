from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_first_pass_propagates_semantic_interest_keywords(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            return SimpleNamespace(
                ok=True,
                parsed_json={
                    "topics": [
                        {
                            "representative_id": 1,
                            "label": "news",
                            "candidate_interest_keywords": ["MCP"],
                            "relation": "new",
                        },
                        {
                            "representative_id": 2,
                            "label": "news",
                            "relation": "new",
                        }
                    ],
                },
                provider="fake",
                model="fake-model",
                elapsed_s=0.1,
                error=None,
            )

        def close(self) -> None:
            pass

    settings = SimpleNamespace(
        storage=SimpleNamespace(database_path=tmp_path / "state.sqlite3"),
        interest=SimpleNamespace(keywords=("MCP", "机器人")),
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
    assert "candidate_interest_keywords" not in result["topics"][1]
    assert result["seen_topics"] == []
    assert "exact_interest_keywords" not in result["topics"][0]
    payload = json.loads(requests[0].prompt.rsplit("\n", 1)[-1])
    assert payload["interest"] == {"keywords": ["MCP", "机器人"]}
    assert "exact_interest_keywords" not in payload["candidates"][0]
    schema = requests[0].json_schema
    topic_required = schema["properties"]["topics"]["items"]["required"]
    assert "related_ids" not in topic_required
    assert "candidate_interest_keywords" not in topic_required
    assert "history_id" not in topic_required
    assert "seen" not in schema["required"]
