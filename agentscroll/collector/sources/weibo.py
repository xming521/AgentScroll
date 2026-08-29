"""使用 ``mcp-server-weibo`` 搜索并标准化微博内容。"""

import asyncio
import html
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import httpx
from mcp_server_weibo.consts import DEFAULT_HEADERS
from mcp_server_weibo.weibo import WeiboCrawler

from . import comments, dates, live_artifacts, page_content, relevance, throttle


_DETAIL_URL = "https://m.weibo.cn/statuses/show?id={feed_id}"
_LONG_TEXT_URL = "https://m.weibo.cn/statuses/extend?id={feed_id}"
_CONTENT_SOURCE = "mcp-server-weibo-feed-detail"


def search_weibo(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """通过 ``mcp-server-weibo`` 搜索微博内容。

    Args:
        topic: 搜索关键词
        from_date: 起始日期 YYYY-MM-DD
        to_date: 结束日期 YYYY-MM-DD
        depth: 搜索深度 quick/default/deep

    Returns:
        微博条目列表，每条包含 id, text, url, author_handle, date,
        engagement, images, videos, relevance, why_relevant 等字段
    """
    limit_map = {"quick": 5, "default": 10, "deep": 20}
    limit = limit_map.get(depth, 10)
    items = asyncio.run(_search_content(topic, limit))
    items = items[:limit]
    live_artifacts.save_stage("weibo", "01_search", topic, items)
    asyncio.run(_enrich_post_contents(items))
    live_artifacts.save_stage("weibo", "02_content", topic, items)
    asyncio.run(_enrich_post_comments(items))
    live_artifacts.save_stage("weibo", "03_comments", topic, items)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={_CONTENT_SOURCE},
    )

    return _rank_items(topic, items, limit)


def collect_hot_topic_posts(
    topics: Iterable[str],
    *,
    posts_per_topic: int = 1,
    from_date: str = "",
    to_date: str = "",
    require_known_date: bool = False,
) -> List[Dict[str, Any]]:
    """Resolve hot-list titles to readable Weibo posts and public comments.

    The returned list preserves the input topic order. Each entry reports whether
    the title produced no search results, produced candidates without readable
    details, or produced posts with normalized content and comment fields.
    """
    if posts_per_topic <= 0:
        raise ValueError("posts_per_topic 必须大于 0")
    normalized_topics = [str(topic).strip() for topic in topics]
    return asyncio.run(
        _collect_hot_topic_posts(
            normalized_topics,
            posts_per_topic=posts_per_topic,
            from_date=from_date,
            to_date=to_date,
            require_known_date=require_known_date,
        )
    )


