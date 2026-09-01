"""Opt-in live tests for AgentScroll's extracted collection layer.

These tests intentionally do not mock network or browser operations. They are
disabled by default because platform availability, anti-bot checks, credentials,
and login state vary by environment.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

import pytest

from agentscroll.collector import ALL_SOURCES, SCENE_SOURCES, collect
from agentscroll.collector.sources import (
    bilibili,
    browser,
    douyin,
    live_artifacts,
    xiaohongshu,
)

QUERY_FLAG = "AGENTSCROLL_RUN_LIVE_TESTS"
BROWSER_FLAG = "AGENTSCROLL_RUN_BROWSER_TESTS"
QUERY_SOURCES_ENV = "AGENTSCROLL_LIVE_SOURCES"
BROWSER_SOURCES_ENV = "AGENTSCROLL_LIVE_BROWSER_SOURCES"
HOTLIST_STAGE_ENV = "AGENTSCROLL_LIVE_HOTLIST_STAGE"
HOTLIST_GROUPS_ENV = "AGENTSCROLL_LIVE_HOTLIST_GROUPS"
HOTLIST_SELECTION_FILE_ENV = "AGENTSCROLL_LIVE_HOTLIST_SELECTION_FILE"
HOTLIST_SNAPSHOT_FILE_ENV = "AGENTSCROLL_LIVE_HOTLIST_SNAPSHOT_FILE"
HOTLIST_TEST_STAGES = {"first-pass", "cards", "full"}
# Xiaohongshu currently requires a non-guest login before its web search emits
# note results. Keep it available as an explicit live target, but do not make a
# default informal run fail solely because this machine has no valid login.
DEFAULT_QUERY_SOURCES = tuple(
    source for source in SCENE_SOURCES["informal"] if source != "xiaohongshu"
)

# These values mean that the collector only discovered an indexed URL and its
# search-engine metadata. They do not prove that the target platform content
# itself was readable.
_DISCOVERY_ONLY_SOURCES = {
    "site-search-fallback",
    "web-search-discovery",
}
_DISCOVERY_ONLY_SOURCE_TAGS = {
    "Bing兜底",
}
_DETAIL_CONTENT_RULES = {
    "weibo": ("post-description", {"mcp-server-weibo-feed-detail"}),
    "xiaohongshu": (
        "post-description",
        {"xiaohongshu-initial-state", "xiaohongshu-detail-dom"},
    ),
    "bilibili": ("post-description", {"bilibili-view-api"}),
    "zhihu": ("post-body", {"zhihu-detail-dom"}),
    "douyin": ("post-description", {"douyin-router-data", "douyin-detail-dom"}),
    "wechat": ("article-body", {"wechat-article-page"}),
    "toutiao": ("article-body", {"toutiao-mobile-detail-page", "linked-page"}),
}
_COMMENT_SOURCES = {
    "weibo",
    "xiaohongshu",
    "bilibili",
    "zhihu",
    "douyin",
    "toutiao",
}
_ARTIFACT_RUN_LOCK = threading.Lock()
_ARTIFACT_RUN_DIR: Path | None = None

BROWSER_CRAWLERS: Dict[str, Callable[[str, int], list[dict[str, Any]]]] = {
    "xiaohongshu": xiaohongshu.crawl_xiaohongshu,
    "bilibili": bilibili.crawl_bilibili,
    "douyin": douyin.crawl_douyin,
}


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_artifact_run_dir() -> Path:
    global _ARTIFACT_RUN_DIR
    with _ARTIFACT_RUN_LOCK:
        if _ARTIFACT_RUN_DIR is not None:
            return _ARTIFACT_RUN_DIR
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = (
            Path.cwd() / "outputs" / "test_artifacts" / "live_collectors" / timestamp
        ).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        live_artifacts.set_artifact_dir(run_dir)
        _ARTIFACT_RUN_DIR = run_dir
        print(f"[live-artifacts] {run_dir}")
        return run_dir


def _selected_sources(
    name: str,
    available: Iterable[str],
    *,
    default: Iterable[str] | None = None,
) -> set[str]:
    allowed = set(available)
    raw = os.environ.get(name, "").strip()
    if not raw:
        selected = set(default) if default is not None else allowed
        unknown_defaults = selected - allowed
        if unknown_defaults:
            raise ValueError(
                f"默认数据源包含未知值：{', '.join(sorted(unknown_defaults))}"
            )
        return selected

    selected = {
        "xiaohongshu" if item.strip().lower() == "xhs" else item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }
    unknown = selected - allowed
    if unknown:
        raise ValueError(
            f"{name} 包含未知数据源：{', '.join(sorted(unknown))}；"
            f"可用值：{', '.join(sorted(allowed))}"
        )
    return selected


def _hotlist_test_stage() -> str:
    stage = os.environ.get(HOTLIST_STAGE_ENV, "first-pass").strip().lower()
    if stage not in HOTLIST_TEST_STAGES:
        raise ValueError(
            f"{HOTLIST_STAGE_ENV} 必须是 "
            f"{', '.join(sorted(HOTLIST_TEST_STAGES))}，当前值：{stage!r}"
        )
    return stage


def _hotlist_selection_artifact() -> Path:
    configured = os.environ.get(HOTLIST_SELECTION_FILE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"筛选结果不存在：{path}")
        return path

    pattern = "*/04_test_result/hotlist_first_pass_newsnow.json"
    candidates = sorted(
        (Path.cwd() / "outputs" / "test_artifacts" / "live_collectors").glob(
            pattern
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"没有找到已有标题筛选结果；请先运行 first-pass，或设置 "
            f"{HOTLIST_SELECTION_FILE_ENV}"
        )
    return candidates[0].resolve()


def _selection_from_artifact(
    hotlist: dict[str, Any],
    artifact_path: Path,
) -> dict[str, Any]:
    from agentscroll.collector.hotlist import list_hotlist_entries

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    raw_topics = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError(f"标题筛选结果没有非空 items：{artifact_path}")

    entries = list_hotlist_entries(hotlist)
    entry_id_by_title = {
        str(entry.get("title") or ""): index
        for index, entry in enumerate(entries, start=1)
    }
    selected_ids: set[int] = set()
    topics = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise ValueError(f"标题筛选结果包含无效话题：{raw_topic!r}")
        title = str(raw_topic.get("title") or "").strip()
        label = str(raw_topic.get("label") or "")
        related_titles = raw_topic.get("related_titles")
        if label not in {"news", "fun"} or not title:
            raise ValueError(f"标题筛选结果包含无效类别或标题：{raw_topic!r}")
        if not isinstance(related_titles, list) or any(
            not isinstance(value, str) for value in related_titles
        ):
            raise ValueError(f"话题 {title!r} 的 related_titles 无效")

        topic_titles = [title, *related_titles]
        missing_titles = [
            value for value in topic_titles if value not in entry_id_by_title
        ]
        if missing_titles:
            raise ValueError(
                f"已有筛选标题不在当前 NewsNow 快照中：{missing_titles}"
            )
        topic_ids = [entry_id_by_title[value] for value in topic_titles]
        if len(topic_ids) != len(set(topic_ids)) or selected_ids.intersection(
            topic_ids
        ):
            raise ValueError(f"标题筛选结果重复使用热榜条目：{topic_titles}")
        selected_ids.update(topic_ids)
        representative_id, *related_ids = topic_ids
        topics.append(
            {
                "representative_id": representative_id,
                "representative": dict(entries[representative_id - 1]),
                "related_ids": related_ids,
                "related": [dict(entries[value - 1]) for value in related_ids],
                "label": label,
            }
        )

    return {
        "input_count": len(entries),
        "topic_count": len(topics),
        "topics": topics,
        "inference": {
            "source": "saved-first-pass-artifact",
            "artifact": str(artifact_path),
        },
    }


def _assert_real_item(source: str, item: Any) -> None:
    assert isinstance(item, dict), f"{source} 返回的条目不是字典：{type(item).__name__}"
    assert item.get("source") not in _DISCOVERY_ONLY_SOURCES, (
        f"{source} 只返回了搜索引擎发现结果，未读取目标平台内容：{item}"
    )
    assert item.get("source_tag") not in _DISCOVERY_ONLY_SOURCE_TAGS, (
        f"{source} 只返回了搜索引擎兜底结果，未读取目标平台内容：{item}"
    )
    assert item.get("url"), f"{source} 首条结果缺少 url：{item}"
    assert any(
        item.get(field) for field in ("text", "title", "desc", "excerpt", "snippet")
    ), f"{source} 首条结果缺少正文或标题字段：{item}"


def _assert_detail_content(source: str, items: Iterable[Any]) -> None:
    expected_type, allowed_sources = _DETAIL_CONTENT_RULES[source]
    invalid = []
    for item in items:
        content = (
            str(item.get("content") or "").strip() if isinstance(item, dict) else ""
        )
        if (
            not isinstance(item, dict)
            or item.get("content_type") != expected_type
            or item.get("content_source") not in allowed_sources
            or len(content) < 20
            or not item.get("content_url")
        ):
            invalid.append(
                {
                    "url": item.get("url") if isinstance(item, dict) else "",
                    "content_url": item.get("content_url")
                    if isinstance(item, dict)
                    else "",
                    "content_type": item.get("content_type")
                    if isinstance(item, dict)
                    else "",
                    "content_source": item.get("content_source")
                    if isinstance(item, dict)
                    else "",
                    "content_length": len(content),
                }
            )
    assert not invalid, (
        f"{source} 存在未从真实详情页/API 读取到正文的返回条目：{invalid}"
    )


def _assert_readable_comments(source: str, items: Iterable[Any]) -> None:
    readable_items = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("comments_status") == "readable"
        and isinstance(item.get("comments"), list)
        and item["comments"]
    ]
    statuses = [
        {
            "url": item.get("url"),
            "comments_status": item.get("comments_status", "not-present"),
            "comments_source": item.get("comments_source", ""),
            "comment_count": len(item.get("comments") or []),
        }
        for item in items
        if isinstance(item, dict)
    ]
    assert readable_items, (
        f"{source} 没有任何帖子从真实评论区/API 读取到评论原文：{statuses}"
    )
    for item in readable_items:
        for comment in item["comments"]:
            assert str(comment.get("text") or "").strip(), (
                f"{source} 返回了正文为空的评论：{comment}"
            )
            forbidden = {"author", "author_name", "author_id", "user", "nickname"}
            assert not forbidden.intersection(comment), (
                f"{source} 评论不应保存作者字段：{comment}"
            )


@pytest.mark.live
@pytest.mark.skipif(
    not _enabled(QUERY_FLAG),
    reason=f"设置 {QUERY_FLAG}=1 才会执行真实平台查询",
)
def test_live_platform_search_and_detail_and_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect platforms concurrently and validate each platform's live data."""
    selected = _selected_sources(
        QUERY_SOURCES_ENV,
        ALL_SOURCES,
        default=DEFAULT_QUERY_SOURCES,
    )
    active_sources = tuple(source for source in ALL_SOURCES if source in selected)
    _ensure_artifact_run_dir()

    # Allow every real collection path, including Playwright fallbacks. Indexed
    # URLs and snippets from a search-engine fallback are still rejected below.
    monkeypatch.delenv("AGENTSCROLL_DISABLE_BROWSER", raising=False)
    monkeypatch.setenv("AGENTSCROLL_ALLOW_DETAIL_BROWSER", "1")
    browser._playwright_available = None

    topic = os.environ.get("AGENTSCROLL_LIVE_TOPIC", "人工智能").strip()
    days = int(os.environ.get("AGENTSCROLL_LIVE_DAYS", "30"))
    depth = os.environ.get("AGENTSCROLL_LIVE_DEPTH", "default").strip().lower()

    result = collect(
        topic,
        sources=active_sources,
        days=days,
        depth=depth,
    )

    failures = []
    for source in active_sources:
        payload = result["sources"][source]
        try:
            assert payload["error"] is None, f"{source} 查询报错：{payload['error']}"
            assert payload["items"], (
                f"{source} 真实查询没有返回数据；可能是平台接口变化、反爬、"
                "验证码、网络限制或缺少登录态"
            )
            platform_items = [
                item
                for item in payload["items"]
                if item.get("source") not in _DISCOVERY_ONLY_SOURCES
                and item.get("source_tag") not in _DISCOVERY_ONLY_SOURCE_TAGS
            ]
            assert platform_items, (
                f"{source} 只发现了搜索引擎中的标题、摘要或链接，"
                "没有读取目标平台页面或平台 API 数据"
            )
            _assert_real_item(source, platform_items[0])
            if source in _DETAIL_CONTENT_RULES:
                _assert_detail_content(source, platform_items)
            if source in _COMMENT_SOURCES:
                _assert_readable_comments(source, platform_items)
        except Exception as exc:
            live_artifacts.save_test_result(
                source,
                "http_detail_comments",
                topic,
                status="failed",
                items=payload["items"],
                error=f"{type(exc).__name__}: {exc}",
            )
            failures.append(f"{source}: {type(exc).__name__}: {exc}")
        else:
            live_artifacts.save_test_result(
                source,
                "http_detail_comments",
                topic,
                status="passed",
                items=payload["items"],
            )

    assert not failures, "\n".join(("真实平台验证失败：", *failures))


