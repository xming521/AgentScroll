"""知乎搜索模块 - 搜索知乎问答和文章。

Author: Jesse (https://github.com/Jesseovo)

通过站外 URL 发现候选，再由 Playwright 读取正文和评论。
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import (
    comments,
    live_artifacts,
    page_content,
    relevance,
    throttle,
    web_search,
)
from .request_profiles import api_headers, navigation_headers


def search_zhihu(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """搜索知乎内容。

    Args:
        topic: 搜索关键词
        from_date: 起始日期 YYYY-MM-DD
        to_date: 结束日期 YYYY-MM-DD
        depth: 搜索深度 quick/default/deep
    Returns:
        知乎问答/文章列表
    """
    limit_map = {"quick": 10, "default": 10, "deep": 10}
    limit = limit_map.get(depth, 10)

    items = _search_via_site_search(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("zhihu", "01_search", topic, items)
    items = _enrich_zhihu_details_with_browser(items)
    live_artifacts.save_stage("zhihu", "02_content", topic, items)
    _enrich_zhihu_comments(items)
    live_artifacts.save_stage("zhihu", "03_comments", topic, items)

    scored = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        excerpt = item.get("excerpt", "")
        combined = f"{title} {excerpt}"
        rel = relevance.token_overlap_relevance(topic, combined)
        item["id"] = f"ZH{i+1}"
        item["relevance"] = rel
        source = item.get("source", "zhihu")
        item["why_relevant"] = f"知乎来源({source}): {title[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


def _hotlist_seed(hotlist_item: Mapping[str, Any]) -> Dict[str, Any]:
    url = str(hotlist_item.get("url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    is_question = (
        parsed.hostname == "www.zhihu.com"
        and bool(re.fullmatch(r"/question/\d+(?:/answer/\d+)?/?", parsed.path))
    )
    is_article = (
        parsed.hostname == "zhuanlan.zhihu.com"
        and bool(re.fullmatch(r"/p/\d+/?", parsed.path))
    )
    if parsed.scheme not in {"http", "https"} or not (is_question or is_article):
        raise ValueError(f"不支持的知乎热榜 URL: {url!r}")
    return {
        "title": str(hotlist_item.get("title") or "").strip(),
        "excerpt": "",
        "url": url,
        "author": "",
        "date": None,
        "engagement": {"voteups": 0, "comments": 0, "collects": 0},
        "source": "newsnow-hotlist",
    }


def _read_article_in_browser(
    page: Any,
    seed: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    url = str(seed.get("url") or "")
    article_match = re.search(r"zhuanlan\.zhihu\.com/p/(\d+)", url)
    if not article_match:
        return None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(
            ".Post-RichTextContainer .RichText, article .RichText",
            state="attached",
            timeout=10000,
        )
    except Exception as exc:
        sys.stderr.write(f"[知乎] 文章详情页加载失败({url}): {exc}\n")
        return None
    if "/signin" in page.url:
        return None

    content_locator = page.locator(
        ".Post-RichTextContainer .RichText, article .RichText"
    ).first
    content = content_locator.inner_text().strip()
    if len(content) < 20:
        return None
    title_locator = page.locator("h1.Post-Title")
    title = (
        title_locator.first.inner_text().strip()
        if title_locator.count()
        else str(seed.get("title") or "").strip()
    )
    article_id = article_match.group(1)
    item = dict(seed)
    item.update({
        "title": title,
        "url": url,
        "text": content,
        "excerpt": content[:300],
        "source": "zhihu-browser-detail",
    })
    page_content.apply_content(
        item,
        content,
        content_type="article-body",
        content_source="zhihu-detail-dom",
        content_url=url,
    )
    parsed_comments, status = _fetch_comments_in_browser(
        page,
        "articles",
        article_id,
    )
    comments.apply(
        item,
        parsed_comments,
        status=status,
        source="zhihu-root-comments-browser",
        evidence={
            "kind": "browser-fetch",
            "endpoint": f"/api/v4/articles/{article_id}/root_comments",
            "target_type": "articles",
            "target_id": article_id,
        },
    )
    return item


def collect_hotlist_threads(
    hotlist_items: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Read selected Zhihu question/article URLs and their root comments."""
    prepared: List[tuple[str, Optional[Dict[str, Any]], Optional[str]]] = []
    for hotlist_item in hotlist_items:
        query = str(hotlist_item.get("title") or "").strip()
        try:
            prepared.append((query, _hotlist_seed(hotlist_item), None))
        except Exception as exc:
            prepared.append((query, None, f"{type(exc).__name__}: {exc}"))

    from . import crawler_bridge

    if not crawler_bridge.is_playwright_available():
        browser_error = "Playwright 浏览器不可用，无法打开知乎详情页"
        return [
            {
                "query": query,
                "status": "error",
                "post_count": 0,
                "posts": [],
                "error": error or browser_error,
            }
            for query, _, error in prepared
        ]

    results: List[Dict[str, Any]] = []
    with crawler_bridge._launch_browser_context("zhihu") as (_, __, page):
        for query, seed, error in prepared:
            post: Optional[Dict[str, Any]] = None
            if seed is not None and error is None:
                url = str(seed.get("url") or "")
                post = (
                    _read_article_in_browser(page, seed)
                    if "zhuanlan.zhihu.com" in url
                    else _read_detail_in_browser(page, seed)
                )
                if post is None:
                    error = "知乎详情页没有返回可读正文"
            results.append({
                "query": query,
                "status": "readable" if post else "error",
                "post_count": 1 if post else 0,
                "posts": [post] if post else [],
                "error": error,
            })
    return results