async def _collect_hot_topic_posts(
    topics: List[str],
    *,
    posts_per_topic: int,
    from_date: str,
    to_date: str,
    require_known_date: bool,
) -> List[Dict[str, Any]]:
    crawler = WeiboCrawler()
    results: List[Dict[str, Any]] = []
    for topic in topics:
        if not topic:
            results.append({
                "query": topic,
                "status": "error",
                "search_count": 0,
                "post_count": 0,
                "posts": [],
                "error": "ValueError: 微博热搜标题不能为空",
            })
            continue
        try:
            feeds = await crawler.search_content(
                keyword=topic,
                limit=posts_per_topic,
                page=1,
            )
            items = [_normalize_feed(feed) for feed in feeds]
            search_count = len(items)
            await _enrich_post_contents(items, crawler=crawler)
            items = _retain_date_range(
                items,
                from_date=from_date,
                to_date=to_date,
                require_known_date=require_known_date,
            )
            items = page_content.retain_readable_details(
                items,
                allowed_sources={_CONTENT_SOURCE},
            )
            posts = _rank_items(topic, items, posts_per_topic)
            await _enrich_post_comments(posts, crawler=crawler)
            results.append({
                "query": topic,
                "status": (
                    "readable"
                    if posts
                    else ("unavailable" if search_count else "empty")
                ),
                "search_count": search_count,
                "post_count": len(posts),
                "posts": posts,
                "error": None,
            })
        except Exception as exc:
            results.append({
                "query": topic,
                "status": "error",
                "search_count": 0,
                "post_count": 0,
                "posts": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def _retain_date_range(
    items: List[Dict[str, Any]],
    *,
    from_date: str,
    to_date: str,
    require_known_date: bool,
) -> List[Dict[str, Any]]:
    if not from_date and not to_date:
        return items
    start = dates.parse_date(from_date)
    end = dates.parse_date(to_date)
    if start is None or end is None:
        raise ValueError(f"无效的微博日期范围：{from_date!r} 至 {to_date!r}")
    retained = []
    for item in items:
        published_at = dates.parse_date(str(item.get("date") or ""))
        if published_at is None:
            if not require_known_date:
                retained.append(item)
            continue
        if start.date() <= published_at.date() <= end.date():
            retained.append(item)
    return retained


def _rank_items(
    topic: str,
    items: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    scored = []
    for i, item in enumerate(items):
        text = item.get("text", "")
        rel = relevance.token_overlap_relevance(topic, text)
        item["id"] = f"WB{i+1}"
        item["relevance"] = rel
        item["why_relevant"] = f"微博讨论：{text[:60]}..."
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


async def _search_content(topic: str, limit: int) -> List[Dict[str, Any]]:
    crawler = WeiboCrawler()
    feeds = await crawler.search_content(keyword=topic, limit=limit, page=1)
    return [_normalize_feed(feed) for feed in feeds]


async def _enrich_post_contents(
    items: List[Dict[str, Any]],
    *,
    crawler: Optional[WeiboCrawler] = None,
) -> None:
    """Read the complete body of every searched Weibo post through MCP."""
    crawler = crawler or WeiboCrawler()
    for item in items:
        feed_id = str(item.get("platform_id") or "")
        if not feed_id:
            page_content.apply_summary_fallback(item, item.get("text"))
            continue
        try:
            await asyncio.to_thread(throttle.wait_for_detail_request, "weibo")
            detail = await _get_feed_detail(crawler, feed_id)
            if not detail:
                page_content.apply_summary_fallback(item, item.get("text"))
                continue

            normalized = _normalize_feed(detail)
            content = normalized.get("text", "")
            item.update({
                "text": content,
                "text_html": normalized.get("text_html", ""),
                "date": normalized.get("date") or item.get("date"),
                "engagement": normalized.get("engagement") or item.get("engagement", {}),
                "images": normalized.get("images") or item.get("images", []),
                "image_thumbnails": (
                    normalized.get("image_thumbnails")
                    or item.get("image_thumbnails", [])
                ),
                "videos": normalized.get("videos") or item.get("videos", {}),
                "post_source": normalized.get("post_source") or item.get("post_source", ""),
                "extraction_evidence": {
                    "kind": "platform-api",
                    "mcp_method": "get_feed_detail",
                    "endpoint": "/statuses/show",
                    "long_text_endpoint": "/statuses/extend",
                    "feed_id": feed_id,
                },
            })
            page_content.apply_content(
                item,
                content,
                content_type="post-description" if content else "unavailable",
                content_source=_CONTENT_SOURCE,
                content_url=_DETAIL_URL.format(feed_id=feed_id),
            )
        except Exception:
            page_content.apply_summary_fallback(item, item.get("text"))


async def _get_feed_detail(crawler: WeiboCrawler, feed_id: str) -> Any:
    """Call the MCP detail method, with compatibility for released 1.2.0."""
    detail_method = getattr(crawler, "get_feed_detail", None)
    if callable(detail_method):
        return await detail_method(feed_id)

    # mcp-server-weibo 1.2.0 has the visitor-cookie transport but does not yet
    # expose the detail method. Use the same crawler session contract so Scroll
    # keeps working until the bundled MCP extension is installed.
    if not crawler.cookies:
        await crawler._ensure_cookies()
    async with httpx.AsyncClient(
        cookies=crawler.cookies,
        trust_env=False,
        timeout=20,
    ) as client:
        response = await client.get(
            _DETAIL_URL.format(feed_id=feed_id),
            headers=DEFAULT_HEADERS,
        )
        response.raise_for_status()
        result = response.json()
        mblog = result.get("data") or {}
        if result.get("ok") != 1 or not mblog:
            return {}

        text = str(mblog.get("text") or "")
        if mblog.get("isLongText") or f'/status/{feed_id}' in text:
            long_response = await client.get(
                _LONG_TEXT_URL.format(feed_id=feed_id),
                headers=DEFAULT_HEADERS,
            )
            long_response.raise_for_status()
            long_result = long_response.json()
            long_text = str(
                (long_result.get("data") or {}).get("longTextContent") or ""
            )
            if long_result.get("ok") == 1 and long_text:
                mblog["text"] = long_text
                mblog["raw_text"] = long_text
        return crawler._to_feed_item(mblog)


async def _enrich_post_comments(
    items: List[Dict[str, Any]],
    *,
    crawler: Optional[WeiboCrawler] = None,
) -> None:
    """Read one public comment page for every returned Weibo post."""
    crawler = crawler or WeiboCrawler()
    for item in items:
        feed_id = str(item.get("platform_id") or "")
        if not feed_id:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="weibo-comments-api",
                evidence={"reason": "missing-platform-id"},
            )
            continue
        try:
            await asyncio.to_thread(throttle.wait_for_detail_request, "weibo")
            raw_comments = await crawler.get_comments(feed_id=feed_id, page=1)
            parsed = []
            for raw in raw_comments[:comments.limit()]:
                data = raw.model_dump() if hasattr(raw, "model_dump") else raw
                if not isinstance(data, dict):
                    continue
                value = comments.entry(
                    data.get("id"),
                    data.get("text"),
                    created_at=data.get("created_at"),
                )
                if value:
                    parsed.append(value)
            expected = ((item.get("engagement") or {}).get("comments") or 0) > 0
            comments.apply(
                item,
                parsed,
                status="unavailable" if expected else "empty",
                source="weibo-comments-api",
                evidence={
                    "kind": "platform-api",
                    "endpoint": "/api/comments/show",
                    "feed_id": feed_id,
                    "page": 1,
                },
            )
        except Exception:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="weibo-comments-api",
                evidence={"kind": "platform-api", "feed_id": feed_id},
            )


def _normalize_feed(feed: Any) -> Dict[str, Any]:
    """将 ``mcp-server-weibo`` 的 FeedItem 转成 collector 通用结构。"""
    user = feed.user
    if hasattr(user, "model_dump"):
        user = user.model_dump()
    elif not isinstance(user, dict):
        user = {}

    pictures = [picture for picture in (feed.pics or []) if isinstance(picture, dict)]
    images = [
        picture.get("large") or picture.get("thumbnail")
        for picture in pictures
        if picture.get("large") or picture.get("thumbnail")
    ]
    thumbnails = [
        picture["thumbnail"] for picture in pictures if picture.get("thumbnail")
    ]

    return {
        "platform_id": str(feed.id),
        "text": _clean_html(feed.text or ""),
        "text_html": feed.text or "",
        "url": f"https://m.weibo.cn/detail/{feed.id}",
        "author_handle": user.get("screen_name", ""),
        "author_id": str(user.get("id", "")),
        "date": _parse_weibo_date(feed.created_at or ""),
        "engagement": {
            "reposts": feed.reposts_count,
            "comments": feed.comments_count,
            "likes": feed.attitudes_count,
        },
        "images": images,
        "image_thumbnails": thumbnails,
        "videos": dict(feed.videos or {}),
        "post_source": feed.source or "",
        "source": "mcp-server-weibo",
    }


def _clean_html(text: str) -> str:
    """清除微博文本中的 HTML 标签。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text


def _parse_weibo_date(date_str: str) -> Optional[str]:
    """将微博日期格式转换为 YYYY-MM-DD。"""
    if not date_str:
        return None
    try:
        # "Tue Jan 01 00:00:00 +0800 2026"
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(dates.CST).strftime("%Y-%m-%d")
    except ValueError:
        pass
    # 相对时间: "x分钟前", "x小时前", "昨天 HH:MM"
    now = datetime.now(dates.CST)
    if "分钟前" in date_str:
        try:
            mins = int(re.search(r"(\d+)", date_str).group(1))
            return (now - timedelta(minutes=mins)).strftime("%Y-%m-%d")
        except Exception:
            pass
    if "小时前" in date_str:
        try:
            hours = int(re.search(r"(\d+)", date_str).group(1))
            return (now - timedelta(hours=hours)).strftime("%Y-%m-%d")
        except Exception:
            pass
    if "昨天" in date_str:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # "MM-DD" 格式
    m = re.match(r"(\d{2})-(\d{2})", date_str)
    if m:
        return f"{now.year}-{m.group(1)}-{m.group(2)}"
    return None
