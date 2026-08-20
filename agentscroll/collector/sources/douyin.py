"""抖音搜索模块 - 搜索抖音短视频内容。

Author: Jesse (https://github.com/Jesseovo)

支持两种模式（按优先级自动切换）：
1. MediaCrawler 浏览器爬虫（需要 Playwright，无需 API Key）
2. 站外搜索发现公开视频 URL
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import comments, dates, live_artifacts, page_content, relevance, throttle, web_search
from .request_profiles import api_headers, navigation_headers


_DETAIL_BROWSER_ENV = "AGENTSCROLL_ALLOW_DETAIL_BROWSER"
_public_share_waf_detected = False


def search_douyin(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """搜索抖音视频。

    Args:
        topic: 搜索关键词
        from_date: 起始日期
        to_date: 结束日期
        depth: 搜索深度
    Returns:
        抖音视频列表
    """
    limit_map = {"quick": 10, "default": 10, "deep": 10}
    limit = limit_map.get(depth, 10)

    items: List[Dict[str, Any]] = []

    if not items:
        try:
            from . import crawler_bridge
            if crawler_bridge.is_playwright_available():
                sys.stderr.write("[抖音] 尝试 MediaCrawler 爬虫模式...\n")
                items = crawler_bridge.crawl_douyin(topic, limit)
                if items:
                    sys.stderr.write(f"[抖音] 爬虫模式获取 {len(items)} 条结果\n")
        except Exception as e:
            sys.stderr.write(f"[抖音] 爬虫模式失败: {e}\n")

    if not items:
        items = _search_via_site_search(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("douyin", "01_search", topic, items)
    _enrich_video_contents(items)
    live_artifacts.save_stage("douyin", "02_content", topic, items)
    _enrich_video_comments(items)
    live_artifacts.save_stage("douyin", "03_comments", topic, items)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={"douyin-router-data", "douyin-detail-dom"},
    )

    scored = []
    for i, item in enumerate(items):
        text = item.get("text", "")
        rel = relevance.token_overlap_relevance(topic, text)
        item["id"] = f"DY{i+1}"
        item["relevance"] = rel
        item["why_relevant"] = f"抖音视频：{text[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


def _fetch_html(url: str, timeout: int = 8) -> str:
    headers = navigation_headers("https://cn.bing.com/")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _search_via_site_search(topic: str, limit: int) -> List[Dict[str, Any]]:
    """Discover video URLs; detail hydration is a separate stage."""
    results = _discover_site_results(topic, limit)
    items: List[Dict[str, Any]] = []
    for result in results:
        title = result["title"]
        snippet = result["snippet"]
        text = f"{title} {snippet}".strip() if snippet else title
        items.append({
            "text": text,
            "url": result["url"],
            "author_name": "",
            "author_id": "",
            "date": None,
            "engagement": {"views": 0, "likes": 0, "comments": 0, "shares": 0},
            "hashtags": re.findall(r"#([^#\s]+)#?", text),
            "duration": 0,
            "source": "web-search-discovery",
        })
    if items:
        sys.stderr.write(
            f"[抖音] 搜索接口无结果，发现 {len(items)} 条公开视频链接，准备逐条读取详情。\n"
        )
    return items


def _discover_site_results(topic: str, limit: int) -> List[Dict[str, str]]:
    """Discover candidate video URLs without treating snippets as platform data."""
    query = f"site:douyin.com/video {topic}"
    allowed = ("douyin.com/video/",)
    providers = (
        lambda: web_search.search_bing(
            query, allowed_url_fragments=allowed, limit=limit, fetcher=_fetch_html
        ),
        lambda: web_search.search_yahoo(
            query, allowed_url_fragments=allowed, limit=limit
        ),
        lambda: web_search.search_duckduckgo(
            query, allowed_url_fragments=allowed, limit=limit
        ),
    )
    for provider in providers:
        try:
            results = provider()
            if results:
                return results
        except Exception:
            continue
    return []


def _fetch_public_share(url: str, topic: str = "") -> Optional[Dict[str, Any]]:
    """Read one post from Douyin's embedded router data or dedicated detail DOM."""
    match = re.search(r"/video/(\d+)", url)
    if not match:
        return None
    aweme_id = match.group(1)
    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    try:
        global _public_share_waf_detected
        if _public_share_waf_detected:
            return None
        throttle.wait_for_detail_request("douyin")
        headers = navigation_headers("https://www.douyin.com/", mobile=True)
        req = urllib.request.Request(share_url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as response:
            page_html = response.read().decode("utf-8", errors="replace")
        if "bid:'waf_js'" in page_html or "Please wait..." in page_html:
            _public_share_waf_detected = True
            sys.stderr.write(
                "[抖音] 公开分享页触发 WAF challenge，停止继续发送 HTTP 详情请求。\n"
            )
            return None
        return _build_douyin_detail(page_html, aweme_id, topic)
    except Exception:
        return None


def _build_douyin_detail(
    page_html: str,
    aweme_id: str,
    topic: str = "",
) -> Optional[Dict[str, Any]]:
    extracted = _extract_douyin_content(page_html, aweme_id)
    if not extracted:
        return None
    text = str(extracted["content"]).strip()
    if topic and relevance.token_overlap_relevance(topic, text) <= 0:
        return None
    metadata = web_search.parse_page_metadata(page_html)
    aweme = extracted.get("aweme") or {"aweme_id": aweme_id, "desc": text}
    item = _parse_aweme(aweme)
    canonical = metadata["canonical"] or f"https://www.douyin.com/video/{aweme_id}"
    item["text"] = text
    item["url"] = canonical
    item["source"] = "public-share-page"
    item["extraction_evidence"] = extracted["evidence"]
    page_content.apply_content(
        item,
        text,
        content_type="post-description",
        content_source=str(extracted["source"]),
        content_url=canonical,
    )
    return item


def _detail_browser_enabled() -> bool:
    return os.environ.get(_DETAIL_BROWSER_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _fetch_public_share_with_browser(page, url: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"/video/(\d+)", url)
    if not match:
        return None
    aweme_id = match.group(1)
    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    try:
        page.goto(share_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_function(
                "() => Boolean(window._ROUTER_DATA)",
                timeout=10000,
            )
        except Exception:
            page.wait_for_timeout(3000)
        return _build_douyin_detail(page.content(), aweme_id)
    except Exception:
        return None


def _extract_douyin_content(
    page_html: str,
    aweme_id: str,
) -> Optional[Dict[str, Any]]:
    router_match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
        page_html,
        flags=re.S,
    )
    if router_match:
        try:
            router_data = json.loads(router_match.group(1))
            loader_data = router_data.get("loaderData", {})
            for loader_key, loader_value in loader_data.items():
                if not isinstance(loader_value, dict):
                    continue
                response = loader_value.get("videoInfoRes", {})
                for aweme in response.get("item_list") or []:
                    if str(aweme.get("aweme_id") or "") != aweme_id:
                        continue
                    desc = str(aweme.get("desc") or "").strip()
                    if desc:
                        return {
                            "content": desc,
                            "aweme": aweme,
                            "source": "douyin-router-data",
                            "evidence": {
                                "kind": "embedded-json",
                                "object": "window._ROUTER_DATA",
                                "path": (
                                    f"loaderData[{loader_key}].videoInfoRes."
                                    f"item_list[aweme_id={aweme_id}].desc"
                                ),
                            },
                        }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    desc = page_content.extract_selector_text(
        page_html,
        preferred_classes=("video-msg-container",),
        min_chars=1,
    )
    if not desc:
        return None
    return {
        "content": desc,
        "aweme": {"aweme_id": aweme_id, "desc": desc},
        "source": "douyin-detail-dom",
        "evidence": {
            "kind": "dom",
            "selector": ".video-msg-container",
        },
    }


def _enrich_video_contents(items: List[Dict[str, Any]]) -> None:
    """Read each video's public share page to obtain its publishing text."""
    unresolved: List[tuple[Dict[str, Any], str]] = []
    for item in items:
        if item.get("content_source") in {
            "douyin-router-data",
            "douyin-detail-dom",
        } and item.get("content"):
            continue
        url = str(item.get("url") or "")
        detail = _fetch_public_share(url) if url else None
        if _merge_douyin_detail(item, detail, url):
            continue
        unresolved.append((item, url))

    if unresolved and _detail_browser_enabled():
        try:
            from . import crawler_bridge
            with crawler_bridge._launch_browser_context(
                "douyin_detail",
                mobile=True,
            ) as (_, _, page):
                still_unresolved = []
                for item, url in unresolved:
                    detail = _fetch_public_share_with_browser(page, url)
                    if not _merge_douyin_detail(item, detail, url):
                        still_unresolved.append((item, url))
                unresolved = still_unresolved
        except Exception as e:
            sys.stderr.write(f"[抖音] Chromium 详情读取失败: {e}\n")

    for item, _ in unresolved:
        if not item.get("content"):
            page_content.apply_summary_fallback(
                item,
                item.get("text"),
            )


def _merge_douyin_detail(
    item: Dict[str, Any],
    detail: Optional[Dict[str, Any]],
    original_url: str,
) -> bool:
    if not detail or not detail.get("content"):
        return False
    detail_text = str(detail.get("text") or "")
    if len(detail_text) > len(str(item.get("text") or "")):
        item["text"] = detail_text
    item["url"] = detail.get("url") or original_url
    item["source"] = "public-share-page"
    item["extraction_evidence"] = detail.get("extraction_evidence", {})
    page_content.apply_content(
        item,
        detail["content"],
        content_type="post-description",
        content_source=str(detail.get("content_source") or ""),
        content_url=detail.get("content_url") or original_url,
    )
    return True


def _enrich_video_comments(items: List[Dict[str, Any]]) -> None:
    """Read public comments from Douyin's mobile share API."""
    for item in items:
        if item.get("content_source") not in {
            "douyin-router-data",
            "douyin-detail-dom",
        } or not item.get("content"):
            continue
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            match = re.search(r"/video/(\d+)", str(item.get("url") or ""))
            aweme_id = match.group(1) if match else ""
        if not aweme_id:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="douyin-mobile-comment-api",
                evidence={"reason": "missing-aweme-id"},
            )
            continue
        item["aweme_id"] = aweme_id
        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        api_url = (
            "https://www.iesdouyin.com/web/api/v2/comment/list/"
            f"?aweme_id={urllib.parse.quote(aweme_id)}&cursor=0"
            f"&count={comments.limit()}"
        )
        request = urllib.request.Request(
            api_url,
            headers=api_headers(share_url, mobile=True),
        )
        try:
            throttle.wait_for_detail_request("douyin")
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_comments = payload.get("comments")
            parsed = _parse_douyin_comments(raw_comments)
            expected = ((item.get("engagement") or {}).get("comments") or 0) > 0
            status = "empty" if isinstance(raw_comments, list) and not expected else "unavailable"
            comments.apply(
                item,
                parsed,
                status=status,
                source="douyin-mobile-comment-api",
                evidence={
                    "kind": "platform-api",
                    "endpoint": "/web/api/v2/comment/list/",
                    "aweme_id": aweme_id,
                    "cursor": 0,
                },
            )
        except Exception as exc:
            sys.stderr.write(f"[抖音] 评论读取失败({aweme_id}): {exc}\n")
            comments.apply(
                item,
                [],
                status="unavailable",
                source="douyin-mobile-comment-api",
                evidence={"kind": "platform-api", "aweme_id": aweme_id},
            )


def _parse_douyin_comments(raw_comments: Any) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict):
            continue
        value = comments.entry(
            raw.get("cid"),
            raw.get("text"),
            created_at=comments.timestamp(raw.get("createTime")),
            likes=raw.get("digg_count"),
            reply_count=raw.get("reply_comment_total"),
        )
        if value:
            parsed.append(value)
        if len(parsed) >= comments.limit():
            break
    return parsed


def _parse_aweme(aweme: dict) -> Dict[str, Any]:
    """解析抖音视频数据。"""
    desc = aweme.get("desc", "")
    author = aweme.get("author", {})
    stats = aweme.get("statistics", {})
    create_time = aweme.get("create_time", 0)
    date_str = None
    if create_time:
        date_str = dates.timestamp_to_date(create_time)

    aweme_id = aweme.get("aweme_id", "")
    hashtags = []
    for tag in aweme.get("text_extra", []):
        if tag.get("hashtag_name"):
            hashtags.append(tag["hashtag_name"])

    return {
        "text": desc,
        "url": f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
        "aweme_id": str(aweme_id or ""),
        "author_name": author.get("nickname", ""),
        "author_id": author.get("uid", ""),
        "date": date_str,
        "engagement": {
            "views": stats.get("play_count", 0),
            "likes": stats.get("digg_count", 0),
            "comments": stats.get("comment_count", 0),
            "shares": stats.get("share_count", 0),
        },
        "hashtags": hashtags,
        "duration": aweme.get("duration", 0),
    }