@pytest.mark.live
@pytest.mark.skipif(
    not _enabled(QUERY_FLAG),
    reason=f"设置 {QUERY_FLAG}=1 才会执行真实热榜与模型测试",
)
def test_live_hotlist_learning_by_stage() -> None:
    """Run title selection, cards from saved selection, or the full workflow."""
    from agentscroll.collector import fetch_newsnow_hotlists
    from agentscroll.workflows import (
        generate_selected_hotlist_knowledge_cards,
        learn_hotlist_snapshot,
        select_hotlist_first_pass,
    )

    run_dir = _ensure_artifact_run_dir()
    stage = _hotlist_test_stage()
    groups = os.environ.get(HOTLIST_GROUPS_ENV, "综合").strip() or "综合"
    saved_snapshot = os.environ.get(HOTLIST_SNAPSHOT_FILE_ENV, "").strip()
    if stage == "cards" and saved_snapshot:
        snapshot_path = Path(saved_snapshot).expanduser().resolve()
        if not snapshot_path.is_file():
            raise FileNotFoundError(f"热榜快照不存在：{snapshot_path}")
        hotlist = json.loads(snapshot_path.read_text(encoding="utf-8"))
        print(f"[hotlist-snapshot] {snapshot_path}")
    else:
        hotlist = fetch_newsnow_hotlists(
            tuple(part.strip() for part in groups.split(",") if part.strip()),
            save=True,
        )
    assert hotlist["total_items"], "NewsNow 没有返回可供测试的热榜标题"

    if stage == "first-pass":
        result = select_hotlist_first_pass(hotlist)
        classified_titles = [
            {
                "label": topic["label"],
                "title": topic["representative"]["title"],
                "related_titles": [item["title"] for item in topic["related"]],
            }
            for topic in result["topics"]
        ]
        assert classified_titles, "第一轮模型没有分类出任何标题"
        assert result["topic_count"] == len(classified_titles) <= 20
        assert all(item["label"] in {"news", "fun"} for item in classified_titles)
        artifact_items = classified_titles
    elif stage == "cards":
        selection_artifact = _hotlist_selection_artifact()
        selection = _selection_from_artifact(hotlist, selection_artifact)
        result = generate_selected_hotlist_knowledge_cards(
            hotlist,
            selection,
            output_dir=run_dir / "knowledge",
            share_output_dir=run_dir / "shares",
        )
        artifact_items = result["cards"]
        assert artifact_items, "已有标题筛选结果没有生成知识卡"
        print(f"[hotlist-selection-artifact] {selection_artifact}")
    else:
        result = learn_hotlist_snapshot(hotlist)
        artifact_items = result["cards"]
        assert artifact_items, "完整热榜学习没有生成知识卡"

    artifact = live_artifacts.save_test_result(
        "newsnow",
        f"hotlist_{stage.replace('-', '_')}",
        groups,
        status="passed",
        items=artifact_items,
    )
    print(f"[hotlist-stage] {stage}")
    print(json.dumps(artifact_items, ensure_ascii=False, indent=2))
    if artifact is not None:
        print(f"[hotlist-artifact] {artifact}")


