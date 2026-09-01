from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agentscroll.collector.sources import weibo


def _feed(feed_id: int, text: str, comments: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=feed_id,
        text=text,
        source="",
        created_at="Tue Sep 01 12:00:00 +0800 2026",
        user={},
        comments_count=comments,
        attitudes_count=0,
        reposts_count=0,
        raw_text=text,
        region_name="",
        pics=[],
        videos={},
    )


def test_hot_topic_prefers_search_result_with_most_comments(monkeypatch) -> None:
    low_comment_feed = _feed(1, "测试话题，评论较少", 1)
    high_comment_feed = _feed(2, "测试话题，评论最多", 20)

    class FakeCrawler:
        search_limits: list[int] = []
        detail_ids: list[str] = []
        comment_ids: list[str] = []

        async def search_content(self, keyword: str, limit: int, page: int):
            assert keyword == "测试话题"
            assert page == 1
            self.search_limits.append(limit)
            return [low_comment_feed, high_comment_feed]

        async def get_feed_detail(self, feed_id: str):
            self.detail_ids.append(feed_id)
            return high_comment_feed if feed_id == "2" else low_comment_feed

        async def get_comments(self, feed_id: str, page: int):
            self.comment_ids.append(feed_id)
            return [{"id": "c1", "text": "真实评论"}]

    monkeypatch.setattr(weibo, "WeiboCrawler", FakeCrawler)
    monkeypatch.setattr(weibo.throttle, "wait_for_detail_request", lambda _: None)

    result = weibo.collect_hot_topic_posts(["测试话题"])

    assert FakeCrawler.search_limits == [weibo._HOT_TOPIC_SEARCH_CANDIDATE_LIMIT]
    assert FakeCrawler.detail_ids == ["2"]
    assert FakeCrawler.comment_ids == ["2"]
    assert result[0]["posts"][0]["platform_id"] == "2"
    assert result[0]["posts"][0]["engagement"]["comments"] == 20


def test_hot_topic_filters_irrelevant_result_before_comment_ranking(monkeypatch) -> None:
    irrelevant_feed = _feed(1, "另一条网红新闻", 100)
    relevant_feed = _feed(2, "测试话题的具体进展", 1)

    class FakeCrawler:
        detail_ids: list[str] = []

        async def search_content(self, keyword: str, limit: int, page: int):
            return [irrelevant_feed, relevant_feed]

        async def get_feed_detail(self, feed_id: str):
            self.detail_ids.append(feed_id)
            return relevant_feed

        async def get_comments(self, feed_id: str, page: int):
            return []

    monkeypatch.setattr(weibo, "WeiboCrawler", FakeCrawler)
    monkeypatch.setattr(weibo.throttle, "wait_for_detail_request", lambda _: None)

    result = weibo.collect_hot_topic_posts(["测试话题"])

    assert FakeCrawler.detail_ids == ["2"]
    assert result[0]["search_count"] == 2
    assert result[0]["posts"][0]["platform_id"] == "2"


def test_safe_search_keeps_first_page_when_next_page_redirects() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested_pages.append(page)
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "ok": 1,
                    "data": {
                        "cards": [
                            {
                                "card_type": 9,
                                "mblog": {
                                    "id": 1,
                                    "text": "第一页结果一",
                                    "comments_count": 2,
                                },
                            },
                            {
                                "card_type": 9,
                                "mblog": {
                                    "id": 2,
                                    "text": "第一页结果二",
                                    "comments_count": 3,
                                },
                            },
                        ],
                        "cardlistInfo": {"page": 2},
                    },
                },
            )
        return httpx.Response(
            302,
            headers={"location": "https://visitor.passport.weibo.cn/visitor"},
        )

    class PagingCrawler:
        cookies = {"SUB": "visitor", "SUBP": "visitor"}
        _transport = httpx.MockTransport(handler)

        async def _ensure_cookies(self) -> None:
            return None

        def _to_feed_item(self, mblog):
            return _feed(
                int(mblog["id"]),
                str(mblog["text"]),
                int(mblog["comments_count"]),
            )

    feeds = asyncio.run(
        weibo._safe_search_content(
            PagingCrawler(),
            keyword="测试话题",
            limit=15,
            page=1,
        )
    )

    assert requested_pages == ["1", "2"]
    assert [feed.id for feed in feeds] == [1, 2]


def test_safe_search_does_not_treat_invalid_first_page_as_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    class InvalidCrawler:
        cookies = {"SUB": "visitor", "SUBP": "visitor"}
        _transport = httpx.MockTransport(handler)

        async def _ensure_cookies(self) -> None:
            return None

        def _to_feed_item(self, mblog):
            return mblog

    with pytest.raises(ValueError):
        asyncio.run(
            weibo._safe_search_content(
                InvalidCrawler(),
                keyword="测试话题",
                limit=15,
                page=1,
            )
        )
