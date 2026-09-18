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

    async def fake_hotflow_comments(crawler, feed_id: str, *, limit: int):
        assert isinstance(crawler, FakeCrawler)
        assert limit == 20
        FakeCrawler.comment_ids.append(feed_id)
        return [{"id": "c1", "text": "真实评论"}]

    monkeypatch.setattr(weibo, "WeiboCrawler", FakeCrawler)
    monkeypatch.setattr(weibo, "_get_hotflow_comments", fake_hotflow_comments)
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

    async def fake_hotflow_comments(_crawler, _feed_id: str, *, limit: int):
        assert limit == 20
        return []

    monkeypatch.setattr(weibo, "WeiboCrawler", FakeCrawler)
    monkeypatch.setattr(weibo, "_get_hotflow_comments", fake_hotflow_comments)
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


def test_hotflow_comments_keep_likes_and_reply_counts(monkeypatch) -> None:
    requested_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_params.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "ok": 1,
                "data": {
                    "data": [
                        {
                            "id": 101,
                            "text": "第一条评论",
                            "created_at": "Tue Sep 01 12:00:00 +0800 2026",
                            "like_count": 12,
                            "total_number": 3,
                        },
                        {
                            "id": 102,
                            "text": "第二条评论",
                            "created_at": "Tue Sep 01 12:01:00 +0800 2026",
                            "like_count": "1.2万",
                            "total_number": 0,
                        },
                        {
                            "id": 103,
                            "text": (
                                '<span class="url-icon"><img alt="[可爱]" '
                                'src="emoji.png" /></span>'
                            ),
                            "created_at": "Tue Sep 01 12:02:00 +0800 2026",
                            "like_count": 2,
                            "total_number": 0,
                        },
                    ],
                    "max_id": 0,
                    "max_id_type": 0,
                },
            },
        )

    class HotflowCrawler:
        cookies = {"SUB": "visitor", "SUBP": "visitor"}
        _transport = httpx.MockTransport(handler)

        async def _ensure_cookies(self) -> None:
            return None

    item = {"platform_id": "123", "engagement": {"comments": 2}}
    monkeypatch.setattr(weibo.throttle, "wait_for_detail_request", lambda _: None)

    asyncio.run(weibo._enrich_post_comments([item], crawler=HotflowCrawler()))

    assert requested_params == [
        {"id": "123", "mid": "123", "max_id_type": "0"}
    ]
    assert item["comments"] == [
        {
            "comment_id": "101",
            "text": "第一条评论",
            "created_at": "Tue Sep 01 12:00:00 +0800 2026",
            "likes": 12,
            "reply_count": 3,
        },
        {
            "comment_id": "102",
            "text": "第二条评论",
            "created_at": "Tue Sep 01 12:01:00 +0800 2026",
            "likes": 12000,
            "reply_count": 0,
        },
        {
            "comment_id": "103",
            "text": "[可爱]",
            "created_at": "Tue Sep 01 12:02:00 +0800 2026",
            "likes": 2,
            "reply_count": 0,
        },
    ]
    assert item["comment_extraction_evidence"] == {
        "kind": "platform-api",
        "endpoint": "/comments/hotflow",
        "feed_id": "123",
        "limit": 20,
    }


@pytest.mark.parametrize("user_id", ["5606716867", "5762999670"])
def test_comments_exclude_specified_user_before_normalization(monkeypatch, user_id) -> None:
    async def fake_hotflow_comments(_crawler, _feed_id, *, limit):
        return [
            {"id": 1, "text": "排除整数 ID", "user": {"id": int(user_id)}},
            {"id": 2, "text": "排除字符串 ID", "user": {"id": user_id}},
            {
                "id": 3,
                "text": "保留其他用户",
                "user": {"id": 123, "screen_name": "吃瓜罗伯特"},
            },
            {"id": 4, "text": "保留没有作者字段的评论"},
        ]

    monkeypatch.setattr(weibo, "_get_hotflow_comments", fake_hotflow_comments)
    monkeypatch.setattr(weibo.throttle, "wait_for_detail_request", lambda _: None)
    item = {"platform_id": "123", "engagement": {"comments": 4}}

    asyncio.run(weibo._enrich_post_comments([item], crawler=object()))

    assert [comment["text"] for comment in item["comments"]] == [
        "保留其他用户",
        "保留没有作者字段的评论",
    ]


def test_hotflow_comments_keep_first_batch_when_next_cursor_redirects() -> None:
    requested_max_ids: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_max_ids.append(request.url.params.get("max_id"))
        if "max_id" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "ok": 1,
                    "data": {
                        "data": [
                            {"id": index, "text": f"评论{index}"}
                            for index in range(1, 21)
                        ],
                        "max_id": 12345,
                        "max_id_type": 0,
                    },
                },
            )
        return httpx.Response(
            302,
            headers={"location": "https://passport.weibo.com/sso/signin"},
        )

    class PagingCrawler:
        cookies = {"SUB": "visitor", "SUBP": "visitor"}
        _transport = httpx.MockTransport(handler)

        async def _ensure_cookies(self) -> None:
            return None

    result = asyncio.run(
        weibo._get_hotflow_comments(PagingCrawler(), "123", limit=25)
    )

    assert requested_max_ids == [None, "12345"]
    assert len(result) == 20
    assert [str(comment["id"]) for comment in result] == [
        str(index) for index in range(1, 21)
    ]