def _fetch_html(url: str, timeout: int = 8) -> str:
    headers = navigation_headers("https://cn.bing.com/")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _search_via_site_search(topic: str, limit: int) -> List[Dict[str, Any]]:
    """Discover public Zhihu URLs when the configured Cookie API has no result."""
    items: List[Dict[str, Any]] = []
    try:
        query = f"site:www.zhihu.com/question {topic}"
        results: List[Dict[str, str]] = []
        for search in (
            web_search.search_bing,
            web_search.search_yahoo,
            web_search.search_duckduckgo,
        ):
            try:
                results = search(
                    query,
                    allowed_url_fragments=("zhihu.com/question/",),
                    limit=limit,
                    fetcher=_fetch_html,
                )
            except Exception as exc:
                sys.stderr.write(
                    f"[知乎] {search.__name__} 问题搜索失败: {exc}\n"
                )
            if results:
                break

        # Articles frequently redirect anonymous sessions to /signin. Only use
        # them when no public question page can be discovered.
        if not results:
            article_query = f"site:zhuanlan.zhihu.com/p {topic}"
            for search in (
                web_search.search_bing,
                web_search.search_yahoo,
                web_search.search_duckduckgo,
            ):
                try:
                    results = search(
                        article_query,
                        allowed_url_fragments=("zhuanlan.zhihu.com/p/",),
                        limit=limit,
                        fetcher=_fetch_html,
                    )
                except Exception as exc:
                    sys.stderr.write(
                        f"[知乎] {search.__name__} 文章搜索失败: {exc}\n"
                    )
                if results:
                    break
        for result in results:
            href = result["url"]
            title = result["title"]
            snippet = result["snippet"]
            content_type = "article" if "zhuanlan.zhihu.com" in href else "question"
            items.append({
                "title": title,
                "excerpt": snippet,
                "url": href,
                "author": "",
                "date": None,
                "content_type": content_type,
                "engagement": {"voteups": 0, "comments": 0, "collects": 0},
                "source": "site-search-fallback",
            })
    except Exception as e:
        sys.stderr.write(f"[知乎] 站内搜索兜底失败: {e}\n")
    if items:
        sys.stderr.write(f"[知乎] Cookie API 无结果，已发现 {len(items)} 条公开链接。\n")
    return items


def _fetch_comments_in_browser(
    page: Any,
    target_type: str,
    target_id: str,
) -> tuple[List[Dict[str, Any]], str]:
    endpoint = (
        f"https://www.zhihu.com/api/v4/{target_type}/{target_id}/root_comments"
        f"?order=normal&limit={comments.limit()}&offset=0&status=open"
    )
    try:
        result = page.evaluate(
            """async (url) => {
                try {
                    const response = await fetch(url, {credentials: "include"});
                    return {status: response.status, body: await response.text()};
                } catch (error) {
                    return {status: 0, body: String(error)};
                }
            }""",
            endpoint,
        )
        status_code = int(result.get("status") or 0)
        payload = json.loads(result.get("body") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], "unavailable"

    parsed = _parse_zhihu_comments(
        payload.get("data") if isinstance(payload, dict) else None
    )
    if parsed:
        return parsed, "readable"
    if status_code in (401, 403) or (
        isinstance(payload, dict) and payload.get("error")
    ):
        return [], "blocked"
    if status_code == 200 and isinstance(payload, dict):
        return [], "empty"
    return [], "unavailable"


