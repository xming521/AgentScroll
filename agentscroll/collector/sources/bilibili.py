"""B站搜索模块 - 搜索哔哩哔哩视频内容。

Author: Jesse (https://github.com/Jesseovo)

支持两种模式（自动切换）：
1. B站公开搜索 API（无需 API Key）
2. 本地 Playwright 浏览器搜索（备用方案）
"""

import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

from . import (
    browser,
    comments,
    dates,
    live_artifacts,
    page_content,
    relevance,
    throttle,
    web_search,
)
from .request_profiles import api_headers, navigation_headers


def search_bilibili(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    *,
    require_known_date: bool = False,
) -> List[Dict[str, Any]]:
    """搜索B站视频。

    Args:
        topic: 搜索关键词
        from_date: 起始日期 YYYY-MM-DD
        to_date: 结束日期 YYYY-MM-DD
        depth: 搜索深度 quick/default/deep

    Returns:
        B站视频列表
    """
    limit_map = {"quick": 5, "default": 10, "deep": 20}
    limit = limit_map.get(depth, 10)
    pages = 1 if depth == "quick" else (2 if depth == "default" else 3)

    items: List[Dict[str, Any]] = []
    opener = _create_public_session(topic)

    for page_num in range(1, pages + 1):
        try:
            page_items = _search_page(topic, page_num, opener=opener)
            items.extend(page_items)
            if len(items) >= limit:
                break
        except Exception as e:
            sys.stderr.write(f"[B站] 搜索第 {page_num} 页失败: {e}\n")
            break

    if not items:
        try:
            if browser.is_playwright_available():
                sys.stderr.write("[B站] API 无结果，尝试 Playwright 浏览器搜索...\n")
                items = crawl_bilibili(topic, limit)
                if items:
                    sys.stderr.write(f"[B站] 爬虫模式获取 {len(items)} 条结果\n")
        except Exception as e:
            sys.stderr.write(f"[B站] 爬虫模式失败: {e}\n")

    if not items:
        items = _search_via_site_search(topic, limit)

    items = _rank_items(topic, items[:limit])
    live_artifacts.save_stage("bilibili", "01_search", topic, items)
    items = _retain_relevant(items)
    _enrich_video_contents(items, opener)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={"bilibili-view-api"},
    )
    items = _rank_items(topic, items)
    live_artifacts.save_stage("bilibili", "02_content", topic, items)
    items = _retain_relevant(items)
    items = _retain_date_range(
        items,
        from_date=from_date,
        to_date=to_date,
        require_known_date=require_known_date,
    )
    _enrich_video_comments(items, opener)
    live_artifacts.save_stage("bilibili", "03_comments", topic, items)
    return items[:limit]


