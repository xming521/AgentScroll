"""今日头条搜索模块 - 提供热点趋势和资讯搜索。

Author: Jesse (https://github.com/Jesseovo)

使用今日头条服务端渲染搜索页和热榜接口。
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from . import comments, dates, live_artifacts, page_content, relevance, throttle, web_search
from .request_profiles import api_headers, navigation_headers


class _DruidCardParser(HTMLParser):
    """Collect server-rendered JSON cards from Toutiao's public search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: List[str] = []
        self._active = False
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if (
            tag == "script"
            and attrs_dict.get("type") == "application/json"
            and "data-druid-card-data-id" in attrs_dict
        ):
            self._active = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._active:
            self.cards.append("".join(self._parts))
            self._active = False
            self._parts = []


def search_toutiao(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """搜索今日头条内容。

    Args:
        topic: 搜索关键词
        from_date: 起始日期
        to_date: 结束日期
        depth: 搜索深度

    Returns:
        头条文章/视频列表
    """
    limit_map = {"quick": 10, "default": 10, "deep": 10}
    limit = limit_map.get(depth, 10)

    items = _search_public_page(topic, limit)

    if depth != "quick":
        hot_items = _get_hot_related(topic)
        existing_titles = {it.get("title", "").lower() for it in items}
        for hi in hot_items:
            if hi.get("title", "").lower() not in existing_titles:
                items.append(hi)

    if not items:
        items = _search_via_site_search(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("toutiao", "01_search", topic, items)
    _enrich_toutiao_articles(items)
    live_artifacts.save_stage("toutiao", "02_content", topic, items)
    _enrich_toutiao_comments(items)
    live_artifacts.save_stage("toutiao", "03_comments", topic, items)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={"toutiao-mobile-detail-page", "linked-page"},
    )

    scored = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        abstract = item.get("abstract", "")
        combined = f"{title} {abstract}"
        rel = relevance.token_overlap_relevance(topic, combined)
        item["id"] = f"TT{i+1}"
        item["relevance"] = rel
        item["why_relevant"] = f"今日头条：{title[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


def _search_public_page(topic: str, limit: int) -> List[Dict[str, Any]]:
    """Parse the current server-rendered search page."""
    items: List[Dict[str, Any]] = []
    try:
        encoded = urllib.parse.quote(topic)
        url = (
            f"https://so.toutiao.com/search?dvpf=pc&keyword={encoded}"
            "&source=input&aid=4916&pd=information&page_num=0"
        )
        headers = navigation_headers("https://www.toutiao.com/")
        headers["Cookie"] = "tt_webid=1"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            page_html = response.read().decode("utf-8", errors="replace")

        parser = _DruidCardParser()
        parser.feed(page_html)
        parser.close()
        seen = set()
        for raw_card in parser.cards:
            try:
                card = json.loads(raw_card).get("data", {})
            except (json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(card, dict) or not card.get("title"):
                continue
            if not (card.get("article_url") or card.get("group_id")):
                continue
            parsed = _parse_article(card)
            if not parsed or not parsed.get("url") or parsed["url"] in seen:
                continue
            seen.add(parsed["url"])
            parsed["source"] = "toutiao-search-page"
            items.append(parsed)
            if len(items) >= limit:
                break
    except Exception as e:
        sys.stderr.write(f"[今日头条] 当前搜索页解析失败: {e}\n")
    return items


def _get_hot_related(topic: str) -> List[Dict[str, Any]]:
    """从头条热榜中查找相关话题。"""
    items = []
    try:
        url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
        headers = api_headers("https://www.toutiao.com/")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        topic_lower = topic.lower()
        for entry in data.get("data", []):
            title = entry.get("Title", "")
            if any(kw in title.lower() for kw in topic_lower.split()):
                items.append({
                    "title": title,
                    "abstract": entry.get("Abstract", ""),
                    "url": entry.get("Url", ""),
                    "source_name": "今日头条热榜",
                    "date": None,
                    "is_hot": True,
                    "hot_value": entry.get("HotValue", 0),
                    "engagement": {
                        "hot_value": entry.get("HotValue", 0),
                    },
                })
    except Exception as e:
        sys.stderr.write(f"[今日头条] 热榜搜索失败: {e}\n")
    return items


def _fetch_html(url: str, timeout: int = 8) -> str:
    headers = navigation_headers("https://cn.bing.com/")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _search_via_site_search(topic: str, limit: int) -> List[Dict[str, Any]]:
    """官方搜索/热榜无结果时，用公开搜索引擎兜底获取头条公开链接。"""
    items: List[Dict[str, Any]] = []
    try:
        query = f"site:toutiao.com {topic}"
        results = web_search.search_bing(
            query,
            allowed_url_fragments=("toutiao.com/",),
            limit=limit,
            fetcher=_fetch_html,
        )
        if not results:
            results = web_search.search_yahoo(
                query,
                allowed_url_fragments=("toutiao.com/",),
                limit=limit,
            )
        for result in results:
            items.append({
                "title": result["title"],
                "abstract": result["snippet"],
                "url": result["url"],
                "source_name": "今日头条",
                "date": None,
                "engagement": {},
                "source": "site-search-fallback",
            })
    except Exception as e:
        sys.stderr.write(f"[今日头条] 站内搜索兜底失败: {e}\n")
    if items:
        sys.stderr.write(f"[今日头条] 官方搜索/热榜无结果，已用公开搜索兜底获取 {len(items)} 条公开链接。\n")
    return items


def _parse_article(entry: dict) -> Optional[Dict[str, Any]]:
    """解析头条文章。"""
    title = entry.get("title", "")
    if not title:
        return None

    date_str = None
    publish_time = entry.get("publish_time") or entry.get("behot_time", 0)
    if publish_time:
        date_str = dates.timestamp_to_date(int(publish_time))

    article_url = entry.get("article_url") or entry.get("display_url", "")
    if article_url and not article_url.startswith("http"):
        article_url = f"https://www.toutiao.com{article_url}"

    return {
        "title": _clean_html(title),
        "abstract": _clean_html(entry.get("abstract", "")),
        "url": article_url,
        "group_id": str(entry.get("group_id") or ""),
        "item_id": str(entry.get("item_id") or entry.get("group_id") or ""),
        "source_name": entry.get("source", "") or entry.get("media_name", ""),
        "date": date_str,
        "engagement": {
            "comments": entry.get("comment_count", 0),
            "likes": entry.get("digg_count", 0) or entry.get("like_count", 0),
            "reads": entry.get("read_count", 0),
        },
    }


def _enrich_toutiao_articles(items: List[Dict[str, Any]]) -> None:
    """Use Toutiao's readable mobile detail page to obtain article bodies."""
    for item in items:
        url = str(item.get("url") or "")
        match = re.search(r"/(?:group|article)/(\d+)", url)
        detail_url = (
            f"https://m.toutiao.com/article/{match.group(1)}/"
            if match
            else url
        )
        try:
            final_url, page_html = page_content.fetch_page(
                detail_url,
                referer="https://www.toutiao.com/",
                mobile=True,
                throttle_key="toutiao",
            )
            content = page_content.extract_main_text(
                page_html,
                preferred_classes=("syl-article-base",),
            )
        except Exception:
            final_url, content = detail_url, ""
        if content:
            page_content.apply_content(
                item,
                content,
                content_type="article-body",
                content_source=(
                    "toutiao-mobile-detail-page"
                    if urllib.parse.urlsplit(detail_url).hostname == "m.toutiao.com"
                    else "linked-page"
                ),
                content_url=final_url,
            )
            continue
        page_content.apply_summary_fallback(
            item,
            item.get("abstract"),
            item.get("title"),
        )


def _enrich_toutiao_comments(items: List[Dict[str, Any]]) -> None:
    """Capture Toutiao's dynamically signed comment response from article pages."""
    pending: List[tuple[Dict[str, Any], str, str]] = []
    for item in items:
        if item.get("content_source") not in {
            "toutiao-mobile-detail-page",
            "linked-page",
        } or not item.get("content"):
            continue
        group_id = str(item.get("group_id") or "")
        if not group_id:
            match = re.search(
                r"/(?:group|article)/(\d+)", str(item.get("url") or "")
            )
            group_id = match.group(1) if match else ""
        if not group_id:
            continue
        item_id = str(item.get("item_id") or group_id)
        item["group_id"] = group_id
        item["item_id"] = item_id
        expected = comments.count((item.get("engagement") or {}).get("comments"))
        if expected <= 0:
            comments.apply(
                item,
                [],
                status="empty",
                source="toutiao-search-card",
                evidence={"kind": "comment-count", "group_id": group_id, "count": 0},
            )
            continue
        pending.append((item, group_id, item_id))

    if not pending:
        return
    if not comments.browser_allowed():
        for item, group_id, _ in pending:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="toutiao-comment-api",
                evidence={"reason": "browser-disabled", "group_id": group_id},
            )
        return

    try:
        from . import crawler_bridge
        with crawler_bridge._launch_browser_context(
            "toutiao",
            mobile=False,
        ) as (_, _, page):
            captured: Dict[str, Any] = {"group_id": "", "payload": None}

            def _on_response(response) -> None:
                try:
                    if (
                        "/article/v4/tab_comments/" in response.url
                        and captured["group_id"]
                        and f"group_id={captured['group_id']}" in response.url
                        and response.status == 200
                    ):
                        captured["payload"] = response.json()
                except Exception:
                    pass

            page.on("response", _on_response)
            for item, group_id, item_id in pending:
                captured["group_id"] = group_id
                captured["payload"] = None
                throttle.wait_for_detail_request("toutiao")
                try:
                    page.goto(
                        f"https://www.toutiao.com/article/{group_id}/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    crawler_bridge._wait_for(
                        page,
                        lambda: isinstance(captured["payload"], dict),
                        timeout_ms=10000,
                    )
                    payload = captured["payload"]
                    raw_comments = (
                        payload.get("data") if isinstance(payload, dict) else None
                    )
                    parsed = _parse_toutiao_comments(raw_comments)
                    comments.apply(
                        item,
                        parsed,
                        status="empty" if isinstance(raw_comments, list) else "blocked",
                        source="toutiao-comment-api",
                        evidence={
                            "kind": "browser-response",
                            "endpoint": "/article/v4/tab_comments/",
                            "group_id": group_id,
                            "item_id": item_id,
                        },
                    )
                except Exception as exc:
                    sys.stderr.write(f"[今日头条] 评论读取失败({group_id}): {exc}\n")
                    comments.apply(
                        item,
                        [],
                        status="unavailable",
                        source="toutiao-comment-api",
                        evidence={"kind": "browser-response", "group_id": group_id},
                    )
    except Exception as exc:
        sys.stderr.write(f"[今日头条] Chromium 评论读取失败: {exc}\n")
        for item, group_id, _ in pending:
            if "comments_status" not in item:
                comments.apply(
                    item,
                    [],
                    status="unavailable",
                    source="toutiao-comment-api",
                    evidence={"kind": "browser-response", "group_id": group_id},
                )


def _parse_toutiao_comments(raw_comments: Any) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for wrapper in raw_comments if isinstance(raw_comments, list) else []:
        raw = wrapper.get("comment") if isinstance(wrapper, dict) else None
        if not isinstance(raw, dict):
            continue
        parent_id = raw.get("id_str") or raw.get("id")
        value = comments.entry(
            parent_id,
            raw.get("text"),
            created_at=comments.timestamp(raw.get("create_time")),
            likes=raw.get("digg_count"),
            reply_count=raw.get("reply_count"),
        )
        if value:
            parsed.append(value)
        seen_replies = set()
        replies = list(raw.get("reply_list") or []) + list(raw.get("new_reply_list") or [])
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            reply_id = str(reply.get("id_str") or reply.get("id") or "")
            if reply_id and reply_id in seen_replies:
                continue
            seen_replies.add(reply_id)
            value = comments.entry(
                reply_id,
                reply.get("text") or reply.get("content"),
                created_at=comments.timestamp(reply.get("create_time")),
                likes=reply.get("digg_count"),
                parent_comment_id=parent_id,
            )
            if value:
                parsed.append(value)
            if len(parsed) >= comments.limit():
                return parsed
        if len(parsed) >= comments.limit():
            break
    return parsed


def _clean_html(text: str) -> str:
    """清除 HTML 标签。"""
    return re.sub(r"<[^>]+>", "", text).strip()
