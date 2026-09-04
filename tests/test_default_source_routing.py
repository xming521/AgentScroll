"""Offline tests for default and explicit platform selection."""

from __future__ import annotations

from agentscroll.collector import search
from agentscroll.workflows import knowledge_card


def test_informal_search_disables_xiaohongshu_but_keeps_explicit_access(
    monkeypatch,
) -> None:
    called_sources: set[str] = set()

    def collect_one(source: str, *_args: object, **_kwargs: object):
        called_sources.add(source)
        return []

    monkeypatch.setattr(search, "_collect_one", collect_one)

    default_result = search.collect("热梗", scene="informal", save=False)

    assert default_result["routing"]["sources"] == [
        "weibo",
        "bilibili",
        "douyin",
    ]
    assert "xiaohongshu" not in called_sources

    called_sources.clear()
    explicit_result = search.collect(
        "热梗",
        sources=["xiaohongshu"],
        save=False,
    )

    assert explicit_result["routing"]["sources"] == ["xiaohongshu"]
    assert called_sources == {"xiaohongshu"}


def test_fun_supplement_disables_xiaohongshu(monkeypatch) -> None:
    import agentscroll.collector as collector

    selected_sources: list[tuple[str, ...]] = []

    def collect(_topic: str, *, sources, **_kwargs: object):
        selected_sources.append(tuple(sources))
        return {"sources": {source: {"items": [], "error": None} for source in sources}}

    monkeypatch.setattr(collector, "collect", collect)

    _, diagnostics = knowledge_card._collect_active_search_evidence(
        [{"topic_id": 1, "title": "测试热梗", "label": "fun"}],
        max_workers=1,
        days=7,
        as_of="2026-09-03",
    )

    assert selected_sources == [("weibo",)]
    assert diagnostics["active_search_source_requests"] == 1