def crawl_bilibili(topic: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Use Playwright to collect Bilibili search results."""
    if not browser.is_playwright_available():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with browser.browser_context("bilibili") as (_, _, page):
            search_url = (
                "https://search.bilibili.com/all?"
                f"keyword={urllib.parse.quote(topic)}&order=totalrank"
            )
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                sys.stderr.write(f"[爬虫-B站] 页面加载失败: {exc}\n")

            browser.wait_for(
                page,
                lambda: bool(
                    page.query_selector(
                        "div.bili-video-card, div[class*='video-list-item'], "
                        "a[href*='/video/BV']"
                    )
                ),
                timeout_ms=8000,
            )
            video_elements = page.query_selector_all(
                "div.bili-video-card, div[class*='video-list-item'], "
                "div[class*='video-item']"
            )
            for element in video_elements[:limit]:
                try:
                    title_element = element.query_selector(
                        "h3[class*='title'], a[class*='title']"
                    )
                    title = (
                        _clean_html(title_element.inner_text())
                        if title_element
                        else ""
                    )
                    link_element = element.query_selector("a[href*='/video/']")
                    link = ""
                    if link_element:
                        href = link_element.get_attribute("href") or ""
                        if href.startswith("//"):
                            link = f"https:{href}"
                        elif href.startswith("/"):
                            link = f"https://www.bilibili.com{href}"
                        else:
                            link = href
                    if not title or "/video/" not in link:
                        continue

                    author_element = element.query_selector(
                        "span[class*='name'], "
                        "span.bili-video-card__info--author"
                    )
                    views_element = element.query_selector(
                        "span[class*='play'], span[class*='view']"
                    )
                    items.append(
                        {
                            "title": title,
                            "url": link,
                            "bvid": "",
                            "channel_name": (
                                author_element.inner_text()
                                if author_element
                                else ""
                            ),
                            "author_mid": "",
                            "date": None,
                            "duration": "",
                            "description": "",
                            "engagement": {
                                "views": comments.count(
                                    views_element.inner_text()
                                    if views_element
                                    else "0"
                                ),
                                "danmaku": 0,
                                "comments": 0,
                                "favorites": 0,
                                "likes": 0,
                            },
                            "source": "crawler",
                        }
                    )
                except Exception:
                    continue

            if not items:
                seen = set()
                for link_element in page.query_selector_all("a[href*='/video/BV']"):
                    try:
                        href = link_element.get_attribute("href") or ""
                        if href.startswith("//"):
                            link = f"https:{href}"
                        elif href.startswith("/"):
                            link = f"https://www.bilibili.com{href}"
                        else:
                            link = href
                        link = link.split("?", 1)[0]
                        if not link or link in seen:
                            continue
                        title = _clean_html(
                            link_element.get_attribute("title")
                            or link_element.inner_text()
                            or ""
                        )
                        if not title:
                            continue
                        seen.add(link)
                        match = re.search(r"/video/(BV[0-9A-Za-z]+)", link)
                        items.append(
                            {
                                "title": title,
                                "url": link,
                                "bvid": match.group(1) if match else "",
                                "channel_name": "",
                                "author_mid": "",
                                "date": None,
                                "duration": "",
                                "description": "",
                                "engagement": {
                                    "views": 0,
                                    "danmaku": 0,
                                    "comments": 0,
                                    "favorites": 0,
                                    "likes": 0,
                                },
                                "source": "crawler-link",
                            }
                        )
                        if len(items) >= limit:
                            break
                    except Exception:
                        continue
    except Exception as exc:
        sys.stderr.write(f"[爬虫-B站] 浏览器爬取失败: {exc}\n")
    return items


def _rank_items(topic: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scored = []
    for i, item in enumerate(items):
        title = _clean_html(item.get("title", ""))
        text = " ".join(
            str(value or "")
            for value in (title, item.get("description"), item.get("content"))
        )
        rel = relevance.token_overlap_relevance(topic, text)
        item["id"] = f"BL{i+1}"
        item["title"] = title
        item["relevance"] = rel
        item["why_relevant"] = f"B站视频：{title[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored


def _retain_relevant(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in items
        if item.get("relevance", 0) >= relevance.MIN_POST_RELEVANCE
    ]


def collect_hot_topic_posts(
    topics: Iterable[str],
    *,
    posts_per_topic: int = 1,
    from_date: str = "",
    to_date: str = "",
    require_known_date: bool = False,
) -> List[Dict[str, Any]]:
    """Resolve selected Bilibili hot-search titles to videos and comments."""
    if posts_per_topic <= 0:
        raise ValueError("posts_per_topic 必须大于 0")
    results: List[Dict[str, Any]] = []
    for value in topics:
        topic = str(value).strip()
        try:
            posts = search_bilibili(
                topic,
                from_date,
                to_date,
                depth="quick",
                require_known_date=require_known_date,
            )
            search_count = len(posts)
            posts = posts[:posts_per_topic]
            results.append({
                "query": topic,
                "status": "readable" if posts else "empty",
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
        raise ValueError(f"无效的 B站日期范围：{from_date!r} 至 {to_date!r}")
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


def _search_via_site_search(topic: str, limit: int) -> List[Dict[str, Any]]:
    """Use indexed Bilibili video pages when its API returns HTTP 412."""
    items: List[Dict[str, Any]] = []
    try:
        results = web_search.search_bing(
            f"site:bilibili.com/video {topic}",
            allowed_url_fragments=("bilibili.com/video/",),
            limit=limit,
        )
        for result in results:
            match = re.search(r"/video/(BV[0-9A-Za-z]+)", result["url"])
            items.append({
                "title": result["title"],
                "url": result["url"],
                "bvid": match.group(1) if match else "",
                "channel_name": "",
                "author_mid": "",
                "date": None,
                "duration": "",
                "description": result["snippet"],
                "engagement": {
                    "views": 0,
                    "danmaku": 0,
                    "comments": 0,
                    "favorites": 0,
                    "likes": 0,
                },
                "source": "site-search-fallback",
            })
    except Exception as e:
        sys.stderr.write(f"[B站] 站内搜索兜底失败: {e}\n")
    if items:
        sys.stderr.write(f"[B站] API/Playwright 路径无结果，已用站内搜索兜底获取 {len(items)} 条公开链接。\n")
    return items


def _create_public_session(topic: str) -> urllib.request.OpenerDirector:
    """Initialize the public visitor session used by Bilibili web search."""
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    page_url = (
        "https://search.bilibili.com/all?keyword="
        f"{urllib.parse.quote(topic)}"
    )
    request = urllib.request.Request(
        page_url,
        headers=navigation_headers("https://www.bilibili.com/"),
    )
    try:
        with opener.open(request, timeout=15) as response:
            response.read()
    except Exception as e:
        sys.stderr.write(f"[B站] 访客会话初始化失败: {e}\n")
    return opener


def _search_page(
    topic: str,
    page: int = 1,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> List[Dict[str, Any]]:
    """搜索B站单页结果。"""
    encoded = urllib.parse.quote(topic)
    url = (
        f"https://api.bilibili.com/x/web-interface/search/type"
        f"?search_type=video&keyword={encoded}&page={page}&page_size=20"
        f"&order=totalrank"
    )
    headers = api_headers("https://search.bilibili.com/")
    req = urllib.request.Request(url, headers=headers)
    request_opener = opener or urllib.request.build_opener()
    with request_opener.open(req, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    items = []
    results = data.get("data", {}).get("result", [])
    if not results:
        return items

    for r in results:
        items.append(_parse_video(r))

    return items


def _fetch_video_detail(
    bvid: str,
    opener: urllib.request.OpenerDirector,
) -> Optional[Dict[str, Any]]:
    if not bvid:
        return None
    url = (
        "https://api.bilibili.com/x/web-interface/view?bvid="
        f"{urllib.parse.quote(bvid)}"
    )
    request = urllib.request.Request(
        url,
        headers=api_headers("https://www.bilibili.com/"),
    )
    try:
        throttle.wait_for_detail_request("bilibili")
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data")
        return data if payload.get("code") == 0 and isinstance(data, dict) else None
    except Exception as e:
        sys.stderr.write(f"[B站] 视频详情读取失败({bvid}): {e}\n")
        return None


def _enrich_video_contents(
    items: List[Dict[str, Any]],
    opener: urllib.request.OpenerDirector,
) -> None:
    """Read the complete video description from Bilibili's detail API."""
    for item in items:
        bvid = str(item.get("bvid") or "")
        if not bvid:
            match = re.search(r"/video/(BV[0-9A-Za-z]+)", str(item.get("url") or ""))
            bvid = match.group(1) if match else ""
        detail = _fetch_video_detail(bvid, opener) if bvid else None
        if detail:
            description = str(detail.get("desc") or "")
            owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
            stat = detail.get("stat") if isinstance(detail.get("stat"), dict) else {}
            item.update({
                "title": detail.get("title") or item.get("title", ""),
                "description": description,
                "url": f"https://www.bilibili.com/video/{bvid}",
                "bvid": bvid,
                "aid": detail.get("aid") or item.get("aid", ""),
                "channel_name": owner.get("name") or item.get("channel_name", ""),
                "author_mid": owner.get("mid") or item.get("author_mid", ""),
                "date": (
                    dates.timestamp_to_date(detail["pubdate"])
                    if detail.get("pubdate")
                    else item.get("date")
                ),
                "duration": detail.get("duration", item.get("duration", "")),
                "engagement": {
                    "views": stat.get("view", 0),
                    "danmaku": stat.get("danmaku", 0),
                    "comments": stat.get("reply", 0),
                    "favorites": stat.get("favorite", 0),
                    "likes": stat.get("like", 0),
                },
            })
            page_content.apply_content(
                item,
                description,
                content_type="post-description" if description else "unavailable",
                content_source="bilibili-view-api",
                content_url=f"https://www.bilibili.com/video/{bvid}",
            )
            continue
        page_content.apply_summary_fallback(
            item,
            item.get("description"),
            item.get("title"),
        )


def _enrich_video_comments(
    items: List[Dict[str, Any]],
    opener: urllib.request.OpenerDirector,
) -> None:
    """Read the first public reply page for every returned Bilibili video."""
    for item in items:
        aid = str(item.get("aid") or "")
        if not aid:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="bilibili-reply-api",
                evidence={"reason": "missing-aid"},
            )
            continue
        url = (
            "https://api.bilibili.com/x/v2/reply/main?type=1"
            f"&oid={urllib.parse.quote(aid)}&mode=3&next=0&ps={comments.limit()}"
        )
        request = urllib.request.Request(
            url,
            headers=api_headers(str(item.get("url") or "https://www.bilibili.com/")),
        )
        try:
            throttle.wait_for_detail_request("bilibili")
            with opener.open(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_replies = (payload.get("data") or {}).get("replies") or []
            parsed = _parse_bilibili_comments(raw_replies)
            status = "empty" if payload.get("code") == 0 else "blocked"
            comments.apply(
                item,
                parsed,
                status=status,
                source="bilibili-reply-api",
                evidence={
                    "kind": "platform-api",
                    "endpoint": "/x/v2/reply/main",
                    "oid": aid,
                },
            )
        except Exception as exc:
            sys.stderr.write(f"[B站] 评论读取失败({aid}): {exc}\n")
            comments.apply(
                item,
                [],
                status="unavailable",
                source="bilibili-reply-api",
                evidence={"kind": "platform-api", "oid": aid},
            )


def _parse_bilibili_comments(raw_replies: Any) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for raw in raw_replies if isinstance(raw_replies, list) else []:
        if not isinstance(raw, dict):
            continue
        parent_id = raw.get("rpid_str") or raw.get("rpid")
        content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
        value = comments.entry(
            parent_id,
            content.get("message"),
            created_at=comments.timestamp(raw.get("ctime")),
            likes=raw.get("like"),
            reply_count=raw.get("rcount"),
        )
        if value:
            parsed.append(value)
        for reply in raw.get("replies") or []:
            if not isinstance(reply, dict):
                continue
            reply_content = (
                reply.get("content") if isinstance(reply.get("content"), dict) else {}
            )
            value = comments.entry(
                reply.get("rpid_str") or reply.get("rpid"),
                reply_content.get("message"),
                created_at=comments.timestamp(reply.get("ctime")),
                likes=reply.get("like"),
                reply_count=reply.get("rcount"),
                parent_comment_id=parent_id,
            )
            if value:
                parsed.append(value)
            if len(parsed) >= comments.limit():
                return parsed
        if len(parsed) >= comments.limit():
            break
    return parsed


def _parse_video(v: dict) -> Dict[str, Any]:
    """解析B站视频搜索结果。"""
    bvid = v.get("bvid", "")
    pubdate = v.get("pubdate", 0)
    date_str = None
    if pubdate:
        date_str = dates.timestamp_to_date(pubdate)

    return {
        "title": v.get("title", ""),
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else v.get("arcurl", ""),
        "bvid": bvid,
        "aid": v.get("aid", ""),
        "channel_name": v.get("author", ""),
        "author_mid": v.get("mid", ""),
        "date": date_str,
        "duration": v.get("duration", ""),
        "description": v.get("description", ""),
        "engagement": {
            "views": v.get("play", 0),
            "danmaku": v.get("danmaku", 0),
            "comments": v.get("review", 0) or v.get("comment", 0),
            "favorites": v.get("favorites", 0),
            "likes": v.get("like", 0),
        },
    }


def _clean_html(text: str) -> str:
    """清除搜索结果中的 HTML 高亮标签。"""
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
