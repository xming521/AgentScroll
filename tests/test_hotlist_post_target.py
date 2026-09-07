import json
from types import SimpleNamespace

import pytest

from agentscroll.collector import hotlist as collector
from agentscroll.workflows import knowledge_card as workflow


@pytest.mark.parametrize("available", [0, 2, 3, 5])
def test_single_search_collects_post_target(monkeypatch, tmp_path, available):
    snapshot = {
        "collected_at": "2026-09-07T12:00:00+08:00",
        "sources": {"weibo": {"items": [{"title": "测试科技事件"}]}},
    }
    selection = {"topics": [{
        "representative_id": 1, "related_ids": [], "label": "news",
    }]}
    requested = []

    def collect_posts(topics, *, posts_per_topic, **kwargs):
        requested.append(posts_per_topic)
        posts = [{
            "url": f"https://m.weibo.cn/detail/{i}",
            "date": "2026-09-07", "content": f"测试科技事件正文{i}",
            "comments": [],
        } for i in range(min(available, posts_per_topic))]
        return [{"status": "readable" if posts else "empty", "posts": posts}]

    monkeypatch.setattr(collector, "collect_hot_topic_posts", collect_posts)
    evidence = collector.collect_selected_hotlist_evidence(snapshot, selection)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False))
    assert requested == [3]
    assert len(evidence["topics"][0]["evidence"]) == min(available, 3)

    if available < 3:
        from agentscroll import config
        from agentscroll.config import SharePolicySettings

        monkeypatch.setattr(config, "load_settings", lambda _: SimpleNamespace(
            provider="test", model="test", max_workers=1,
            sharing=SimpleNamespace(policy=SharePolicySettings()),
        ))
        result = workflow._generate_hotlist_knowledge_cards(evidence)
        assert result["needs_research_count"] == 1
        assert result["inference"]["request_count"] == 0


def test_collection_counts_unique_recent_posts_and_stops(monkeypatch, tmp_path):
    snapshot = {
        "collected_at": "2026-09-07T12:00:00+08:00",
        "sources": {"weibo": {"items": [
            {"title": f"测试科技事件{i}"} for i in range(5)
        ]}},
    }
    selection = {"topics": [{
        "representative_id": 1, "related_ids": [2, 3, 4, 5], "label": "news",
    }]}
    calls = []

    def collect_posts(topics, **kwargs):
        calls.append(topics)
        results = []
        for title in topics:
            index = int(title[-1])
            ids = [0, 0, 2, 3, 4]
            results.append({"status": "readable", "posts": [{
                "url": f"https://m.weibo.cn/detail/{ids[index]}",
                "date": "2026-08-01" if index == 2 else "2026-09-07",
                "content": "测试科技事件正文", "comments": [],
            }]})
        return results

    monkeypatch.setattr(collector, "collect_hot_topic_posts", collect_posts)
    evidence = collector.collect_selected_hotlist_evidence(snapshot, selection)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False))
    posts = evidence["topics"][0]["evidence"]
    assert len(posts) == 3
    assert len({p["url"] for p in posts}) == 3
    assert calls == [
        ["测试科技事件0", "测试科技事件1", "测试科技事件2"],
        ["测试科技事件3", "测试科技事件4"],
    ]