def _answer_from_card(
    page: Any,
    card: Any,
    seed: Dict[str, Any],
    question_id: str,
) -> Optional[Dict[str, Any]]:
    content_element = card.locator(".RichContent-inner")
    if not content_element.count():
        return None
    content = content_element.first.inner_text().strip()
    if len(content) < 20:
        return None

    try:
        metadata = json.loads(card.get_attribute("data-zop") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    answer_id = str(metadata.get("itemId") or card.get_attribute("name") or "")
    if not answer_id.isdigit():
        return None

    canonical_url = (
        f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    )
    item = dict(seed)
    item.update({
        "title": str(metadata.get("title") or seed.get("title") or "").strip(),
        "url": canonical_url,
        "text": content,
        "excerpt": content[:300],
        "source": "zhihu-browser-detail",
    })
    page_content.apply_content(
        item,
        content,
        content_type="post-body",
        content_source="zhihu-detail-dom",
        content_url=canonical_url,
    )
    parsed_comments, status = _fetch_comments_in_browser(
        page,
        "answers",
        answer_id,
    )
    comments.apply(
        item,
        parsed_comments,
        status=status,
        source="zhihu-root-comments-browser",
        evidence={
            "kind": "browser-fetch",
            "endpoint": f"/api/v4/answers/{answer_id}/root_comments",
            "target_type": "answers",
            "target_id": answer_id,
        },
    )
    return item


def _read_detail_in_browser(
    page: Any,
    seed: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    url = str(seed.get("url") or "")
    question_match = re.search(r"/question/(\d+)", url)
    if not question_match:
        return None

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(
            ".AnswerItem .RichContent-inner",
            state="attached",
            timeout=10000,
        )
    except Exception as exc:
        sys.stderr.write(f"[知乎] 详情页加载失败({url}): {exc}\n")
        return None
    if "/signin" in page.url:
        return None

    question_id = question_match.group(1)
    first_readable: Optional[Dict[str, Any]] = None
    cards = page.locator(".AnswerItem")
    for index in range(cards.count()):
        item = _answer_from_card(page, cards.nth(index), seed, question_id)
        if item is None:
            continue
        if first_readable is None:
            first_readable = item
        if item.get("comments_status") == "readable":
            return item
    return first_readable


def _enrich_zhihu_details_with_browser(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not items:
        return items
    try:
        from . import crawler_bridge

        if not crawler_bridge.is_playwright_available():
            return items
        enriched: List[Dict[str, Any]] = []
        with crawler_bridge._launch_browser_context("zhihu") as (_, _, page):
            for seed in items:
                item = _read_detail_in_browser(page, seed)
                if item is not None:
                    enriched.append(item)
        return enriched or items
    except Exception as exc:
        sys.stderr.write(f"[知乎] Chromium 详情读取失败: {exc}\n")
        return items


def _enrich_zhihu_comments(
    items: List[Dict[str, Any]],
) -> None:
    """Read root comments for answers, articles, and questions when permitted."""
    for item in items:
        if item.get("comments_status") == "readable":
            continue
        target = _zhihu_comment_target(str(item.get("url") or ""))
        if not target:
            continue
        target_type, target_id = target
        endpoint = (
            f"https://www.zhihu.com/api/v4/{target_type}/{target_id}/root_comments"
            f"?order=normal&limit={comments.limit()}&offset=0&status=open"
        )
        headers = api_headers(str(item.get("url") or "https://www.zhihu.com/"))
        request = urllib.request.Request(endpoint, headers=headers)
        payload: Any = None
        try:
            throttle.wait_for_detail_request("zhihu")
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
        except Exception as exc:
            sys.stderr.write(f"[知乎] 评论读取失败({target_id}): {exc}\n")

        raw_comments = payload.get("data") if isinstance(payload, dict) else None
        parsed = _parse_zhihu_comments(raw_comments)
        if isinstance(payload, dict) and payload.get("error"):
            status = "blocked"
        elif isinstance(raw_comments, list):
            status = "empty"
        else:
            status = "unavailable"
        comments.apply(
            item,
            parsed,
            status=status,
            source="zhihu-root-comments-api",
            evidence={
                "kind": "platform-api",
                "endpoint": f"/api/v4/{target_type}/{target_id}/root_comments",
                "target_type": target_type,
                "target_id": target_id,
            },
        )


def _zhihu_comment_target(url: str) -> Optional[tuple[str, str]]:
    answer = re.search(r"/answer/(\d+)", url)
    if answer:
        return "answers", answer.group(1)
    article = re.search(r"zhuanlan\.zhihu\.com/p/(\d+)", url)
    if article:
        return "articles", article.group(1)
    question = re.search(r"/question/(\d+)", url)
    if question:
        return "questions", question.group(1)
    return None


def _parse_zhihu_comments(raw_comments: Any) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict):
            continue
        parent_id = raw.get("id")
        value = comments.entry(
            parent_id,
            raw.get("content"),
            created_at=comments.timestamp(raw.get("created_time")),
            likes=raw.get("like_count"),
            reply_count=raw.get("child_comment_count"),
        )
        if value:
            parsed.append(value)
        for reply in raw.get("child_comments") or []:
            if not isinstance(reply, dict):
                continue
            value = comments.entry(
                reply.get("id"),
                reply.get("content"),
                created_at=comments.timestamp(reply.get("created_time")),
                likes=reply.get("like_count"),
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
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    return text.strip()