@pytest.mark.live
@pytest.mark.browser
@pytest.mark.skipif(
    not _enabled(BROWSER_FLAG),
    reason=f"设置 {BROWSER_FLAG}=1 才会启动真实 Playwright 浏览器",
)
@pytest.mark.parametrize("source", tuple(BROWSER_CRAWLERS))
def test_live_browser_crawler(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch Chromium and execute the source's real browser crawler."""
    selected = _selected_sources(BROWSER_SOURCES_ENV, BROWSER_CRAWLERS)
    if source not in selected:
        pytest.skip(f"{source} 未包含在 {BROWSER_SOURCES_ENV} 中")
    _ensure_artifact_run_dir()

    monkeypatch.delenv("AGENTSCROLL_DISABLE_BROWSER", raising=False)
    browser._playwright_available = None
    topic = os.environ.get("AGENTSCROLL_LIVE_TOPIC", "人工智能").strip()
    limit = int(os.environ.get("AGENTSCROLL_LIVE_BROWSER_LIMIT", "5"))
    items: list[dict[str, Any]] = []
    try:
        assert browser.is_playwright_available(), (
            "Playwright 不可用；请安装 browser 可选依赖并执行 "
            "`python3 -m playwright install chromium`"
        )
        items = BROWSER_CRAWLERS[source](topic, limit)
        assert items, (
            f"{source} 浏览器爬虫没有返回数据；可能需要登录、通过验证码，"
            "或更新页面/XHR 解析逻辑"
        )
        _assert_real_item(source, items[0])
    except Exception as exc:
        live_artifacts.save_test_result(
            source,
            "browser",
            topic,
            status="failed",
            items=items,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        live_artifacts.save_test_result(
            source,
            "browser",
            topic,
            status="passed",
            items=items,
        )
